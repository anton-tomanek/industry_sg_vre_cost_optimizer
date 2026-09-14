"""Build InferencePiece.pvout_model with the UC3.4 staged baseline→correction spec.

UC3.4 ErrorCorrection.model_spec uses mode=pvout_correction with
base_forecast_column=PVOUT, i.e. it expects PVOUT on prediction rows to already
hold the baseline forecast. In this workflow PVOUT is the physically modelled
production from Open-Meteo weather, so serving must:

  1) run the baseline model absolutely (price_level) → inject as PVOUT_PRED
  2) run the corrector with base_forecast_column=PVOUT_PRED

This glue piece does not change UC3.4 piece code.
"""
from __future__ import annotations

try:
    from domino.base_piece import BasePiece
except ModuleNotFoundError:
    from local_compat.base_piece import BasePiece

from .models import InputModel, OutputModel


try:
    from common.piece_onedata import onedata_piece
except ModuleNotFoundError:
    from pieces.common.piece_onedata import onedata_piece


class PvoutStagedInferenceSpecPiece(BasePiece):
    @onedata_piece("PvoutStagedInferenceSpecPiece")
    def piece_function(self, input_data: InputModel, secrets_data=None) -> OutputModel:
        feats = list(input_data.feature_columns or [])
        if not feats:
            raise ValueError("feature_columns is required for staged PVOUT inference.")
        pred_col = (input_data.pred_column or "PVOUT_PRED").strip() or "PVOUT_PRED"
        dt_col = (input_data.datetime_column or "datetime").strip() or "datetime"
        target = (input_data.target_column or "PVOUT").strip() or "PVOUT"

        entry = {
            "model_id": "pvout",
            # Top-level paths help Evaluate/Explainable when they read ForecastEntry.
            "model_path": input_data.correction_model_path,
            "data_path": input_data.data_path,
            "datetime_column": dt_col,
            "feature_columns": feats,
            "target_column": target,
            "stages": [
                {
                    "mode": "price_level",
                    "model_path": input_data.baseline_model_path,
                    "feature_columns": feats,
                    "inject_forecast_as": pred_col,
                },
                {
                    "mode": "pvout_correction",
                    "model_path": input_data.correction_model_path,
                    "feature_columns": feats,
                    "base_forecast_column": pred_col,
                },
            ],
        }

        return OutputModel(
            message=(
                f"Built staged pvout_model (baseline→{pred_col}→correction) "
                f"with {len(feats)} features."
            ),
            pvout_model=[entry],
            datetime_column=dt_col,
            data_path=input_data.data_path,
            feature_columns=feats,
        )
