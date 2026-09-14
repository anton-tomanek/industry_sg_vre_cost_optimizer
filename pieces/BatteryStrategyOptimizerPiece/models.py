from pydantic import BaseModel, Field

try:
    from common.onedata_models import OneDataSecretsModel, RunIdInputMixin
except ModuleNotFoundError:
    from pieces.common.onedata_models import OneDataSecretsModel, RunIdInputMixin



class InputModel(RunIdInputMixin):
    load_csv: str = Field(description="Path to historical load CSV")
    scenario_yaml: str = Field(
        description="Path to scenario YAML. Thresholds come from the price series, so the unsized user scenario is enough."
    )


class SecretsModel(OneDataSecretsModel):
    pass


class OutputModel(BaseModel):
    message: str
    battery_strategy_recommendation_json: str
