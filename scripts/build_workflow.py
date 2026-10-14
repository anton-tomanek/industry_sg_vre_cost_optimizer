"""Build the Domino workflow import files from a declarative DAG.

The workflow used to live only as a hand-maintained 64 kB `.customization`
blob. Adding the AI forecasting chain meant fourteen new nodes, each with a
JSON-Schema copy of its Pydantic models and hand-written upstream references,
which is not something to edit by hand and keep correct.

This script is the single source of truth instead. It reads the Pydantic models
straight from `pieces/*/models.py`, so the schemas in the import file can never
drift from the code, and it wires the graph from the DAG declared below.

Regenerate after changing any piece interface:

    python3 scripts/build_workflow.py
    python3 scripts/export_workflow_json.py

Node UUIDs are derived deterministically from the piece name, so repeated runs
produce a stable diff instead of a fresh graph every time.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PIECES_DIR = ROOT / "pieces"
CUSTOMIZATION = ROOT / "UC3.2.customization"
LOCAL_CUSTOMIZATION = ROOT / "test_cost_optimizer_local.customization"
TEST_CUSTOMIZATION = ROOT / "Test.customization"
COMPILED_METADATA = ROOT / ".domino" / "compiled_metadata.json"
DEPENDENCIES_MAP = ROOT / ".domino" / "dependencies_map.json"
CONFIG = ROOT / "config.toml"

REPOSITORY_URL = "https://github.com/filipchrvala/industry_sg_vre_cost_optimizer"
REPOSITORY_ID = 14
PIECE_ID = 100

# Stable namespace so a piece keeps its node UUID across regenerations.
UUID_NAMESPACE = uuid.UUID("6c7d1d1b-ebc0-41cf-94af-98c9378610e0")

ONEDATA_INPUT_BASE = "onedata:///SCDI/UC3.2_COST_OPTIMIZER/inputs"


def _upstream(piece: str, argument: str) -> tuple[str, str]:
    """Reference an upstream output the way the Domino GUI writes it."""
    return piece, argument


# The workflow DAG. Each entry maps an input name to either a literal value or
# an _upstream(...) reference. Anything not listed keeps its schema default.
WORKFLOW: dict[str, dict[str, Any]] = {
    "UserInputPiece": {
        "row": 0,
        "col": 0,
        "inputs": {
            "load_csv": f"{ONEDATA_INPUT_BASE}/load_and_prices.csv",
            "prices_csv": "",
            "scenario_yaml": f"{ONEDATA_INPUT_BASE}/scenario.yaml",
        },
    },
    "CatalogSyncPiece": {
        "row": 0,
        "col": 1,
        "inputs": {
            "scenario_yaml": _upstream("UserInputPiece", "scenario_yaml"),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
    "TechnicalLimitsPiece": {
        "row": 1,
        "col": 1,
        "inputs": {
            "load_csv": _upstream("UserInputPiece", "load_csv"),
            "scenario_yaml": _upstream("UserInputPiece", "scenario_yaml"),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
    # --- AI production forecast chain, ported from UC3.4 ---------------------
    # It runs before sizing: the size sweep evaluates hundreds of candidate
    # arrays by rescaling this one forecast, so the shape has to exist first.
    "OpenMeteoPVDataPiece": {
        "row": 3,
        "col": 1,
        "inputs": {
            "scenario_yaml": _upstream("UserInputPiece", "scenario_yaml"),
            "load_csv": _upstream("UserInputPiece", "load_csv"),
            "time_resolution": "auto",
            "output_mode": "batch_sample",
            "output_format": "csv",
        },
    },
    # Placed after feature selection: the correction model has to train on the
    # same preprocessed columns it will be served, with only the target swapped
    # for one derived from ground measurement.
    "ShmuCalibrationPiece": {
        "row": 4,
        "col": 5,
        "inputs": {
            "weather_csv_path": _upstream("OpenMeteoPVDataPiece", "file_path"),
            "training_base_csv": _upstream("PvoutModelFeatureSelectPiece", "data_path"),
            "scenario_yaml": _upstream("UserInputPiece", "scenario_yaml"),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
    "DataPreprocessingPiece": {
        "row": 3,
        "col": 2,
        "inputs": {
            "data_path": _upstream("OpenMeteoPVDataPiece", "file_path"),
            "target_column": _upstream("OpenMeteoPVDataPiece", "target_column"),
            "preprocessing_option": "prediction",
            "keep_datetime": True,
        },
    },
    "ModelDeciderPiece": {
        "row": 3,
        "col": 3,
        "inputs": {
            "data_path": _upstream("DataPreprocessingPiece", "data_path"),
            "feature_columns": _upstream("DataPreprocessingPiece", "feature_columns"),
            "target_column": _upstream("DataPreprocessingPiece", "target_column"),
            "problem_type": "pvout_prediction",
            "available_models": ["xgb_regressor_model", "linear_regression_model"],
        },
    },
    "DataNormalizationPiece": {
        "row": 3,
        "col": 4,
        "inputs": {
            "data_path": _upstream("ModelDeciderPiece", "data_path"),
            "feature_columns": _upstream("ModelDeciderPiece", "feature_columns"),
            "target_column": _upstream("ModelDeciderPiece", "target_column"),
            "normalization_type": _upstream("ModelDeciderPiece", "normalization_type"),
            "model_type": _upstream("ModelDeciderPiece", "model_type"),
        },
    },
    "PvoutModelFeatureSelectPiece": {
        "row": 3,
        "col": 5,
        "inputs": {
            "data_path": _upstream("DataNormalizationPiece", "data_path"),
            "feature_columns": _upstream("DataNormalizationPiece", "feature_columns"),
            "target_column": _upstream("DataNormalizationPiece", "target_column"),
        },
    },
    "PVOUTPredictionModelTrainPiece": {
        "row": 3,
        "col": 6,
        "inputs": {
            "data_path": _upstream("PvoutModelFeatureSelectPiece", "data_path"),
            "feature_columns": _upstream("PvoutModelFeatureSelectPiece", "feature_columns"),
            "target_column": _upstream("PvoutModelFeatureSelectPiece", "target_column"),
            "model_type": "xgb_regressor_model",
        },
    },
    "PVOUTErrorCorrectionModelTrainPiece": {
        "row": 4,
        "col": 6,
        "inputs": {
            # Trained on the measurement-based target from SHMU when one is
            # available, so the correction stage learns a real bias rather than
            # the residual of a formula against itself.
            "data_path": _upstream("ShmuCalibrationPiece", "calibrated_training_csv"),
            "baseline_model_path": _upstream("PVOUTPredictionModelTrainPiece", "model_path"),
            "feature_columns": _upstream("PVOUTPredictionModelTrainPiece", "feature_columns"),
            "target_column": _upstream("PVOUTPredictionModelTrainPiece", "target_column"),
            "model_type": "error_correction_xgb_regressor_model",
        },
    },
    "PvoutStagedInferenceSpecPiece": {
        "row": 3,
        "col": 7,
        "inputs": {
            "baseline_model_path": _upstream("PVOUTPredictionModelTrainPiece", "model_path"),
            "correction_model_path": _upstream("PVOUTErrorCorrectionModelTrainPiece", "model_path"),
            "data_path": _upstream("PvoutModelFeatureSelectPiece", "data_path"),
            "feature_columns": _upstream("PvoutModelFeatureSelectPiece", "feature_columns"),
            "datetime_column": _upstream("PvoutModelFeatureSelectPiece", "datetime_column"),
            "target_column": _upstream("PvoutModelFeatureSelectPiece", "target_column"),
        },
    },
    "InferencePiece": {
        "row": 3,
        "col": 8,
        "inputs": {
            "pvout_model": _upstream("PvoutStagedInferenceSpecPiece", "pvout_model"),
            "datetime_column": _upstream("PvoutStagedInferenceSpecPiece", "datetime_column"),
        },
    },
    "PvoutToVirtualSolarPiece": {
        "row": 3,
        "col": 9,
        "inputs": {
            "forecast_csv_path": _upstream("InferencePiece", "forecast_csv_path"),
            "load_csv": _upstream("UserInputPiece", "load_csv"),
            "scenario_yaml": _upstream("UserInputPiece", "scenario_yaml"),
            "irradiance_scale_factor": _upstream("ShmuCalibrationPiece", "irradiance_scale_factor"),
        },
    },
    # Thresholds depend on the price series, not on the later kWp/kWh choice,
    # so this runs on the unsized scenario and feeds Sizing + BatterySim.
    "BatteryStrategyOptimizerPiece": {
        "row": 2,
        "col": 1,
        "inputs": {
            "load_csv": _upstream("UserInputPiece", "load_csv"),
            "scenario_yaml": _upstream("UserInputPiece", "scenario_yaml"),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
    # --- sizing, dispatch, economics and reporting ---------------------------
    "SizingOptimizationPiece": {
        "row": 1,
        "col": 10,
        "inputs": {
            "load_csv": _upstream("UserInputPiece", "load_csv"),
            "scenario_yaml": _upstream("UserInputPiece", "scenario_yaml"),
            "virtual_solar_csv": _upstream("PvoutToVirtualSolarPiece", "virtual_solar_csv"),
            "technical_limits_json": _upstream("TechnicalLimitsPiece", "technical_limits_json"),
            "battery_strategy_recommendation_json": _upstream(
                "BatteryStrategyOptimizerPiece", "battery_strategy_recommendation_json"
            ),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
    "CatalogRankerPiece": {
        "row": 0,
        "col": 11,
        "inputs": {
            "pv_catalog_json": _upstream("CatalogSyncPiece", "pv_catalog_json"),
            "scenario_yaml": _upstream("SizingOptimizationPiece", "sized_scenario_yaml"),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
    "BatterySimPiece": {
        "row": 1,
        "col": 12,
        "inputs": {
            "load_csv": _upstream("UserInputPiece", "load_csv"),
            "scenario_yaml": _upstream("SizingOptimizationPiece", "sized_scenario_yaml"),
            "virtual_solar_csv": _upstream("PvoutToVirtualSolarPiece", "virtual_solar_csv"),
            "battery_strategy_recommendation_json": _upstream(
                "BatteryStrategyOptimizerPiece", "battery_strategy_recommendation_json"
            ),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
    "SimulatePiece": {
        "row": 1,
        "col": 13,
        "inputs": {
            "load_csv": _upstream("UserInputPiece", "load_csv"),
            "scenario_yaml": _upstream("SizingOptimizationPiece", "sized_scenario_yaml"),
            "virtual_solar_csv": _upstream("PvoutToVirtualSolarPiece", "virtual_solar_csv"),
            "battery_dispatch_csv": _upstream("BatterySimPiece", "battery_dispatch_csv"),
            "battery_summary_csv": _upstream("BatterySimPiece", "battery_summary_csv"),
            "ranked_catalog_json": _upstream(
                "CatalogRankerPiece", "catalog_ranked_recommendation_json"
            ),
            "inverter_catalog_json": _upstream("CatalogSyncPiece", "inverter_catalog_json"),
            "battery_catalog_json": _upstream("CatalogSyncPiece", "battery_catalog_json"),
            "catalog_manifest_json": _upstream("CatalogSyncPiece", "catalog_manifest_json"),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
    "SizingHeatmapPiece": {
        "row": 3,
        "col": 13,
        "inputs": {
            "load_csv": _upstream("UserInputPiece", "load_csv"),
            "scenario_yaml": _upstream("SizingOptimizationPiece", "sized_scenario_yaml"),
            "virtual_solar_csv": _upstream("PvoutToVirtualSolarPiece", "virtual_solar_csv"),
            "technical_limits_json": _upstream("TechnicalLimitsPiece", "technical_limits_json"),
            "battery_strategy_recommendation_json": _upstream(
                "BatteryStrategyOptimizerPiece", "battery_strategy_recommendation_json"
            ),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
    "KPIPiece": {
        "row": 1,
        "col": 14,
        "inputs": {
            "report_json": _upstream("SimulatePiece", "report_json"),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
    "InvestmentEvalPiece": {
        "row": 1,
        "col": 15,
        "inputs": {
            "report_json": _upstream("SimulatePiece", "report_json"),
            "kpi_results_csv": _upstream("KPIPiece", "kpi_results_csv"),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
    "DashboardPiece": {
        "row": 1,
        "col": 16,
        "inputs": {
            "report_json": _upstream("SimulatePiece", "report_json"),
            "kpi_results_csv": _upstream("KPIPiece", "kpi_results_csv"),
            "investment_evaluation_csv": _upstream(
                "InvestmentEvalPiece", "investment_evaluation_csv"
            ),
            "heatmap_json": _upstream("SizingHeatmapPiece", "heatmap_json"),
            "catalog_ranked_recommendation_json": _upstream(
                "CatalogRankerPiece", "catalog_ranked_recommendation_json"
            ),
            "calibration_json": _upstream("ShmuCalibrationPiece", "calibration_json"),
            "battery_dispatch_csv": _upstream("BatterySimPiece", "battery_dispatch_csv"),
            "load_csv": _upstream("UserInputPiece", "load_csv"),
            "user_input_summary_json": _upstream("UserInputPiece", "user_input_summary_json"),
            "run_id": _upstream("UserInputPiece", "run_id"),
        },
    },
}

COLUMN_WIDTH = 260
ROW_HEIGHT = 150


def version() -> str:
    text = CONFIG.read_text(encoding="utf-8") if CONFIG.is_file() else ""
    match = re.search(r'VERSION\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.1.42"


def source_image() -> str:
    return f"ghcr.io/filipchrvala/industry_sg_vre_cost_optimizer:{version()}-group0"


def node_uuid(piece_name: str) -> str:
    return str(uuid.uuid5(UUID_NAMESPACE, piece_name))


def node_id(piece_name: str, index: int) -> str:
    return f"{101 + index}_{node_uuid(piece_name)}"


def upstream_id(piece_name: str) -> str:
    return f"{piece_name[:10]}_{node_uuid(piece_name).replace('-', '')}"


def upstream_label(piece_name: str, argument: str) -> str:
    pretty = " ".join(part.capitalize() for part in argument.split("_"))
    return f"{piece_name} ({node_uuid(piece_name)[:8]}) - {pretty}"


def load_models_module(piece_name: str):
    """Import a piece's models.py without importing the piece itself.

    piece.py pulls in domino.base_piece and the scientific stack, which are not
    installed everywhere this script runs. The Pydantic models are standalone.
    """
    path = PIECES_DIR / piece_name / "models.py"
    if not path.is_file():
        raise FileNotFoundError(f"{piece_name} has no models.py")

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(PIECES_DIR) not in sys.path:
        sys.path.insert(1, str(PIECES_DIR))

    module_name = f"_wfbuild_{piece_name}_models"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def schemas_for(piece_name: str) -> dict[str, Any]:
    module = load_models_module(piece_name)
    out: dict[str, Any] = {}
    for attr, key in (
        ("InputModel", "input_schema"),
        ("OutputModel", "output_schema"),
        ("SecretsModel", "secrets_schema"),
    ):
        model = getattr(module, attr, None)
        out[key] = model.model_json_schema() if model is not None else {}
    return out


def piece_metadata(piece_name: str) -> dict[str, Any]:
    meta_path = PIECES_DIR / piece_name / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    meta.setdefault("name", piece_name)
    meta.setdefault("description", piece_name)
    meta.setdefault("dependency", {"requirements_file": "requirements_0.txt"})
    meta["dependency"].setdefault("dockerfile", None)
    meta.setdefault("tags", [])
    meta.setdefault(
        "style",
        {
            "node_label": piece_name,
            "node_type": "default",
            "node_style": {"backgroundColor": "#ebebeb"},
            "useIcon": True,
            "icon_class_name": "fa-solid:gear",
            "iconStyle": {"cursor": "pointer"},
        },
    )
    meta.setdefault("dependencies_group", "0")
    meta.update(schemas_for(piece_name))
    return meta


def container_resources(meta: dict[str, Any]) -> dict[str, Any]:
    res = meta.get("container_resources") or {}
    requests = res.get("requests") or {}
    limits = res.get("limits") or {}
    return {
        "requests": {
            "cpu": requests.get("cpu", 100),
            "memory": requests.get("memory", 128),
        },
        "limits": {
            "cpu": limits.get("cpu", 500),
            "memory": limits.get("memory", 128),
        },
        "use_gpu": bool(res.get("use_gpu", False)),
    }


def build() -> dict[str, Any]:
    image = source_image()
    order = list(WORKFLOW)
    ids = {name: node_id(name, i) for i, name in enumerate(order)}

    pieces: dict[str, Any] = {}
    pieces_data: dict[str, Any] = {}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()

    for name in order:
        spec = WORKFLOW[name]
        nid = ids[name]
        meta = piece_metadata(name)
        resources = container_resources(meta)

        pieces[nid] = {
            "id": PIECE_ID,
            "name": name,
            "description": meta.get("description", name),
            "dependency": meta["dependency"],
            "source_image": image,
            "input_schema": meta["input_schema"],
            "output_schema": meta["output_schema"],
            "secrets_schema": meta["secrets_schema"],
            "container_resources": resources,
            "tags": meta.get("tags", []),
            "style": meta["style"],
            "source_url": f"{REPOSITORY_URL}/tree/main/pieces/{name}",
            "repository_url": REPOSITORY_URL,
            "repository_id": REPOSITORY_ID,
        }

        inputs: dict[str, Any] = {}
        declared = spec.get("inputs", {})
        for field, default in (meta["input_schema"].get("properties") or {}).items():
            if field not in declared:
                if "default" in default:
                    continue
                inputs[field] = {
                    "fromUpstream": False,
                    "upstreamId": "",
                    "upstreamArgument": "",
                    "upstreamValue": "",
                    "value": "",
                }
                continue

            value = declared[field]
            if isinstance(value, tuple):
                src_piece, src_arg = value
                if src_piece not in ids:
                    raise KeyError(f"{name}.{field} references unknown piece {src_piece}")
                inputs[field] = {
                    "fromUpstream": True,
                    "upstreamId": upstream_id(src_piece),
                    "upstreamArgument": src_arg,
                    "upstreamValue": upstream_label(src_piece, src_arg),
                    "value": "",
                }
                key = (ids[src_piece], nid)
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append(make_edge(ids[src_piece], nid))
            else:
                inputs[field] = {
                    "fromUpstream": False,
                    "upstreamId": "",
                    "upstreamArgument": "",
                    "upstreamValue": "",
                    "value": value,
                }

        pieces_data[nid] = {
            "storage": {"storageAccessMode": "Read/Write"},
            "containerResources": {
                "cpu": resources["limits"]["cpu"],
                "memory": resources["limits"]["memory"],
                "useGpu": resources["use_gpu"],
            },
            "inputs": inputs,
        }

        position = {
            "x": float(spec.get("col", 0) * COLUMN_WIDTH),
            "y": float(spec.get("row", 0) * ROW_HEIGHT),
        }
        style = meta["style"]
        nodes.append(
            {
                "id": nid,
                "type": "CustomNode",
                "position": position,
                "data": {
                    "name": name,
                    "style": {
                        "module": name,
                        "label": style.get("node_label", name),
                        "nodeType": style.get("node_type", "default"),
                        "nodeStyle": style.get("node_style", {"backgroundColor": "#ebebeb"}),
                        "useIcon": style.get("useIcon", True),
                        "iconClassName": style.get("icon_class_name", "fa-solid:gear"),
                        "iconStyle": style.get("iconStyle", {"cursor": "pointer"}),
                    },
                    "validationError": False,
                    "orientation": "horizontal",
                },
                "width": 150,
                "height": 70,
                "selected": False,
                "positionAbsolute": dict(position),
                "dragging": False,
            }
        )

    return {
        "workflowPieces": pieces,
        "workflowPiecesData": pieces_data,
        "workflowNodes": nodes,
        "workflowEdges": edges,
    }


def make_edge(source: str, target: str) -> dict[str, Any]:
    source_handle = f"source-{source}"
    target_handle = f"target-{target}"
    return {
        "source": source,
        "sourceHandle": source_handle,
        "target": target,
        "targetHandle": target_handle,
        "id": f"reactflow__edge-{source}{source_handle}-{target}{target_handle}",
        "markerEnd": {"type": "arrowclosed", "width": 20, "height": 20},
    }


def write_compiled_metadata() -> None:
    compiled = {}
    for piece_dir in sorted(PIECES_DIR.iterdir()):
        if not (piece_dir / "metadata.json").is_file() or not (piece_dir / "models.py").is_file():
            continue
        compiled[piece_dir.name] = piece_metadata(piece_dir.name)
    COMPILED_METADATA.parent.mkdir(parents=True, exist_ok=True)
    COMPILED_METADATA.write_text(
        json.dumps(compiled, indent=4, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote {COMPILED_METADATA} ({len(compiled)} pieces)")

    dep_map = json.loads(DEPENDENCIES_MAP.read_text(encoding="utf-8"))
    group = dep_map.get("group0") or {}
    group["pieces"] = sorted(compiled)
    group["source_image"] = source_image()
    dep_map["group0"] = group
    DEPENDENCIES_MAP.write_text(
        json.dumps(dep_map, indent=4, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Wrote {DEPENDENCIES_MAP}")


def validate(payload: dict[str, Any]) -> list[str]:
    """Catch the wiring mistakes that only surface as a failed Domino import."""
    problems: list[str] = []
    pieces = payload["workflowPieces"]
    by_upstream_id = {
        f"{p['name'][:10]}_{nid.split('_', 1)[1].replace('-', '')}": (nid, p)
        for nid, p in pieces.items()
    }

    for nid, data in payload["workflowPiecesData"].items():
        name = pieces[nid]["name"]
        schema_props = pieces[nid]["input_schema"].get("properties") or {}
        required = set(pieces[nid]["input_schema"].get("required") or [])

        for field, cfg in data["inputs"].items():
            if field not in schema_props:
                problems.append(f"{name}.{field} is not in InputModel")
            if not cfg["fromUpstream"]:
                if field in required and cfg["value"] in ("", None):
                    problems.append(f"{name}.{field} is required but has no value")
                continue
            target = by_upstream_id.get(cfg["upstreamId"])
            if target is None:
                problems.append(f"{name}.{field} points at unknown node {cfg['upstreamId']}")
                continue
            out_props = target[1]["output_schema"].get("properties") or {}
            if cfg["upstreamArgument"] not in out_props:
                problems.append(
                    f"{name}.{field} reads '{cfg['upstreamArgument']}' which "
                    f"{target[1]['name']} does not output"
                )

        for field in required:
            if field not in data["inputs"]:
                problems.append(f"{name}.{field} is required but unwired")

    node_ids = {n["id"] for n in payload["workflowNodes"]}
    for edge in payload["workflowEdges"]:
        for end in ("source", "target"):
            if edge[end] not in node_ids:
                problems.append(f"edge {end} {edge[end]} has no node")

    # A cycle would deadlock the scheduler rather than fail loudly.
    incoming = {nid: 0 for nid in pieces}
    adjacency: dict[str, list[str]] = {nid: [] for nid in pieces}
    for edge in payload["workflowEdges"]:
        adjacency[edge["source"]].append(edge["target"])
        incoming[edge["target"]] += 1
    queue = [n for n, deg in incoming.items() if deg == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for nxt in adjacency[node]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                queue.append(nxt)
    if visited != len(pieces):
        problems.append("workflow graph contains a cycle")

    return problems


def main() -> int:
    payload = build()
    problems = validate(payload)
    if problems:
        print("Workflow validation failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    CUSTOMIZATION.write_text(text, encoding="utf-8")
    TEST_CUSTOMIZATION.write_text(text, encoding="utf-8")
    print(
        f"Wrote {CUSTOMIZATION} "
        f"({len(payload['workflowPieces'])} pieces, {len(payload['workflowEdges'])} edges)"
    )
    write_local_variant(payload)
    write_compiled_metadata()
    return 0


def write_local_variant(payload: dict[str, Any]) -> None:
    """Same graph, but UserInputPiece reads the demo files on disk.

    Local Domino has no OneData space, so the production import would fail at
    the first node. This copy points at examples/demo_site, which
    scripts/make_demo_inputs.py can regenerate at any time.
    """
    demo = ROOT / "examples" / "demo_site"
    local = json.loads(json.dumps(payload))
    for nid, piece in local["workflowPieces"].items():
        if piece.get("name") != "UserInputPiece":
            continue
        inputs = local["workflowPiecesData"][nid]["inputs"]
        for field, path in (
            ("load_csv", demo / "load_and_prices.csv"),
            ("scenario_yaml", demo / "scenario.yaml"),
        ):
            if field in inputs:
                inputs[field] = {
                    "fromUpstream": False,
                    "upstreamId": "",
                    "upstreamArgument": "",
                    "upstreamValue": "",
                    "value": str(path),
                }
        break
    LOCAL_CUSTOMIZATION.write_text(
        json.dumps(local, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote {LOCAL_CUSTOMIZATION} (local demo inputs)")


if __name__ == "__main__":
    raise SystemExit(main())
