from pydantic import BaseModel, Field


class InputModel(BaseModel):
    baseline_model_path: str = Field(
        description="PVOUTPredictionModelTrainPiece.model_path (absolute baseline).",
    )
    correction_model_path: str = Field(
        description="PVOUTErrorCorrectionModelTrainPiece.model_path (residual corrector).",
    )
    data_path: str = Field(description="Preprocessed/normalized CSV used at inference.")
    feature_columns: list[str] = Field(
        default_factory=list,
        description="Model feature columns (no datetime).",
    )
    datetime_column: str = Field(default="datetime")
    target_column: str = Field(default="PVOUT")
    pred_column: str = Field(
        default="PVOUT_PRED",
        description="Column name written by baseline stage for the corrector base.",
    )


class OutputModel(BaseModel):
    message: str
    pvout_model: list[dict] = Field(
        description=(
            "Ready-to-bind InferencePiece.pvout_model entry with stages: "
            "baseline (price_level) → inject PVOUT_PRED → error correction."
        ),
    )
    datetime_column: str = Field(default="datetime")
    data_path: str
    feature_columns: list[str] = Field(default_factory=list)


try:
    from common.onedata_models import OneDataSecretsModel
except ModuleNotFoundError:
    from pieces.common.onedata_models import OneDataSecretsModel


class SecretsModel(OneDataSecretsModel):
    """OneData secrets so the piece honours the repository secrets set in Domino
    (output dir, host, token) instead of the built-in defaults."""
