"""Select UC3.4 model feature columns without dropping datetime from the dataset CSV.

UC3.4 DataPreprocessingPiece with keep_datetime=True echoes `datetime` inside
`feature_columns`. PVOUTPredictionModelTrainPiece then coerces every feature via
pd.to_numeric and dropna — which wipes all rows when `datetime` is included.

This SoMES glue piece does NOT modify UC3.4 code and does NOT remove datetime
from the preprocessed CSV. It only splits:
  - model_feature_columns → safe for XGB train / inference features
  - datetime_column → for InferencePiece.datetime_column
"""
from __future__ import annotations

try:
    from domino.base_piece import BasePiece
except ModuleNotFoundError:
    from local_compat.base_piece import BasePiece

from .models import InputModel, OutputModel

NON_MODEL_COLUMNS = {
    "datetime",
    "Date",
    "Time",
    "date",
    "timestamp",
    "timestamp_utc",
    "pred_sequence_id",
}


try:
    from common.piece_onedata import onedata_piece
except ModuleNotFoundError:
    from pieces.common.piece_onedata import onedata_piece


class PvoutModelFeatureSelectPiece(BasePiece):
    @onedata_piece("PvoutModelFeatureSelectPiece")
    def piece_function(self, input_data: InputModel, secrets_data=None) -> OutputModel:
        target = (input_data.target_column or "PVOUT").strip() or "PVOUT"
        raw = list(input_data.feature_columns or [])
        model_features = [
            c
            for c in raw
            if c
            and c not in NON_MODEL_COLUMNS
            and c != target
            and not str(c).upper().startswith("PVOUT_UNC")
        ]
        if not model_features:
            raise ValueError(
                "No model feature columns left after excluding datetime/target. "
                f"Received feature_columns={raw!r}"
            )

        dt_col = "datetime"
        if "datetime" in raw:
            dt_col = "datetime"
        elif "Date" in raw:
            dt_col = "Date"

        return OutputModel(
            message=(
                f"Selected {len(model_features)} model features; "
                f"datetime kept in dataset as '{dt_col}' for InferencePiece."
            ),
            data_path=input_data.data_path,
            feature_columns=model_features,
            datetime_column=dt_col,
            target_column=target,
        )
