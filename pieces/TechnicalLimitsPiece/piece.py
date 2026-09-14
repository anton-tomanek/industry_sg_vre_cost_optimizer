from __future__ import annotations

import json
import shutil
import traceback
from pathlib import Path
from typing import Any

import pandas as pd
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
    from common.capex_budget import apply_capex_axis_budgets, capex_limit_notes
except ModuleNotFoundError:
    from pieces.common.capex_budget import apply_capex_axis_budgets, capex_limit_notes


def _load_consumption_csv(path: Path | str) -> pd.DataFrame:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    df = pd.read_csv(p, sep=None, engine="python", encoding="utf-8-sig")
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    if "datetime" not in df.columns:
        raise ValueError("CSV must contain column: datetime")
    if "load_kw" not in df.columns:
        if "load_mw" in df.columns:
            df["load_kw"] = pd.to_numeric(df["load_mw"], errors="coerce") * 1000.0
        else:
            raise ValueError("CSV must contain load_kw (or load_mw)")
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    df["load_kw"] = pd.to_numeric(df["load_kw"], errors="coerce").fillna(0.0).clip(lower=0.0)
    return df.sort_values("datetime").reset_index(drop=True)


def _infer_timestep_hours(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 0.25
    d = df["datetime"].diff().dt.total_seconds().median() / 3600.0
    return float(d) if pd.notna(d) and d > 0 else 0.25


def _estimate_annual_load_mwh(load_kw: pd.Series, dt_h: float) -> float:
    n = len(load_kw)
    days = n * dt_h / 24.0
    return float(load_kw.sum() * dt_h / 1000.0) * (365.0 / max(days, 1e-6))


def _technical_bounds_kwp_kwh(cfg: dict[str, Any], df: pd.DataFrame, dt_h: float) -> dict[str, Any]:
    eq = cfg.get("equipment") or {}
    c = eq.get("constraints") or {}
    lay = eq.get("layout") or {}
    pv_ref = cfg.get("pv") or {}
    bat_ref = cfg.get("battery") or {}
    load = df["load_kw"].astype(float)
    annual_mwh = _estimate_annual_load_mwh(load, dt_h)

    roof = float(c.get("max_roof_area_m2") or 0)
    ground = float(c.get("max_ground_area_m2") or 0)
    batt_m2 = float(c.get("max_battery_area_m2") or 0)
    inst = c.get("installation") or {}
    mount = str(inst.get("mount_type", "roof")).lower()
    kwp_per_m2 = float(lay.get("kwp_per_m2_roof", 0.18))
    # Energy density of an installed C&I battery system per square metre of
    # allocated site area. The previous default of 0.25 kWh/m2 was low by more
    # than two orders of magnitude: catalog cabinets run 42-95 kWh/m2 of device
    # footprint and containers exceed 130, so any site with a stated battery area
    # was capped at a few tens of kWh and the optimiser could never return a
    # battery. 30 kWh/m2 is the cabinet figure derated for access aisles, fire
    # separation and the power conversion system.
    kwh_per_m2 = float(lay.get("kwh_per_m2_battery_area", 30.0))

    area_pv = ground if mount == "ground" and ground > 1e-6 else roof
    max_kwp = area_pv * kwp_per_m2 if area_pv > 1e-6 else 0.0
    max_kwh = batt_m2 * kwh_per_m2 if batt_m2 > 1e-6 else 0.0
    notes: list[str] = []

    if max_kwp <= 1e-6:
        base_kwp = annual_mwh if annual_mwh > 1e-6 else 300.0
        max_kwp = max(100.0, base_kwp * 1.8)
        notes.append("No area limit for PV: estimated from annual load.")
    else:
        notes.append("PV area limit applied.")

    if max_kwh <= 1e-6:
        daily_kwh = (annual_mwh * 1000.0 / 365.0) if annual_mwh > 1e-6 else 1500.0
        max_kwh = max(100.0, daily_kwh * 0.65)
        notes.append("No battery area limit: estimated from average daily load.")

    if c.get("max_battery_kwh") is not None:
        max_kwh = min(max_kwh, float(c["max_battery_kwh"]))
        notes.append("Hard cap from max_battery_kwh applied.")

    if c.get("roof_load_limit_kg_per_m2") is not None and roof > 1e-6 and mount != "ground":
        max_kwp = min(max_kwp, roof * kwp_per_m2 * 0.92)
        notes.append("Reduced max PV due to roof load limit factor (0.92).")

    eur_kwp = float(pv_ref.get("specific_capex_eur_per_kwp", 800.0))
    eur_kwh = float(bat_ref.get("specific_capex_eur_per_kwh", 400.0))
    cap = apply_capex_axis_budgets(
        c,
        max_kwp=max_kwp,
        max_kwh=max_kwh,
        eur_per_kwp=eur_kwp,
        eur_per_kwh=eur_kwh,
    )
    notes.extend(capex_limit_notes(cap, slovak=False))

    return {
        "max_kwp": max(0.0, max_kwp),
        "max_kwh": max(0.0, max_kwh),
        "max_kwp_budget": cap["max_kwp_budget"],
        "max_kwh_budget": cap["max_kwh_budget"],
        "capex_mode": cap.get("capex_mode"),
        "max_total_capex_eur": cap.get("max_total_capex_eur"),
        "max_capex_eur": cap.get("max_capex_eur") or 0.0,
        "max_pv_capex_eur": cap.get("max_pv_capex_eur"),
        "max_battery_capex_eur": cap.get("max_battery_capex_eur"),
        "annual_load_mwh_est": round(annual_mwh, 3),
        "notes": notes,
    }


class TechnicalLimitsPiece(BasePiece):
    """Calculate technical bounds from scenario constraints."""

    def piece_function(self, input_data: InputModel, secrets_data=None) -> OutputModel:
        _stage = None
        _piece_out = None
        _run_id = None
        if od is not None:
            input_data, _stage = od.stage_inputs(input_data, secrets_data)
            _run_id = od.resolve_run_id(input_data, secrets_data, generate=False)
        csv_path = Path(input_data.load_csv)
        scenario_path = Path(input_data.scenario_yaml)
        out_dir = Path(self.results_path or scenario_path.parent)
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "technical_limits.log"

        def _log(msg: str) -> None:
            text = f"[TechnicalLimitsPiece] {msg}"
            print(text, flush=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")

        _log(f"Input load_csv={csv_path}")
        _log(f"Input scenario_yaml={scenario_path}")
        if not csv_path.is_file():
            _log("ERROR: load_csv file missing")
            raise FileNotFoundError(f"Load CSV not found: {csv_path}")
        if not scenario_path.is_file():
            _log("ERROR: scenario_yaml file missing")
            raise FileNotFoundError(f"Scenario YAML not found: {scenario_path}")

        import yaml

        try:
            cfg = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
            df = _load_consumption_csv(csv_path)
            dt_h = _infer_timestep_hours(df)
            bounds = _technical_bounds_kwp_kwh(cfg, df, dt_h)
            _log(f"Loaded rows={len(df)}, inferred_step_h={dt_h:.6f}")
        except Exception as exc:
            trace = traceback.format_exc()
            (out_dir / "technical_limits_error.txt").write_text(trace, encoding="utf-8")
            _log(f"ERROR during computation: {exc}")
            if od is not None:
                od.cleanup_on_error(self.results_path, secrets_data, "TechnicalLimitsPiece", _stage, run_id=_run_id)
            raise

        out_json = out_dir / "technical_limits.json"
        out_json.write_text(json.dumps(bounds, indent=2, ensure_ascii=False), encoding="utf-8")
        scenario_out = out_dir / Path(scenario_path).name
        if scenario_path.resolve() != scenario_out.resolve():
            shutil.copy2(scenario_path, scenario_out)
        _log(f"Wrote technical limits to {out_json}")
        _piece_out = OutputModel(
            message="Technical limits calculated",
            technical_limits_json=str(out_json),
            scenario_yaml=str(scenario_out),
        )
        if od is not None and _piece_out is not None:
            return od.finish_piece(
                _piece_out, self.results_path, secrets_data, "TechnicalLimitsPiece", _stage, run_id=_run_id
            )
        if _stage is not None:
            _stage.cleanup()
        return _piece_out
