from pydantic import BaseModel, ConfigDict, Field, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    problem_type: str | None = Field(
        default=None,
        title="Problem Type",
        description=(
            "Forecasting problem this DAG addresses. "
            "One of: `pvout_prediction`, `pvout_error_correction`, `price_prediction`. "
            "Currently informational only — the decider does not branch on this value yet."
        ),
    )
    horizon: int | None = Field(
        default=1,
        title="Horizon",
        description=(
            "Prediction horizon in steps. "
            "Examples: `1` for single-step forecasting, `96` for 24 hours at 15-minute resolution."
        ),
    )
    available_models: list[str] = Field(
        default_factory=list,
        title="Available Models",
        description=(
            "Candidate model identifiers. Click `+` to add one or more of: "
            "`xgb_regressor_model`, `linear_regression_model`, `interval_xgb_regressor_model`, "
            "`eda_rule_baseline`, `tabpfn_regressor_model`. "
            "The decider picks `xgb_regressor_model` when available."
        ),
    )
    feature_columns: list[str] = Field(
        default_factory=list,
        title="Feature Columns",
        description=(
            "Optional explicit feature list. Usually left empty — downstream trainer/inference "
            "should wire its own `Feature Columns` directly from the preprocessor."
        ),
    )
    target_column: str | None = Field(
        default="PVOUT",
        title="Target Column",
        description="Target column name to forecast. Default `PVOUT` for irradiance datasets.",
    )
    data_path: str | None = Field(
        default=None,
        title="Data Path",
        description=(
            "Optional passthrough of the preprocessed data path. When wired upstream "
            "from `DataPreprocessingPiece.Data Path`, this is echoed to downstream "
            "pieces so the linear chain only needs one upstream edge per node."
        ),
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
        return self.model_dump(exclude_none=True, exclude_defaults=True)

    def payload_as_dict(self) -> dict:
        return self.to_payload_dict()


class OutputModel(BaseModel):
    message: str = Field(description="Human-readable status message.")
    model_type: str = Field(
        default="xgb_regressor_model",
        description="Selected model type (consumable upstream → trainer.model_type).",
    )
    normalization_type: str = Field(
        default="none",
        description="Recommended normalization (consumable upstream → normalization.normalization_type).",
    )
    feature_columns: list[str] = Field(
        default_factory=list,
        description="Echoed feature columns (consumable upstream → trainer / inference).",
    )
    target_column: str = Field(
        default="PVOUT",
        description="Echoed target column (consumable upstream → trainer.target_column).",
    )
    data_path: str | None = Field(
        default=None,
        description="Echoed preprocessed data path (consumable upstream → normalization / trainer / inference).",
    )
    decision_path: str | None = Field(
        default=None, description="Path to the on-disk decision.json artifact."
    )
    artifacts: dict = Field(
        default_factory=dict,
        description="Optional outputs (e.g., selected model id/version, decision rationale).",
    )


try:
    from common.onedata_models import OneDataSecretsModel
except ModuleNotFoundError:
    from pieces.common.onedata_models import OneDataSecretsModel


class SecretsModel(OneDataSecretsModel):
    """OneData secrets so the piece honours the repository secrets set in Domino
    (output dir, host, token) instead of the built-in defaults."""
