from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from typing import Optional

OUTPUT_FORMAT_ALIASES: dict[str, str] = {
    "json": "json",
    "csv": "csv",
}

TARGET_COLUMN = "PVOUT"

# Open-Meteo CSV columns. `datetime` is canonical for Domino preprocessing.
OPEN_METEO_CSV_FIELDNAMES = [
    "datetime",
    "Date",
    "Time",
    "GHI",
    "DNI",
    "DIF",
    "GTI",
    "SE",
    "SA",
    "PVOUT",
    "TEMP",
    "WS",
    "WG",
    "WD",
    "RH",
    "AP",
    "PVOUT_UNC_LOW",
    "PVOUT_UNC_HIGH",
]

# Open-Meteo endpoints — no API key required.
# The archive (ERA5) only serves hourly data. The historical-forecast API serves
# 15-minute data back to 2021, which is what a 15-minute load profile needs.
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"

TIME_RESOLUTIONS = ("15min", "hourly", "auto")


class InputModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    scenario_yaml: str | None = Field(
        default=None,
        title="Scenario YAML",
        description=(
            "Optional SoMES scenario.yaml. When set, site.latitude/longitude and "
            "pv.installed_kwp / pv.tilt_deg override the manual fields below."
        ),
    )
    latitude: float = Field(
        default=48.15,
        title="Latitude",
        description="Fallback latitude if scenario_yaml is missing site.latitude.",
    )
    longitude: float = Field(
        default=17.11,
        title="Longitude",
        description="Fallback longitude if scenario_yaml is missing site.longitude.",
    )
    load_csv: str | None = Field(
        default=None,
        title="Load profile CSV",
        description=(
            "Metered load profile. When set, the weather window is taken from its "
            "first and last timestamp, so production is modelled over exactly the "
            "period the investment case is evaluated on, and start_date / end_date "
            "below are ignored."
        ),
    )
    start_date: str = Field(
        default="2026-01-01",
        title="Start Date",
        description="First day of the requested period in YYYY-MM-DD format.",
    )
    end_date: str = Field(
        default="2026-01-07",
        title="End Date",
        description="Last day of the requested period in YYYY-MM-DD format.",
    )
    pvout_peak_kw: float = Field(
        default=5.2,
        title="PV System Peak Power (kWp)",
        description="Fallback kWp if scenario_yaml has no pv.installed_kwp / solar.capacity_kWp.",
    )
    panel_tilt: float = Field(
        default=30.0,
        title="Panel Tilt (degrees)",
        description="Fallback tilt if scenario_yaml has no pv.tilt_deg.",
    )
    time_resolution: str | None = Field(
        default="15min",
        title="Time Resolution",
        description=(
            "One of: `15min` (historical-forecast API, matches a 15-minute load "
            "profile), `hourly` (ERA5 archive, longer history), or `auto` to try "
            "15-minute first and fall back to hourly."
        ),
    )
    output_mode: str | None = Field(
        default="batch_sample",
        title="Output Mode",
        description="One of: `batch_sample`, `realtime_stream`.",
    )
    output_format: str | None = Field(
        default=None,
        title="Output Format",
        description="One of: `json`, `csv`.",
        validation_alias=AliasChoices(
            "output_format",
            "outputFormat",
            "Output format",
            "Output Format",
            "export_format",
            "file_format",
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _unwrap_payload(cls, data):
        if not isinstance(data, dict):
            return data

        if isinstance(data.get("payload"), dict):
            merged = dict(data["payload"])
            for key, value in data.items():
                if key != "payload":
                    merged[key] = value
            data = merged

        if not data.get("output_format"):
            for alias in (
                "outputFormat",
                "Output format",
                "Output Format",
                "export_format",
                "file_format",
            ):
                if alias in data and data.get(alias) is not None:
                    data["output_format"] = data.pop(alias)
                    break

        output_mode = data.get("output_mode")
        if output_mode is not None:
            data["output_mode"] = str(output_mode).strip().lower()

        resolution = data.get("time_resolution")
        if resolution is not None:
            normalized = str(resolution).strip().lower().replace("_", "").replace("-", "")
            data["time_resolution"] = {
                "15min": "15min",
                "15minutely": "15min",
                "minutely15": "15min",
                "quarterhourly": "15min",
                "hourly": "hourly",
                "1h": "hourly",
                "auto": "auto",
            }.get(normalized, normalized)

        output_format = data.get("output_format")
        if output_format is not None:
            normalized = str(output_format).strip().lower()
            data["output_format"] = OUTPUT_FORMAT_ALIASES.get(normalized, normalized)

        return data

    def to_payload_dict(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True, exclude_unset=True)


class OutputModel(BaseModel):
    file_path: Optional[str] = Field(default=None, title="Dataset file path")
    target_column: Optional[str] = Field(
        default=None,
        title="Target column",
        description="Suggested target column for the generated dataset (pass downstream via fromUpstream).",
    )


try:
    from common.onedata_models import OneDataSecretsModel
except ModuleNotFoundError:
    from pieces.common.onedata_models import OneDataSecretsModel


class SecretsModel(OneDataSecretsModel):
    """OneData secrets so the piece honours the repository secrets set in Domino
    (output dir, host, token) instead of the built-in defaults."""
