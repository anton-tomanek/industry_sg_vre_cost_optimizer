from __future__ import annotations

import json
import shutil
import unicodedata
from pathlib import Path

import pandas as pd
import yaml
from domino.base_piece import BasePiece

from .models import InputModel, OutputModel

try:
    from common import onedata_io as od
except ModuleNotFoundError:
    try:
        from pieces.common import onedata_io as od
    except ModuleNotFoundError:
        od = None

try:
    from common.okte_prices import align_okte_to_load, fetch_okte_dam_prices
except ModuleNotFoundError:
    from pieces.common.okte_prices import align_okte_to_load, fetch_okte_dam_prices


class UserInputPiece(BasePiece):
    """Validate and pass-through user inputs for downstream pieces."""

    @staticmethod
    def _read_csv_auto(path: Path) -> pd.DataFrame:
        suffix = path.suffix.lower()
        if suffix in {".xlsx", ".xlsm", ".xls"}:
            return pd.read_excel(path, sheet_name=0)
        return pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig", decimal=",")

    # Accepted names of the time column after normalisation (lowercase, no
    # diacritics, spaces -> "_"); e.g. "Dátum a čas" -> "datum_a_cas".
    DATETIME_ALIASES = ("datetime", "date_time", "timestamp", "datum_a_cas", "datum_cas", "datum")

    @staticmethod
    def _normalize_column_name(name) -> str:
        key = unicodedata.normalize("NFKD", str(name).strip().lower())
        key = "".join(ch for ch in key if not unicodedata.combining(ch))
        return key.replace(" ", "_")

    @classmethod
    def _normalize_datetime_column(cls, df: pd.DataFrame) -> pd.DataFrame:
        cols = {c: str(c).strip().lower().replace(" ", "_") for c in df.columns}
        df = df.rename(columns=cols)
        dt_col = None
        for cand in ("datetime", "date_time", "timestamp"):
            if cand in df.columns:
                dt_col = cand
                break
        if dt_col is None:
            # Local-language headers (e.g. "datum a cas" in the UC3.2 Excel samples).
            for col in df.columns:
                if cls._normalize_column_name(col) in cls.DATETIME_ALIASES:
                    dt_col = col
                    break
        if dt_col is None:
            raise ValueError(
                "Súbor musí obsahovať stĺpec datetime/date_time/timestamp (alebo „dátum a čas“)"
            )
        if dt_col not in ("datetime", "date_time", "timestamp"):
            df = df.rename(columns={dt_col: "date_time"})
            dt_col = "date_time"
        raw_dt = df[dt_col].astype(str).str.strip()
        # Support both ISO (YYYY-MM-DD ...) and local day-first formats (dd.mm.yyyy ...).
        dt_iso = pd.to_datetime(raw_dt, errors="coerce", dayfirst=False, format="mixed")
        dt_local = pd.to_datetime(raw_dt, errors="coerce", dayfirst=True, format="mixed")
        df["datetime"] = dt_iso.fillna(dt_local)
        # Keep a single datetime column. Leaving date_time/timestamp in place
        # used to get summed into load_kw (Excel serial / ns since epoch).
        leftover = [c for c in df.columns if c != "datetime" and c in {"date_time", "timestamp", "date", "time"}]
        if leftover:
            df = df.drop(columns=leftover)
        return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    @staticmethod
    def _power_columns(columns: list[str]) -> list[str]:
        skip = {
            "datetime",
            "date_time",
            "timestamp",
            "date",
            "time",
            "price_eur_per_kwh",
            "price_eur_kwh",
            "price_eur_mwh",
        }
        hints = ("prikon", "load", "power", "odber", "consumption", "vykon", "kw")
        out = []
        for name in columns:
            key = str(name).strip().lower().replace(" ", "_")
            if key in skip or "price" in key or "cena" in key:
                continue
            if any(h in key for h in hints):
                out.append(name)
        return out

    @classmethod
    def _build_load_kw(cls, df: pd.DataFrame) -> pd.Series:
        if "load_kw" in df.columns:
            return pd.to_numeric(df["load_kw"], errors="coerce").fillna(0.0)
        candidates = cls._power_columns(list(df.columns))
        if not candidates:
            raise ValueError(
                "Súbor musí mať stĺpec load_kw, alebo stĺpce odberu (prikon A/B/C…)."
            )
        parts = []
        for col in candidates:
            series = df[col]
            if pd.api.types.is_datetime64_any_dtype(series):
                continue
            num = pd.to_numeric(series, errors="coerce")
            if num.notna().sum() == 0:
                continue
            median = float(num.median())
            if abs(median) > 1e6:
                continue
            parts.append(num.fillna(0.0))
        if not parts:
            raise ValueError(
                "Nenašiel sa použiteľný stĺpec odberu. Použite load_kw alebo prikon A/B/C…"
            )
        load = parts[0]
        for extra in parts[1:]:
            load = load.add(extra, fill_value=0.0)
        if float(load.median()) > 1e6:
            raise ValueError(
                "Odber vyšiel ako nezmyselne veľký výkon. Skontrolujte, či sú hodnoty v kW."
            )
        return load

    @staticmethod
    def _collapse_duplicate_timestamps(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or "datetime" not in df.columns:
            return df
        if not df["datetime"].duplicated().any():
            return df

        agg: dict[str, str] = {}
        for col in df.columns:
            if col == "datetime":
                continue
            if col == "load_kw":
                agg[col] = "sum"
            elif col == "price_eur_per_kwh":
                agg[col] = "mean"
            else:
                agg[col] = "first"
        return df.groupby("datetime", as_index=False).agg(agg).sort_values("datetime").reset_index(drop=True)

    @staticmethod
    def _infer_step_hours(df: pd.DataFrame, fallback_minutes: float = 15.0) -> float:
        if len(df) < 2:
            return max(fallback_minutes, 1.0) / 60.0
        step = df["datetime"].diff().dt.total_seconds().median()
        if pd.notna(step) and step > 0:
            return float(step) / 3600.0
        return max(fallback_minutes, 1.0) / 60.0

    @staticmethod
    def _repair_missing_intervals(df: pd.DataFrame, step_hours: float) -> tuple[pd.DataFrame, int]:
        if df.empty:
            return df, 0
        step_minutes = max(1, int(round(step_hours * 60.0)))
        freq = f"{step_minutes}min"
        full_index = pd.date_range(df["datetime"].min(), df["datetime"].max(), freq=freq)
        repaired = (
            df.set_index("datetime")
            .reindex(full_index)
            .rename_axis("datetime")
            .reset_index()
            .sort_values("datetime")
            .reset_index(drop=True)
        )
        filled = int(repaired["load_kw"].isna().sum()) if "load_kw" in repaired.columns else 0
        if "load_kw" in repaired.columns:
            repaired["load_kw"] = pd.to_numeric(repaired["load_kw"], errors="coerce").interpolate(
                method="linear", limit_direction="both"
            ).fillna(0.0)
        if "price_eur_per_kwh" in repaired.columns:
            repaired["price_eur_per_kwh"] = pd.to_numeric(repaired["price_eur_per_kwh"], errors="coerce")
            med = float(repaired["price_eur_per_kwh"].median()) if repaired["price_eur_per_kwh"].notna().any() else 0.0
            repaired["price_eur_per_kwh"] = repaired["price_eur_per_kwh"].interpolate(
                method="linear", limit_direction="both"
            ).fillna(med)
        return repaired, filled

    @staticmethod
    def _coerce_price_column(df: pd.DataFrame) -> pd.DataFrame:
        if "price_eur_per_kwh" in df.columns:
            return df
        if "price_eur_kwh" in df.columns:
            return df.rename(columns={"price_eur_kwh": "price_eur_per_kwh"})
        if "price_eur_mwh" in df.columns:
            out = df.copy()
            out["price_eur_per_kwh"] = pd.to_numeric(out["price_eur_mwh"], errors="coerce") / 1000.0
            return out
        return df

    @staticmethod
    def _has_usable_prices(df: pd.DataFrame) -> bool:
        if "price_eur_per_kwh" not in df.columns:
            return False
        return bool(pd.to_numeric(df["price_eur_per_kwh"], errors="coerce").notna().any())

    def piece_function(self, input_data: InputModel, secrets_data=None) -> OutputModel:
        _stage = None
        _piece_out = None
        _run_id = None
        if od is not None:
            input_data, _stage = od.stage_inputs(input_data, secrets_data)
            _run_id = od.resolve_run_id(input_data, secrets_data, generate=True)
        load_csv = Path(input_data.load_csv)
        prices_csv = Path(input_data.prices_csv) if input_data.prices_csv else None
        scenario_yaml = Path(input_data.scenario_yaml)
        out_dir = Path(self.results_path or load_csv.parent)
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "user_input.log"

        def _log(msg: str) -> None:
            text = f"[UserInputPiece] {msg}"
            print(text, flush=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")

        if not _run_id:
            import uuid
            _run_id = uuid.uuid4().hex[:12]
        _log(f"Workflow run_id={_run_id}")
        _log(f"Input prices_csv={prices_csv}")
        _log(f"Input scenario_yaml={scenario_yaml}")
        if not load_csv.is_file():
            raise FileNotFoundError(f"Load file not found: {load_csv}")
        if not scenario_yaml.is_file():
            raise FileNotFoundError(f"Scenario YAML not found: {scenario_yaml}")
        scenario_copy = out_dir / "scenario_resolved.yaml"
        shutil.copy2(scenario_yaml, scenario_copy)
        _log(f"Copied scenario to shared output path: {scenario_copy}")
        scenario = yaml.safe_load(scenario_copy.read_text(encoding="utf-8")) or {}
        timestep_minutes = float(scenario.get("timestep_minutes", 15))
        prod_cfg = scenario.get("production") or {}
        gap_repair_enabled = bool(prod_cfg.get("gap_repair_enabled", True))

        # Load file may carry odber as load_kw or prikon A/B/C, and optional prices.
        df = self._normalize_datetime_column(self._read_csv_auto(load_csv))
        cols = [c.strip().lower().replace(" ", "_") for c in df.columns]
        df.columns = cols
        df = self._coerce_price_column(df)
        used = self._power_columns(list(df.columns))
        df["load_kw"] = self._build_load_kw(df)
        _log(f"Odber = súčet stĺpcov {used or ['load_kw']}; median={float(df['load_kw'].median()):.2f} kW")
        has_price = self._has_usable_prices(df)
        merge_mode = "single_csv"
        price_source = "load_csv"
        overlap_rows = None
        if has_price:
            merged = df.copy()
            merged["load_kw"] = pd.to_numeric(merged["load_kw"], errors="coerce").fillna(0.0)
            merged["price_eur_per_kwh"] = pd.to_numeric(merged["price_eur_per_kwh"], errors="coerce")
            merged = self._collapse_duplicate_timestamps(
                merged[["datetime", "load_kw", "price_eur_per_kwh"]].dropna(subset=["price_eur_per_kwh"])
            )
            merged_path = out_dir / "load_and_prices_merged.csv"
            merged.to_csv(merged_path, index=False)
            merge_mode = "single_csv_normalized"
            _log("Ceny vzaté zo súboru odberu.")
        else:
            load_df = self._collapse_duplicate_timestamps(df[["datetime", "load_kw"]].copy())

            if prices_csv is not None and prices_csv.is_file():
                p = self._normalize_datetime_column(self._read_csv_auto(prices_csv))
                p.columns = [c.strip().lower().replace(" ", "_") for c in p.columns]
                p = self._coerce_price_column(p)
                if "price_eur_per_kwh" not in p.columns:
                    raise ValueError("Prices CSV must contain price_eur_per_kwh (or price_eur_kwh / price_eur_mwh).")
                p = p[["datetime", "price_eur_per_kwh"]]
                p = self._collapse_duplicate_timestamps(p)
                merged = load_df.merge(p, on="datetime", how="inner").dropna(subset=["price_eur_per_kwh"])
                if merged.empty:
                    raise ValueError("No overlapping datetimes between load CSV and prices CSV.")
                merge_mode = "two_csv_merged"
                price_source = "prices_csv"
                overlap_rows = int(len(merged))
            else:
                _log(
                    f"No uploaded prices; fetching OKTE DAM for "
                    f"{load_df['datetime'].min()} .. {load_df['datetime'].max()}"
                )
                okte = fetch_okte_dam_prices(load_df["datetime"].min(), load_df["datetime"].max())
                merged = align_okte_to_load(load_df, okte)
                merge_mode = "okte_dam"
                price_source = "okte_dam"
                overlap_rows = int(len(merged))
                okte.to_csv(out_dir / "okte_dam_prices.csv", index=False)
                _log(f"OKTE DAM rows={len(okte)} aligned_to_load={len(merged)}")

            merged_path = out_dir / "load_and_prices_merged.csv"
            merged.to_csv(merged_path, index=False)

        merged_df = self._read_csv_auto(Path(merged_path))
        merged_df = self._normalize_datetime_column(merged_df)
        inferred_step_h = self._infer_step_hours(merged_df, fallback_minutes=timestep_minutes)
        configured_step_h = max(timestep_minutes, 1.0) / 60.0
        repair_step_h = min(inferred_step_h, configured_step_h)
        repaired_intervals = 0
        if gap_repair_enabled:
            merged_df, repaired_intervals = self._repair_missing_intervals(merged_df, repair_step_h)
            merged_df.to_csv(merged_path, index=False)
        start_dt = merged_df["datetime"].min()
        end_dt = merged_df["datetime"].max()
        expected_intervals = int(round(((end_dt - start_dt).total_seconds() / 3600.0) / repair_step_h)) + 1
        missing_intervals_est = max(0, expected_intervals - len(merged_df))
        summary = {
            "message": "User input validated",
            "merge_mode": merge_mode,
            "price_source": price_source,
            "input_paths": {
                "load_csv": str(load_csv),
                "prices_csv": str(prices_csv) if prices_csv else "",
                "scenario_yaml": str(scenario_yaml),
            },
            "resolved_paths": {
                "load_csv": str(merged_path),
                "scenario_yaml": str(scenario_copy),
            },
            "rows_merged": int(len(merged_df)),
            "rows_overlap_when_two_csv": overlap_rows,
            "datetime_min": str(merged_df["datetime"].min()),
            "datetime_max": str(merged_df["datetime"].max()),
            "inferred_step_hours": round(inferred_step_h, 6),
            "repair_step_hours": round(repair_step_h, 6),
            "expected_intervals_in_range": expected_intervals,
            "missing_intervals_estimate": int(missing_intervals_est),
            "gap_repair_enabled": gap_repair_enabled,
            "repaired_intervals_count": int(repaired_intervals),
        }
        summary_path = out_dir / "user_input_summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (out_dir / "user_input_validated.json").write_text(
            json.dumps(summary["resolved_paths"], indent=2, ensure_ascii=False), encoding="utf-8"
        )

        _piece_out = OutputModel(
            message="User input validated",
            load_csv=str(merged_path),
            scenario_yaml=str(scenario_copy),
            run_id=_run_id or "",
            user_input_summary_json=str(summary_path),
        )
        if od is not None and _piece_out is not None:
            return od.finish_piece(
                _piece_out, self.results_path, secrets_data, "UserInputPiece", _stage, run_id=_run_id
            )
        if _stage is not None:
            _stage.cleanup()
        return _piece_out
