import math
import csv
import sys
from datetime import datetime
from typing import Any
from pathlib import Path
import json

from domino.base_piece import BasePiece

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from common_somes import pv_model
    from common_somes.pv_model import ArraySpec
except ModuleNotFoundError:
    from pieces.common_somes import pv_model
    from pieces.common_somes.pv_model import ArraySpec

from .models import (
    InputModel,
    OutputModel,
    TARGET_COLUMN,
    OPEN_METEO_CSV_FIELDNAMES,
    OPEN_METEO_ARCHIVE_URL,
    OPEN_METEO_HISTORICAL_FORECAST_URL,
)

_OPEN_METEO_VARS = [
    "shortwave_radiation",
    "direct_normal_irradiance",
    "diffuse_radiation",
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "surface_pressure",
]

# The archive (ERA5) exposes fewer 15-minute-capable variables than the
# historical-forecast API, so each endpoint gets its own variable list.
_ARCHIVE_VARS = ",".join(_OPEN_METEO_VARS)
_MINUTELY_15_VARS = ",".join(_OPEN_METEO_VARS)


def _fetch_open_meteo(
    latitude: float,
    longitude: float,
    start_date: str,
    end_date: str,
    resolution: str = "15min",
    timeout: int = 90,
) -> tuple[dict[str, Any], str]:
    """Fetch weather for the period and return ``(series, resolution_used)``.

    ``15min`` uses the historical-forecast API, whose ``minutely_15`` block lines
    up with a 15-minute load profile. ``hourly`` uses the ERA5 archive, which
    reaches further back. ``auto`` tries 15-minute first and falls back.
    """
    import requests

    base = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date,
        "end_date": end_date,
        "wind_speed_unit": "ms",
        "timezone": "auto",
    }

    def _try_15min() -> tuple[dict[str, Any], int] | None:
        resp = requests.get(
            OPEN_METEO_HISTORICAL_FORECAST_URL,
            params={**base, "minutely_15": _MINUTELY_15_VARS},
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        block = body.get("minutely_15") or {}
        if not block.get("time"):
            return None
        return block, int(body.get("utc_offset_seconds") or 0)

    def _try_hourly() -> tuple[dict[str, Any], int]:
        resp = requests.get(
            OPEN_METEO_ARCHIVE_URL,
            params={**base, "hourly": _ARCHIVE_VARS},
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("hourly") or {}, int(body.get("utc_offset_seconds") or 0)

    if resolution == "hourly":
        block, offset = _try_hourly()
        return {**block, "_utc_offset_seconds": offset}, "hourly"

    if resolution == "auto":
        try:
            got = _try_15min()
            if got:
                block, offset = got
                return {**block, "_utc_offset_seconds": offset}, "15min"
        except Exception:
            pass
        block, offset = _try_hourly()
        return {**block, "_utc_offset_seconds": offset}, "hourly"

    got = _try_15min()
    if not got:
        raise RuntimeError(
            "Open-Meteo returned no 15-minute data for the requested period. "
            "Use time_resolution='hourly' or 'auto' for periods the "
            "historical-forecast API does not cover."
        )
    block, offset = got
    return {**block, "_utc_offset_seconds": offset}, "15min"


def _build_records(
    series: dict[str, Any],
    lat: float,
    lon: float,
    spec: "ArraySpec",
) -> list[dict[str, Any]]:
    """Turn a raw Open-Meteo block into PVOUT training rows.

    PVOUT is produced by the physical model in ``common_somes.pv_model`` rather
    than by scaling GHI, so it carries information that the downstream model has
    to learn from several weather variables at once.
    """
    times = series.get("time", [])
    n = len(times)
    utc_offset = int(series.get("_utc_offset_seconds") or 0)

    def _col(key: str) -> list:
        return series.get(key) or [None] * n

    ghi_col = _col("shortwave_radiation")
    dni_col = _col("direct_normal_irradiance")
    dif_col = _col("diffuse_radiation")
    temp_col = _col("temperature_2m")
    rh_col = _col("relative_humidity_2m")
    ws_col = _col("wind_speed_10m")
    wg_col = _col("wind_gusts_10m")
    wd_col = _col("wind_direction_10m")
    ap_col = _col("surface_pressure")

    records = []
    for i, ts_str in enumerate(times):
        dt = datetime.fromisoformat(ts_str)
        ghi = max(0.0, ghi_col[i] or 0.0)
        temp = temp_col[i] if temp_col[i] is not None else 15.0
        wind = ws_col[i] if ws_col[i] is not None else 1.0

        result = pv_model.ac_power_kw(
            dt=dt,
            ghi=ghi,
            dni=dni_col[i],
            dif=dif_col[i],
            temp_c=float(temp),
            wind_ms=float(wind),
            lat=lat,
            lon=lon,
            spec=spec,
            utc_offset_seconds=utc_offset,
        )
        pvout = result["pvout_kw"]

        records.append({
            "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "Date": dt.strftime("%d.%m.%Y"),
            "Time": dt.strftime("%H:%M"),
            "GHI": round(ghi, 2),
            "DNI": round(result["dni"], 2),
            "DIF": round(result["dif"], 2),
            "GTI": round(result["gti"], 2),
            "SE": round(result["elevation_deg"], 2),
            "SA": round(result["azimuth_deg"], 2),
            "PVOUT": round(pvout, 3),
            "TEMP": round(float(temp), 2),
            "WS": round(float(wind), 2),
            "WG": round(wg_col[i] or 0.0, 2),
            "WD": round(wd_col[i] or 0.0, 2),
            "RH": round(rh_col[i] or 0.0, 2),
            "AP": round(ap_col[i] or 0.0, 2),
            # Cell temperature, DC power and clipped power are deliberately not
            # emitted: each is a near-deterministic function of PVOUT and would
            # leak the target back into the feature set.
            # Kept for schema compatibility with the UC3.4 preprocessing chain;
            # PvoutModelFeatureSelectPiece excludes any PVOUT_UNC* column.
            "PVOUT_UNC_LOW": round(pvout * 0.92, 3),
            "PVOUT_UNC_HIGH": round(pvout * 1.08, 3),
        })
    return records


def _window_from_load_csv(path: str | None) -> tuple[str, str] | None:
    """Read the analysed period straight off the metered load profile.

    Weather over a different period than the load would pair January production
    with July consumption, which quietly destroys the self-consumption estimate
    the whole investment case rests on. Returns None when the file is absent or
    unreadable, leaving the explicit dates in charge.
    """
    if not path:
        return None
    p = Path(str(path))
    if not p.is_file():
        return None
    try:
        import pandas as pd

        frame = pd.read_csv(p, sep=None, engine="python", encoding="utf-8-sig")
        frame.columns = [c.strip().lower().replace(" ", "_") for c in frame.columns]
        if "datetime" not in frame.columns:
            return None
        stamps = pd.to_datetime(frame["datetime"], errors="coerce").dropna()
        if stamps.empty:
            return None
        return stamps.min().strftime("%Y-%m-%d"), stamps.max().strftime("%Y-%m-%d")
    except Exception:
        return None


class OpenMeteoPVDataPiece(BasePiece):
    def piece_function(self, input_data: InputModel, secrets_data=None):
        try:
            from common import onedata_io as od
        except ModuleNotFoundError:
            try:
                from pieces.common import onedata_io as od
            except ModuleNotFoundError:
                od = None
        stage = None
        if od is not None:
            try:
                input_data, stage = od.stage_inputs(input_data, secrets_data)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("OneData stage skipped: %s", exc)

        payload = input_data.to_payload_dict()
        extra = getattr(input_data, "model_extra", None) or {}
        if not payload.get("output_format"):
            for key in (
                "output_format",
                "outputFormat",
                "Output format",
                "Output Format",
                "export_format",
                "file_format",
            ):
                val = extra.get(key)
                if val is not None and str(val).strip() != "":
                    payload["output_format"] = val
                    break
        self.logger.info("Running Open-Meteo PV Data.")

        try:
            repo_root = Path(__file__).resolve().parents[2]
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            from pieces.common_somes.scenario_site import (
                apply_scenario_site_to_mapping,
                load_scenario_yaml,
            )

            cfg = load_scenario_yaml(payload.get("scenario_yaml"))
            if cfg:
                payload = apply_scenario_site_to_mapping(payload, cfg)
                self.logger.info(
                    "Applied site/PV from scenario_yaml: lat=%s lon=%s kwp=%s tilt=%s",
                    payload.get("latitude"),
                    payload.get("longitude"),
                    payload.get("pvout_peak_kw"),
                    payload.get("panel_tilt"),
                )

            output_mode = str(payload.get("output_mode", "batch_sample")).strip().lower()
            if output_mode not in {"batch_sample", "realtime_stream"}:
                raise ValueError("output_mode must be `batch_sample` or `realtime_stream`.")

            output_format = str(payload.get("output_format", "json")).strip().lower()
            if output_format not in {"json", "csv"}:
                raise ValueError("output_format must be `json` or `csv`.")

            # to_payload_dict() drops unset fields, so indexing it raises for any
            # field the caller left on its default. Read through the validated
            # model, which always carries one.
            def _field(name: str):
                return payload.get(name, getattr(input_data, name, None))

            latitude = float(_field("latitude"))
            longitude = float(_field("longitude"))
            start_date = str(_field("start_date"))
            end_date = str(_field("end_date"))

            window = _window_from_load_csv(_field("load_csv"))
            if window:
                start_date, end_date = window
                self.logger.info(
                    "Weather window taken from the load profile: %s .. %s",
                    start_date,
                    end_date,
                )
            pvout_peak_kw = float(_field("pvout_peak_kw") or 5.2)
            panel_tilt = float(_field("panel_tilt") or 30.0)

            resolution = str(_field("time_resolution") or "15min").strip().lower()
            if resolution not in {"15min", "hourly", "auto"}:
                raise ValueError("time_resolution must be `15min`, `hourly` or `auto`.")

            spec = pv_model.array_spec_from_scenario(
                cfg or {}, installed_kwp=pvout_peak_kw, tilt_deg=panel_tilt
            )

            self.logger.info(
                "Fetching Open-Meteo data for lat=%.4f lon=%.4f from %s to %s at %s",
                latitude,
                longitude,
                start_date,
                end_date,
                resolution,
            )
            series, resolution_used = _fetch_open_meteo(
                latitude, longitude, start_date, end_date, resolution
            )
            self.logger.info("Open-Meteo returned %s resolution.", resolution_used)
            self.logger.info("PV array model: %s", spec.describe())

            records = _build_records(series, latitude, longitude, spec)
            self.logger.info("Built %d records from API response.", len(records))

            if not records:
                self.logger.warning("No records returned for the requested date range.")
                if stage is not None:
                    stage.cleanup()
                return OutputModel(file_path=None)

            file_suffix = "stream" if output_mode == "realtime_stream" else "batch"
            file_name = f"open_meteo_{file_suffix}.{output_format}"
            file_path = str(Path(self.results_path) / file_name)

            if output_format == "json":
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(records, indent=4))
            else:
                with open(file_path, "w", encoding="utf-8", newline="") as csvfile:
                    writer = csv.DictWriter(
                        csvfile,
                        fieldnames=OPEN_METEO_CSV_FIELDNAMES,
                        delimiter=";",
                        extrasaction="ignore",
                    )
                    writer.writeheader()
                    writer.writerows(records)

            self.logger.info("Open-Meteo dataset saved to %s", file_path)
            self.display_result = {"file_type": "csv", "file_path": file_path}

            out = OutputModel(
                file_path=file_path,
                target_column=TARGET_COLUMN,
            )
            if od is not None:
                # Mirror to OneData so the next piece (own K8s pod) can stage the CSV.
                run_id = od.resolve_run_id(input_data, secrets_data, generate=False)
                return od.finish_piece(
                    out, self.results_path, secrets_data, "OpenMeteoPVDataPiece", stage, run_id=run_id
                )
            if stage is not None:
                stage.cleanup()
            return out
        except Exception:
            if stage is not None:
                try:
                    stage.cleanup()
                except Exception:
                    pass
            self.logger.exception(
                "Open-Meteo PV Data failed. input_payload=%s", payload
            )
            raise
