from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import traceback

import numpy as np
import pandas as pd
from domino.base_piece import BasePiece

from .models import InputModel, OutputModel
from .render import build_html, unit_prices_from_heatmap

try:
    from common import onedata_io as od
except ModuleNotFoundError:
    try:
        from pieces.common import onedata_io as od
    except ModuleNotFoundError:
        od = None


class DashboardPiece(BasePiece):
    """Build finance-focused dashboard payload for CFO decisions."""

    @staticmethod
    def _read_json(path: str | None) -> dict | None:
        """Optional side inputs must never sink the dashboard.

        The heatmap, calibration report and equipment ranking each enrich the
        output but none is essential, and a run without SHMÚ coverage or with a
        catalog outage should still produce a report.
        """
        if not path:
            return None
        p = Path(str(path).strip())
        if not p.is_file() or p.stat().st_size == 0:
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _build_consumption(profile_path: Path, dispatch_path: Path) -> dict:
        """Daily and monthly grid energy with and without the proposed plant.

        The comparison the board asks for is energy bought from the grid, not
        site load. Site load barely changes; what changes is how much of it the
        meter still sees. Interval kWh is summed to the day and the month so
        the chart shows consumption, not a 15-minute mean that looks like noise.
        """
        empty = {
            "daily": {"x": [], "without": [], "with": []},
            "monthly": {"x": [], "without": [], "with": []},
            "totals": {},
            "interval": {},
            "timestep": {},
            "legacy_chart": {"title": "", "x": [], "series": []},
        }
        frame = DashboardPiece._read_profile(profile_path)
        if frame is None:
            frame = DashboardPiece._read_dispatch(dispatch_path)
        if frame is None or frame.empty:
            return empty

        frame = frame.copy()
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
        frame = frame.dropna(subset=["datetime"]).sort_values("datetime")
        frame["without_kwh"] = pd.to_numeric(frame["without_kwh"], errors="coerce").fillna(0.0).clip(lower=0.0)
        frame["with_kwh"] = pd.to_numeric(frame["with_kwh"], errors="coerce").fillna(0.0).clip(lower=0.0)

        daily = (
            frame.assign(day=frame["datetime"].dt.floor("D"))
            .groupby("day", as_index=False)[["without_kwh", "with_kwh"]]
            .sum()
        )
        monthly = (
            frame.assign(month=frame["datetime"].dt.to_period("M").dt.to_timestamp())
            .groupby("month", as_index=False)[["without_kwh", "with_kwh"]]
            .sum()
        )
        if len(monthly) >= 4:
            core = monthly["without_kwh"].iloc[:-1]
            if float(monthly["without_kwh"].iloc[-1]) < 0.4 * float(core.median()):
                monthly = monthly.iloc[:-1]

        without_total = float(frame["without_kwh"].sum())
        with_total = float(frame["with_kwh"].sum())
        dt_h = 0.25
        if len(frame) >= 2:
            step = frame["datetime"].diff().dt.total_seconds().median()
            if pd.notna(step) and step > 0:
                dt_h = float(step) / 3600.0
        step_minutes = int(round(dt_h * 60.0))
        start = frame["datetime"].iloc[0]
        interval = {
            "start": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "step_minutes": step_minutes,
            "n": int(len(frame)),
            "without_kw": (frame["without_kwh"] / max(dt_h, 1e-9)).round(2).tolist(),
            "with_kw": (frame["with_kwh"] / max(dt_h, 1e-9)).round(2).tolist(),
        }
        totals = {
            "without_kwh": round(without_total, 1),
            "with_kwh": round(with_total, 1),
            "saved_kwh": round(without_total - with_total, 1),
            "saved_pct": round((1.0 - with_total / without_total) * 100.0, 1) if without_total > 0 else None,
            "days": int(len(daily)),
            "intervals": int(len(frame)),
            "step_minutes": step_minutes,
        }

        legacy = {
            "title": "Spotreba zo siete: bez FVE a batérie vs s FVE a batériou",
            "resolution": "daily_sum_kwh",
            "x": daily["day"].dt.strftime("%Y-%m-%d").tolist(),
            "series": [
                {
                    "name": "Bez FVE a batérie",
                    "unit": "kWh/deň",
                    "values": daily["without_kwh"].round(1).tolist(),
                },
                {
                    "name": "S FVE a batériou",
                    "unit": "kWh/deň",
                    "values": daily["with_kwh"].round(1).tolist(),
                },
            ],
        }
        return {
            "daily": {
                "x": daily["day"].dt.strftime("%Y-%m-%d").tolist(),
                "without": daily["without_kwh"].round(1).tolist(),
                "with": daily["with_kwh"].round(1).tolist(),
                "unit": "kWh/deň",
            },
            "monthly": {
                "x": monthly["month"].dt.strftime("%Y-%m").tolist(),
                "without": monthly["without_kwh"].round(1).tolist(),
                "with": monthly["with_kwh"].round(1).tolist(),
                "unit": "kWh/mesiac",
            },
            "totals": totals,
            "interval": interval,
            "timestep": {
                "step_minutes": step_minutes,
                "step_hours": round(dt_h, 6),
                "intervals": int(len(frame)),
                "period_start": str(frame["datetime"].iloc[0]),
                "period_end": str(frame["datetime"].iloc[-1]),
            },
            "legacy_chart": legacy,
        }

    @staticmethod
    def _read_profile(path: Path) -> pd.DataFrame | None:
        if not path.is_file():
            return None
        raw = pd.read_csv(path)
        if "datetime" not in raw.columns:
            return None
        without = next(
            (c for c in ("baseline_energy_kwh_interval", "baseline_kwh") if c in raw.columns),
            None,
        )
        with_ = next(
            (c for c in ("optimized_energy_kwh_interval", "optimized_kwh") if c in raw.columns),
            None,
        )
        if without is None or with_ is None:
            return None
        return raw[["datetime", without, with_]].rename(
            columns={without: "without_kwh", with_: "with_kwh"}
        )

    @staticmethod
    def _read_dispatch(path: Path) -> pd.DataFrame | None:
        """Fall back to BatterySim dispatch when SimulatePiece left no profile."""
        if not path.is_file():
            return None
        raw = pd.read_csv(path)
        if "datetime" not in raw.columns or "load_kw" not in raw.columns:
            return None
        if "pv_battery_grid_kw" not in raw.columns:
            return None
        dt_h = 0.25
        if len(raw) >= 2:
            stamps = pd.to_datetime(raw["datetime"], errors="coerce")
            step = stamps.diff().dt.total_seconds().median()
            if pd.notna(step) and step > 0:
                dt_h = float(step) / 3600.0
        load = pd.to_numeric(raw["load_kw"], errors="coerce").fillna(0.0).clip(lower=0.0)
        grid = pd.to_numeric(raw["pv_battery_grid_kw"], errors="coerce").fillna(0.0).clip(lower=0.0)
        return pd.DataFrame(
            {
                "datetime": raw["datetime"],
                "without_kwh": load * dt_h,
                "with_kwh": grid * dt_h,
            }
        )

    PRICE_SOURCE_COPY = {
        "okte_dam": (
            "OKTE ISOT — denný trh (DAM)",
            "Ceny boli stiahnuté z verejného rozhrania OKTE ISOT (isot.okte.sk) "
            "za rovnaké obdobie ako nameraný odber. Hodinová cena denného trhu "
            "je priradená k 15-minútovým intervalom odberu (spätné priradenie).",
        ),
        "load_csv": (
            "Cenník v profile odberu",
            "Použité boli ceny zo stĺpca price_eur_per_kwh v nahratom súbore odberu.",
        ),
        "prices_csv": (
            "Samostatný cenník",
            "Ceny pochádzajú zo samostatného súboru, zladeného s časovými značkami odberu.",
        ),
    }

    @staticmethod
    def _read_summary(path: str | None, load_path: Path | None) -> dict:
        candidates = []
        if path and str(path).strip():
            candidates.append(Path(str(path).strip()))
        if load_path is not None:
            candidates.append(load_path.parent / "user_input_summary.json")
        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_size > 0:
                try:
                    data = json.loads(candidate.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, dict):
                    return data
        return {}

    @classmethod
    def _build_prices(cls, load_path: Path, summary: dict) -> dict:
        """Price series actually used in the economics — for the finance reader."""
        empty = {
            "available": False,
            "source": None,
            "source_label": "Ceny nie sú k dispozícii",
            "source_detail": "Zlúčený profil odberu a cien sa do dashboardu nedostal.",
            "period_start": None,
            "period_end": None,
            "rows": 0,
            "priced_rows": 0,
            "coverage_pct": None,
            "mean_eur_per_mwh": None,
            "weighted_mean_eur_per_mwh": None,
            "min_eur_per_mwh": None,
            "max_eur_per_mwh": None,
            "p10_p50_p90_eur_per_mwh": [],
            "mean_eur_per_kwh": None,
            "monthly": {"x": [], "mean_eur_per_mwh": [], "weighted_eur_per_mwh": []},
            "hourly": {"x": [], "mean_eur_per_mwh": []},
        }
        if not load_path.is_file():
            return empty
        try:
            raw = pd.read_csv(load_path)
        except Exception:
            return empty
        cols = {str(c).strip().lower().replace(" ", "_"): c for c in raw.columns}
        dt_key = next((cols[k] for k in ("datetime", "date_time", "timestamp") if k in cols), None)
        price_key = cols.get("price_eur_per_kwh") or cols.get("price_eur_kwh")
        if price_key is None and "price_eur_mwh" in cols:
            raw = raw.copy()
            raw["price_eur_per_kwh"] = pd.to_numeric(raw[cols["price_eur_mwh"]], errors="coerce") / 1000.0
            price_key = "price_eur_per_kwh"
        if dt_key is None or price_key is None:
            return empty

        frame = pd.DataFrame(
            {
                "datetime": pd.to_datetime(raw[dt_key], errors="coerce"),
                "price": pd.to_numeric(raw[price_key], errors="coerce"),
            }
        )
        load_key = cols.get("load_kw")
        if load_key is not None:
            frame["load_kw"] = pd.to_numeric(raw[load_key], errors="coerce").fillna(0.0).clip(lower=0.0)
        else:
            frame["load_kw"] = 1.0
        frame = frame.dropna(subset=["datetime"]).sort_values("datetime")
        priced = frame.dropna(subset=["price"])
        if priced.empty:
            return empty

        mwh = priced["price"].astype(float) * 1000.0
        load_w = priced["load_kw"].astype(float).clip(lower=0.0)
        weights = load_w if float(load_w.sum()) > 0 else pd.Series(1.0, index=priced.index)
        source = str(summary.get("price_source") or "").strip() or "unknown"
        label, detail = cls.PRICE_SOURCE_COPY.get(
            source,
            (
                "Ceny v zlúčenom profile",
                "Zdroj cien nie je v katalogu označený; nižšie sú hodnoty, s ktorými model skutočne počítal.",
            ),
        )
        priced = priced.copy()
        priced["mwh"] = mwh.to_numpy()
        priced["month"] = priced["datetime"].dt.to_period("M").dt.to_timestamp()
        priced["hour"] = priced["datetime"].dt.hour
        monthly_rows = []
        for month, group in priced.groupby("month", sort=True):
            w = group["load_kw"].to_numpy(dtype=float)
            p = group["mwh"].to_numpy(dtype=float)
            monthly_rows.append(
                {
                    "month": month,
                    "mean": float(p.mean()),
                    "weighted": float(np.average(p, weights=np.maximum(w, 1e-9))),
                }
            )
        hourly_rows = []
        for hour, group in priced.groupby("hour", sort=True):
            hourly_rows.append({"hour": int(hour), "mean": float(group["mwh"].mean())})

        coverage = 100.0 * len(priced) / max(len(frame), 1)
        return {
            "available": True,
            "source": source,
            "source_label": label,
            "source_detail": detail,
            "merge_mode": summary.get("merge_mode"),
            "period_start": str(priced["datetime"].min()),
            "period_end": str(priced["datetime"].max()),
            "rows": int(len(frame)),
            "priced_rows": int(len(priced)),
            "coverage_pct": round(coverage, 1),
            "mean_eur_per_mwh": round(float(mwh.mean()), 2),
            "weighted_mean_eur_per_mwh": round(float(np.average(mwh, weights=np.maximum(weights, 1e-9))), 2),
            "min_eur_per_mwh": round(float(mwh.min()), 2),
            "max_eur_per_mwh": round(float(mwh.max()), 2),
            "p10_p50_p90_eur_per_mwh": [round(float(np.percentile(mwh, p)), 2) for p in (10, 50, 90)],
            "mean_eur_per_kwh": round(float(priced["price"].mean()), 5),
            "monthly": {
                "x": [row["month"].strftime("%Y-%m") for row in monthly_rows],
                "mean_eur_per_mwh": [round(row["mean"], 2) for row in monthly_rows],
                "weighted_eur_per_mwh": [round(row["weighted"], 2) for row in monthly_rows],
            },
            "hourly": {
                "x": [f"{row['hour']:02d}:00" for row in hourly_rows],
                "mean_eur_per_mwh": [round(row["mean"], 2) for row in hourly_rows],
            },
        }

    GRID_KG_CO2_PER_KWH = 0.155
    GRID_CO2_SOURCE = (
        "Emisná intenzita elektriny zo siete 0,155 kg CO₂/kWh (EEA, greenhouse gas "
        "emission intensity of electricity generation, Slovensko). Úspora v tonách = "
        "(kWh nákupu zo siete bez systému − kWh nákupu so systémom) × 0,155 / 1 000. "
        "Počíta sa len vyhnutý nákup zo siete, nie životný cyklus panelov a batérie."
    )

    @classmethod
    def _build_peaks(cls, report: dict) -> dict:
        scenarios = report.get("scenarios") or {}
        baseline = (scenarios.get("baseline") or {}).get("monthly_peak_detail") or {}
        plant = (
            (scenarios.get("optimized") or {}).get("monthly_peak_detail")
            or (scenarios.get("pv_and_battery") or {}).get("monthly_peak_detail")
            or {}
        )
        months = sorted(set(baseline) | set(plant))
        without = [
            round(float((baseline.get(m) or {}).get("monthly_peak_kw") or 0.0), 1) for m in months
        ]
        with_ = [
            round(float((plant.get(m) or {}).get("monthly_peak_kw") or 0.0), 1) for m in months
        ]
        if len(without) >= 4:
            core = sorted(without[:-1])
            median = core[len(core) // 2]
            if without[-1] < 0.4 * max(median, 1.0):
                months, without, with_ = months[:-1], without[:-1], with_[:-1]
        mrk = report.get("mrk_and_rv") or {}
        return {
            "x": months,
            "without": without,
            "with": with_,
            "contract_kw": mrk.get("current_contract_rv_kw"),
            "proposed_kw": mrk.get("recommended_rv_kw_conservative"),
            "unit": "kW",
        }

    @classmethod
    def _build_capex(cls, report: dict, heatmap: dict | None) -> dict:
        cap = report.get("capex_inputs") or {}
        resolved = ((report.get("equipment") or {}).get("resolved") or {})
        rec = (heatmap or {}).get("recommended") or {}
        resolved_kwp = float(resolved.get("installed_kwp") or 0.0)
        resolved_kwh = float(resolved.get("energy_kwh") or 0.0)
        pv_capex = float(cap.get("pv_capex_eur") or 0.0)
        bat_capex = float(cap.get("battery_capex_eur") or 0.0)
        eur_kwp = cap.get("specific_capex_eur_per_kwp")
        eur_kwh = cap.get("specific_capex_eur_per_kwh")
        try:
            eur_kwp = float(eur_kwp) if eur_kwp is not None else None
        except (TypeError, ValueError):
            eur_kwp = None
        try:
            eur_kwh = float(eur_kwh) if eur_kwh is not None else None
        except (TypeError, ValueError):
            eur_kwh = None
        if eur_kwp is None and resolved_kwp > 1e-9:
            eur_kwp = pv_capex / resolved_kwp
        if eur_kwh is None and resolved_kwh > 1e-9:
            eur_kwh = bat_capex / resolved_kwh
        inf_kwp, inf_kwh = unit_prices_from_heatmap(heatmap)
        if eur_kwp is None:
            eur_kwp = inf_kwp
        if eur_kwh is None:
            eur_kwh = inf_kwh
        kwp = float(rec.get("pv_kwp") if rec.get("pv_kwp") is not None else resolved_kwp)
        kwh = float(rec.get("battery_kwh") if rec.get("battery_kwh") is not None else resolved_kwh)
        if eur_kwp is not None:
            pv_capex = kwp * eur_kwp
        if eur_kwh is not None:
            bat_capex = kwh * eur_kwh
        elif kwh <= 1e-9:
            bat_capex = 0.0
        return {
            "pv_kwp": kwp,
            "battery_kwh": kwh,
            "eur_per_kwp": round(eur_kwp, 2) if eur_kwp is not None else None,
            "eur_per_kwh": round(eur_kwh, 2) if eur_kwh is not None else None,
            "pv_capex_eur": round(pv_capex, 2),
            "battery_capex_eur": round(bat_capex, 2),
            "total_capex_eur": round(pv_capex + bat_capex, 2),
        }

    @classmethod
    def _build_co2(cls, consumption: dict) -> dict:
        saved = float(((consumption.get("totals") or {}).get("saved_kwh") or 0.0))
        kg = saved * cls.GRID_KG_CO2_PER_KWH
        return {
            "saved_kwh": round(saved, 1),
            "kg_co2_per_kwh": cls.GRID_KG_CO2_PER_KWH,
            "t_co2": round(kg / 1000.0, 2),
            "method": cls.GRID_CO2_SOURCE,
        }

    def piece_function(self, input_data: InputModel, secrets_data=None) -> OutputModel:
        _stage = None
        _piece_out = None
        _run_id = None
        if od is not None:
            input_data, _stage = od.stage_inputs(input_data, secrets_data)
            _run_id = od.resolve_run_id(input_data, secrets_data, generate=False)
        rep_path = Path((input_data.report_json or "").strip())
        kpi_path = Path((input_data.kpi_results_csv or "").strip())
        inv_path = Path((input_data.investment_evaluation_csv or "").strip())
        out_dir = Path(self.results_path or rep_path.parent)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "dashboard_started.txt").write_text(
            f"report_json={rep_path}\nkpi_results_csv={kpi_path}\n"
            f"investment_evaluation_csv={inv_path}\nresults_path={self.results_path}\n",
            encoding="utf-8",
        )
        log_path = out_dir / "dashboard.log"

        def _log(msg: str) -> None:
            text = f"[DashboardPiece] {msg}"
            print(text, flush=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")

        _log(f"Input report_json={rep_path}")
        _log(f"Input kpi_results_csv={kpi_path}")
        _log(f"Input investment_evaluation_csv={inv_path}")
        if not rep_path.is_file():
            raise FileNotFoundError(f"Report JSON not found: {rep_path}")
        if not kpi_path.is_file():
            raise FileNotFoundError(f"KPI CSV not found: {kpi_path}")
        if not inv_path.is_file():
            raise FileNotFoundError(f"Investment CSV not found: {inv_path}")

        try:
            rep = json.loads(rep_path.read_text(encoding="utf-8"))
            kpi_df = pd.read_csv(kpi_path)
            inv_df = pd.read_csv(inv_path)

            exec_ = rep.get("executive_summary") or {}
            mrk = rep.get("mrk_and_rv") or {}
            unc = rep.get("uncertainty_assessment") or {}
            inv = (inv_df.to_dict(orient="records") or [{}])[0]
            art = rep.get("artifacts") or {}
            profile_path = Path(art.get("baseline_vs_optimized_profile_csv") or "")
            consumption = self._build_consumption(
                profile_path=profile_path,
                dispatch_path=Path((input_data.battery_dispatch_csv or "").strip() or ""),
            )
            load_path = Path((getattr(input_data, "load_csv", None) or "").strip() or "")
            price_summary = self._read_summary(
                getattr(input_data, "user_input_summary_json", None),
                load_path if load_path.is_file() else None,
            )
            prices = self._build_prices(load_path, price_summary)
            # Kept for older consumers of dashboard_data.json.
            chart = consumption.get("legacy_chart") or {
                "title": "Spotreba zo siete: bez FVE a batérie vs s FVE a batériou",
                "x": [],
                "series": [],
            }

            payload = {
                "format": "cfo_finance_dashboard_v1",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "decision_kpis": {
                    "operating_cost_baseline_eur": exec_.get("operating_cost_baseline_eur"),
                    "operating_cost_with_pv_battery_eur": exec_.get("operating_cost_pv_battery_eur"),
                    "operating_savings_period_eur": exec_.get("operating_savings_eur_period"),
                    "operating_savings_annual_estimate_eur": exec_.get("operating_savings_eur_per_year_estimate"),
                    "total_capex_eur": inv.get("total_capex_eur"),
                    "simple_payback_years": inv.get("simple_payback_years"),
                    "discounted_payback_years": inv.get("discounted_payback_years"),
                    "npv_operating_eur": inv.get("npv_operating_eur"),
                    "p50_annual_savings_eur": unc.get("p50_annual_savings_eur"),
                    "p90_annual_savings_eur": unc.get("p90_annual_savings_eur"),
                    "p50_npv_eur": unc.get("p50_npv_eur"),
                    "p90_npv_eur": unc.get("p90_npv_eur"),
                    "irr_pct": unc.get("irr_pct"),
                    "p90_irr_pct": unc.get("p90_irr_pct"),
                    "probability_npv_positive": unc.get("probability_npv_positive"),
                    "uncertainty_method": unc.get("method"),
                    "rv_downsizing_potential_kw": mrk.get("rv_downsizing_potential_kw"),
                    "rv_fixed_fee_savings_period_eur": mrk.get("estimated_fixed_rv_fee_savings_if_resized_eur_for_period"),
                    "proposed_mrk_kw": mrk.get("recommended_rv_kw_conservative"),
                    "current_mrk_kw": mrk.get("current_contract_rv_kw"),
                    "mrk_savings_annual_eur": mrk.get("estimated_fixed_rv_fee_savings_eur_per_year"),
                    "mrk_peak_after_kw": mrk.get("max_monthly_peak_import_kw_after_optimization"),
                    "mrk_fee_eur_per_kw_month": mrk.get("fee_eur_per_kw_month"),
                    "mrk_safety_margin_pct": mrk.get("safety_margin_pct"),
                    "mrk_peak_reserve_enabled": ((rep.get("mrk_peak_reserve") or {}).get("enabled")),
                    "mrk_peak_reserve_reason": ((rep.get("mrk_peak_reserve") or {}).get("reason")),
                    "mrk_peak_reserve_kwh": ((rep.get("mrk_peak_reserve") or {}).get("reserve_kwh")),
                    "mrk_peak_reserve_benefit_eur": ((rep.get("mrk_peak_reserve") or {}).get("benefit_eur_year")),
                    "mrk_peak_reserve_cost_eur": ((rep.get("mrk_peak_reserve") or {}).get("cost_eur_year")),
                    "trading_only_annual_margin_eur_estimate": ((rep.get("trading_only_analysis") or {}).get("annual_margin_eur_estimate")),
                    "battery_annual_equivalent_cycles_est": ((rep.get("battery_lifetime_assessment") or {}).get("annual_equivalent_cycles_est")),
                    "battery_estimated_life_years_effective": ((rep.get("battery_lifetime_assessment") or {}).get("estimated_life_years_effective")),
                    "battery_estimated_life_years_by_cycles": ((rep.get("battery_lifetime_assessment") or {}).get("estimated_life_years_by_cycles")),
                    "battery_cycle_life_at_eol": ((rep.get("battery_lifetime_assessment") or {}).get("cycle_life_at_eol")),
                    "battery_calendar_life_years": ((rep.get("battery_lifetime_assessment") or {}).get("calendar_life_years")),
                    "finance_annual_net_cashflow_after_finance_eur": ((rep.get("finance_layer") or {}).get("annual_net_cashflow_after_finance_eur")),
                    "finance_npv_after_finance_eur": ((rep.get("finance_layer") or {}).get("npv_after_finance_eur")),
                    "battery_kwh": ((rep.get("equipment") or {}).get("resolved") or {}).get("energy_kwh"),
                    "discount_rate": ((rep.get("equipment") or {}).get("investment_metrics") or {}).get("discount_rate"),
                    "analysis_horizon_years": ((rep.get("equipment") or {}).get("investment_metrics") or {}).get("analysis_horizon_years"),
                    "distribution_eur_per_kwh": ((rep.get("economics") or {}).get("distribution_eur_per_kwh")),
                    "feed_in_surplus_eur_per_kwh": ((rep.get("economics") or {}).get("feed_in_surplus_eur_per_kwh")),
                },
                "single_chart": chart,
                "consumption": consumption,
                "prices": prices,
                "mrk_proposal": mrk,
                "battery_lifetime_assessment": (rep.get("battery_lifetime_assessment") or {}),
                "c_rate_sweep": (rep.get("c_rate_sweep") or []),
                "trading_only_analysis": (rep.get("trading_only_analysis") or {}),
                "finance_layer": (rep.get("finance_layer") or {}),
                "quality_flags": {
                    "report_schema_version": ((rep.get("meta") or {}).get("schema_version")),
                    "catalog_url_outage_detected": (((rep.get("equipment") or {}).get("catalog_sync_status") or {}).get("url_outage_detected")),
                    "historical_prices_in_csv": ((rep.get("input_quality") or {}).get("historical_prices_in_csv")),
                    "price_source": prices.get("source"),
                    "price_source_label": prices.get("source_label"),
                },
            }

            heatmap = self._read_json(input_data.heatmap_json)
            calibration = self._read_json(input_data.calibration_json)
            ranking = self._read_json(input_data.catalog_ranked_recommendation_json)
            peaks = self._build_peaks(rep)
            capex = self._build_capex(rep, heatmap)
            co2 = self._build_co2(consumption)
            if heatmap:
                payload["sizing_heatmap"] = heatmap
            if calibration:
                payload["forecast_calibration"] = calibration
            if ranking:
                payload["equipment_ranking"] = ranking
            hardware = ((rep.get("equipment") or {}).get("hardware_recommendation") or {})
            if hardware:
                payload["hardware_recommendation"] = hardware
            payload["peaks"] = peaks
            payload["capex_breakdown"] = capex
            payload["co2"] = co2
            payload["timestep"] = consumption.get("timestep") or {
                "step_hours": (rep.get("meta") or {}).get("dt_hours"),
                "intervals": (rep.get("meta") or {}).get("intervals"),
            }

            out_json = out_dir / "dashboard_data.json"
            out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            _log(f"Wrote dashboard JSON: {out_json}; kpi_rows={len(kpi_df)}")

            site_name = str(((rep.get("meta") or {}).get("site_name") or "")).strip()
            out_html = out_dir / "dashboard.html"
            out_html.write_text(
                build_html(
                    payload,
                    heatmap=heatmap,
                    calibration=calibration,
                    ranking=ranking,
                    site_name=site_name,
                ),
                encoding="utf-8",
            )
            _log(f"Wrote dashboard HTML: {out_html}")
            self.display_result = {"file_type": "html", "file_path": str(out_html)}

            _piece_out = OutputModel(
                dashboard_data_json=str(out_json), dashboard_html=str(out_html)
            )
        except Exception as exc:
            (out_dir / "dashboard_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            _log(f"ERROR during dashboard assembly: {exc}")
            if od is not None:
                od.cleanup_on_error(self.results_path, secrets_data, "DashboardPiece", _stage, run_id=_run_id)
            raise
        if od is not None and _piece_out is not None:
            return od.finish_piece(
                _piece_out, self.results_path, secrets_data, "DashboardPiece", _stage, run_id=_run_id
            )
        if _stage is not None:
            _stage.cleanup()
        return _piece_out
