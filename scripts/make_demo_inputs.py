"""Generate a realistic demo site so the workflow can be run without customer data.

The profile is a two-shift metal fabrication plant on the Slovak grid: a strong
weekday base with a night shift at reduced output, a weekend at maintenance
level, plus a summer cooling load. Prices follow the day-ahead shape, dipping at
midday when there is PV on the system and peaking in the evening.

This is demo data, not a benchmark. It exists so a reviewer can run the workflow
end to end and see the report, not to validate the economics.
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "examples" / "demo_site"


def build_load(start: datetime, days: int, step_minutes: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = int(days * 24 * 60 / step_minutes)
    index = pd.date_range(start, periods=steps, freq=f"{step_minutes}min")

    hour = index.hour + index.minute / 60.0
    weekday = index.dayofweek
    day_of_year = index.dayofyear

    base_kw = 320.0
    day_shift = 520.0 * np.exp(-0.5 * ((hour - 10.5) / 3.4) ** 2)
    late_shift = 380.0 * np.exp(-0.5 * ((hour - 17.0) / 2.6) ** 2)
    night_shift = 150.0 * np.exp(-0.5 * ((hour - 2.0) / 3.0) ** 2)

    load = base_kw + day_shift + late_shift + night_shift

    weekend = weekday >= 5
    load = np.where(weekend, base_kw * 0.55 + night_shift * 0.4, load)

    # Summer cooling, peaking with the afternoon ambient temperature.
    summer = np.clip(np.cos(2 * math.pi * (day_of_year - 200) / 365.0), 0, None)
    load += 130.0 * summer * np.clip(np.sin((hour - 7) / 13 * math.pi), 0, None)

    # Two weeks of shutdown in July, as most plants in the region have.
    shutdown = (day_of_year >= 196) & (day_of_year <= 209)
    load = np.where(shutdown, base_kw * 0.35, load)

    load *= rng.normal(1.0, 0.045, size=steps)
    load = np.clip(load, 40.0, None)

    price_base = 0.118
    daily = 0.036 * np.sin(2 * math.pi * (hour - 20) / 24.0)
    midday_dip = -0.028 * np.exp(-0.5 * ((hour - 13.0) / 2.6) ** 2)
    evening_peak = 0.052 * np.exp(-0.5 * ((hour - 19.0) / 1.9) ** 2)
    winter = np.clip(-np.cos(2 * math.pi * (day_of_year - 15) / 365.0), 0, None)
    price = price_base + daily + midday_dip + evening_peak + 0.022 * winter
    price *= rng.normal(1.0, 0.09, size=steps)
    price = np.clip(price, 0.015, 0.62)

    return pd.DataFrame(
        {
            "datetime": index.strftime("%Y-%m-%d %H:%M:%S"),
            "load_kw": np.round(load, 2),
            "price_eur_per_kwh": np.round(price, 5),
        }
    )


def build_scenario(annual_mwh: float, peak_kw: float) -> dict:
    return {
        "site": {
            "name": "Demo Industrial Site — Nitra",
            "latitude": 48.3061,
            "longitude": 18.0764,
            "altitude_m": 144,
            "timezone": "Europe/Bratislava",
        },
        "use_pv": True,
        "use_battery": True,
        "pv": {
            "installed_kwp": 600.0,
            "panel_tilt": 25.0,
            "azimuth": 180.0,
            "specific_capex_eur_per_kwp": 720.0,
            "om_eur_per_kwp_year": 11.0,
            "degradation_pct_per_year": 0.45,
            "mount_type": "roof",
        },
        "battery": {
            "energy_kwh": 800.0,
            "max_c_rate": 0.5,
            "charge_efficiency": 0.96,
            "discharge_efficiency": 0.96,
            "initial_soc_pct": 50.0,
            "specific_capex_eur_per_kwh": 295.0,
            "cycle_life": 6000,
            "max_fraction_capacity_from_grid_charge": 0.72,
            "peak_shaving_reserve_pct": 30.0,
        },
        "mrk": {
            "contract_kw": round(peak_kw * 0.92, 0),
            "fee_eur_per_kw_month": 4.2,
            "excess_peak_penalty_eur_per_kw": 12.0,
        },
        "energy": {
            "feed_in_surplus_eur_per_kwh": 0.045,
            "distribution_eur_per_kwh": 0.045,
        },
        "analysis": {
            "amortization_years": 15,
            "discount_rate": 0.075,
            "enable_trading_only_scenario": True,
        },
        "equipment": {
            "selection_mode": "auto",
            "system_scope": "pv_and_battery",
            "auto": {
                "objective": "max_npv",
                "kwp_step": 100.0,
                "kwh_step": 200.0,
                "min_pv_kwp": 100.0,
                "min_battery_kwh": 200.0,
                "max_configurations": 120,
            },
            "constraints": {
                "max_roof_area_m2": 7800.0,
                "max_battery_area_m2": 160.0,
                "capex_mode": "joint",
                "max_total_capex_eur": 1_400_000.0,
                "max_capex_eur": 1_400_000.0,
                "roof_load_limit_kg_per_m2": 25.0,
                "installation": {
                    "mount_type": "roof",
                    "shading": "low",
                    "priority": "balanced",
                    "allow_bifacial": True,
                },
            },
            "layout": {
                "kwp_per_m2_roof": 0.19,
                "kwh_per_m2_battery_area": 30.0,
            },
        },
        "_notes": {
            "annual_consumption_mwh": round(annual_mwh, 1),
            "measured_peak_kw": round(peak_kw, 1),
            "provenance": "Synthetic demo profile from scripts/make_demo_inputs.py",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--step-minutes", type=int, default=15)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--seed", type=int, default=20260906)
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start)
    df = build_load(start, args.days, args.step_minutes, args.seed)

    step_h = args.step_minutes / 60.0
    annual_mwh = float(df["load_kw"].sum()) * step_h / 1000.0 * (365.0 / args.days)
    peak_kw = float(df["load_kw"].max())

    args.out.mkdir(parents=True, exist_ok=True)
    load_path = args.out / "load_and_prices.csv"
    scenario_path = args.out / "scenario.yaml"

    df.to_csv(load_path, index=False)
    scenario_path.write_text(
        yaml.safe_dump(
            build_scenario(annual_mwh, peak_kw), allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )

    print(f"Wrote {load_path} ({len(df)} rows)")
    print(f"Wrote {scenario_path}")
    print(f"Annual consumption {annual_mwh:,.0f} MWh, peak {peak_kw:,.0f} kW")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
