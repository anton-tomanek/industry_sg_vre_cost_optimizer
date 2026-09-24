from pydantic import BaseModel, ConfigDict, Field, model_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    preprocessing_option: str | None = Field(
        default=None,
        description="One of: `none`, `prediction`, `correction`.",
    )
    data_path: str | None = Field(
        default=None,
        description=(
            "Path to Open-Meteo weather/PV CSV (columns include `datetime` or `Date`+`Time`). "
            "Wire upstream from Open-Meteo PV Data `File Path`."
        ),
    )
    data_path_okte: str | None = Field(
        default=None,
        description=(
            "Optional path to OKTE-style CSV (electricity market). "
            "When both weather and OKTE paths are provided, the piece inner-joins "
            "them on `datetime`."
        ),
    )
    save_data_path: str | None = Field(
        default=None, description="Optional output path."
    )
    test_size: float | None = Field(default=None, description="Optional split size.")
    keep_datetime: bool | None = Field(
        default=None, description="Keep datetime column if supported."
    )
    target_column: str | None = Field(
        default=None,
        description="Target column for prediction mode. Defaults to `PVOUT`.",
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
    data_path: str | None = Field(
        default=None,
        description="Path to preprocessed CSV (consumable upstream → normalization / trainer / inference).",
    )
    feature_columns: list[str] = Field(
        default_factory=list,
        description="Resolved feature columns (consumable upstream → trainer / inference).",
    )
    target_column: str = Field(
        default="PVOUT",
        description="Target column used (consumable upstream → trainer).",
    )
    artifacts: dict = Field(
        default_factory=dict,
        description="Optional outputs (e.g., cleaned dataset URI, dropped rows stats).",
    )


try:
    from common.onedata_models import OneDataSecretsModel
except ModuleNotFoundError:
    from pieces.common.onedata_models import OneDataSecretsModel


class SecretsModel(OneDataSecretsModel):
    """OneData secrets so the piece honours the repository secrets set in Domino
    (output dir, host, token) instead of the built-in defaults."""
