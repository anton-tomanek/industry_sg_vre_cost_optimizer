from pathlib import Path

from domino.base_piece import BasePiece

from .models import InputModel, OutputModel
from .utils.modes import preprocess_correction, preprocess_prediction


try:
    from common.piece_onedata import onedata_piece
except ModuleNotFoundError:
    from pieces.common.piece_onedata import onedata_piece


class DataPreprocessingPiece(BasePiece):
    @onedata_piece("DataPreprocessingPiece")
    def piece_function(self, input_data: InputModel):
        self.logger.info("Running DataPreprocessingPiece.")

        payload = input_data.payload_as_dict()
        preprocessing_option = (
            payload.get("preprocessing_option")
            or payload.get("preprocessing_type")
            or payload.get("mode")
            or "none"
        )
        preprocessing_option = str(preprocessing_option).lower()

        if preprocessing_option == "none":
            return OutputModel(
                message="DataPreprocessingPiece executed (none).",
                artifacts={"input_payload": payload},
            )

        if preprocessing_option in ("prediction", "correction") and not payload.get(
            "save_data_path"
        ):
            payload["save_data_path"] = str(
                Path(self.results_path) / "preprocessed.csv"
            )

        if preprocessing_option == "prediction":
            result = preprocess_prediction(payload)
            artifacts = dict(result["artifacts"])
            saved_path = payload.get("save_data_path")
            if saved_path:
                artifacts["data_path"] = saved_path
                self.display_result = {"file_type": "txt", "file_path": saved_path}
            return OutputModel(
                message=result["message"],
                data_path=saved_path,
                feature_columns=list(artifacts.get("features") or []),
                target_column=str(artifacts.get("target_column") or ""),
                artifacts=artifacts,
            )

        if preprocessing_option == "correction":
            result = preprocess_correction(payload)
            artifacts = dict(result["artifacts"])
            saved_path = payload.get("save_data_path")
            pred_path = None
            if saved_path:
                root, ext = Path(saved_path).stem, Path(saved_path).suffix
                pred_path = str(Path(saved_path).with_name(f"{root}_pred{ext}"))
                true_path = str(Path(saved_path).with_name(f"{root}_true{ext}"))
                artifacts["data_path_pred"] = pred_path
                artifacts["data_path_true"] = true_path
                self.display_result = {"file_type": "txt", "file_path": pred_path}
            return OutputModel(
                message=result["message"],
                data_path=pred_path,
                feature_columns=[],
                target_column="PVOUT",
                artifacts=artifacts,
            )

        raise ValueError(
            f"Invalid preprocessing option: {preprocessing_option}. "
            "Expected one of: none, prediction, correction."
        )
