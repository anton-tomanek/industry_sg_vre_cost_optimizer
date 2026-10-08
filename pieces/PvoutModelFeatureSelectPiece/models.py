from pydantic import BaseModel, Field


class InputModel(BaseModel):
    data_path: str = Field(description="Passthrough preprocessed/normalized CSV path (keeps datetime column).")
    feature_columns: list[str] = Field(
        default_factory=list,
        description="Feature list from UC3.4 preprocess/decider (may include datetime).",
    )
    target_column: str = Field(default="PVOUT", description="Target column name.")


class OutputModel(BaseModel):
    message: str
    data_path: str = Field(description="Same CSV path; datetime column remains in the file.")
    feature_columns: list[str] = Field(
        description="Numeric/model feature columns for PVOUT train + inference."
    )
    datetime_column: str = Field(
        default="datetime",
        description="Datetime column name for InferencePiece (still present in CSV).",
    )
    target_column: str = Field(default="PVOUT")


try:
    from common.onedata_models import OneDataSecretsModel
except ModuleNotFoundError:
    from pieces.common.onedata_models import OneDataSecretsModel


class SecretsModel(OneDataSecretsModel):
    """OneData secrets so the piece honours the repository secrets set in Domino
    (output dir, host, token) instead of the built-in defaults."""
