"""Adapt the UC3.4 inference forecast into virtual_solar.csv for BatterySimPiece.

Two things have to happen here that the UC3.4 chain does not do.

Night rows. DataPreprocessingPiece filters out every step with GHI <= 1, which
removes roughly half the year. That is reasonable for training a daylight model
and wrong for a dispatch profile: BatterySimPiece requires one PV value per load
timestamp and rejects any length mismatch. The forecast is therefore reindexed
onto the load timeline with zeros at night.

Calibration. When ShmuCalibrationPiece found a measured bias, the scale factor is
applied here so a single output carries the corrected profile.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

try:
    from domino.base_piece import BasePiece
except ModuleNotFoundError:
    from local_compat.base_piece import BasePiece

from .models import InputModel, OutputModel

_VALUE_COLUMN_CANDIDATES = ("final_forecast", "PVOUT", "pv_kw", "prediction", "y_pred")


def _read_datetime(df: pd.DataFrame, source: Path) -> pd.Series:
    if "datetime" in df.columns:
        return pd.to_datetime(df["datetime"], errors="coerce")
    if "Date" in df.columns and "Time" in df.columns:
        return pd.to_datetime(
            df["Date"].astype(str).str.strip() + " " + df["Time"].astype(str).str.strip(),
            dayfirst=True,
            errors="coerce",
        )
    raise ValueError(f"Forecast CSV missing datetime/Date+Time columns: {list(df.columns)}")


def _read_csv_any_separator(path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for sep in (",", ";"):
        try:
            df = pd.read_csv(path, sep=sep)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        if df.shape[1] > 1:
            return df
    if last_error is not None:
        raise last_error
    return pd.read_csv(path)


try:
    from common.piece_onedata import onedata_piece
except ModuleNotFoundError:
    from pieces.common.piece_onedata import onedata_piece


class PvoutToVirtualSolarPiece(BasePiece):
    @onedata_piece("PvoutToVirtualSolarPiece")
    def piece_function(self, input_data: InputModel, secrets_data=None) -> OutputModel:
        src = Path(str(input_data.forecast_csv_path))
        if not src.is_file():
            raise FileNotFoundError(f"Forecast CSV not found: {src}")

        df = _read_csv_any_separator(src)

        value_col = next((c for c in _VALUE_COLUMN_CANDIDATES if c in df.columns), None)
        if value_col is None:
            numeric = [
                c for c in df.columns
                if c.lower() not in {"datetime", "date", "time", "horizon", "pred_sequence_id"}
            ]
            if not numeric:
                raise ValueError(f"No forecast column in {src}; columns={list(df.columns)}")
            value_col = numeric[0]

        scale = float(input_data.irradiance_scale_factor or 1.0)
        values = pd.to_numeric(df[value_col], errors="coerce").fillna(0.0).clip(lower=0.0) * scale

        forecast = (
            pd.DataFrame({"datetime": _read_datetime(df, src), "pv_kw": values})
            .dropna(subset=["datetime"])
            .drop_duplicates(subset=["datetime"])
            .sort_values("datetime")
            .reset_index(drop=True)
        )

        report = {
            "forecast_column": value_col,
            "forecast_rows": int(len(forecast)),
            "irradiance_scale_factor": scale,
            "aligned_to_load": False,
        }

        if input_data.load_csv:
            forecast, report = self._align_to_load(forecast, Path(str(input_data.load_csv)), report)

        # Sizing sweeps hundreds of array sizes against this one forecast, so the
        # shape is also published per installed kWp. Rescaling it beats rerunning
        # the model chain per candidate and keeps every candidate on the same
        # weather.
        reference_kwp = self._reference_kwp(input_data.scenario_yaml)
        if reference_kwp > 0:
            forecast["pv_kw_per_kwp"] = (forecast["pv_kw"] / reference_kwp).round(6)
        report["reference_kwp"] = reference_kwp

        out_dir = Path(self.results_path or ".")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "virtual_solar.csv"
        report_path = out_dir / "virtual_solar_coverage.json"

        forecast.to_csv(out_path, index=False)
        report["output_rows"] = int(len(forecast))
        report["total_energy_kwh_at_15min"] = round(float(forecast["pv_kw"].sum()) * 0.25, 2)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

        self.display_result = {"file_type": "csv", "file_path": str(out_path)}

        if report["aligned_to_load"]:
            message = (
                f"virtual_solar.csv aligned to load: {report['output_rows']} rows, "
                f"{report['night_rows_filled']} night steps filled, "
                f"{report['unmatched_load_steps']} load steps without a forecast"
            )
        else:
            message = (
                f"virtual_solar.csv from column '{value_col}': {report['output_rows']} rows "
                "(not aligned to a load profile)"
            )

        return OutputModel(
            message=message,
            virtual_solar_csv=str(out_path),
            coverage_report_json=str(report_path),
            reference_kwp=reference_kwp,
        )

    @staticmethod
    def _reference_kwp(scenario_yaml: str | None) -> float:
        if not scenario_yaml:
            return 0.0
        path = Path(str(scenario_yaml))
        if not path.is_file():
            return 0.0
        try:
            import yaml

            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return float((cfg.get("pv") or {}).get("installed_kwp", 0.0) or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _align_to_load(
        forecast: pd.DataFrame, load_path: Path, report: dict
    ) -> tuple[pd.DataFrame, dict]:
        """Reindex the daylight forecast onto the load timeline, zero-filling night."""
        if not load_path.is_file():
            report["alignment_skipped"] = f"load CSV not found: {load_path}"
            return forecast, report

        load = _read_csv_any_separator(load_path)
        if "datetime" not in load.columns:
            report["alignment_skipped"] = "load CSV has no datetime column"
            return forecast, report

        load_index = pd.to_datetime(load["datetime"], errors="coerce").dropna()
        if load_index.empty:
            report["alignment_skipped"] = "load CSV datetime column could not be parsed"
            return forecast, report

        series = forecast.set_index("datetime")["pv_kw"]

        # Match on wall-clock time. The weather series may run over a different
        # calendar year than the metered load, in which case it is mapped by
        # month/day/time so a representative year can drive a historical profile.
        aligned = series.reindex(load_index.values)
        matched = int(aligned.notna().sum())

        if matched < 0.5 * len(load_index):
            by_stamp = series.copy()
            by_stamp.index = pd.MultiIndex.from_arrays(
                [by_stamp.index.month, by_stamp.index.day, by_stamp.index.hour, by_stamp.index.minute]
            )
            by_stamp = by_stamp[~by_stamp.index.duplicated(keep="first")]
            key = pd.MultiIndex.from_arrays(
                [load_index.dt.month, load_index.dt.day, load_index.dt.hour, load_index.dt.minute]
            )
            mapped = by_stamp.reindex(key)
            if int(mapped.notna().sum()) > matched:
                aligned = pd.Series(mapped.to_numpy(), index=load_index.values)
                matched = int(aligned.notna().sum())
                report["alignment_mode"] = "calendar_day_and_time"
            else:
                report["alignment_mode"] = "exact_timestamp"
        else:
            report["alignment_mode"] = "exact_timestamp"

        filled = aligned.fillna(0.0).clip(lower=0.0)

        report["aligned_to_load"] = True
        report["load_rows"] = int(len(load_index))
        report["matched_steps"] = matched
        report["night_rows_filled"] = int(len(load_index)) - matched
        report["unmatched_load_steps"] = int(len(load_index)) - matched

        return (
            pd.DataFrame({"datetime": load_index.to_numpy(), "pv_kw": filled.to_numpy()}),
            report,
        )
