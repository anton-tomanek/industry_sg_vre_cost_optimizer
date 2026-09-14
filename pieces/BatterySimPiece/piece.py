from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import traceback

import numpy as np
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


class BatterySimPiece(BasePiece):
    """Generate battery SOC profile using dispatch model."""

    @staticmethod
    def _load_simulate_module():
        repo_root = Path(__file__).resolve().parents[2]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        return importlib.import_module("pieces.SimulatePiece.piece")

    @staticmethod
    def _write_best_effort(path: Path, content: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception:
            pass

    def piece_function(self, input_data: InputModel, secrets_data=None) -> OutputModel:
        _stage = None
        _piece_out = None
        _run_id = None
        if od is not None:
            input_data, _stage = od.stage_inputs(input_data, secrets_data)
            _run_id = od.resolve_run_id(input_data, secrets_data, generate=False)
        csv_path = Path(input_data.load_csv)
        scenario_path = Path(input_data.scenario_yaml)
        solar_path = Path(input_data.virtual_solar_csv)
        strategy_path = Path(input_data.battery_strategy_recommendation_json) if input_data.battery_strategy_recommendation_json else None
        out_dir = Path(self.results_path or scenario_path.parent)
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "battery_sim.log"
        bootstrap = {
            "load_csv": str(csv_path),
            "scenario_yaml": str(scenario_path),
            "virtual_solar_csv": str(solar_path),
            "battery_strategy_recommendation_json": str(strategy_path) if strategy_path else "",
            "results_path": str(self.results_path or ""),
            "resolved_output_dir": str(out_dir),
            "cwd": os.getcwd(),
            "python_path": sys.path,
        }
        self._write_best_effort(
            out_dir / "battery_sim_started.txt",
            (
                f"load_csv={csv_path}\n"
                f"scenario_yaml={scenario_path}\n"
                f"virtual_solar_csv={solar_path}\n"
                f"battery_strategy_recommendation_json={strategy_path or ''}\n"
                f"results_path={self.results_path}\n"
                f"resolved_output_dir={out_dir}\n"
                f"cwd={os.getcwd()}\n"
            ),
        )
        self._write_best_effort(
            out_dir / "battery_sim_bootstrap.json",
            json.dumps(bootstrap, indent=2, ensure_ascii=False),
        )

        def _log(msg: str) -> None:
            text = f"[BatterySimPiece] {msg}"
            print(text, flush=True)
            try:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(text + "\n")
            except Exception:
                pass

        try:
            _log(f"Input load_csv={csv_path}")
            _log(f"Input scenario_yaml={scenario_path}")
            _log(f"Input virtual_solar_csv={solar_path}")
            _log(f"Input battery_strategy_recommendation_json={strategy_path or '(empty)'}")
            if not csv_path.is_file():
                raise FileNotFoundError(f"Load CSV not found: {csv_path}")
            if not scenario_path.is_file():
                raise FileNotFoundError(f"Scenario YAML not found: {scenario_path}")
            if not solar_path.is_file():
                raise FileNotFoundError(f"Virtual solar CSV not found: {solar_path}")

            sim = self._load_simulate_module()
            cfg = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
            sim._apply_system_scope(cfg)
            sim._sync_scope_flags_to_sizes(cfg)
            df = sim.load_consumption_csv(csv_path)
            solar_df = pd.read_csv(solar_path)
            if "pv_kw" not in solar_df.columns:
                raise ValueError("virtual_solar_csv must contain pv_kw column")
            if "datetime" in solar_df.columns:
                solar_df = solar_df.copy()
                solar_df["datetime"] = pd.to_datetime(solar_df["datetime"], errors="coerce")
                solar_df = solar_df.dropna(subset=["datetime"]).drop_duplicates(
                    subset=["datetime"], keep="first"
                )
                aligned = pd.DataFrame({"datetime": pd.to_datetime(df["datetime"])}).merge(
                    solar_df, on="datetime", how="left", sort=False
                )
                solar_df = aligned
            elif len(solar_df) != len(df):
                raise ValueError(
                    f"virtual_solar_csv rows ({len(solar_df)}) must match load_csv rows ({len(df)})"
                )
            solar_series = pd.to_numeric(solar_df["pv_kw"], errors="coerce").fillna(0.0).clip(lower=0.0)

            # The forecast is produced before sizing runs, so it carries the
            # reference array size. Dispatching it unscaled would simulate a
            # different plant than the one being costed.
            sized_kwp = float((cfg.get("pv") or {}).get("installed_kwp", 0.0) or 0.0)
            dt_h = sim.infer_timestep_hours(df)
            if "pv_kw_per_kwp" in solar_df.columns and sized_kwp > 0:
                per_kwp = (
                    pd.to_numeric(solar_df["pv_kw_per_kwp"], errors="coerce")
                    .fillna(0.0)
                    .clip(lower=0.0)
                    .to_numpy(dtype=float)
                )
                solar_series = pd.Series(np.asarray(per_kwp, dtype=float) * sized_kwp)
                _log(f"Rescaled AI forecast to the sized array: {sized_kwp:.1f} kWp")
            price = sim.build_price_series(df, cfg).values.astype(float)
            load_kw = df["load_kw"].astype(float).values
            pv_kw = solar_series.astype(float).values

            bat = cfg.get("battery") or {}
            mrk = cfg.get("mrk") or {}
            en = cfg.get("energy") or {}
            analysis = cfg.get("analysis") or {}
            pv_cfg = cfg.get("pv") or {}
            energy_kwh = float(bat.get("energy_kwh", 0.0))
            eta_c = float(bat.get("charge_efficiency", 0.95))
            eta_d = float(bat.get("discharge_efficiency", 0.95))

            # Dispatch decides when storing energy pays off, so it needs the real
            # levelised cost of this plant. Hardcoding it meant every site was
            # dispatched as if PV cost 0.12 EUR/kWh and battery throughput 0.02,
            # regardless of the CAPEX, yield and cycle life in the scenario.
            installed_kwp = float(pv_cfg.get("installed_kwp", 0.0))
            if installed_kwp > 0:
                yield_kwp = sim.annual_kwh_per_kwp_from_profile(pv_kw / installed_kwp, dt_h)
            else:
                yield_kwp = 0.0
            years = int(analysis.get("amortization_years", 12))
            discount_rate = float(analysis.get("discount_rate", 0.08))
            use_pv_flag = bool(cfg.get("use_pv", True))
            use_bat_flag = bool(cfg.get("use_battery", True))
            pv_capex = installed_kwp * float(pv_cfg.get("specific_capex_eur_per_kwp", 800.0))
            bat_capex = energy_kwh * float(bat.get("specific_capex_eur_per_kwh", 400.0))

            levelized = sim.compute_levelized_economics(
                pv_cfg,
                bat,
                analysis,
                en,
                installed_kwp=installed_kwp,
                yield_kwp=yield_kwp,
                energy_kwh=energy_kwh,
                pv_capex=pv_capex,
                bat_capex=bat_capex,
                eta_c=eta_c,
                eta_d=eta_d,
                years=years,
                dr=discount_rate,
                use_pv=use_pv_flag,
                use_bat=use_bat_flag,
            )
            wear = sim.battery_dispatch_wear_eur_per_kwh(bat)
            _log(
                "Levelized economics: pv_lcoe={pv_lcoe:.4f} EUR/kWh, "
                "dispatch_wear={wear:.4f} EUR/kWh (CAPEX stays in NPV)".format(
                    pv_lcoe=float(levelized["pv_lcoe_eur_per_kwh"]),
                    wear=wear,
                )
            )
            strategy_thresholds = sim.load_battery_strategy_thresholds(strategy_path)
            dispatch_kwargs = {}
            if strategy_thresholds:
                for key in ("price_low", "price_high", "price_expensive"):
                    if key in strategy_thresholds:
                        dispatch_kwargs[key] = strategy_thresholds[key]

            common_kwargs = {
                "price": price,
                "dt_h": dt_h,
                "energy_kwh": energy_kwh,
                "max_c_rate": float(bat.get("max_c_rate", 0.5)),
                "eta_c": eta_c,
                "eta_d": eta_d,
                "initial_soc_pct": float(bat.get("initial_soc_pct", 50.0)),
                "mrk_contract_kw": float(mrk.get("contract_kw", 0.0)),
                "feed_in_eur_per_kwh": float(en.get("feed_in_surplus_eur_per_kwh", 0.05)),
                "pv_lcoe_eur_per_kwh": float(levelized["pv_lcoe_eur_per_kwh"]),
                "battery_throughput_eur_per_kwh": wear,
                "distribution_eur_per_kwh": sim.distribution_tariff_eur_per_kwh(cfg),
                "max_fraction_from_grid_charge": float(bat.get("max_fraction_capacity_from_grid_charge", 0.72)),
                "excess_penalty_eur_per_kw": float(mrk.get("excess_peak_penalty_eur_per_kw", 0.0)),
                "fee_eur_per_kw_month": float(mrk.get("fee_eur_per_kw_month", 0.0)),
                "peak_shaving_reserve_pct": float(bat.get("peak_shaving_reserve_pct", 30.0)),
                "timestamps": df["datetime"],
                **dispatch_kwargs,
            }

            grid_bat_only, soc_bat_only, _p_bat_only, export_bat_only = sim.dispatch_battery(
                net_load_kw=load_kw,
                **common_kwargs,
            )
            grid_pv_bat, soc_pv_bat, _p_pv_bat, export_pv_bat = sim.dispatch_battery(
                net_load_kw=load_kw - pv_kw,
                **common_kwargs,
            )

            enable_trading = bool(analysis.get("enable_trading_only_scenario", True)) and energy_kwh > 1e-6
            if enable_trading:
                trading_grid, trading_export = sim.dispatch_trading_only(
                    price,
                    dt_h,
                    energy_kwh=energy_kwh,
                    max_c_rate=float(bat.get("max_c_rate", 0.5)),
                    eta_c=eta_c,
                    eta_d=eta_d,
                    initial_soc_pct=float(bat.get("initial_soc_pct", 50.0)),
                    price_low=dispatch_kwargs.get("price_low"),
                    price_high=dispatch_kwargs.get("price_high"),
                )
                trading_cycles = float(np.sum((np.clip(trading_grid, 0.0, None) * dt_h * eta_c) / max(energy_kwh, 1e-9)))
            else:
                trading_grid = np.zeros(len(df), dtype=float)
                trading_export = np.zeros(len(df), dtype=float)
                trading_cycles = 0.0

            use_pv = bool(cfg.get("use_pv", True))
            primary_soc = soc_pv_bat if use_pv else soc_bat_only
            out_df = pd.DataFrame({"datetime": df["datetime"], "soc_pct": primary_soc})

            cycles_battery_only = float(sim.equivalent_full_cycles(pd.Series(soc_bat_only)))
            cycles_pv_battery = float(sim.equivalent_full_cycles(pd.Series(soc_pv_bat)))
            throughput_mwh = float(
                (pd.Series(primary_soc).diff().abs().fillna(0.0).sum() / 100.0) * energy_kwh / 1000.0
            )
            summary_df = pd.DataFrame(
                [
                    {
                        "capacity_kWh": energy_kwh,
                        "primary_dispatch_scenario": "pv_and_battery" if use_pv else "battery_only",
                        "cycles_equivalent": round(cycles_pv_battery if use_pv else cycles_battery_only, 4),
                        "energy_throughput_MWh": round(throughput_mwh, 4),
                        "battery_only_cycles_equivalent": round(cycles_battery_only, 4),
                        "pv_battery_cycles_equivalent": round(cycles_pv_battery, 4),
                        "trading_only_cycles_equivalent": round(trading_cycles, 4),
                        "strategy_source_json": str(strategy_path) if strategy_path else "",
                        "strategy_charge_below_eur_per_kwh": dispatch_kwargs.get("price_low"),
                        "strategy_discharge_above_eur_per_kwh": dispatch_kwargs.get("price_high"),
                        "strategy_expensive_hour_threshold_eur_per_kwh": dispatch_kwargs.get("price_expensive"),
                    }
                ]
            )
            dispatch_df = pd.DataFrame(
                {
                    "datetime": df["datetime"],
                    "load_kw": load_kw,
                    "pv_kw": pv_kw,
                    "price_eur_per_kwh": price,
                    "battery_only_grid_kw": grid_bat_only,
                    "battery_only_export_kw": export_bat_only,
                    "battery_only_soc_pct": soc_bat_only,
                    "pv_battery_grid_kw": grid_pv_bat,
                    "pv_battery_export_kw": export_pv_bat,
                    "pv_battery_soc_pct": soc_pv_bat,
                    "trading_only_grid_kw": trading_grid,
                    "trading_only_export_kw": trading_export,
                }
            )
            _log(f"Computed battery dispatch rows={len(dispatch_df)}")
        except Exception as exc:
            self._write_best_effort(out_dir / "battery_sim_error.txt", traceback.format_exc())
            _log(f"ERROR during battery simulation: {exc}")
            if od is not None:
                od.cleanup_on_error(self.results_path, secrets_data, "BatterySimPiece", _stage, run_id=_run_id)
            raise

        out_csv = out_dir / "virtual_battery_soc.csv"
        summary_csv = out_dir / "battery_summary.csv"
        dispatch_csv = out_dir / "battery_dispatch.csv"
        out_df.to_csv(out_csv, index=False)
        summary_df.to_csv(summary_csv, index=False)
        dispatch_df.to_csv(dispatch_csv, index=False)
        _log(f"Wrote outputs: {out_csv}, {summary_csv}, {dispatch_csv}")
        _piece_out = OutputModel(
            message="Battery simulation finished",
            virtual_battery_soc_csv=str(out_csv),
            battery_summary_csv=str(summary_csv),
            battery_dispatch_csv=str(dispatch_csv),
        )
        if od is not None and _piece_out is not None:
            return od.finish_piece(
                _piece_out, self.results_path, secrets_data, "BatterySimPiece", _stage, run_id=_run_id
            )
        if _stage is not None:
            _stage.cleanup()
        return _piece_out
