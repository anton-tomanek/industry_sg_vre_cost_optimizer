import json
from pathlib import Path

from domino.base_piece import BasePiece

from .models import InputModel, OutputModel


TREE_MODELS = {"xgb_regressor_model", "interval_xgb_regressor_model", "eda_rule_baseline"}


try:
    from common.piece_onedata import onedata_piece
except ModuleNotFoundError:
    from pieces.common.piece_onedata import onedata_piece


class ModelDeciderPiece(BasePiece):
    @onedata_piece("ModelDeciderPiece")
    def piece_function(self, input_data: InputModel):
        self.logger.info("Running ModelDeciderPiece.")

        payload = input_data.payload_as_dict()

        model_type = (
            payload.get("model_type")
            or self._pick_model(payload.get("available_models"))
            or "xgb_regressor_model"
        )
        model_type = str(model_type).lower()

        # Tree-based models don't need scaling; everything else defaults to z_score.
        normalization_type = payload.get("normalization_type")
        if normalization_type is None:
            normalization_type = "none" if model_type in TREE_MODELS else "z_score"
        normalization_type = str(normalization_type).lower()

        # Strip the chosen target_column from feature_columns so downstream pieces
        # never train with the target leaking in as a predictor. This matters when
        # DataPreprocessing merges weather + price data into one dataset and two
        # ModelDecider nodes (one per target) fan out from it — each must echo a
        # feature list that excludes its own target.
        target_column_resolved = str(payload.get("target_column") or "PVOUT")
        raw_feature_columns = payload.get("feature_columns") or []
        filtered_feature_columns = [
            c for c in raw_feature_columns if c != target_column_resolved
        ]

        decision = {
            "model_type": model_type,
            "normalization_type": normalization_type,
            "feature_columns": filtered_feature_columns,
            "target_column": target_column_resolved,
            "data_path": payload.get("data_path"),
            "problem_type": payload.get("problem_type"),
            "horizon": payload.get("horizon"),
        }

        decision_path = str(Path(self.results_path) / "decision.json")
        Path(decision_path).parent.mkdir(parents=True, exist_ok=True)
        with open(decision_path, "w", encoding="utf-8") as f:
            json.dump(decision, f, indent=2)
        self.display_result = {"file_type": "txt", "file_path": decision_path}

        artifacts = dict(decision)
        artifacts["decision_path"] = decision_path

        return OutputModel(
            message=f"ModelDeciderPiece selected model_type={model_type}, normalization_type={normalization_type}.",
            model_type=model_type,
            normalization_type=normalization_type,
            feature_columns=list(decision.get("feature_columns") or []),
            target_column=str(decision.get("target_column") or "PVOUT"),
            data_path=decision.get("data_path"),
            decision_path=decision_path,
            artifacts=artifacts,
        )

    @staticmethod
    def _pick_model(available_models):
        if not available_models:
            return None
        preferred = ["xgb_regressor_model", "linear_regression_model", "eda_rule_baseline"]
        for candidate in preferred:
            if candidate in available_models:
                return candidate
        return available_models[0]
