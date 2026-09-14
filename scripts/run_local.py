"""Run the whole workflow locally, outside Domino.

The DAG comes from scripts/build_workflow.py, the same declaration the Domino
import file is generated from, so a local run exercises the wiring that will
actually be deployed. If a piece renames an output, this run fails in the same
place the platform would.

    python3 scripts/make_demo_inputs.py
    python3 scripts/run_local.py --inputs examples/demo_site --out .local_run

`domino.base_piece` is stubbed with the harness in pieces/local_compat, so no
Domino installation is needed.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PIECES = ROOT / "pieces"


def install_domino_stub() -> None:
    """Point `domino.base_piece` at the local harness before any piece imports."""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(PIECES))

    from local_compat import install_domino_stub as install

    install()


def _uncache_project_modules() -> None:
    """Drop in-process piece imports so a local web rerun picks up code edits.

    The form server is one long-lived process. Without this, the first heatmap
    (or dashboard) import is frozen for every later submit.
    """
    root = str(ROOT.resolve())
    for key, mod in list(sys.modules.items()):
        file = getattr(mod, "__file__", None) or ""
        if not file:
            continue
        try:
            path = str(Path(file).resolve())
        except (OSError, RuntimeError):
            continue
        if not path.startswith(root):
            continue
        rel = path[len(root):].lstrip("\\/").replace("\\", "/")
        if rel.startswith("pieces/local_compat"):
            continue
        if rel.startswith(("pieces/", "webapp/", "scripts/")):
            sys.modules.pop(key, None)


def topological_order(workflow: dict[str, dict]) -> list[str]:
    incoming = {name: set() for name in workflow}
    for name, spec in workflow.items():
        for value in (spec.get("inputs") or {}).values():
            if isinstance(value, tuple):
                incoming[name].add(value[0])

    ordered: list[str] = []
    remaining = dict(incoming)
    while remaining:
        ready = sorted(n for n, deps in remaining.items() if not (deps - set(ordered)))
        if not ready:
            raise RuntimeError(f"Cycle among: {sorted(remaining)}")
        for name in ready:
            ordered.append(name)
            remaining.pop(name)
    return ordered


def coerce(value: Any) -> Any:
    """Pydantic models are not JSON, and outputs are passed between pieces as data."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [coerce(v) for v in value]
    return value


def run_piece(
    name: str,
    spec: dict,
    outputs: dict[str, dict],
    out_root: Path,
    overrides: dict[str, dict],
) -> tuple[dict | None, str]:
    piece_module = importlib.import_module(f"{name}.piece")
    models_module = importlib.import_module(f"{name}.models")
    piece_class = getattr(piece_module, name)
    input_model = models_module.InputModel

    kwargs: dict[str, Any] = {}
    for field, value in (spec.get("inputs") or {}).items():
        if isinstance(value, tuple):
            src_piece, src_arg = value
            upstream = outputs.get(src_piece)
            if upstream is None:
                return None, f"upstream {src_piece} produced nothing"
            if src_arg not in upstream:
                return None, f"{src_piece} has no output '{src_arg}'"
            kwargs[field] = coerce(upstream[src_arg])
        else:
            kwargs[field] = value
    kwargs.update(overrides.get(name, {}))

    results_path = out_root / name
    results_path.mkdir(parents=True, exist_ok=True)

    piece = piece_class(results_path=str(results_path))

    # Pieces ported from UC3.4 declare piece_function(self, input_data) without
    # the secrets argument, so it cannot be passed unconditionally.
    import inspect

    signature = inspect.signature(piece.piece_function)
    if len(signature.parameters) >= 2:
        result = piece.piece_function(input_model(**kwargs), None)
    else:
        result = piece.piece_function(input_model(**kwargs))
    return coerce(result), ""


