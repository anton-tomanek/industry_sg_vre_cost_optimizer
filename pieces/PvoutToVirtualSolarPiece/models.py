from typing import Optional

from pydantic import BaseModel, Field


class InputModel(BaseModel):
    forecast_csv_path: str = Field(
        description="Path to UC3.4 InferencePiece forecast CSV (final_forecast / PVOUT)."
    )
    load_csv: Optional[str] = Field(
        default=None,
        description=(
            "Load CSV to align against. The forecast covers daylight only, because "
            "preprocessing drops night rows; when this is set the profile is "
            "reindexed onto the load timestamps with zeros at night, which is what "
            "BatterySimPiece requires."
        ),
    )
    irradiance_scale_factor: float = Field(
        default=1.0,
        description="Scale from ShmuCalibrationPiece; 1.0 leaves the forecast untouched.",
    )
    scenario_yaml: Optional[str] = Field(
        default=None,
        description=(
            "Scenario the forecast was generated for. Its pv.installed_kwp is the "
            "reference size used to normalise the profile per kWp, so sizing can "
            "rescale the same shape to any candidate array."
        ),
    )


class OutputModel(BaseModel):
    message: str
    virtual_solar_csv: str = Field(
        description="datetime, pv_kw and pv_kw_per_kwp for the dispatch and sizing pieces"
    )
    coverage_report_json: str = Field(
        description="Row counts, alignment result and the applied scale factor."
    )
    reference_kwp: float = Field(
        description="Installed kWp the pv_kw column corresponds to."
    )


try:
    from common.onedata_models import OneDataSecretsModel
except ModuleNotFoundError:
    from pieces.common.onedata_models import OneDataSecretsModel


class SecretsModel(OneDataSecretsModel):
    """OneData secrets so the piece honours the repository secrets set in Domino
    (output dir, host, token) instead of the built-in defaults."""
