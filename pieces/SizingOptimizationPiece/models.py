from typing import Optional

from pydantic import BaseModel, Field

try:
    from common.onedata_models import OneDataSecretsModel, RunIdInputMixin
except ModuleNotFoundError:
    from pieces.common.onedata_models import OneDataSecretsModel, RunIdInputMixin



class InputModel(RunIdInputMixin):
    load_csv: str = Field(description="Path to historical load CSV")
    scenario_yaml: str = Field(description="Path to scenario YAML")
    virtual_solar_csv: Optional[str] = Field(
        default=None,
        description=(
            "AI-forecast production profile. Candidate sizes are evaluated by "
            "rescaling this shape, so self-consumption reflects the real weather "
            "at the site instead of a generic sine."
        ),
    )
    technical_limits_json: str = Field(description="Path to technical limits json")
    battery_strategy_recommendation_json: Optional[str] = Field(
        default=None,
        description="Charge/discharge thresholds from BatteryStrategyOptimizerPiece, used while scoring sizes.",
    )


class SecretsModel(OneDataSecretsModel):
    pass


class OutputModel(BaseModel):
    message: str
    sized_scenario_yaml: str
    sizing_optimization_json: str
