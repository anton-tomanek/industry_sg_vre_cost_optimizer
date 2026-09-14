from typing import Optional

from pydantic import Field

try:
    from common.onedata_models import OneDataSecretsModel, RunIdInputMixin
except ModuleNotFoundError:
    from pieces.common.onedata_models import OneDataSecretsModel, RunIdInputMixin

from pydantic import BaseModel


class InputModel(RunIdInputMixin):
    load_csv: str = Field(description="Historical load CSV.")
    scenario_yaml: str = Field(description="Sized scenario YAML, used as the economic baseline.")
    virtual_solar_csv: Optional[str] = Field(
        default=None,
        description="AI-forecast production profile, rescaled per candidate array size.",
    )
    technical_limits_json: Optional[str] = Field(
        default=None,
        description="Upper bounds for the sweep. Falls back to bounds derived from the load.",
    )
    battery_strategy_recommendation_json: Optional[str] = Field(
        default=None,
        description="Charge/discharge thresholds from BatteryStrategyOptimizerPiece, used for every cell.",
    )
    pv_steps: int = Field(
        default=12,
        description="Number of PV sizes on the x axis.",
        ge=2,
        le=40,
    )
    battery_steps: int = Field(
        default=10,
        description="Number of battery sizes on the y axis.",
        ge=2,
        le=40,
    )


class SecretsModel(OneDataSecretsModel):
    pass


class OutputModel(BaseModel):
    message: str
    heatmap_json: str = Field(
        description="Grid of annual savings, NPV, payback and CAPEX over PV x battery size."
    )
    heatmap_csv: str = Field(description="The same grid in long form, one row per combination.")
    recommended_kwp: float = Field(description="PV size of the best cell under the objective.")
    recommended_kwh: float = Field(description="Battery size of the best cell under the objective.")
