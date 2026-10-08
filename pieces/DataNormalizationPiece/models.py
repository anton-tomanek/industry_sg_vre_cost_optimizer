from pydantic import BaseModel, ConfigDict, Field, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    normalization_type: str | None = Field(
        default="none",
        title="Normalization Type",
        description=(
            "One of: `none`, `min_max`, `z_score`, `logaritmic`, `exponential`. "
            "Use `none` (passthrough) for tree-based models like XGBoost. "
            "Usually wired upstream from `ModelDeciderPiece.Normalization Type`."
        ),
    )
    features: list[str] = Field(
        default_factory=list,
        title="Features",
        description=(
            "Optional list of column names to normalize. Empty = apply to all numeric columns. "
            "Click `+` and add column names like `GHI`, `TEMP`, `WS`."
        ),
    )
    data_path: str | None = Field(
        default=None,
        title="Data Path",
        description=(
            "Path to input CSV. Wire upstream from `DataPreprocessingPiece.Data Path` "
            "(the preprocessor's `preprocessed.csv`)."
        ),
    )
    dataframe: str | None = Field(
        default=None,
        title="Dataframe",
        description=(
            "Optional inline dataframe payload (JSON object). "
            "Leave empty when `Data Path` is provided."
        ),
    )
    feature_columns: list[str] = Field(
        default_factory=list,
        title="Feature Columns",
        description=(
            "Optional passthrough of feature columns from upstream. "
            "Echoed to downstream pieces so trainer/inference need only one upstream edge."
        ),
    )
    target_column: str | None = Field(
        default=None,
        title="Target Column",
        description="Optional passthrough of the target column from upstream. Echoed downstream.",
    )
    model_type: str | None = Field(
        default=None,
        title="Model Type",
        description="Optional passthrough of the selected model type from upstream. Echoed downstream.",
    )

    @model_validator(mode="before")
    @classmethod
    def _unwrap_payload(cls, data):
        if isinstance(data, dict) and isinstance(data.get("payload"), dict):
            merged = dict(data["payload"])
            for key, value in data.items():
                if key != "payload":
                    merged[key] = value
            return merged
        return data

    def to_payload_dict(self) -> dict:
        out = self.model_dump(exclude_none=True, exclude_defaults=True)
        if "normalization_type" in out and "type" not in out:
            out["type"] = out["normalization_type"]
        return out

    def payload_as_dict(self) -> dict:
        return self.to_payload_dict()


class OutputModel(BaseModel):
    message: str = Field(description="Human-readable status message.")
    data_path: str | None = Field(
        default=None,
        description="Path to normalized CSV (consumable upstream → trainer / inference).",
    )
    normalization_type: str = Field(
        default="none",
        description="Normalization type that was applied.",
    )
    features: list[str] = Field(
        default_factory=list,
        description="Feature columns that were normalized.",
    )
    feature_columns: list[str] = Field(
        default_factory=list,
        description="Echoed feature columns from upstream (consumable upstream → trainer / inference).",
    )
    target_column: str = Field(
        default="PVOUT",
        description="Echoed target column from upstream (consumable upstream → trainer.target_column).",
    )
    model_type: str = Field(
        default="xgb_regressor_model",
        description="Echoed selected model type from upstream (consumable upstream → trainer.model_type).",
    )
    artifacts: dict = Field(
        default_factory=dict,
        description="Optional outputs (e.g., normalized dataset URI, fitted scaler params).",
    )


try:
    from common.onedata_models import OneDataSecretsModel
except ModuleNotFoundError:
    from pieces.common.onedata_models import OneDataSecretsModel


class SecretsModel(OneDataSecretsModel):
    """OneData secrets so the piece honours the repository secrets set in Domino
    (output dir, host, token) instead of the built-in defaults."""