def run_workflow(
    *,
    load_csv: Path,
    scenario_yaml: Path,
    out: Path,
    skip: list[str] | None = None,
    stop_on_error: bool = False,
    prices_csv: str = "",
    on_event=None,
) -> dict[str, Any]:
    """Execute the declared DAG and optionally report each piece as it finishes.

    ``on_event`` receives a small dict (start / piece_start / piece_end / done)
    so a web UI can show the same run the CLI prints, without scraping stdout.
    """
    install_domino_stub()
    _uncache_project_modules()
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from build_workflow import WORKFLOW  # noqa: E402

    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    load_csv = Path(load_csv).resolve()
    scenario_yaml = Path(scenario_yaml).resolve()
    for path in (load_csv, scenario_yaml):
        if not path.is_file():
            raise FileNotFoundError(f"Missing input: {path}")

    overrides = {
        "UserInputPiece": {
            "load_csv": str(load_csv),
            "prices_csv": prices_csv or "",
            "scenario_yaml": str(scenario_yaml),
        }
    }

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    order = topological_order(WORKFLOW)
    skipped = set(skip or [])
    outputs: dict[str, dict] = {}
    report: list[dict] = []

    emit({"type": "start", "pieces": order, "total": len(order)})

    for index, name in enumerate(order):
        if name in skipped:
            print(f"SKIP  {name}")
            item = {"piece": name, "status": "skipped"}
            report.append(item)
            emit({"type": "piece_end", **item, "index": index, "total": len(order)})
            continue

        emit({"type": "piece_start", "piece": name, "index": index, "total": len(order)})
        started = time.time()
        try:
            result, problem = run_piece(name, WORKFLOW[name], outputs, out, overrides)
        except Exception as exc:  # noqa: BLE001
            elapsed = time.time() - started
            (out / f"{name}.error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            print(f"FAIL  {name} ({elapsed:.1f}s): {type(exc).__name__}: {exc}")
            item = {
                "piece": name,
                "status": "failed",
                "seconds": round(elapsed, 2),
                "error": str(exc),
            }
            report.append(item)
            emit({"type": "piece_end", **item, "index": index, "total": len(order)})
            if stop_on_error:
                break
            continue

        elapsed = time.time() - started
        if result is None:
            print(f"FAIL  {name} ({elapsed:.1f}s): {problem}")
            item = {
                "piece": name,
                "status": "failed",
                "seconds": round(elapsed, 2),
                "error": problem,
            }
            report.append(item)
            emit({"type": "piece_end", **item, "index": index, "total": len(order)})
            if stop_on_error:
                break
            continue

        outputs[name] = result
        message = str(result.get("message", "")).strip()
        print(f"OK    {name} ({elapsed:.1f}s){f': {message}' if message else ''}")
        item = {
            "piece": name,
            "status": "ok",
            "seconds": round(elapsed, 2),
            "message": message,
        }
        report.append(item)
        emit({"type": "piece_end", **item, "index": index, "total": len(order)})

    failed = [r["piece"] for r in report if r["status"] == "failed"]
    dashboard = out / "DashboardPiece" / "dashboard.html"
    summary = {
        "pieces": report,
        "ok": not failed,
        "failed": failed,
        "dashboard": str(dashboard) if dashboard.is_file() else None,
    }
    (out / "run_summary.json").write_text(
        json.dumps({**summary, "outputs": outputs}, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(
        f"\n{len(report) - len(failed) - len(skipped)} ok, "
        f"{len(failed)} failed, {len(skipped)} skipped"
    )
    emit({"type": "done", **{k: summary[k] for k in ("ok", "failed", "dashboard")}})
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, default=ROOT / "examples" / "demo_site")
    parser.add_argument("--out", type=Path, default=ROOT / ".local_run")
    parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        help="Pieces to skip, for example ShmuCalibrationPiece when offline.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop at the first failure instead of reporting every piece.",
    )
    parser.add_argument(
        "--prices",
        type=Path,
        default=None,
        help="Optional prices file. If omitted and the load has no prices, OKTE DAM is fetched.",
    )
    args = parser.parse_args()

    load_csv = (args.inputs / "load_and_prices.csv").resolve()
    if not load_csv.is_file():
        for name in ("load.xlsx", "load.csv", "load_upload.xlsx"):
            candidate = (args.inputs / name).resolve()
            if candidate.is_file():
                load_csv = candidate
                break
    scenario_yaml = (args.inputs / "scenario.yaml").resolve()
    if not load_csv.is_file() or not scenario_yaml.is_file():
        print(f"Missing input under {args.inputs}\nRun scripts/make_demo_inputs.py first.")
        return 2

    try:
        summary = run_workflow(
            load_csv=load_csv,
            scenario_yaml=scenario_yaml,
            out=args.out,
            skip=args.skip,
            stop_on_error=args.stop_on_error,
            prices_csv=str(args.prices) if args.prices else "",
        )
    except FileNotFoundError as exc:
        print(exc)
        return 2
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
