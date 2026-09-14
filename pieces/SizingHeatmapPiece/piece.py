"""Sweep PV and battery sizes and report the economics of every combination.

SizingOptimizationPiece returns a single recommended pair. That is the answer to
"what should we build", but it hides how sharp the optimum is. A board asked to
approve capital wants to see whether the recommendation sits on a plateau, where
adding storage stops paying, and how much of the benefit a cheaper half-sized
system would still capture.

Every cell runs the same full economic simulation the recommendation came from,
against the same AI production forecast rescaled to that cell's array size, so
the grid and the headline result cannot disagree.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from domino.base_piece import BasePiece

try:
    from pieces.simulate_import import load_simulate_module
except ModuleNotFoundError:
    from simulate_import import load_simulate_module

from .models import InputModel, OutputModel

try:
    from common import onedata_io as od
except ModuleNotFoundError:
    try:
        from pieces.common import onedata_io as od
    except ModuleNotFoundError:
        od = None

try:
    from common.capex_budget import (
        configuration_over_budget,
        copy_capex_bound_fields,
        has_capex_limit,
    )
except ModuleNotFoundError:
    from pieces.common.capex_budget import (
        configuration_over_budget,
        copy_capex_bound_fields,
        has_capex_limit,
    )


class SizingHeatmapPiece(BasePiece):
    """Build the PV x battery economics grid behind the sizing recommendation."""

    def piece_function(self, input_data: InputModel, secrets_data=None) -> OutputModel:
        _stage = None
        _run_id = None
        if od is not None:
            input_data, _stage = od.stage_inputs(input_data, secrets_data)
            _run_id = od.resolve_run_id(input_data, secrets_data, generate=False)

        csv_path = Path(input_data.load_csv)
        scenario_path = Path(input_data.scenario_yaml)
        out_dir = Path(self.results_path or scenario_path.parent)
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "sizing_heatmap.log"

        def _log(msg: str) -> None:
            text = f"[SizingHeatmapPiece] {msg}"
            print(text, flush=True)
            try:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except Exception:
                pass

        try:
            if not csv_path.is_file():
                raise FileNotFoundError(f"Load CSV not found: {csv_path}")
            if not scenario_path.is_file():
                raise FileNotFoundError(f"Scenario YAML not found: {scenario_path}")

            sim = load_simulate_module()
            cfg = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
            sim._apply_system_scope(cfg)
            df = sim.load_consumption_csv(csv_path)
            dt_h = sim.infer_timestep_hours(df)

            reference_kwp = float((cfg.get("pv") or {}).get("installed_kwp", 0.0) or 0.0)
            pv_profile = sim.load_pv_profile_per_kwp(
                input_data.virtual_solar_csv, df, reference_kwp=reference_kwp
            )
            profile_source = "ai_forecast" if pv_profile is not None else "synthetic_fallback"
            strategy_thresholds = sim.load_battery_strategy_thresholds(
                getattr(input_data, "battery_strategy_recommendation_json", None)
            )
            _log(f"PV profile source: {profile_source}")
            _log(
                "Battery strategy thresholds: "
                + (str(strategy_thresholds) if strategy_thresholds else "fallback quantiles in dispatch")
            )

            bounds = self._bounds(sim, cfg, df, dt_h, input_data.technical_limits_json)
            max_kwp = float(bounds.get("max_kwp") or 0.0)
            max_kwh = float(bounds.get("max_kwh") or 0.0)
            cons = (cfg.get("equipment") or {}).get("constraints") or {}
            max_capex = float(
                bounds.get("max_capex_eur")
                or cons.get("max_capex_eur")
                or 0.0
            )
            max_total_capex = bounds.get("max_total_capex_eur")
            if max_total_capex is None:
                max_total_capex = cons.get("max_total_capex_eur")
            max_pv_capex = bounds.get("max_pv_capex_eur")
            if max_pv_capex is None:
                max_pv_capex = cons.get("max_pv_capex_eur")
            max_battery_capex = bounds.get("max_battery_capex_eur")
            if max_battery_capex is None:
                max_battery_capex = cons.get("max_battery_capex_eur")
            eur_kwp = float((cfg.get("pv") or {}).get("specific_capex_eur_per_kwp", 800.0))
            eur_kwh = float((cfg.get("battery") or {}).get("specific_capex_eur_per_kwh", 400.0))
            _log(f"Sweep bounds: max_kwp={max_kwp:.1f}, max_kwh={max_kwh:.1f}")

            analysis = cfg.get("analysis") or {}
            years = int(analysis.get("amortization_years", 12))
            discount_rate = float(analysis.get("discount_rate", 0.08))
            objective = str(
                ((cfg.get("equipment") or {}).get("auto") or {}).get("objective", "max_npv")
            ).lower()
            axis_bias = (
                "large"
                if objective in (
                    "max_annual_savings",
                    "max_savings",
                    "annual_savings",
                    "highest_savings",
                )
                else "uniform"
            )
            auto = (cfg.get("equipment") or {}).get("auto") or {}
            kwp_step = float(auto.get("kwp_step") or 100.0)
            kwh_step = float(auto.get("kwh_step") or 200.0)
            sized_kwp = float((cfg.get("pv") or {}).get("installed_kwp") or 0.0)
            sized_kwh = float((cfg.get("battery") or {}).get("energy_kwh") or 0.0)
            kwp_axis = self._axis(max_kwp, int(input_data.pv_steps), bias=axis_bias)
            kwh_axis = self._battery_axis(
                max_kwh, int(input_data.battery_steps), bias=axis_bias, step=kwh_step
            )
            kwp_axis = self._inject_search_steps(kwp_axis, max_kwp, kwp_step, count=3)
            budget_kwp = float(bounds.get("max_kwp_budget") or 0.0)
            budget_kwh = float(bounds.get("max_kwh_budget") or 0.0)
            extras_kwp = [v for v in (budget_kwp, sized_kwp) if v > 1e-6 and v <= max_kwp + 1e-6]
            extras_kwh = [v for v in (budget_kwh, sized_kwh) if v > 1e-6 and v <= max_kwh + 1e-6]
            if extras_kwp:
                kwp_axis = sorted({*kwp_axis, *[float(round(v)) for v in extras_kwp]})
            if extras_kwh:
                kwh_axis = sorted({*kwh_axis, *[float(round(v)) for v in extras_kwh]})
            _log(
                "Axes: pv_kwp="
                + ",".join(str(int(round(v))) for v in kwp_axis)
                + " battery_kwh="
                + ",".join(str(int(round(v))) for v in kwh_axis)
            )

            rows: list[dict] = []
            grids = {
                key: [[None] * len(kwp_axis) for _ in kwh_axis]
                for key in (
                    "annual_savings_eur",
                    "npv_eur",
                    "simple_payback_years",
                    "discounted_payback_years",
                    "total_capex_eur",
                    "self_consumption_pct",
                    "operating_cost_baseline_eur",
                    "operating_cost_optimized_eur",
                    "battery_cycles_per_year",
                    "battery_life_years",
                    "cashflow_after_om_eur",
                    "proposed_mrk_kw",
                    "mrk_cut_kw",
                    "mrk_savings_annual_eur",
                    "peak_after_kw",
                )
            }

            best_budget = None
            best_any = None
            for j, kwh in enumerate(kwh_axis):
                for i, kwp in enumerate(kwp_axis):
                    cell = self._evaluate(
                        sim,
                        cfg,
                        df,
                        kwp,
                        kwh,
                        pv_profile,
                        years,
                        discount_rate,
                        objective,
                        dt_h,
                        strategy_thresholds,
                    )
                    rows.append(cell)
                    for key in grids:
                        grids[key][j][i] = cell.get(key)
                    if not cell["feasible"]:
                        continue
                    if best_any is None or cell["score"] < best_any["score"]:
                        best_any = cell
                    if configuration_over_budget(
                        kwp=kwp,
                        kwh=kwh,
                        budget=bounds,
                        total_capex_eur=cell.get("total_capex_eur"),
                        eur_per_kwp=eur_kwp,
                        eur_per_kwh=eur_kwh,
                    ):
                        continue
                    if best_budget is None or cell["score"] < best_budget["score"]:
                        best_budget = cell

            sized_cell = next(
                (
                    row
                    for row in rows
                    if abs(float(row.get("kwp") or 0) - sized_kwp) <= 1.0
                    and abs(float(row.get("kwh") or 0) - sized_kwh) <= 1.0
                    and row.get("feasible")
                ),
                None,
            )
            best = sized_cell or best_budget
            if best is None and not has_capex_limit(bounds):
                best = best_any
            if best is None:
                zero = next(
                    (row for row in rows if row.get("kwp", 1) <= 1e-9 and row.get("kwh", 1) <= 1e-9),
                    None,
                )
                best = zero or best_any
            if best is None:
                if objective in ("shortest_payback", "min_payback", "payback"):
                    best = min(
                        rows,
                        key=lambda r: r.get("simple_payback_years")
                        if r.get("simple_payback_years") is not None
                        else 1e18,
                    )
                elif objective in (
                    "max_annual_savings",
                    "max_savings",
                    "annual_savings",
                    "highest_savings",
                ):
                    best = max(rows, key=lambda r: r.get("annual_savings_eur") or -1e18)
                else:
                    best = max(rows, key=lambda r: r.get("npv_eur") or -1e18)

            _log(
                f"Evaluated {len(rows)} combinations; best {best['kwp']:.0f} kWp / "
                f"{best['kwh']:.0f} kWh"
            )

            payload = {
                "format": "uc32_sizing_heatmap_v1",
                "objective": objective,
                "pv_profile_source": profile_source,
                "battery_strategy_thresholds": strategy_thresholds,
                "amortization_years": years,
                "discount_rate": discount_rate,
                "mrk": {
                    "current_kw": float((cfg.get("mrk") or {}).get("contract_kw") or 0.0),
                    "fee_eur_per_kw_month": float((cfg.get("mrk") or {}).get("fee_eur_per_kw_month") or 0.0),
                    "safety_margin_pct": float(
                        (cfg.get("mrk") or {}).get("rv_downsizing_safety_margin_pct", 8.0) or 8.0
                    ),
                },
                "axes": {
                    "pv_kwp": kwp_axis,
                    "battery_kwh": kwh_axis,
                },
                "bounds": bounds,
                "capex_mode": bounds.get("capex_mode"),
                "max_capex_eur": max_capex,
                "max_total_capex_eur": max_total_capex,
                "max_pv_capex_eur": max_pv_capex,
                "max_battery_capex_eur": max_battery_capex,
                "grids": grids,
                "recommended": {
                    "pv_kwp": best["kwp"],
                    "battery_kwh": best["kwh"],
                    "annual_savings_eur": best.get("annual_savings_eur"),
                    "npv_eur": best.get("npv_eur"),
                    "simple_payback_years": best.get("simple_payback_years"),
                    "discounted_payback_years": best.get("discounted_payback_years"),
                    "total_capex_eur": best.get("total_capex_eur"),
                    "operating_cost_baseline_eur": best.get("operating_cost_baseline_eur"),
                    "operating_cost_optimized_eur": best.get("operating_cost_optimized_eur"),
                    "battery_cycles_per_year": best.get("battery_cycles_per_year"),
                    "battery_life_years": best.get("battery_life_years"),
                    "cashflow_after_om_eur": best.get("cashflow_after_om_eur"),
                    "proposed_mrk_kw": best.get("proposed_mrk_kw"),
                    "mrk_cut_kw": best.get("mrk_cut_kw"),
                    "mrk_savings_annual_eur": best.get("mrk_savings_annual_eur"),
                    "peak_after_kw": best.get("peak_after_kw"),
                    "current_mrk_kw": best.get("current_mrk_kw"),
                },
                "current_scenario": {
                    "pv_kwp": reference_kwp,
                    "battery_kwh": float((cfg.get("battery") or {}).get("energy_kwh", 0.0) or 0.0),
                },
            }

            heatmap_json = out_dir / "sizing_heatmap.json"
            heatmap_csv = out_dir / "sizing_heatmap.csv"
            heatmap_json.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            pd.DataFrame(rows).to_csv(heatmap_csv, index=False)
            self.display_result = {"file_type": "json", "file_path": str(heatmap_json)}

            piece_out = OutputModel(
                message=(
                    f"Swept {len(kwp_axis)}x{len(kwh_axis)} sizes on the {profile_source}; "
                    f"best {best['kwp']:.0f} kWp / {best['kwh']:.0f} kWh"
                ),
                heatmap_json=str(heatmap_json),
                heatmap_csv=str(heatmap_csv),
                recommended_kwp=float(best["kwp"]),
                recommended_kwh=float(best["kwh"]),
            )
        except Exception as exc:
            (out_dir / "sizing_heatmap_error.txt").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
            _log(f"ERROR: {exc}")
            if od is not None:
                od.cleanup_on_error(
                    self.results_path, secrets_data, "SizingHeatmapPiece", _stage, run_id=_run_id
                )
            raise

        if od is not None:
            return od.finish_piece(
                piece_out, self.results_path, secrets_data, "SizingHeatmapPiece", _stage,
                run_id=_run_id,
            )
        if _stage is not None:
            _stage.cleanup()
        return piece_out

    @staticmethod
    def _bounds(sim, cfg: dict, df: pd.DataFrame, dt_h: float, limits_path: str | None) -> dict:
        bounds = dict(sim.technical_bounds_kwp_kwh(cfg, df, dt_h))
        if limits_path and Path(str(limits_path)).is_file():
            upstream = json.loads(Path(str(limits_path)).read_text(encoding="utf-8")) or {}
            copy_capex_bound_fields(bounds, upstream)
            bounds["source"] = "TechnicalLimitsPiece"
            notes = upstream.get("notes")
            if isinstance(notes, list):
                bounds["notes"] = list(notes)
        else:
            bounds["source"] = "derived_from_load"
        return bounds

    @staticmethod
    def _round_axis(values: list[float], maximum: float) -> list[float]:
        if maximum <= 0:
            return [0.0]
        span = max(maximum / max(len(values), 2), 1.0)
        magnitude = 10 ** max(0, int(np.floor(np.log10(span))))
        rounded = sorted({max(0.0, float(round(v / magnitude) * magnitude)) for v in values})
        exact = float(round(maximum))
        if exact > 0 and all(abs(v - exact) > magnitude * 0.2 for v in rounded):
            rounded.append(exact)
            rounded = sorted(set(rounded))
        if not rounded or rounded[0] != 0.0:
            rounded = [0.0] + [v for v in rounded if v > 1e-9]
        clipped = [v for v in rounded if v <= maximum + 1e-6]
        if exact > 0 and exact <= maximum + 1e-6 and (
            not clipped or abs(clipped[-1] - exact) > magnitude * 0.2
        ):
            clipped.append(exact)
            clipped = sorted(set(clipped))
        return [v for v in clipped if v >= 0]

    @staticmethod
    def _inject_search_steps(
        axis: list[float],
        maximum: float,
        step: float,
        extras: list[float] | None = None,
        count: int = 3,
    ) -> list[float]:
        """Keep the search step visible at the small end (200, 400, … kWh)."""
        extra = [0.0]
        if step and step > 1e-6:
            extra.extend(float(step) * i for i in range(1, max(1, int(count)) + 1))
        extra.extend(extras or [])
        keep = [float(v) for v in extra if 0.0 <= float(v) <= float(maximum) + 1e-6]
        return sorted({float(v) for v in list(axis) + keep})

    @staticmethod
    def _battery_axis(
        maximum: float,
        steps: int,
        bias: str = "uniform",
        step: float | None = None,
    ) -> list[float]:
        """Battery axis from 0 to the technical limit, including small packs.

        Highest annual savings still densifies the large end, but the form step
        (and typical 200/400 kWh cabinets) stay on the map so a 0 kWh pick is
        not the only small option.
        """
        if maximum <= 0:
            return [0.0]
        base = SizingHeatmapPiece._axis(maximum, max(4, int(steps)), bias=bias)
        return SizingHeatmapPiece._inject_search_steps(
            base,
            maximum,
            float(step or 200.0),
            extras=[200.0, 400.0],
            count=3,
        )

    @staticmethod
    def _axis(maximum: float, steps: int, bias: str = "uniform") -> list[float]:
        """Axis from zero to the technical limit, rounded to readable sizes.

        Zero is included on both axes so the grid also answers "PV only" and
        "battery only". ``bias='large'`` puts more ticks near the maximum.
        """
        if maximum <= 0:
            return [0.0]
        steps = max(2, int(steps))
        if bias == "large":
            ts = np.linspace(0.0, 1.0, steps)
            raw = [float(maximum * (t ** 1.65)) for t in ts]
        else:
            raw = [float(v) for v in np.linspace(0.0, maximum, steps)]
        axis = SizingHeatmapPiece._round_axis(raw, maximum)
        if bias == "large" and maximum > 0:
            floor = maximum * 0.08
            axis = [v for v in axis if v <= 1e-9 or v >= floor]
            if 0.0 not in axis:
                axis = [0.0] + axis
        return axis

    @staticmethod
    def _evaluate(
        sim,
        cfg: dict,
        df: pd.DataFrame,
        kwp: float,
        kwh: float,
        pv_profile,
        years: int,
        discount_rate: float,
        objective: str,
        dt_h: float,
        strategy_thresholds=None,
    ) -> dict:
        import copy

        trial = copy.deepcopy(cfg)
        trial.setdefault("pv", {})["installed_kwp"] = float(kwp)
        trial.setdefault("battery", {})["energy_kwh"] = float(kwh)
        trial["use_pv"] = kwp > 0
        trial["use_battery"] = kwh > 0

        cell = {
            "kwp": float(kwp),
            "kwh": float(kwh),
            "feasible": False,
            "score": float("inf"),
            "annual_savings_eur": None,
            "npv_eur": None,
            "simple_payback_years": None,
            "discounted_payback_years": None,
            "total_capex_eur": None,
            "self_consumption_pct": None,
            "operating_cost_baseline_eur": None,
            "operating_cost_optimized_eur": None,
            "battery_cycles_per_year": None,
            "battery_life_years": None,
            "cashflow_after_om_eur": None,
            "proposed_mrk_kw": None,
            "mrk_cut_kw": None,
            "mrk_savings_annual_eur": None,
            "peak_after_kw": None,
            "current_mrk_kw": None,
        }
        mrk_cfg = trial.get("mrk") or {}
        contract_kw = float(mrk_cfg.get("contract_kw") or 0.0)
        cell["current_mrk_kw"] = contract_kw
        if kwp <= 0 and kwh <= 0:
            peak_now = float(df["load_kw"].max()) if "load_kw" in df.columns and len(df) else None
            cell.update(
                {
                    "annual_savings_eur": 0.0,
                    "npv_eur": 0.0,
                    "total_capex_eur": 0.0,
                    "proposed_mrk_kw": contract_kw,
                    "mrk_cut_kw": 0.0,
                    "mrk_savings_annual_eur": 0.0,
                    "peak_after_kw": peak_now,
                }
            )
            return cell

        try:
            bundle = sim._sim_bundle(
                trial,
                df,
                pv_profile_per_kwp=pv_profile,
                battery_strategy_thresholds=strategy_thresholds,
            )
            score, fin = sim._score_financials(
                bundle, dr=discount_rate, years=years, objective=objective
            )
        except Exception:
            return cell

        days = float(bundle.get("days_in_sample") or 365.0)
        ann = 365.0 / max(days, 1e-9)
        optimized = sim.primary_optimized_scenario(bundle)
        baseline = bundle.get("baseline") or {}
        om = float((trial.get("pv") or {}).get("om_eur_per_kwp_year", 0.0) or 0.0) * float(kwp)
        om += float((trial.get("battery") or {}).get("om_eur_per_kwh_year", 0.0) or 0.0) * float(kwh)
        annual_sav = fin.get("annual_net_cashflow_eur")
        if annual_sav is None:
            gross = fin.get("annual_operating_savings_eur")
            annual_sav = (float(gross) - om) if gross is not None else None
        cycles_year = None
        life_years = None
        if kwh > 1e-6 and optimized is not None:
            soh = sim.build_battery_soh_assessment(
                equivalent_cycles_period=float(optimized.get("equivalent_full_cycles", 0.0) or 0.0),
                days_in_sample=days,
                battery_cfg=trial.get("battery") or {},
            )
            cycles_year = soh.get("annual_equivalent_cycles_est")
            life_years = soh.get("estimated_life_years_effective")
        cell.update(
            {
                "score": float(score),
                "feasible": bool(np.isfinite(score)),
                "annual_savings_eur": annual_sav,
                "npv_eur": fin.get("npv_eur"),
                "simple_payback_years": fin.get("simple_payback_years"),
                "discounted_payback_years": fin.get("discounted_payback_years"),
                "total_capex_eur": fin.get("total_capex_eur"),
                "operating_cost_baseline_eur": round(float(baseline.get("total_operating_eur") or 0.0) * ann, 2),
                "operating_cost_optimized_eur": round(
                    float((optimized or {}).get("total_operating_eur") or 0.0) * ann, 2
                ),
                "battery_cycles_per_year": cycles_year,
                "battery_life_years": life_years,
                "cashflow_after_om_eur": (
                    round(float(annual_sav), 2) if annual_sav is not None else None
                ),
            }
        )
        fee = float(mrk_cfg.get("fee_eur_per_kw_month") or 0.0)
        safety = float(mrk_cfg.get("rv_downsizing_safety_margin_pct", 8.0) or 8.0)
        try:
            rv = sim.mrk_peak_reduction_and_rv_opportunity(
                baseline.get("monthly_peak_detail") or {},
                (optimized or {}).get("monthly_peak_detail") or {},
                contract_kw=contract_kw,
                fee_eur_per_kw_month=fee,
                safety_margin_pct=safety,
            )
            cell["proposed_mrk_kw"] = rv.get("recommended_rv_kw_conservative")
            cell["mrk_cut_kw"] = rv.get("rv_downsizing_potential_kw")
            cell["mrk_savings_annual_eur"] = rv.get("estimated_fixed_rv_fee_savings_eur_per_year")
            cell["peak_after_kw"] = rv.get("max_monthly_peak_import_kw_after_optimization")
        except Exception:
            pass

        if pv_profile is not None and kwp > 0:
            produced = np.asarray(pv_profile, dtype=float) * kwp
            load = df["load_kw"].astype(float).to_numpy()
            generated = float(produced.sum() * dt_h)
            if generated > 0:
                self_used = float(np.minimum(produced, load).sum() * dt_h)
                cell["self_consumption_pct"] = round(self_used / generated * 100.0, 1)

        return cell
