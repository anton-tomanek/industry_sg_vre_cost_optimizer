from __future__ import annotations

import copy
import json
from pathlib import Path
import traceback

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


class SizingOptimizationPiece(BasePiece):
    """Resolve final scenario sizing (manual or auto)."""

    def piece_function(self, input_data: InputModel, secrets_data=None) -> OutputModel:
        _stage = None
        _piece_out = None
        _run_id = None
        if od is not None:
            input_data, _stage = od.stage_inputs(input_data, secrets_data)
            _run_id = od.resolve_run_id(input_data, secrets_data, generate=False)
        csv_path = Path(input_data.load_csv)
        scenario_path = Path(input_data.scenario_yaml)
        tl_path = Path(input_data.technical_limits_json)
        out_dir = Path(self.results_path or scenario_path.parent)
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "sizing_optimization.log"

        def _log(msg: str) -> None:
            text = f"[SizingOptimizationPiece] {msg}"
            print(text, flush=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")

        _log(f"Input load_csv={csv_path}")
        _log(f"Input scenario_yaml={scenario_path}")
        _log(f"Input technical_limits_json={tl_path}")
        if not csv_path.is_file():
            raise FileNotFoundError(f"Load CSV not found: {csv_path}")
        if not scenario_path.is_file():
            raise FileNotFoundError(f"Scenario YAML not found: {scenario_path}")
        if not tl_path.is_file():
            raise FileNotFoundError(f"Technical limits JSON not found: {tl_path}")

        try:
            sim = load_simulate_module()
            cfg = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
            sim._apply_system_scope(cfg)
            df = sim.load_consumption_csv(csv_path)

            eq = cfg.get("equipment") or {}
            mode = str(eq.get("selection_mode", "manual")).lower()
            auto_log = None
            final_cfg = copy.deepcopy(cfg)

            # Honour the bounds TechnicalLimitsPiece already derived. They were
            # read here only to check the file exists, while the search recomputed
            # its own, so roof area and CAPEX caps could differ from what the
            # report showed.
            technical_limits = json.loads(tl_path.read_text(encoding="utf-8")) or {}
            _log(
                f"Technical limits: max_kwp={technical_limits.get('max_kwp')}, "
                f"max_kwh={technical_limits.get('max_kwh')}"
            )

            pv_profile = sim.load_pv_profile_per_kwp(
                input_data.virtual_solar_csv,
                df,
                reference_kwp=float((cfg.get("pv") or {}).get("installed_kwp", 0.0) or 0.0),
            )
            strategy_thresholds = sim.load_battery_strategy_thresholds(
                getattr(input_data, "battery_strategy_recommendation_json", None)
            )
            _log(
                "PV profile for the size sweep: "
                + ("AI forecast" if pv_profile is not None else "synthetic fallback")
            )
            _log(
                "Battery strategy thresholds: "
                + (str(strategy_thresholds) if strategy_thresholds else "fallback quantiles in dispatch")
            )

            if mode == "auto":
                final_cfg, auto_log = sim._auto_optimize_sizes(
                    final_cfg,
                    df,
                    bounds_override=technical_limits,
                    pv_profile_per_kwp=pv_profile,
                    battery_strategy_thresholds=strategy_thresholds,
                )
            _log(f"Resolved selection_mode={mode}, rows={len(df)}")
        except Exception as exc:
            (out_dir / "sizing_optimization_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            _log(f"ERROR during sizing optimization: {exc}")
            if od is not None:
                od.cleanup_on_error(self.results_path, secrets_data, "SizingOptimizationPiece", _stage, run_id=_run_id)
            raise

        sized_yaml = out_dir / "scenario_sized.yaml"
        sized_yaml.write_text(yaml.safe_dump(final_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

        out_json = out_dir / "sizing_optimization.json"
        out_json.write_text(
            json.dumps(
                {
                    "selection_mode": mode,
                    "technical_limits_applied": technical_limits,
                    "battery_strategy_thresholds": strategy_thresholds,
                    "auto_optimization": auto_log,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _log(f"Wrote outputs: {sized_yaml}, {out_json}")
        _piece_out = OutputModel(
            message="Sizing optimization finished",
            sized_scenario_yaml=str(sized_yaml),
            sizing_optimization_json=str(out_json),
        )
        if od is not None and _piece_out is not None:
            return od.finish_piece(
                _piece_out, self.results_path, secrets_data, "SizingOptimizationPiece", _stage, run_id=_run_id
            )
        if _stage is not None:
            _stage.cleanup()
        return _piece_out
