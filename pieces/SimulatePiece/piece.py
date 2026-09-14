"""
Celá MRK / PV / batéria simulácia je v tomto súbore (Domino piece = 3 súbory: metadata, models, piece).
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any
import uuid
import traceback

import numpy as np
import pandas as pd
import yaml
from domino.base_piece import BasePiece

from .models import InputModel, OutputModel

try:
    from common import onedata_io as od
except ModuleNotFoundError:
    try:
        from pieces.common import onedata_io as od
    except ModuleNotFoundError:
        od = None

try:
    from common.capex_budget import (
        apply_capex_axis_budgets,
        capex_limit_notes,
        configuration_over_budget,
        copy_capex_bound_fields,
    )
except ModuleNotFoundError:
    from pieces.common.capex_budget import (
        apply_capex_axis_budgets,
        capex_limit_notes,
        configuration_over_budget,
        copy_capex_bound_fields,
    )

# --- battery strategy (voliteľné prahy z BatteryStrategyOptimizerPiece) ---


def load_battery_strategy_thresholds(path: Path | str | None) -> dict[str, float] | None:
    """Načíta charge/discharge prahy z JSON; prázdny alebo chýbajúci súbor → None (auto z cien)."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    low = data.get("charge_below_eur_per_kwh")
    high = data.get("discharge_above_eur_per_kwh")
    if low is None or high is None:
        return None
    out: dict[str, float] = {
        "price_low": float(low),
        "price_high": float(high),
    }
    exp = data.get("expensive_hour_threshold_eur_per_kwh")
    if exp is not None:
        out["price_expensive"] = float(exp)
    return out


# --- load (historická spotreba) ---


def load_consumption_csv(path: Path | str) -> pd.DataFrame:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    df = pd.read_csv(p, sep=None, engine="python", encoding="utf-8-sig")
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    if "datetime" not in df.columns:
        raise ValueError("CSV must contain column: datetime")
    if "load_kw" not in df.columns:
        if "load_mw" in df.columns:
            df["load_kw"] = pd.to_numeric(df["load_mw"], errors="coerce") * 1000.0
        else:
            raise ValueError("CSV must contain load_kw (or load_mw)")
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    df["load_kw"] = pd.to_numeric(df["load_kw"], errors="coerce").fillna(0.0).clip(lower=0.0)
    df = df.sort_values("datetime").reset_index(drop=True)
    if "price_eur_per_kwh" in df.columns:
        df["price_eur_per_kwh"] = pd.to_numeric(df["price_eur_per_kwh"], errors="coerce")
    else:
        df["price_eur_per_kwh"] = None
    return df


def infer_timestep_hours(df: pd.DataFrame) -> float:
    if len(df) < 2:
        return 0.25
    d = df["datetime"].diff().dt.total_seconds().median() / 3600.0
    return float(d) if pd.notna(d) and d > 0 else 0.25


# --- ceny ---


def build_price_series(df: pd.DataFrame, cfg: dict) -> pd.Series:
    if "price_eur_per_kwh" not in df.columns:
        raise ValueError("CSV must contain mandatory column: price_eur_per_kwh")
    s = pd.to_numeric(df["price_eur_per_kwh"], errors="coerce")
    if not s.notna().any():
        raise ValueError("CSV price_eur_per_kwh is mandatory and must contain at least one numeric value")
    med = float(s.median())
    return s.fillna(med).astype(float)


# --- FVE (syntetický profil) ---


def synthetic_pv_kw(dt: pd.Series, installed_kwp: float) -> pd.Series:
    """Last-resort PV shape when the AI forecast is missing. Not a user yield."""
    if installed_kwp <= 0:
        return pd.Series(0.0, index=dt.index, name="pv_kw")

    t = pd.DatetimeIndex(pd.to_datetime(dt))
    hours = (t.hour + t.minute / 60.0).astype(float)
    day_of_year = t.dayofyear.values.astype(float)
    seasonal = 0.85 + 0.15 * np.cos(2 * math.pi * (day_of_year - 172) / 365.0)
    solar_elev = np.clip(np.sin((hours - 6.0) / 12.0 * np.pi), 0.0, 1.0) ** 1.2
    raw = np.asarray(seasonal * solar_elev * installed_kwp, dtype=float)
    return pd.Series(np.clip(raw, 0.0, installed_kwp * 1.15), index=dt.index, name="pv_kw")


def annual_kwh_per_kwp_from_profile(pv_per_kwp: np.ndarray | None, dt_h: float) -> float:
    """Year-equivalent specific yield of a 1 kWp series (AI forecast or fallback)."""
    if pv_per_kwp is None or dt_h <= 0:
        return 0.0
    arr = np.asarray(pv_per_kwp, dtype=float)
    if len(arr) == 0:
        return 0.0
    hours = float(len(arr)) * float(dt_h)
    if hours <= 1e-9:
        return 0.0
    return float(np.sum(arr) * dt_h) * (8760.0 / hours)


def load_pv_profile_per_kwp(
    path: Path | str | None,
    df: pd.DataFrame,
    *,
    reference_kwp: float | None = None,
) -> np.ndarray | None:
    """Read the AI-forecast production shape normalised to one installed kWp.

    PvoutToVirtualSolarPiece writes ``pv_kw_per_kwp`` alongside the absolute
    profile. Older files carry only ``pv_kw``, which is divided by the reference
    size it was generated at. Returns None when nothing usable is available, so
    callers keep working on the synthetic fallback rather than failing.
    """
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None

    raw = pd.read_csv(p)
    if "pv_kw_per_kwp" in raw.columns:
        series = pd.to_numeric(raw["pv_kw_per_kwp"], errors="coerce")
    elif "pv_kw" in raw.columns:
        if not reference_kwp or reference_kwp <= 0:
            return None
        series = pd.to_numeric(raw["pv_kw"], errors="coerce") / float(reference_kwp)
    else:
        return None

    if "datetime" in raw.columns:
        indexed = pd.Series(
            series.to_numpy(), index=pd.to_datetime(raw["datetime"], errors="coerce")
        )
        indexed = indexed[~indexed.index.isna()]
        indexed = indexed[~indexed.index.duplicated(keep="first")]
        aligned = indexed.reindex(pd.to_datetime(df["datetime"]))
        if aligned.notna().sum() == 0:
            return None
        return aligned.fillna(0.0).clip(lower=0.0).to_numpy(dtype=float)

    if len(series) != len(df):
        return None
    return series.fillna(0.0).clip(lower=0.0).to_numpy(dtype=float)


def plant_om_eur_per_year(cfg: dict[str, Any] | None, kwp: float, kwh: float) -> float:
    pv = (cfg or {}).get("pv") or {}
    bat = (cfg or {}).get("battery") or {}
    om = float(pv.get("om_eur_per_kwp_year", 0.0) or 0.0) * max(0.0, float(kwp))
    om += float(bat.get("om_eur_per_kwh_year", 0.0) or 0.0) * max(0.0, float(kwh))
    return float(om)


def plant_degradation_frac(cfg: dict[str, Any] | None) -> float:
    try:
        pct = float(((cfg or {}).get("pv") or {}).get("degradation_pct_per_year", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return float(min(0.05, max(0.0, pct / 100.0)))


def battery_dispatch_wear_eur_per_kwh(bat_cfg: dict[str, Any] | None) -> float:
    """Wear / round-trip friction used inside dispatch — not CAPEX recovery.

    Putting the battery annuity into the dispatch threshold (~0.11 €/kWh)
    meant the pack almost never cycled, so the optimiser always picked 0 kWh.
    CAPEX belongs in NPV, not in the hourly bid.
    """
    try:
        wear = float((bat_cfg or {}).get("throughput_cost_eur_per_kwh", 0.02) or 0.02)
    except (TypeError, ValueError):
        wear = 0.02
    return float(min(0.05, max(0.0, wear)))


# --- batéria (ekonomický dispatch: LCOE FVE vs sieť vs náklad kWh z batérie) ---


def _dis_cap_kw(soc_pct: float, e_kwh: float, eta_d: float, dt_h: float, pmax: float) -> float:
    e_avail = max(0.0, soc_pct / 100.0 * e_kwh * eta_d)
    return float(min(pmax, e_avail / dt_h if dt_h > 0 else 0.0))


def _ch_cap_kw(soc_pct: float, e_kwh: float, eta_c: float, dt_h: float, pmax: float) -> float:
    room = max(0.0, (100.0 - soc_pct) / 100.0 * e_kwh)
    return float(min(pmax, room / (dt_h * eta_c) if dt_h > 0 else 0.0))


def compute_levelized_economics(
    pv_cfg: dict,
    bat_cfg: dict,
    an_cfg: dict,
    en_cfg: dict,
    *,
    installed_kwp: float,
    yield_kwp: float,
    energy_kwh: float,
    pv_capex: float,
    bat_capex: float,
    eta_c: float,
    eta_d: float,
    years: int,
    dr: float,
    use_pv: bool,
    use_bat: bool,
) -> dict[str, float]:
    """
    Orientačné €/kWh:
    - PV LCOE: anuita CAPEX + OPEX / odhad ročnej výroby (so zjednodušenou degradáciou).
    - Batéria: anuita CAPEX / ročný prietok kWh (cykly × kapacita); + efektívne pri AC.
    """
    deg = float(pv_cfg.get("degradation_pct_per_year", 0.5)) / 100.0
    om_kwp = float(pv_cfg.get("om_eur_per_kwp_year", 0.0))
    avg_yield_factor = max(0.5, 1.0 - deg * (years / 2.0))
    annual_pv_kwh = max(1.0, installed_kwp * yield_kwp * avg_yield_factor)

    pv_ann = (
        annual_capex_charge_eur(pv_capex, 0.0, years, dr) + om_kwp * installed_kwp if use_pv and installed_kwp > 0 else 0.0
    )
    pv_lcoe = pv_ann / annual_pv_kwh if annual_pv_kwh > 0 else 0.0

    max_efc_y = float(bat_cfg.get("max_equivalent_full_cycles_per_year", 300.0))
    cal_life = int(bat_cfg.get("calendar_life_years", years))
    throughput_kwh_year = max(1.0, max_efc_y * energy_kwh)
    max_cycles_life = max_efc_y * cal_life
    bat_ann = annual_capex_charge_eur(0.0, bat_capex, years, dr) if use_bat and energy_kwh > 0 else 0.0
    bat_fin_per_kwh_throughput = bat_ann / throughput_kwh_year if throughput_kwh_year > 0 else 0.0
    bat_replace_per_kwh = (bat_capex / max(max_cycles_life * energy_kwh, 1.0)) if bat_capex > 0 else 0.0
    bat_marginal_throughput = bat_fin_per_kwh_throughput + 0.5 * bat_replace_per_kwh
    eta_rt = eta_c * eta_d
    bat_at_grid = bat_marginal_throughput / max(eta_rt, 1e-6)

    feed_in = float(en_cfg.get("feed_in_surplus_eur_per_kwh", 0.05))
    pv_to_batt = max(feed_in, pv_lcoe) / max(eta_c, 1e-6)

    return {
        "pv_lcoe_eur_per_kwh": float(pv_lcoe),
        "pv_annual_kwh_est": float(annual_pv_kwh),
        "battery_marginal_eur_per_kwh_throughput": float(bat_marginal_throughput),
        "battery_eur_per_kwh_at_grid_effective": float(bat_at_grid),
        "opportunity_pv_to_battery_eur_per_kwh_stored": float(pv_to_batt),
        "round_trip_efficiency": float(eta_rt),
    }


def decide_mrk_peak_reserve(
    net_load_kw: np.ndarray,
    dt_h: float,
    *,
    energy_kwh: float,
    max_c_rate: float,
    eta_c: float,
    eta_d: float,
    mrk_contract_kw: float,
    peak_shaving_reserve_pct: float,
    excess_penalty_eur_per_kw: float,
    fee_eur_per_kw_month: float = 0.0,
    wear: float = 0.02,
    price_low: float = 0.0,
    price_high: float = 0.0,
    timestamps: Any = None,
) -> dict[str, Any]:
    """Hold an energy reserve for MRK peaks only when clipping them pays.

    The form value is a *cap* (how much of the pack we are willing to lock),
    not a mandate. Dispatch already clips import when the interval is over the
    contract; the reserve only withholds that slice from day-ahead arbitrage
    so the pack is not empty when a peak arrives.

    Benefit is the period-scaled penalty plus monthly MRK fee on the kW the
    reserved energy can actually shave in months where (load − PV) exceeds
    the contract. Cost is the arbitrage spread that slice of energy would
    otherwise cycle. If there is no overflow, or the spread is worth more
    than the peak charges, the reserve stays at 0 kWh.
    """
    requested_pct = float(np.clip(float(peak_shaving_reserve_pct or 0.0), 0.0, 95.0))
    e_kwh = float(energy_kwh or 0.0)
    contract = float(mrk_contract_kw or 0.0)
    empty = {
        "enabled": False,
        "reason": "off_no_request",
        "requested_pct": requested_pct,
        "reserve_kwh": 0.0,
        "overflow_months": 0,
        "max_overflow_kw": 0.0,
        "shave_kw": 0.0,
        "benefit_eur_year": 0.0,
        "cost_eur_year": 0.0,
    }
    if requested_pct <= 1e-9 or e_kwh <= 1e-6:
        return empty
    if contract <= 1e-6:
        return {**empty, "reason": "off_no_contract"}

    net_pos = np.maximum(np.asarray(net_load_kw, dtype=float), 0.0)
    monthly_peaks: list[float]
    if timestamps is not None and len(np.asarray(timestamps)) == len(net_pos) and len(net_pos) > 0:
        ts = pd.to_datetime(pd.Series(timestamps), errors="coerce")
        dfp = pd.DataFrame({"p": net_pos, "ts": ts}).dropna(subset=["ts"])
        if dfp.empty:
            monthly_peaks = [float(net_pos.max())]
        else:
            monthly_peaks = (
                dfp.groupby(dfp["ts"].dt.to_period("M"))["p"].max().astype(float).tolist()
            )
    else:
        monthly_peaks = [float(net_pos.max())] if len(net_pos) else []
    if not monthly_peaks:
        return {**empty, "reason": "off_no_overflow"}

    pmax = max(0.0, float(max_c_rate) * e_kwh)
    reserve_kwh = e_kwh * requested_pct / 100.0
    shave_cap = min(pmax, reserve_kwh * float(eta_d) / max(float(dt_h), 1e-9))
    n_over = 0
    max_overflow = 0.0
    period_penalty = 0.0
    period_fee = 0.0
    for peak in monthly_peaks:
        overflow = max(0.0, float(peak) - contract)
        if overflow <= 1e-6:
            continue
        n_over += 1
        max_overflow = max(max_overflow, overflow)
        shave = min(shave_cap, overflow)
        period_penalty += float(excess_penalty_eur_per_kw or 0.0) * shave
        period_fee += float(fee_eur_per_kw_month or 0.0) * shave
    if n_over <= 0:
        return {
            **empty,
            "reason": "off_no_overflow",
            "requested_pct": requested_pct,
        }

    n_months = max(1, len(monthly_peaks))
    year_scale = 12.0 / float(n_months)
    benefit_year = (period_penalty + period_fee) * year_scale

    spread = max(0.0, float(price_high) - float(price_low))
    rt = max(1e-6, float(eta_c) * float(eta_d))
    net_spread = max(0.0, spread * rt - max(0.0, float(wear)))
    cycles_per_day = 0.45 if net_spread > 0.01 else 0.08
    missed_cycles_year = min(280.0, 365.0 * cycles_per_day)
    cost_year = reserve_kwh * missed_cycles_year * net_spread
    enabled = benefit_year > cost_year + 1.0
    return {
        "enabled": bool(enabled),
        "reason": "on_worth" if enabled else "off_not_worth",
        "requested_pct": round(requested_pct, 2),
        "reserve_kwh": round(reserve_kwh if enabled else 0.0, 2),
        "overflow_months": int(n_over),
        "max_overflow_kw": round(max_overflow, 2),
        "shave_kw": round(min(shave_cap, max_overflow), 2),
        "benefit_eur_year": round(float(benefit_year), 2),
        "cost_eur_year": round(float(cost_year), 2),
    }


def dispatch_battery(
    net_load_kw: np.ndarray,
    price: np.ndarray,
    dt_h: float,
    *,
    energy_kwh: float,
    max_c_rate: float,
    eta_c: float,
    eta_d: float,
    initial_soc_pct: float,
    mrk_contract_kw: float,
    feed_in_eur_per_kwh: float = 0.05,
    pv_lcoe_eur_per_kwh: float = 0.12,
    battery_throughput_eur_per_kwh: float = 0.02,
    max_fraction_from_grid_charge: float = 0.72,
    excess_penalty_eur_per_kw: float = 0.0,
    fee_eur_per_kw_month: float = 0.0,
    peak_shaving_reserve_pct: float = 30.0,
    price_low: float | None = None,
    price_high: float | None = None,
    price_expensive: float | None = None,
    distribution_eur_per_kwh: float = 0.0,
    timestamps: Any = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Dvojkomorový model: energia z FVE (spv) vs. zo siete (sg).
    - Zo siete max. max_fraction_from_grid_charge kapacity – zvyšok rezerva na FVE a špičkové štiepenie MRK.
    - Uložené kWh z FVE stoja predaj do siete (feed-in), nie LCOE — to je utopený náklad.
    - Vybíjanie: MRK nad zmluvou; potom keď nákup (DAM + distribúcia) stojí viac ako energia v batérii.
    - Nabíjanie zo siete: lacný kvantil + arbitráž vs. drahý + MRK headroom.
    - throughput je opotrebenie, nie anuita CAPEX (tá ide do NPV).
    - Rezerva na špičky MRK sa drží len keď zrezanie prekročenia zmluvy
      (penále + mesačný poplatok za kW) prevýši stratu z arbitráže.
    """
    n = len(net_load_kw)
    pmax = max(0.0, max_c_rate * energy_kwh)
    grid = np.zeros(n)
    export = np.zeros(n)
    soc_pct = np.zeros(n + 1)
    soc_pct[0] = float(initial_soc_pct)
    p_batt = np.zeros(n)

    p = np.asarray(price, dtype=float)
    p_low = float(price_low) if price_low is not None else float(np.quantile(p, 0.30))
    p_high = float(price_high) if price_high is not None else float(np.quantile(p, 0.75))
    p_exp = float(price_expensive) if price_expensive is not None else float(np.percentile(p, 70.0))
    p_med = float(np.median(p))
    try:
        dist = max(0.0, float(distribution_eur_per_kwh or 0.0))
    except (TypeError, ValueError):
        dist = 0.0
    wear = max(0.0, float(battery_throughput_eur_per_kwh or 0.0))
    net_pos = np.maximum(np.asarray(net_load_kw, dtype=float), 0.0)
    charge_ceiling_kw = float(np.quantile(net_pos, 0.85)) if len(net_pos) else 0.0
    if mrk_contract_kw > 0.0:
        charge_ceiling_kw = min(charge_ceiling_kw, mrk_contract_kw)

    E = float(energy_kwh)
    soc_kwh0 = float(initial_soc_pct) / 100.0 * E
    spv = soc_kwh0 * 0.65
    sg = min(soc_kwh0 * 0.35, max_fraction_from_grid_charge * E)
    spv = max(0.0, soc_kwh0 - sg)
    max_sg = max_fraction_from_grid_charge * E

    # LCOE is sunk once the array is built. Stored surplus only forgoes feed-in.
    _ = pv_lcoe_eur_per_kwh
    opp_kwh = max(0.0, float(feed_in_eur_per_kwh)) / max(eta_c, 1e-6)
    reserve_decision = decide_mrk_peak_reserve(
        net_load_kw,
        dt_h,
        energy_kwh=E,
        max_c_rate=max_c_rate,
        eta_c=eta_c,
        eta_d=eta_d,
        mrk_contract_kw=mrk_contract_kw,
        peak_shaving_reserve_pct=peak_shaving_reserve_pct,
        excess_penalty_eur_per_kw=excess_penalty_eur_per_kw,
        fee_eur_per_kw_month=fee_eur_per_kw_month,
        wear=wear,
        price_low=p_low,
        price_high=p_high,
        timestamps=timestamps,
    )
    dispatch_battery.last_peak_reserve = reserve_decision
    reserve_enabled = bool(reserve_decision.get("enabled"))
    reserve_kwh = float(reserve_decision.get("reserve_kwh") or 0.0)
    value_eur = spv * opp_kwh + sg * (p_med / max(eta_c, 1e-6))

    def _total_kwh() -> float:
        return spv + sg

    def _s_pct() -> float:
        tot = _total_kwh()
        return 100.0 * tot / max(E, 1e-9)

    for t in range(n):
        net = float(net_load_kw[t])
        pr = float(p[t])
        s = _s_pct()
        soc_k = _total_kwh()

        if E <= 1e-6:
            grid[t] = max(0.0, net)
            soc_pct[t + 1] = s
            continue

        if net <= 0.0:
            surplus = -net
            ch = min(surplus, _ch_cap_kw(s, E, eta_c, dt_h, pmax))
            kwh_in = ch * dt_h * eta_c
            room = max(0.0, E - spv - sg)
            add = min(kwh_in, room)
            spv += add
            value_eur += opp_kwh * add
            ch_eff = add / max(eta_c * dt_h, 1e-12) if add > 1e-12 else 0.0
            soc_pct[t + 1] = min(100.0, (spv + sg) / max(E, 1e-9) * 100.0)
            grid[t] = 0.0
            export[t] = max(0.0, surplus - ch_eff)
            p_batt[t] = -ch_eff
            continue

        dis_cap = _dis_cap_kw(s, E, eta_d, dt_h, pmax)
        over_mrk = max(0.0, net - mrk_contract_kw)
        avg_eur = value_eur / max(soc_k, 1e-9)
        thr = avg_eur / max(eta_d, 1e-9) + wear
        above_reserve_kwh = max(0.0, soc_k - reserve_kwh)
        dis_cap_above_reserve = min(dis_cap, above_reserve_kwh * max(eta_d, 1e-9) / max(dt_h, 1e-9))
        import_unit = pr + dist

        want_dis = 0.0
        if over_mrk > 1e-6:
            want_dis = max(want_dis, min(over_mrk, dis_cap))
        if import_unit >= thr and soc_k > 1e-6:
            want_dis = max(want_dis, min(net, dis_cap_above_reserve))
        if pr >= p_exp and soc_k > 0.08 * E:
            want_dis = max(want_dis, min(net, dis_cap_above_reserve))

        dis = float(np.clip(want_dis, 0.0, min(dis_cap, net)))
        kwh_out = dis * dt_h / max(eta_d, 1e-9)
        tot_b = spv + sg
        if kwh_out > 1e-9 and tot_b > 1e-9:
            r = min(1.0, kwh_out / tot_b)
            value_eur *= 1.0 - r
            spv *= 1.0 - r
            sg *= 1.0 - r

        soc_k_after = spv + sg
        s_after = soc_k_after / E * 100.0

        ch = 0.0
        arbitrage_ok = ((pr + dist) / max(eta_c, 1e-9) + wear) < (
            (p_high + dist) / max(eta_d, 1e-9) - 0.004
        )
        room_grid = max(0.0, max_sg - sg)
        room_total = max(0.0, E - spv - sg)
        need_reserve_refill = reserve_enabled and soc_k_after < reserve_kwh
        if pr <= p_low and s_after < 92.0 and (arbitrage_ok or need_reserve_refill) and room_grid > 1e-6:
            headroom = max(0.0, charge_ceiling_kw - (net - dis)) if charge_ceiling_kw > 0.0 else pmax
            if charge_ceiling_kw > 0.0 and headroom > 1e-6:
                ch = min(_ch_cap_kw(s_after, E, eta_c, dt_h, pmax), pmax * 0.9, headroom)
            elif charge_ceiling_kw <= 0.0:
                ch = min(_ch_cap_kw(s_after, E, eta_c, dt_h, pmax), pmax * 0.9)

        kwh_ch = ch * dt_h * eta_c
        kwh_ch = min(kwh_ch, room_total, room_grid)
        ch_grid = kwh_ch / max(eta_c * dt_h, 1e-12) if kwh_ch > 1e-12 else 0.0
        sg += kwh_ch
        value_eur += pr * ch_grid * dt_h

        grid[t] = max(0.0, net - dis + ch_grid)
        export[t] = 0.0
        soc_k_final = spv + sg
        soc_pct[t + 1] = float(np.clip(soc_k_final / E * 100.0, 0.0, 100.0))
        p_batt[t] = dis - ch_grid

    return grid, soc_pct[1:], p_batt, export


# --- náklady ---


def distribution_tariff_eur_per_kwh(cfg: dict[str, Any] | None) -> float:
    """Network / distribution charge added on top of the commodity (DAM) price."""
    try:
        return max(0.0, float(((cfg or {}).get("energy") or {}).get("distribution_eur_per_kwh") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def energy_cost_eur(
    grid_import_kw: pd.Series,
    price_eur_per_kwh: pd.Series,
    dt_h: float,
    distribution_eur_per_kwh: float = 0.0,
) -> float:
    e_kwh = grid_import_kw.clip(lower=0.0) * dt_h
    try:
        dist = max(0.0, float(distribution_eur_per_kwh or 0.0))
    except (TypeError, ValueError):
        dist = 0.0
    unit = price_eur_per_kwh.astype(float) + dist
    return float((e_kwh * unit).sum())


def feed_in_revenue_eur(
    surplus_kw: pd.Series,
    feed_in_eur_per_kwh: float,
    dt_h: float,
) -> float:
    exp_kwh = surplus_kw.clip(lower=0.0) * dt_h
    return float(exp_kwh.sum() * feed_in_eur_per_kwh)


def mrk_component_monthly(
    grid_import_kw: pd.Series,
    timestamps: pd.Series,
    *,
    contract_kw: float,
    fee_eur_per_kw_month: float,
    excess_penalty_eur_per_kw: float,
) -> tuple[float, dict]:
    df = pd.DataFrame({"ts": pd.to_datetime(timestamps), "g": grid_import_kw.astype(float).values})
    df["month"] = df["ts"].dt.to_period("M")
    months = df["month"].unique()
    total_fixed = 0.0
    total_excess = 0.0
    detail: dict[str, dict] = {}
    for m in months:
        sub = df[df["month"] == m]
        peak = float(sub["g"].max()) if len(sub) else 0.0
        fixed = contract_kw * fee_eur_per_kw_month
        excess_kw = max(0.0, peak - contract_kw)
        excess_cost = excess_kw * excess_penalty_eur_per_kw
        total_fixed += fixed
        total_excess += excess_cost
        detail[str(m)] = {
            "monthly_peak_kw": peak,
            "fixed_rv_eur": fixed,
            "excess_kw": excess_kw,
            "excess_penalty_eur": excess_cost,
        }
    return float(total_fixed + total_excess), detail


def annual_capex_charge_eur(pv_capex: float, bat_capex: float, years: int, discount_rate: float) -> float:
    if years <= 0:
        return 0.0
    total = pv_capex + bat_capex
    if discount_rate <= 0:
        return total / years
    r = discount_rate
    ann = r * (1 + r) ** years / ((1 + r) ** years - 1)
    return float(total * ann)


def equivalent_full_cycles(soc_pct_series: pd.Series) -> float:
    ch = soc_pct_series.diff().abs().sum()
    return float(ch / 200.0)


def _annual_cycles_from_period(cycles_period: float, days_in_sample: float) -> float:
    if days_in_sample <= 1e-9:
        return 0.0
    return float(cycles_period) * (365.0 / float(days_in_sample))


def build_battery_soh_assessment(
    *,
    equivalent_cycles_period: float,
    days_in_sample: float,
    battery_cfg: dict[str, Any],
) -> dict[str, Any]:
    annual_cycles = _annual_cycles_from_period(equivalent_cycles_period, days_in_sample)
    cal_raw = battery_cfg.get("calendar_life_years")
    try:
        cal_life_years = float(cal_raw) if cal_raw is not None and str(cal_raw).strip() != "" else None
    except (TypeError, ValueError):
        cal_life_years = None
    if cal_life_years is not None and cal_life_years <= 0:
        cal_life_years = None
    cycle_life_at_eol = float(
        battery_cfg.get("cycle_life_at_eol")
        or battery_cfg.get("cycle_life")
        or 8000
    )
    eol_capacity_pct = float(battery_cfg.get("eol_capacity_pct", 80.0))
    life_by_cycles = cycle_life_at_eol / max(annual_cycles, 1e-9) if annual_cycles > 1e-9 else float("inf")
    if cal_life_years is not None:
        expected_life_years = min(cal_life_years, life_by_cycles) if math.isfinite(life_by_cycles) else cal_life_years
        cap_fade_per_year_cal = (100.0 - eol_capacity_pct) / max(cal_life_years, 1e-9)
    else:
        expected_life_years = life_by_cycles
        cap_fade_per_year_cal = 0.0
    cap_fade_per_cycle = (100.0 - eol_capacity_pct) / max(cycle_life_at_eol, 1e-9)
    cap_fade_per_year_cycles = annual_cycles * cap_fade_per_cycle
    cap_fade_per_year_total = cap_fade_per_year_cal + cap_fade_per_year_cycles
    return {
        "equivalent_cycles_period": round(float(equivalent_cycles_period), 3),
        "annual_equivalent_cycles_est": round(float(annual_cycles), 2),
        "calendar_life_years": round(cal_life_years, 2) if cal_life_years is not None else None,
        "cycle_life_at_eol": round(cycle_life_at_eol, 1),
        "end_of_life_capacity_pct": round(eol_capacity_pct, 2),
        "estimated_life_years_by_cycles": round(float(life_by_cycles), 2) if math.isfinite(life_by_cycles) else None,
        "estimated_life_years_effective": round(float(expected_life_years), 2) if math.isfinite(expected_life_years) else None,
        "estimated_capacity_fade_pct_per_year": round(float(cap_fade_per_year_total), 3),
    }


def apply_finance_layer(
    *,
    annual_operating_savings_eur: float,
    total_capex_eur: float,
    analysis_years: int,
    discount_rate: float,
    finance_cfg: dict[str, Any],
) -> dict[str, Any]:
    om_abs = float(finance_cfg.get("o_and_m_eur_per_year", 0.0))
    om_pct = float(finance_cfg.get("o_and_m_pct_of_capex", 0.0))
    ancillary = float(finance_cfg.get("ancillary_revenue_eur_per_year", 0.0))
    debt_ratio = float(finance_cfg.get("debt_ratio_of_capex", 0.0))
    debt_rate = float(finance_cfg.get("debt_interest_rate", 0.0))
    debt_years = int(finance_cfg.get("debt_years", max(1, analysis_years)))
    tax_rate = float(finance_cfg.get("tax_rate_pct", 0.0)) / 100.0

    annual_om = om_abs + (om_pct / 100.0) * total_capex_eur
    debt_principal = max(0.0, min(1.0, debt_ratio)) * total_capex_eur
    debt_annual_payment = annual_capex_charge_eur(debt_principal, 0.0, debt_years, debt_rate)
    pre_tax = annual_operating_savings_eur + ancillary - annual_om - debt_annual_payment
    tax = max(0.0, pre_tax) * max(0.0, tax_rate)
    annual_after_tax = pre_tax - tax
    npv_after_tax = -total_capex_eur + _npv_annuity(annual_after_tax, analysis_years, discount_rate)
    payback_after_tax = (total_capex_eur / annual_after_tax) if annual_after_tax > 1e-9 else None

    return {
        "annual_operating_savings_eur_before_finance": round(float(annual_operating_savings_eur), 2),
        "annual_o_and_m_eur": round(float(annual_om), 2),
        "annual_ancillary_revenue_eur": round(float(ancillary), 2),
        "annual_debt_service_eur": round(float(debt_annual_payment), 2),
        "annual_tax_eur": round(float(tax), 2),
        "annual_net_cashflow_after_finance_eur": round(float(annual_after_tax), 2),
        "simple_payback_after_finance_years": round(float(payback_after_tax), 3) if payback_after_tax else None,
        "npv_after_finance_eur": round(float(npv_after_tax), 2),
    }


def dispatch_trading_only(
    price: np.ndarray,
    dt_h: float,
    *,
    energy_kwh: float,
    max_c_rate: float,
    eta_c: float,
    eta_d: float,
    initial_soc_pct: float,
    price_low: float | None = None,
    price_high: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    n = len(price)
    if energy_kwh <= 1e-9 or n == 0:
        return np.zeros(n), np.zeros(n)
    pmax = max(0.0, max_c_rate * energy_kwh)
    p = np.asarray(price, dtype=float)
    p_low = float(price_low) if price_low is not None else float(np.quantile(p, 0.30))
    p_high = float(price_high) if price_high is not None else float(np.quantile(p, 0.75))
    soc = float(np.clip(initial_soc_pct, 0.0, 100.0)) / 100.0 * energy_kwh
    imp_kw = np.zeros(n)
    exp_kw = np.zeros(n)
    for t in range(n):
        pr = float(p[t])
        if pr <= p_low:
            room = max(0.0, energy_kwh - soc)
            ch_kw = min(pmax, room / max(dt_h * eta_c, 1e-9))
            if ch_kw > 1e-12:
                imp_kw[t] = ch_kw
                soc += ch_kw * dt_h * eta_c
        elif pr >= p_high:
            avail_kw = min(pmax, soc * eta_d / max(dt_h, 1e-9))
            if avail_kw > 1e-12:
                exp_kw[t] = avail_kw
                soc -= avail_kw * dt_h / max(eta_d, 1e-9)
        soc = float(np.clip(soc, 0.0, energy_kwh))
    return imp_kw, exp_kw


def run_c_rate_sweep(cfg: dict[str, Any], df: pd.DataFrame, c_rates: list[float]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cr in c_rates:
        trial = copy.deepcopy(cfg)
        trial.setdefault("battery", {})["max_c_rate"] = float(cr)
        bundle = _sim_bundle(trial, df)
        optimized = primary_optimized_scenario(bundle)
        if optimized is None:
            continue
        annual_factor = 365.0 / max(bundle["days_in_sample"], 1e-9)
        annual_savings = (
            float(bundle["baseline"]["total_operating_eur"]) - float(optimized["total_operating_eur"])
        ) * annual_factor
        cycles_period = float(optimized.get("equivalent_full_cycles", 0.0))
        soh = build_battery_soh_assessment(
            equivalent_cycles_period=cycles_period,
            days_in_sample=float(bundle["days_in_sample"]),
            battery_cfg=trial.get("battery") or {},
        )
        rows.append(
            {
                "c_rate": float(cr),
                "annual_operating_savings_eur": round(float(annual_savings), 2),
                "equivalent_cycles_period": round(cycles_period, 3),
                "annual_equivalent_cycles_est": soh["annual_equivalent_cycles_est"],
                "estimated_life_years_effective": soh["estimated_life_years_effective"],
                "optimized_total_operating_eur_period": round(float(optimized["total_operating_eur"]), 2),
            }
        )
    return rows


def analyze_price_input_quality(
    df: pd.DataFrame,
    price: pd.Series,
    load_kw: np.ndarray,
) -> dict[str, Any]:
    """Štatistiky cien; či sú z historického CSV."""
    col = "price_eur_per_kwh"
    has_hist = col in df.columns and pd.to_numeric(df[col], errors="coerce").notna().any()
    ps = price.astype(float).values
    lw = np.maximum(load_kw.astype(float), 1e-6)
    wmean = float(np.average(ps, weights=lw))
    return {
        "historical_prices_in_csv": bool(has_hist),
        "recommendation": (
            None
            if has_hist
            else "Pre predajnú analýzu doplňte do CSV stĺpec price_eur_per_kwh (historické OKTE alebo váš ceník)."
        ),
        "price_simple_mean_eur_per_kwh": round(float(np.mean(ps)), 6),
        "price_load_weighted_mean_eur_per_kwh": round(wmean, 6),
        "price_p10_p50_p90_eur_per_kwh": [
            round(float(np.percentile(ps, p)), 6) for p in (10, 50, 90)
        ],
    }


def validate_input_contracts(
    cfg: dict[str, Any],
    df: pd.DataFrame,
    price: pd.Series,
    dt_h: float,
) -> dict[str, Any]:
    """Fail-fast checks for production readiness with optional strict mode."""
    prod = cfg.get("production") or {}
    strict = bool(prod.get("strict_validation", False))
    min_days = float(prod.get("min_sample_days", 7.0))
    max_gap = float(prod.get("max_missing_interval_share", 0.02))
    n = len(df)
    days = n * dt_h / 24.0

    problems: list[str] = []
    warnings: list[str] = []
    if n < 96:
        problems.append("Dataset too short: required at least 96 intervals (1 day @15min).")
    if days < min_days:
        problems.append(f"Dataset too short for production confidence: {days:.2f} < {min_days:.2f} days.")

    dif = df["datetime"].diff().dropna().dt.total_seconds().values
    if len(dif) > 0:
        med = float(np.median(dif))
        miss = float(np.mean(np.abs(dif - med) > 1e-6))
        if miss > max_gap:
            problems.append(
                f"Irregular timestep share too high: {miss:.3%} > {max_gap:.3%}."
            )
    p = price.astype(float).values
    if np.any(~np.isfinite(p)):
        problems.append("Price series contains non-finite values after preprocessing.")
    if np.percentile(p, 5) < 0:
        warnings.append("Price series includes negative values; review tariff source.")

    status = "ok"
    if problems:
        status = "failed" if strict else "warning"
    result = {
        "status": status,
        "strict_validation": strict,
        "problems": problems,
        "warnings": warnings,
        "sample_days": round(days, 3),
        "intervals": n,
        "dt_hours": round(float(dt_h), 6),
    }
    if strict and problems:
        raise ValueError("Production validation failed: " + " | ".join(problems))
    return result


def irr(cashflows: list[float], *, lo: float = -0.95, hi: float = 2.0) -> float | None:
    """Internal rate of return by bisection on the net present value.

    Bisection rather than Newton because the sign change is guaranteed to be
    bracketed here, and a CAPEX-then-savings profile has exactly one root.
    Returns None when the project never turns positive, which is the honest
    answer rather than a number that looks like a rate of return.
    """

    def npv_at(rate: float) -> float:
        return sum(cf / (1.0 + rate) ** i for i, cf in enumerate(cashflows))

    npv_lo, npv_hi = npv_at(lo), npv_at(hi)
    if npv_lo * npv_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        value = npv_at(mid)
        if abs(value) < 1e-6:
            return mid
        if npv_lo * value <= 0:
            hi = mid
        else:
            lo, npv_lo = mid, value
    return (lo + hi) / 2.0


def build_uncertainty_assessment(
    bundle: dict[str, Any],
    *,
    optimized: dict[str, Any] | None,
    iterations: int = 4000,
    seed: int = 20260906,
) -> dict[str, Any]:
    """Monte Carlo over the drivers that actually move the investment case.

    The previous version labelled a fixed -20% savings scenario "P90". A
    percentile is a statement about a distribution, and quoting one that is not
    derived from any distribution invites a reader to size a risk buffer against
    a number that carries no probability. Savings, CAPEX, yield and price drift
    are sampled instead, and the reported percentiles are read off the result.

    The distributions are lognormal for multiplicative quantities so they cannot
    go negative, with spreads set from typical EPC tender variance and the
    year-to-year variability of irradiance in Central Europe.
    """
    if optimized is None:
        return {"note": "No optimized scenario active."}

    base = bundle["baseline"]
    days = max(bundle["days_in_sample"], 1e-6)
    ann = 365.0 / days
    capex = float(bundle["pv_capex"]) + float(bundle["battery_capex"])
    base_sav = (float(base["total_operating_eur"]) - float(optimized["total_operating_eur"])) * ann
    years = int(bundle.get("years", 12))
    dr = float(bundle.get("discount_rate", 0.08))
    annual_om = float(bundle.get("annual_om_eur") or 0.0)
    deg = float(bundle.get("degradation_frac") or 0.0)

    if base_sav <= 0 or capex <= 0:
        return {
            "method": "monte_carlo_v1",
            "note": "Savings or CAPEX are non-positive; percentiles would be meaningless.",
            "p50_annual_savings_eur": round(base_sav, 2),
            "p90_annual_savings_eur": round(base_sav, 2),
        }

    rng = np.random.default_rng(seed)

    # Annual irradiance in Central Europe varies about 5% one standard deviation
    # year to year, which is the dominant uncertainty on production.
    yield_factor = rng.lognormal(mean=0.0, sigma=0.05, size=iterations)
    # Spot price level over the amortisation period is the dominant uncertainty
    # on the value of each kWh, and it is far wider than production.
    price_factor = rng.lognormal(mean=0.0, sigma=0.18, size=iterations)
    # Self-consumption and dispatch performance against the model.
    performance_factor = rng.lognormal(mean=-0.015, sigma=0.06, size=iterations)
    # EPC tender spread, skewed upward because overruns are more common.
    capex_factor = rng.lognormal(mean=0.01, sigma=0.07, size=iterations)

    savings_gross = base_sav * yield_factor * price_factor * performance_factor
    savings = savings_gross - annual_om
    capexes = capex * capex_factor

    paybacks = np.where(savings > 1e-9, capexes / np.maximum(savings, 1e-9), np.inf)
    annuity_om = (1.0 - (1.0 + dr) ** -years) / dr if dr > 0 else float(years)
    fade = float(min(0.05, max(0.0, deg)))
    q = (1.0 - fade) / (1.0 + dr) if dr > -0.999 else 0.0
    if abs(1.0 - q) < 1e-12:
        deg_factor = float(years) / (1.0 + dr) if dr > -0.999 else float(years)
    else:
        deg_factor = (1.0 / (1.0 + dr)) * (1.0 - q ** years) / (1.0 - q)
    npvs = -capexes + savings_gross * deg_factor - annual_om * annuity_om

    def pct(values: np.ndarray, q: float) -> float:
        return float(np.percentile(values, q))

    # P90 in project finance is the conservative end: the value exceeded in 90%
    # of outcomes, which is the 10th percentile of the savings distribution.
    result = {
        "method": "monte_carlo_v1",
        "iterations": iterations,
        "seed": seed,
        "assumptions": {
            "annual_yield_sigma_pct": 5.0,
            "price_level_sigma_pct": 18.0,
            "performance_sigma_pct": 6.0,
            "capex_sigma_pct": 7.0,
            "distribution": "lognormal, independent",
            "note": (
                "Percentiles are read off the simulated distribution. P90 is the "
                "conservative end: the value exceeded in 90% of outcomes."
            ),
        },
        "p10_annual_savings_eur": round(pct(savings, 90), 2),
        "p50_annual_savings_eur": round(pct(savings, 50), 2),
        "p90_annual_savings_eur": round(pct(savings, 10), 2),
        "p10_npv_eur": round(pct(npvs, 90), 2),
        "p50_npv_eur": round(pct(npvs, 50), 2),
        "p90_npv_eur": round(pct(npvs, 10), 2),
        "p50_simple_payback_years": round(pct(paybacks[np.isfinite(paybacks)], 50), 3),
        "p90_simple_payback_years": round(pct(paybacks[np.isfinite(paybacks)], 90), 3),
        "probability_npv_positive": round(float(np.mean(npvs > 0)), 4),
        "probability_payback_under_10y": round(float(np.mean(paybacks < 10.0)), 4),
    }

    # The deterministic envelope stays for continuity with the existing report
    # consumers, but it is no longer where the percentiles come from.
    scenarios = {
        "worst": {"annual_savings_eur": pct(savings, 10), "capex_eur": pct(capexes, 90)},
        "base": {"annual_savings_eur": pct(savings, 50), "capex_eur": pct(capexes, 50)},
        "best": {"annual_savings_eur": pct(savings, 90), "capex_eur": pct(capexes, 10)},
    }
    for value in scenarios.values():
        sav = max(1e-9, float(value["annual_savings_eur"]))
        cap = max(0.0, float(value["capex_eur"]))
        value["simple_payback_years"] = round(cap / sav, 3)
        value["annual_savings_eur"] = round(float(value["annual_savings_eur"]), 2)
        value["capex_eur"] = round(float(value["capex_eur"]), 2)
    result["scenarios"] = scenarios

    base_irr = irr([-capex] + [base_sav] * years)
    p90_irr = irr([-pct(capexes, 90)] + [pct(savings, 10)] * years)
    result["irr_pct"] = round(base_irr * 100.0, 2) if base_irr is not None else None
    result["p90_irr_pct"] = round(p90_irr * 100.0, 2) if p90_irr is not None else None

    return result


def mrk_peak_reduction_and_rv_opportunity(
    baseline_detail: dict[str, dict],
    optimized_detail: dict[str, dict],
    *,
    contract_kw: float,
    fee_eur_per_kw_month: float,
    safety_margin_pct: float,
) -> dict[str, Any]:
    """Porovnanie mesačných špičiek a konzervatívny návrh nižšieho RV."""
    reductions: list[float] = []
    for m, row in optimized_detail.items():
        b = baseline_detail.get(m)
        if not b:
            continue
        reductions.append(float(b["monthly_peak_kw"]) - float(row["monthly_peak_kw"]))
    peaks_opt = [float(v["monthly_peak_kw"]) for v in optimized_detail.values()]
    max_peak_opt = max(peaks_opt) if peaks_opt else 0.0
    sm = max(0.0, safety_margin_pct) / 100.0
    if not peaks_opt:
        # No monthly peaks → no evidence to change the contract.
        recommended_kw = float(contract_kw)
    else:
        raw_rec = max_peak_opt * (1.0 + sm)
        recommended_kw = float(math.ceil(raw_rec / 5.0) * 5.0)
        if recommended_kw <= 0 and contract_kw > 0:
            recommended_kw = 5.0
        # Never present a *higher* contract as a "proposal to cut".
        if recommended_kw >= contract_kw > 0:
            recommended_kw = float(contract_kw)
    cut_kw = max(0.0, contract_kw - recommended_kw)
    months = len(optimized_detail)
    fixed_savings_period = cut_kw * fee_eur_per_kw_month * float(months) if months > 0 else 0.0
    fixed_savings_year = cut_kw * fee_eur_per_kw_month * 12.0

    return {
        "mean_monthly_peak_reduction_kw": round(float(np.mean(reductions)), 3) if reductions else 0.0,
        "max_monthly_peak_import_kw_after_optimization": round(max_peak_opt, 3),
        "recommended_rv_kw_conservative": recommended_kw,
        "current_contract_rv_kw": contract_kw,
        "rv_downsizing_potential_kw": round(cut_kw, 3),
        "fee_eur_per_kw_month": round(float(fee_eur_per_kw_month), 4),
        "safety_margin_pct": round(float(safety_margin_pct), 2),
        "estimated_fixed_rv_fee_savings_if_resized_eur_for_period": round(fixed_savings_period, 2),
        "estimated_fixed_rv_fee_savings_eur_per_year": round(fixed_savings_year, 2),
        "disclaimer": (
            "Návrh MRK je orientačný z historických maxím po simulácii; zmena zmluvy s PDS, rezerva pri výkyvoch "
            "záťaže a riziko prekročenia pri vyššom odbere musia posúdiť prevádzka a zmluva."
        ),
    }


def build_executive_summary(
    baseline: dict[str, Any],
    pvbat: dict[str, Any] | None,
    *,
    days_in_sample: float,
    price_quality: dict[str, Any],
    mrk_rv: dict[str, Any],
    econ: dict[str, float],
) -> dict[str, Any]:
    """Jedna strana pre klienta: úspora za obdobie + annualizácia."""
    if pvbat is None:
        return {"note": "Scenár PV+batéria nie je zapnutý."}
    base_tot = float(baseline["total_operating_eur"])
    alt_tot = float(pvbat["total_operating_eur"])
    sav_op = base_tot - alt_tot
    ann = 365.0 / max(days_in_sample, 1e-6)
    return {
        "operating_cost_baseline_eur": round(base_tot, 2),
        "operating_cost_pv_battery_eur": round(alt_tot, 2),
        "operating_savings_eur_period": round(sav_op, 2),
        "operating_savings_eur_per_year_estimate": round(sav_op * ann, 2),
        "days_in_sample": round(days_in_sample, 2),
        "annualization_factor": round(ann, 4),
        "energy_savings_breakdown_eur_period": {
            "from_lower_energy_bill": round(
                float(baseline["energy_cost_eur"]) - float(pvbat["energy_cost_eur"]), 2
            ),
            "from_mrk_and_rv_component": round(
                float(baseline["mrk_cost_period_eur"]) - float(pvbat["mrk_cost_period_eur"]), 2
            ),
            "from_pv_feed_in_effect": round(
                float(pvbat["feed_in_revenue_eur"]) - float(baseline.get("feed_in_revenue_eur", 0.0)), 2
            ),
        },
        "rv_reservation_opportunity": {
            "peak_reduction_mean_kw": mrk_rv.get("mean_monthly_peak_reduction_kw"),
            "recommended_rv_kw": mrk_rv.get("recommended_rv_kw_conservative"),
            "extra_savings_fixed_rv_fees_eur_period": mrk_rv.get(
                "estimated_fixed_rv_fee_savings_if_resized_eur_for_period"
            ),
        },
        "input_quality": price_quality,
        "reference_economics_eur_per_kwh": {
            "pv_lcoe": round(float(econ.get("pv_lcoe_eur_per_kwh", 0.0)), 6),
            "battery_marginal_throughput": round(float(econ.get("battery_marginal_eur_per_kwh_throughput", 0.0)), 6),
        },
    }


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_hardware_catalogs(
    *,
    battery_catalog_path: str = "",
    inverter_catalog_path: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    pv_path = _project_root() / "catalog" / "pv_modules_catalog.json"
    bt_path = _project_root() / "catalog" / "battery_catalog.json"
    bt_online_path = Path(battery_catalog_path) if battery_catalog_path else None
    inv_path = Path(inverter_catalog_path) if inverter_catalog_path else None
    pv_mods: list[dict[str, Any]] = []
    bt_prods: list[dict[str, Any]] = []
    inv_items: list[dict[str, Any]] = []
    if pv_path.is_file():
        data = json.loads(pv_path.read_text(encoding="utf-8"))
        pv_mods = list(data.get("modules", []))
    if bt_online_path and bt_online_path.is_file():
        data = json.loads(bt_online_path.read_text(encoding="utf-8"))
        bt_prods = list(data.get("products", []))
    elif bt_path.is_file():
        data = json.loads(bt_path.read_text(encoding="utf-8"))
        bt_prods = list(data.get("products", []))
    if inv_path and inv_path.is_file():
        data = json.loads(inv_path.read_text(encoding="utf-8"))
        inv_items = list(data.get("items", []))
    return pv_mods, bt_prods, inv_items


def _apply_system_scope(cfg: dict[str, Any]) -> None:
    eq = cfg.get("equipment") or {}
    scope = str(eq.get("system_scope") or "").strip().lower()
    if not scope:
        return
    if scope in ("pv_only", "pv"):
        cfg["use_pv"], cfg["use_battery"] = True, False
    elif scope in ("battery_only", "battery"):
        cfg["use_pv"], cfg["use_battery"] = False, True
    elif scope in ("pv_and_battery", "both", "pv+battery"):
        cfg["use_pv"], cfg["use_battery"] = True, True


def _sync_scope_flags_to_sizes(cfg: dict[str, Any]) -> None:
    """Align use_pv / use_battery with the resolved kWp and kWh.

    Auto search under ``pv_and_battery`` may pick 0 kWh. Leaving
    ``use_battery=True`` then scores the empty-battery dispatch path, which
    drops feed-in revenue and understates PV-only savings.
    """
    scope = str(((cfg.get("equipment") or {}).get("system_scope") or "")).strip().lower()
    kwp = float(((cfg.get("pv") or {}).get("installed_kwp") or 0.0))
    kwh = float(((cfg.get("battery") or {}).get("energy_kwh") or 0.0))
    if scope in ("pv_only", "pv"):
        cfg["use_pv"], cfg["use_battery"] = True, False
        return
    if scope in ("battery_only", "battery"):
        cfg["use_pv"], cfg["use_battery"] = False, True
        return
    cfg["use_pv"] = kwp > 1e-6
    cfg["use_battery"] = kwh > 1e-6


def _estimate_annual_load_mwh(load_kw: np.ndarray, dt_h: float) -> float:
    n = len(load_kw)
    days = n * dt_h / 24.0
    return float(np.sum(load_kw) * dt_h / 1000.0) * (365.0 / max(days, 1e-6))


def technical_bounds_kwp_kwh(cfg: dict[str, Any], df: pd.DataFrame, dt_h: float) -> dict[str, Any]:
    """Max. kWp / kWh z plochy, strešného zaťaženia alebo odhadu zo spotreby (ako TechnicalLimitsPiece).

    Stropy CAPEX sa sem nepremietajú do osí mapy — obmedzujú len výber
    odporúčanej veľkosti. V režime celkového CAPEX ide o jeden spoločný
    rozpočet (FVE + batéria); v režime rozdelenia sú stropy nezávislé.
    """
    eq = cfg.get("equipment") or {}
    c = eq.get("constraints") or {}
    lay = eq.get("layout") or {}
    pv_ref = cfg.get("pv") or {}
    bat_ref = cfg.get("battery") or {}
    load = df["load_kw"].astype(float).values
    annual_mwh = _estimate_annual_load_mwh(load, dt_h)

    roof = float(c.get("max_roof_area_m2") or 0)
    ground = float(c.get("max_ground_area_m2") or 0)
    batt_m2 = float(c.get("max_battery_area_m2") or 0)
    inst = c.get("installation") or {}
    mount = str(inst.get("mount_type", "roof")).lower()
    kwp_per_m2 = float(lay.get("kwp_per_m2_roof", 0.18))
    # Energy density of an installed C&I battery system per square metre of
    # allocated site area. The previous default of 0.25 kWh/m2 was low by more
    # than two orders of magnitude: catalog cabinets run 42-95 kWh/m2 of device
    # footprint and containers exceed 130, so any site with a stated battery area
    # was capped at a few tens of kWh and the optimiser could never return a
    # battery. 30 kWh/m2 is the cabinet figure derated for access aisles, fire
    # separation and the power conversion system.
    kwh_per_m2 = float(lay.get("kwh_per_m2_battery_area", 30.0))

    area_pv = ground if mount == "ground" and ground > 1e-6 else roof
    max_kwp = area_pv * kwp_per_m2 if area_pv > 1e-6 else 0.0
    max_kwh = batt_m2 * kwh_per_m2 if batt_m2 > 1e-6 else 0.0
    notes: list[str] = []

    if max_kwp <= 1e-6:
        base_kwp = annual_mwh if annual_mwh > 1e-6 else 300.0
        max_kwp = max(100.0, base_kwp * 1.8)
        notes.append("Bez limitu strechy: max. kWp odhad zo spotreby.")
    else:
        notes.append("Limit plochy FVE aplikovaný.")

    if max_kwh <= 1e-6:
        daily_kwh = (annual_mwh * 1000.0 / 365.0) if annual_mwh > 1e-6 else 1500.0
        max_kwh = max(100.0, daily_kwh * 0.65)
        notes.append("Bez plochy ESS: max. kWh odhad z priemerného denného odberu.")

    if c.get("max_battery_kwh") is not None:
        max_kwh = min(max_kwh, float(c["max_battery_kwh"]))
        notes.append("Pevný strop max_battery_kwh.")

    if c.get("roof_load_limit_kg_per_m2") is not None and roof > 1e-6 and mount != "ground":
        max_kwp = min(max_kwp, roof * kwp_per_m2 * 0.92)
        notes.append("Zníženie max. kWp kvôli strešnému zaťaženiu (faktor 0.92).")

    eur_kwp = float(pv_ref.get("specific_capex_eur_per_kwp", 800.0))
    eur_kwh = float(bat_ref.get("specific_capex_eur_per_kwh", 400.0))
    cap = apply_capex_axis_budgets(
        c,
        max_kwp=max_kwp,
        max_kwh=max_kwh,
        eur_per_kwp=eur_kwp,
        eur_per_kwh=eur_kwh,
    )
    notes.extend(capex_limit_notes(cap, slovak=True))

    return {
        "max_kwp": max(0.0, max_kwp),
        "max_kwh": max(0.0, max_kwh),
        "max_kwp_budget": cap["max_kwp_budget"],
        "max_kwh_budget": cap["max_kwh_budget"],
        "capex_mode": cap.get("capex_mode"),
        "max_total_capex_eur": cap.get("max_total_capex_eur"),
        "max_capex_eur": cap.get("max_capex_eur") or 0.0,
        "max_pv_capex_eur": cap.get("max_pv_capex_eur"),
        "max_battery_capex_eur": cap.get("max_battery_capex_eur"),
        "annual_load_mwh_est": round(annual_mwh, 3),
        "notes": notes,
    }


def _npv_annuity(annual: float, years: int, dr: float) -> float:
    if annual <= 0:
        return float("-inf")
    if dr <= 0:
        return annual * float(years)
    return float(annual * ((1.0 - (1.0 + dr) ** (-years)) / dr))


def _discounted_payback_years(capex: float, annual: float, dr: float, max_years: int = 40) -> float | None:
    if annual <= 1e-9 or capex <= 0:
        return None
    cum = 0.0
    for t in range(1, max_years + 1):
        inc = annual / (1.0 + dr) ** t if dr > 0 else annual
        prev = cum
        cum += inc
        if cum >= capex:
            if inc <= 1e-12:
                return float(t)
            frac = (capex - prev) / inc
            return float(t - 1 + frac)
    return None


def _npv_degrading_cashflows(
    annual_gross: float,
    annual_om: float,
    years: int,
    dr: float,
    deg: float,
) -> float:
    """NPV of year-1 bill savings that fade with PV degradation, minus flat O&M."""
    total = 0.0
    fade = float(min(0.05, max(0.0, deg)))
    n = max(1, int(years))
    for t in range(1, n + 1):
        cf = float(annual_gross) * ((1.0 - fade) ** (t - 1)) - float(annual_om)
        total += cf / ((1.0 + dr) ** t) if dr > 0 else cf
    return float(total)


def _discounted_payback_degrading(
    capex: float,
    annual_gross: float,
    annual_om: float,
    dr: float,
    deg: float,
    max_years: int = 40,
) -> float | None:
    if capex <= 0:
        return None
    fade = float(min(0.05, max(0.0, deg)))
    cum = 0.0
    for t in range(1, max_years + 1):
        cf = float(annual_gross) * ((1.0 - fade) ** (t - 1)) - float(annual_om)
        inc = cf / ((1.0 + dr) ** t) if dr > 0 else cf
        prev = cum
        cum += inc
        if cum >= capex:
            if inc <= 1e-12:
                return float(t)
            return float(t - 1 + (capex - prev) / inc)
    return None


def primary_optimized_scenario(bundle: dict[str, Any]) -> dict[str, Any] | None:
    """Aktívny optimalizačný scenár podľa use_pv / use_battery."""
    up, ub = bool(bundle.get("use_pv")), bool(bundle.get("use_bat"))
    if ub and float(bundle.get("energy_kwh") or 0.0) <= 1e-6:
        ub = False
    if up and ub:
        return bundle.get("pv_and_battery")
    if up and not ub:
        return bundle.get("pv_only")
    if ub and not up:
        return bundle.get("battery_only")
    return None


def _score_financials(
    bundle: dict[str, Any],
    *,
    dr: float,
    years: int,
    objective: str,
) -> tuple[float, dict[str, Any]]:
    baseline = bundle["baseline"]
    both = primary_optimized_scenario(bundle)
    if both is None:
        return float("inf"), {"reason": "žiadny aktívny optimalizačný scenár"}
    ann = 365.0 / max(bundle["days_in_sample"], 1e-6)
    sav_period = float(baseline["total_operating_eur"]) - float(both["total_operating_eur"])
    annual_sav = sav_period * ann
    annual_om = float(bundle.get("annual_om_eur") or 0.0)
    deg = float(bundle.get("degradation_frac") or 0.0)
    annual_net = annual_sav - annual_om
    capex = float(bundle["pv_capex"]) + float(bundle["battery_capex"])
    meta = {
        "annual_operating_savings_eur": round(annual_sav, 2),
        "annual_om_eur": round(annual_om, 2),
        "annual_net_cashflow_eur": round(annual_net, 2),
        "simple_payback_years": None,
        "npv_eur": None,
        "total_capex_eur": round(capex, 2),
    }
    if annual_net <= 1e-6 or capex <= 0:
        return float("inf"), {**meta, "reason": "nekladná úspora po O&M alebo nulový CAPEX"}

    pb = capex / annual_net
    npv_v = -capex + _npv_degrading_cashflows(annual_sav, annual_om, years, dr, deg)
    dpb = _discounted_payback_degrading(
        capex, annual_sav, annual_om, dr, deg, max_years=max(years + 5, 40)
    )
    meta["simple_payback_years"] = round(pb, 3)
    meta["npv_eur"] = round(npv_v, 2)
    meta["discounted_payback_years"] = round(dpb, 3) if dpb is not None else None

    if objective in ("max_npv", "max_npv_operating", "npv"):
        return float(-npv_v), meta
    if objective in ("max_annual_savings", "max_savings", "annual_savings", "highest_savings"):
        return float(-annual_net), meta
    return float(pb), meta


def _norm_list(vals: list[float], x: float) -> float:
    if not vals:
        return 0.5
    lo, hi = min(vals), max(vals)
    if hi <= lo + 1e-12:
        return 0.5
    return float(max(0.0, min(1.0, (x - lo) / (hi - lo))))


def rank_pv_modules_for_site(
    modules: list[dict[str, Any]],
    *,
    installation: dict[str, Any],
) -> list[dict[str, Any]]:
    """Zjednodušené skóre podľa priority (z pitonak panel_selection)."""
    inst = {
        "mount_type": str(installation.get("mount_type", "roof")),
        "shading": str(installation.get("shading", "low")),
        "priority": str(installation.get("priority", "balanced")),
        "allow_bifacial": bool(installation.get("allow_bifacial", True)),
    }
    shading_w = {"none": 0.05, "low": 0.15, "medium": 0.35, "high": 0.55}.get(inst["shading"], 0.15)
    prof = {
        "balanced": (1.0, 1.0, 1.0, 1.0, 1.0),
        "max_energy_per_area": (1.6, 0.75, 0.85, 1.0, 1.35),
        "lowest_capex_per_wp": (0.85, 1.55, 0.9, 1.05, 0.95),
        "best_shading_tolerance": (0.85, 0.85, 1.55, 1.0, 1.25),
    }.get(inst["priority"], (1.0, 1.0, 1.0, 1.0, 1.0))
    w_pd, w_eco, w_sh, w_wc, w_eff = prof

    usable = [m for m in modules if str(m.get("manufacturer", "")).strip() != "(šablóna)"]
    if not usable:
        usable = modules
    effs = [float(m.get("efficiency_pct", 20)) for m in usable]
    eurs = [float(m.get("eur_per_wp", 0.35)) for m in usable]
    pds = [float(m.get("power_wp", 300)) / max(float(m.get("area_m2", 2.0)), 0.1) for m in usable]
    ranked: list[dict[str, Any]] = []
    for m in usable:
        eff = float(m.get("efficiency_pct", 20))
        ewp = float(m.get("eur_per_wp", 0.35))
        pd = float(m.get("power_wp", 300)) / max(float(m.get("area_m2", 2.0)), 0.1)
        half = bool(m.get("half_cut", False))
        bif = bool(m.get("bifacial", False))
        sh_score = 0.55 * (1.0 if half else 0.35) + 0.45 * min(1.0, eff / 24.0)
        score = (
            w_pd * _norm_list(pds, pd)
            + w_eco * (1.0 - _norm_list(eurs, ewp))
            + (w_sh * shading_w) * sh_score
            + w_wc * (1.0 if half else 0.4)
            + w_eff * _norm_list(effs, eff)
            + (0.12 * w_pd if (bif and inst["allow_bifacial"]) else 0.0)
        )
        ranked.append({"module": m, "score": round(score, 4)})
    ranked.sort(key=lambda r: -r["score"])
    return ranked


def pick_battery_product(required_kwh: float, products: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not products:
        return None
    fit = [p for p in products if float(p.get("nominal_kwh", 0)) >= required_kwh * 0.9]
    pool = fit if fit else products
    best = min(pool, key=lambda p: abs(float(p.get("nominal_kwh", 0)) - required_kwh))
    return best


def recommend_inverters_for_pv(installed_kwp: float, inverter_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if installed_kwp <= 1e-6 or not inverter_items:
        return None
    usable: list[dict[str, Any]] = []
    for inv in inverter_items:
        paco_w = float(inv.get("paco_w", 0.0) or 0.0)
        if paco_w < 10000.0:
            continue
        name = str(inv.get("name", "")).strip().lower()
        model = str(inv.get("model", "")).strip().lower()
        if name in ("units", "[0]") or model in ("units", "[0]"):
            continue
        vac = float(inv.get("vac", 0.0) or 0.0)
        if vac >= 380.0:
            usable.append(inv)
    if not usable:
        for inv in inverter_items:
            paco_w = float(inv.get("paco_w", 0.0) or 0.0)
            if paco_w >= 10000.0:
                usable.append(inv)
    if not usable:
        return None

    target_ratio = 1.2
    required_ac_kw = installed_kwp / target_ratio
    best: dict[str, Any] | None = None
    best_key: tuple[float, float, float] | None = None
    for inv in usable:
        paco_kw = float(inv.get("paco_w", 0.0) or 0.0) / 1000.0
        if paco_kw <= 1e-9:
            continue
        count = max(1, int(math.ceil(required_ac_kw / paco_kw)))
        total_ac_kw = count * paco_kw
        dc_ac_ratio = installed_kwp / max(total_ac_kw, 1e-9)
        ratio_penalty = abs(dc_ac_ratio - target_ratio)
        oversize_penalty = max(0.0, total_ac_kw - required_ac_kw)
        key = (ratio_penalty, oversize_penalty, -paco_kw)
        if best_key is None or key < best_key:
            best_key = key
            best = {
                "selected_rank_1": {
                    "manufacturer": inv.get("manufacturer"),
                    "model": inv.get("model"),
                    "vac": inv.get("vac"),
                    "paco_w": inv.get("paco_w"),
                    "count": count,
                    "total_ac_kw": round(total_ac_kw, 3),
                    "dc_ac_ratio": round(dc_ac_ratio, 3),
                    "sizing_rationale": (
                        "Voľba podľa cieľového DC/AC ratio ~1.2 a minimálneho nadimenzovania AC výkonu."
                    ),
                }
            }
    return best


def build_hardware_recommendation(
    cfg: dict[str, Any],
    *,
    installed_kwp: float,
    energy_kwh: float,
    pv_modules: list[dict[str, Any]],
    battery_products: list[dict[str, Any]],
    inverter_items: list[dict[str, Any]],
) -> dict[str, Any]:
    eq = cfg.get("equipment") or {}
    c = eq.get("constraints") or {}
    inst = c.get("installation") or {}
    use_pv = bool(cfg.get("use_pv", True))
    use_bat = bool(cfg.get("use_battery", True))
    out: dict[str, Any] = {"pv": None, "battery": None, "inverter": None}
    if use_pv and installed_kwp > 1e-6 and pv_modules:
        ranked = rank_pv_modules_for_site(pv_modules, installation=inst)
        top = ranked[0]["module"] if ranked else pv_modules[0]
        wp = float(top.get("power_wp", 600))
        n_mod = max(1, int(math.ceil(installed_kwp * 1000.0 / max(wp, 1.0))))
        achieved_kwp = round(n_mod * wp / 1000.0, 3)
        out["pv"] = {
            "selected_rank_1": {
                "manufacturer": top.get("manufacturer"),
                "model": top.get("model"),
                "technology": top.get("technology"),
                "power_wp": wp,
                "module_count": n_mod,
                "installed_power_kwp_target": round(installed_kwp, 3),
                "installed_power_kwp_nominal_modules": achieved_kwp,
                "area_m2_est": round(n_mod * float(top.get("area_m2", 2.0)), 1),
                "selection_rationale": [
                    f"Priorita: {inst.get('priority', 'balanced')}, tieň: {inst.get('shading', 'low')}.",
                    "Modul z interného katalógu (pitonak); pred realizáciou overte dostupnosť a cenu u dodávateľa.",
                ],
            },
            "alternatives_top_3": [
                {
                    "manufacturer": r["module"].get("manufacturer"),
                    "model": r["module"].get("model"),
                    "score": r["score"],
                }
                for r in ranked[:3]
            ],
        }
    if use_bat and energy_kwh > 1e-6 and battery_products:
        prod = pick_battery_product(energy_kwh, battery_products)
        if prod:
            out["battery"] = {
                "product_id": prod.get("id"),
                "manufacturer": prod.get("manufacturer"),
                "product_line": prod.get("product_line"),
                "nominal_kwh": prod.get("nominal_kwh"),
                "max_power_kw": prod.get("max_power_kw"),
                "chemistry": prod.get("chemistry"),
                "form_factor": prod.get("form_factor"),
                "note": "Orientačný produkt z katalógu; presný typ rack/inverter podľa projektu DS.",
            }
    if use_pv and installed_kwp > 1e-6 and inverter_items:
        out["inverter"] = recommend_inverters_for_pv(installed_kwp, inverter_items)
    return out


def _sim_bundle(
    cfg: dict[str, Any],
    df: pd.DataFrame,
    *,
    battery_strategy_thresholds: dict[str, float] | None = None,
    pv_profile_per_kwp: np.ndarray | None = None,
) -> dict[str, Any]:
    """Jedna plná ekonomická simulácia (baseline + scenáre) pre danú konfiguráciu.

    ``pv_profile_per_kwp`` is the AI-forecast production shape normalised to one
    installed kWp, aligned to ``df``. When supplied, every scenario scales it by
    the trial size instead of falling back to ``synthetic_pv_kw``. The synthetic
    profile is a smooth sine that ignores weather, latitude and orientation, so
    self-consumption and peak coincidence came out systematically wrong; sizing
    and payback are only as good as the shape they are derived from.
    """
    dt_h = infer_timestep_hours(df)
    strat = battery_strategy_thresholds or {}
    kw_dispatch = {
        k: strat[k]
        for k in ("price_low", "price_high", "price_expensive")
        if k in strat
    }
    price = build_price_series(df, cfg)
    load = df["load_kw"].astype(float).values
    n = len(df)
    ts = df["datetime"]

    pv_cfg = cfg.get("pv") or {}
    bat_cfg = cfg.get("battery") or {}
    mrk_cfg = cfg.get("mrk") or {}
    an_cfg = cfg.get("analysis") or {}
    en_cfg = cfg.get("energy") or {}

    _sync_scope_flags_to_sizes(cfg)
    use_pv = bool(cfg.get("use_pv", True))
    use_bat = bool(cfg.get("use_battery", True))

    installed_kwp = float(pv_cfg.get("installed_kwp", 0.0))
    e_kwh = float(bat_cfg.get("energy_kwh", 0.0))
    c_rate = float(bat_cfg.get("max_c_rate", 0.5))
    eta_c = float(bat_cfg.get("charge_efficiency", 0.95))
    eta_d = float(bat_cfg.get("discharge_efficiency", 0.95))
    soc0 = float(bat_cfg.get("initial_soc_pct", 50.0))

    mrk_kw = float(mrk_cfg.get("contract_kw", 0.0))
    fee_m = float(mrk_cfg.get("fee_eur_per_kw_month", 0.0))
    pen = float(mrk_cfg.get("excess_peak_penalty_eur_per_kw", 0.0))

    pv_capex = installed_kwp * float(pv_cfg.get("specific_capex_eur_per_kwp", 800.0)) if use_pv else 0.0
    bat_capex = e_kwh * float(bat_cfg.get("specific_capex_eur_per_kwh", 400.0)) if use_bat else 0.0
    years = int(an_cfg.get("amortization_years", 12))
    dr = float(an_cfg.get("discount_rate", 0.08))

    feed_in = float(en_cfg.get("feed_in_surplus_eur_per_kwh", 0.05))
    distribution = distribution_tariff_eur_per_kwh(cfg)
    max_frac_grid = float(bat_cfg.get("max_fraction_capacity_from_grid_charge", 0.72))
    peak_reserve_pct = float(bat_cfg.get("peak_shaving_reserve_pct", 30.0))
    wear = battery_dispatch_wear_eur_per_kwh(bat_cfg)
    annual_om = plant_om_eur_per_year(cfg, installed_kwp if use_pv else 0.0, e_kwh if use_bat else 0.0)
    deg = plant_degradation_frac(cfg)
    if pv_profile_per_kwp is not None:
        yield_kwp = annual_kwh_per_kwp_from_profile(pv_profile_per_kwp, dt_h)
    else:
        yield_kwp = annual_kwh_per_kwp_from_profile(
            synthetic_pv_kw(df["datetime"], 1.0).to_numpy(), dt_h
        )

    econ_global = compute_levelized_economics(
        pv_cfg,
        bat_cfg,
        an_cfg,
        en_cfg,
        installed_kwp=installed_kwp,
        yield_kwp=yield_kwp,
        energy_kwh=e_kwh,
        pv_capex=pv_capex,
        bat_capex=bat_capex,
        eta_c=eta_c,
        eta_d=eta_d,
        years=years,
        dr=dr,
        use_pv=use_pv,
        use_bat=use_bat,
    )

    profiles_kw: dict[str, np.ndarray] = {}

    def scenario_case(name: str, pv_on: bool, bat_on: bool, *, trading_only: bool = False) -> dict[str, Any]:
        ann_capex = annual_capex_charge_eur(
            pv_capex if pv_on and use_pv else 0.0,
            bat_capex if bat_on and use_bat else 0.0,
            years,
            dr,
        )
        if pv_on and installed_kwp > 0:
            if pv_profile_per_kwp is not None:
                pv_kw = np.asarray(pv_profile_per_kwp, dtype=float) * installed_kwp
            else:
                pv_kw = synthetic_pv_kw(df["datetime"], installed_kwp).to_numpy()
        else:
            pv_kw = np.zeros(n)

        net = load - pv_kw
        baseline_grid = np.maximum(net, 0.0)
        surplus = np.maximum(-net, 0.0)
        export_kw = surplus.copy()

        if trading_only and bat_on and e_kwh > 1e-6:
            grid, export_kw = dispatch_trading_only(
                price.values.astype(float),
                dt_h,
                energy_kwh=e_kwh,
                max_c_rate=c_rate,
                eta_c=eta_c,
                eta_d=eta_d,
                initial_soc_pct=soc0,
                price_low=kw_dispatch.get("price_low"),
                price_high=kw_dispatch.get("price_high"),
            )
            soc = np.full(n, np.nan)
            cycles = float(np.sum((np.clip(grid, 0.0, None) * dt_h * eta_c) / max(e_kwh, 1e-9)))
            peak_reserve = {}
        elif bat_on and e_kwh > 1e-6:
            g, soc, _p_b, export_kw = dispatch_battery(
                net.astype(float),
                price.values.astype(float),
                dt_h,
                energy_kwh=e_kwh,
                max_c_rate=c_rate,
                eta_c=eta_c,
                eta_d=eta_d,
                initial_soc_pct=soc0,
                mrk_contract_kw=mrk_kw,
                feed_in_eur_per_kwh=feed_in,
                pv_lcoe_eur_per_kwh=float(econ_global["pv_lcoe_eur_per_kwh"]),
                battery_throughput_eur_per_kwh=wear,
                max_fraction_from_grid_charge=max_frac_grid,
                excess_penalty_eur_per_kw=pen,
                fee_eur_per_kw_month=fee_m,
                peak_shaving_reserve_pct=peak_reserve_pct,
                price_low=kw_dispatch.get("price_low"),
                price_high=kw_dispatch.get("price_high"),
                price_expensive=kw_dispatch.get("price_expensive"),
                distribution_eur_per_kwh=distribution,
                timestamps=ts,
            )
            grid = g
            cycles = equivalent_full_cycles(pd.Series(soc))
            peak_reserve = dict(getattr(dispatch_battery, "last_peak_reserve", {}) or {})
        else:
            grid = baseline_grid
            soc = np.full(n, np.nan)
            cycles = 0.0
            peak_reserve = {}

        e_cost = energy_cost_eur(pd.Series(grid), price, dt_h, distribution)
        rev = feed_in_revenue_eur(pd.Series(export_kw), feed_in, dt_h) if pv_on else 0.0
        if trading_only:
            mrk_cost, mrk_detail = 0.0, {}
        else:
            mrk_cost, mrk_detail = mrk_component_monthly(
                pd.Series(grid),
                ts,
                contract_kw=mrk_kw,
                fee_eur_per_kw_month=fee_m,
                excess_penalty_eur_per_kw=pen,
            )
        total_op = e_cost + mrk_cost - rev

        profiles_kw[name] = np.asarray(grid, dtype=float).copy()
        return {
            "label": name,
            "energy_cost_eur": round(e_cost, 2),
            "mrk_cost_period_eur": round(mrk_cost, 2),
            "feed_in_revenue_eur": round(rev, 2),
            "total_operating_eur": round(total_op, 2),
            "annual_capex_charge_eur": round(ann_capex, 2),
            "total_with_capex_eur": round(total_op + ann_capex, 2),
            "equivalent_full_cycles": round(cycles, 2),
            "monthly_peak_detail": mrk_detail,
            "mrk_peak_reserve": peak_reserve,
        }

    baseline = scenario_case("baseline_no_storage", False, False)
    pv_only = scenario_case("pv_only", True, False) if use_pv else None
    bat_only = scenario_case("battery_only", False, True) if use_bat else None
    both = scenario_case("pv_and_battery", True, True) if (use_pv and use_bat) else None
    enable_trading = bool((an_cfg.get("enable_trading_only_scenario", True)))
    trading_only = scenario_case("battery_trading_only", False, True, trading_only=True) if (use_bat and enable_trading) else None
    days_in_sample = float(n) * dt_h / 24.0

    return {
        "baseline": baseline,
        "pv_only": pv_only,
        "battery_only": bat_only,
        "pv_and_battery": both,
        "battery_trading_only": trading_only,
        "econ_global": econ_global,
        "pv_capex": pv_capex,
        "battery_capex": bat_capex,
        "use_pv": use_pv,
        "use_bat": use_bat,
        "days_in_sample": days_in_sample,
        "dt_h": dt_h,
        "installed_kwp": installed_kwp,
        "energy_kwh": e_kwh,
        "years": years,
        "discount_rate": dr,
        "profiles_kw": profiles_kw,
        "annual_om_eur": annual_om,
        "degradation_frac": deg,
    }


def _auto_optimize_sizes(
    cfg: dict[str, Any],
    df: pd.DataFrame,
    bounds_override: dict[str, Any] | None = None,
    pv_profile_per_kwp: np.ndarray | None = None,
    battery_strategy_thresholds: dict[str, float] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prehľadáva (kWp, kWh) a vracia najlepšiu konfiguráciu + log.

    ``bounds_override`` accepts the technical limits already computed upstream by
    TechnicalLimitsPiece, so the search respects the same roof area, CAPEX cap and
    roof load limits that the rest of the workflow reports. Without it the bounds
    were silently recomputed here and the upstream piece had no effect.
    """
    base = copy.deepcopy(cfg)
    eq = base.get("equipment") or {}
    auto = eq.get("auto") or {}
    _apply_system_scope(base)
    scope = str((base.get("equipment") or {}).get("system_scope", "pv_and_battery")).lower()
    use_pv = bool(base.get("use_pv", True))
    use_bat = bool(base.get("use_battery", True))
    an_cfg = base.get("analysis") or {}
    years = int(an_cfg.get("amortization_years", 12))
    dr = float(an_cfg.get("discount_rate", 0.08))
    objective_default = "max_npv" if scope not in ("pv_only", "pv", "battery_only", "battery") else "shortest_payback"
    objective = str(auto.get("objective", objective_default)).lower()
    target_pb = auto.get("target_payback_years")
    max_cfgs = int(auto.get("max_configurations", 180))
    dt_h = infer_timestep_hours(df)
    bounds = technical_bounds_kwp_kwh(base, df, dt_h)
    if bounds_override:
        copy_capex_bound_fields(bounds, bounds_override)
        bounds["bounds_source"] = "TechnicalLimitsPiece"
        upstream_notes = bounds_override.get("notes")
        if isinstance(upstream_notes, list):
            bounds["notes"] = list(upstream_notes)
    else:
        bounds["bounds_source"] = "recomputed_locally"

    kwp_step = float(auto.get("kwp_step") or 100.0)
    kwh_step = float(auto.get("kwh_step") or 200.0)
    kwp_min = float(auto.get("kwp_min", 0.0))
    kwh_min = float(auto.get("kwh_min", 0.0))
    min_pv = float(auto.get("min_pv_kwp", 100.0))
    require_pv = bool(auto.get("require_pv", use_pv and scope not in ("battery_only", "battery")))
    require_battery = bool(auto.get("require_battery", use_bat and scope not in ("pv_only", "pv")))
    min_bat = float(auto.get("min_battery_kwh", max(100.0, kwh_step) if require_battery else 0.0))

    max_kwp = float(bounds.get("max_kwp_budget") or bounds.get("max_kwp") or 0.0)
    max_kwh = float(bounds.get("max_kwh_budget") or bounds.get("max_kwh") or 0.0)
    cons = eq.get("constraints") or {}
    max_pv_capex = bounds.get("max_pv_capex_eur")
    if max_pv_capex is None:
        max_pv_capex = cons.get("max_pv_capex_eur")
    max_battery_capex = bounds.get("max_battery_capex_eur")
    if max_battery_capex is None:
        max_battery_capex = cons.get("max_battery_capex_eur")
    max_total_capex = bounds.get("max_total_capex_eur")
    if max_total_capex is None:
        max_total_capex = cons.get("max_total_capex_eur")
    max_capex = float(
        bounds.get("max_capex_eur")
        or cons.get("max_capex_eur")
        or 0.0
    )
    eur_kwp = float((base.get("pv") or {}).get("specific_capex_eur_per_kwp", 800.0))
    eur_kwh = float((base.get("battery") or {}).get("specific_capex_eur_per_kwh", 400.0))

    def frange(a: float, b: float, step: float) -> list[float]:
        if step <= 0:
            return [round(a, 4)]
        if b < a - 1e-9:
            return [round(max(0.0, b), 4)]
        out = []
        x = a
        while x <= b + 1e-9:
            out.append(round(x, 4))
            x += step
        return out

    def _over_budget(kwp: float, kwh: float, total: float | None = None) -> bool:
        return configuration_over_budget(
            kwp=kwp,
            kwh=kwh,
            budget=bounds,
            total_capex_eur=total,
            eur_per_kwp=eur_kwp,
            eur_per_kwh=eur_kwh,
        )

    def _axis_pairs() -> list[tuple[float, float]]:
        if scope in ("pv_only", "pv"):
            kwp_vals = frange(max(kwp_min, min_pv), max_kwp, kwp_step)
            kwh_vals = [0.0]
        elif scope in ("battery_only", "battery"):
            kwp_vals = [0.0]
            kwh_vals = frange(max(kwh_min, min_bat), max_kwh, kwh_step)
        else:
            kwp_floor = max(kwp_min, min_pv if require_pv else 0.0)
            kwh_floor = max(kwh_min, min_bat if require_battery else 0.0)
            kwp_vals = frange(kwp_floor, max_kwp, kwp_step)
            kwh_vals = frange(kwh_floor, max_kwh, kwh_step)
        return [(k, w) for k in kwp_vals for w in kwh_vals if not _over_budget(k, w)]

    pairs = _axis_pairs()
    if len(pairs) > max_cfgs:
        raw_n = max(len(pairs), 1)
        factor = math.ceil(raw_n / max_cfgs)
        kwp_step *= factor
        kwh_step *= factor
        pairs = _axis_pairs()
    if not pairs:
        pairs = [(0.0, 0.0)]

    def _eval_pair(kwp: float, kwh: float) -> tuple[float, dict[str, Any], dict[str, Any]]:
        trial = copy.deepcopy(base)
        trial.setdefault("pv", {})["installed_kwp"] = float(kwp)
        trial.setdefault("battery", {})["energy_kwh"] = float(kwh)
        _sync_scope_flags_to_sizes(trial)
        bundle = _sim_bundle(
            trial,
            df,
            pv_profile_per_kwp=pv_profile_per_kwp,
            battery_strategy_thresholds=battery_strategy_thresholds,
        )
        score, fin = _score_financials(bundle, dr=dr, years=years, objective=objective)
        return score, fin, trial

    best_cfg: dict[str, Any] | None = None
    best_key: tuple[float, float, float] | None = None
    grid_rows: list[dict[str, Any]] = []
    pool: list[tuple[tuple[float, float, float], dict[str, Any]]] = []
    for kwp, kwh in pairs:
        score, fin, trial = _eval_pair(kwp, kwh)
        row = {
            "kwp": kwp,
            "kwh": kwh,
            "score": score,
            "annual_operating_savings_eur": fin.get("annual_operating_savings_eur"),
            "annual_net_cashflow_eur": fin.get("annual_net_cashflow_eur"),
            "simple_payback_years": fin.get("simple_payback_years"),
            "npv_eur": fin.get("npv_eur"),
            "total_capex_eur": fin.get("total_capex_eur"),
        }
        grid_rows.append(row)
        if not math.isfinite(score):
            continue
        if _over_budget(kwp, kwh, fin.get("total_capex_eur")):
            continue
        cand_key = (score, float(fin.get("total_capex_eur") or 1e18), -float(fin.get("npv_eur") or 0))
        pool.append((cand_key, trial))
        meets_target = True
        if target_pb is not None and fin.get("simple_payback_years") is not None:
            if float(fin["simple_payback_years"]) > float(target_pb) + 1e-6:
                meets_target = False
        if meets_target and (best_key is None or cand_key < best_key):
            best_key = cand_key
            best_cfg = trial

    if best_cfg is None and pool:
        best_key, best_cfg = min(pool, key=lambda x: x[0])
    if best_cfg is None:
        best_cfg = copy.deepcopy(base)
        best_cfg.setdefault("pv", {})["installed_kwp"] = 0.0
        best_cfg.setdefault("battery", {})["energy_kwh"] = 0.0
    _sync_scope_flags_to_sizes(best_cfg)

    log = {
        "bounds": bounds,
        "pv_profile_source": (
            "ai_forecast" if pv_profile_per_kwp is not None else "synthetic_sine_fallback"
        ),
        "objective": objective,
        "require_pv": require_pv,
        "require_battery": require_battery,
        "candidates_evaluated": len(pairs),
        "capex_mode": bounds.get("capex_mode"),
        "max_capex_eur": max_capex,
        "max_total_capex_eur": max_total_capex,
        "max_pv_capex_eur": max_pv_capex,
        "max_battery_capex_eur": max_battery_capex,
        "battery_strategy_thresholds": battery_strategy_thresholds,
        "target_payback_years": target_pb,
        "grid": grid_rows[: min(40, len(grid_rows))],
        "message": "Automatický výber: najlepší nález podľa cieľa (po filtri cieľovej návratnosti ak je zadaná).",
    }
    return best_cfg, log


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_aligned_frame(path: Path | str, df: pd.DataFrame, required_cols: list[str]) -> pd.DataFrame:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    raw = pd.read_csv(p)
    missing = [c for c in required_cols if c not in raw.columns]
    if missing:
        raise ValueError(f"{p.name} missing required columns: {missing}")
    if "datetime" in raw.columns:
        raw = raw.copy()
        raw["datetime"] = pd.to_datetime(raw["datetime"], errors="coerce")
        exp = pd.DataFrame({"datetime": pd.to_datetime(df["datetime"])})
        merged = exp.merge(raw, on="datetime", how="left", sort=False)
        if merged[required_cols].isna().any().any():
            raise ValueError(f"{p.name} does not align cleanly with load_csv timestamps")
        return merged
    if len(raw) != len(df):
        raise ValueError(f"{p.name} rows ({len(raw)}) must match load_csv rows ({len(df)})")
    raw = raw.copy()
    raw.insert(0, "datetime", pd.to_datetime(df["datetime"]))
    return raw


def _scenario_from_precomputed(
    *,
    name: str,
    ts: pd.Series,
    price: pd.Series,
    dt_h: float,
    grid_kw: np.ndarray,
    export_kw: np.ndarray,
    ann_capex: float,
    mrk_contract_kw: float,
    fee_eur_per_kw_month: float,
    excess_penalty_eur_per_kw: float,
    feed_in_eur_per_kwh: float,
    cycles: float = 0.0,
    trading_only: bool = False,
    distribution_eur_per_kwh: float = 0.0,
) -> dict[str, Any]:
    grid = pd.Series(np.asarray(grid_kw, dtype=float))
    export = pd.Series(np.asarray(export_kw, dtype=float))
    e_cost = energy_cost_eur(grid, price, dt_h, distribution_eur_per_kwh)
    rev = feed_in_revenue_eur(export, feed_in_eur_per_kwh, dt_h)
    if trading_only:
        mrk_cost, mrk_detail = 0.0, {}
    else:
        mrk_cost, mrk_detail = mrk_component_monthly(
            grid,
            ts,
            contract_kw=mrk_contract_kw,
            fee_eur_per_kw_month=fee_eur_per_kw_month,
            excess_penalty_eur_per_kw=excess_penalty_eur_per_kw,
        )
    total_op = e_cost + mrk_cost - rev
    return {
        "label": name,
        "energy_cost_eur": round(e_cost, 2),
        "mrk_cost_period_eur": round(mrk_cost, 2),
        "feed_in_revenue_eur": round(rev, 2),
        "total_operating_eur": round(total_op, 2),
        "annual_capex_charge_eur": round(ann_capex, 2),
        "total_with_capex_eur": round(total_op + ann_capex, 2),
        "equivalent_full_cycles": round(float(cycles), 2),
        "monthly_peak_detail": mrk_detail,
    }


def run_analysis(
    csv_path: Path | str,
    scenario_path: Path | str,
    *,
    output_dir: Path | str | None = None,
    virtual_solar_csv: str,
    battery_dispatch_csv: str,
    battery_summary_csv: str,
    battery_catalog_json: str = "",
    inverter_catalog_json: str = "",
) -> dict[str, Any]:
    csv_path = Path(csv_path)
    scenario_path = Path(scenario_path)
    if output_dir is None:
        raise ValueError("output_dir is required")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    cfg = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
    df = load_consumption_csv(csv_path)
    dt_h = infer_timestep_hours(df)
    price = build_price_series(df, cfg)
    validation = validate_input_contracts(cfg, df, price, dt_h)
    load = df["load_kw"].astype(float).values
    n = len(df)

    _apply_system_scope(cfg)
    eq = cfg.get("equipment") or {}
    selection_mode = str(eq.get("selection_mode", "manual")).lower()
    auto_log: dict[str, Any] | None = None

    pv_cfg = cfg.get("pv") or {}
    bat_cfg = cfg.get("battery") or {}
    mrk_cfg = cfg.get("mrk") or {}
    an_cfg = cfg.get("analysis") or {}
    en_cfg = cfg.get("energy") or {}

    _sync_scope_flags_to_sizes(cfg)
    use_pv = bool(cfg.get("use_pv", True))
    use_bat = bool(cfg.get("use_battery", True))
    installed_kwp = float(pv_cfg.get("installed_kwp", 0.0))
    e_kwh = float(bat_cfg.get("energy_kwh", 0.0))
    eta_c = float(bat_cfg.get("charge_efficiency", 0.95))
    years = int(an_cfg.get("amortization_years", 12))
    dr = float(an_cfg.get("discount_rate", 0.08))
    feed_in = float(en_cfg.get("feed_in_surplus_eur_per_kwh", 0.05))
    distribution = distribution_tariff_eur_per_kwh(cfg)
    mrk_kw = float(mrk_cfg.get("contract_kw", 0.0))
    fee_m = float(mrk_cfg.get("fee_eur_per_kw_month", 0.0))
    pen = float(mrk_cfg.get("excess_peak_penalty_eur_per_kw", 0.0))

    pv_capex = installed_kwp * float(pv_cfg.get("specific_capex_eur_per_kwp", 800.0)) if use_pv else 0.0
    bat_capex = e_kwh * float(bat_cfg.get("specific_capex_eur_per_kwh", 400.0)) if use_bat else 0.0

    solar_frame = _load_aligned_frame(virtual_solar_csv, df, ["pv_kw"])
    pv_kw = pd.to_numeric(solar_frame["pv_kw"], errors="coerce").fillna(0.0).clip(lower=0.0).to_numpy()
    # virtual_solar.csv is generated before sizing, at the form reference kWp.
    # Scale the per-kWp shape to the resolved array so pv_only matches the heatmap.
    pv_per = load_pv_profile_per_kwp(virtual_solar_csv, df)
    if pv_per is not None and installed_kwp > 0:
        pv_kw = np.asarray(pv_per, dtype=float) * installed_kwp
    if pv_per is not None:
        yield_kwp = annual_kwh_per_kwp_from_profile(pv_per, dt_h)
    elif installed_kwp > 0:
        yield_kwp = annual_kwh_per_kwp_from_profile(pv_kw / installed_kwp, dt_h)
    else:
        yield_kwp = 0.0
    econ_global = compute_levelized_economics(
        pv_cfg,
        bat_cfg,
        an_cfg,
        en_cfg,
        installed_kwp=installed_kwp,
        yield_kwp=yield_kwp,
        energy_kwh=e_kwh,
        pv_capex=pv_capex,
        bat_capex=bat_capex,
        eta_c=eta_c,
        eta_d=float(bat_cfg.get("discharge_efficiency", 0.95)),
        years=years,
        dr=dr,
        use_pv=use_pv,
        use_bat=use_bat,
    )

    dispatch_required = [
        "battery_only_grid_kw",
        "battery_only_export_kw",
        "battery_only_soc_pct",
        "pv_battery_grid_kw",
        "pv_battery_export_kw",
        "pv_battery_soc_pct",
    ]
    dispatch_frame = _load_aligned_frame(battery_dispatch_csv, df, dispatch_required)
    summary_frame = pd.read_csv(Path(battery_summary_csv))
    summary_row = (summary_frame.to_dict(orient="records") or [{}])[0]

    baseline_grid = load.astype(float)
    pv_only_grid = np.maximum(load - pv_kw, 0.0)
    pv_only_export = np.maximum(pv_kw - load, 0.0)

    battery_only_grid = pd.to_numeric(dispatch_frame["battery_only_grid_kw"], errors="coerce").fillna(0.0).to_numpy()
    battery_only_export = pd.to_numeric(dispatch_frame["battery_only_export_kw"], errors="coerce").fillna(0.0).to_numpy()
    battery_only_soc = pd.to_numeric(dispatch_frame["battery_only_soc_pct"], errors="coerce").fillna(0.0).to_numpy()
    pv_battery_grid = pd.to_numeric(dispatch_frame["pv_battery_grid_kw"], errors="coerce").fillna(0.0).to_numpy()
    pv_battery_export = pd.to_numeric(dispatch_frame["pv_battery_export_kw"], errors="coerce").fillna(0.0).to_numpy()
    pv_battery_soc = pd.to_numeric(dispatch_frame["pv_battery_soc_pct"], errors="coerce").fillna(0.0).to_numpy()
    trading_grid = pd.to_numeric(dispatch_frame.get("trading_only_grid_kw", 0.0), errors="coerce").fillna(0.0).to_numpy()
    trading_export = pd.to_numeric(dispatch_frame.get("trading_only_export_kw", 0.0), errors="coerce").fillna(0.0).to_numpy()

    baseline = _scenario_from_precomputed(
        name="baseline_no_storage",
        ts=df["datetime"],
        price=price,
        dt_h=dt_h,
        grid_kw=baseline_grid,
        export_kw=np.zeros(n),
        ann_capex=0.0,
        mrk_contract_kw=mrk_kw,
        fee_eur_per_kw_month=fee_m,
        excess_penalty_eur_per_kw=pen,
        feed_in_eur_per_kwh=feed_in,
        distribution_eur_per_kwh=distribution,
        cycles=0.0,
    )
    pv_only = (
        _scenario_from_precomputed(
            name="pv_only",
            ts=df["datetime"],
            price=price,
            dt_h=dt_h,
            grid_kw=pv_only_grid,
            export_kw=pv_only_export,
            ann_capex=annual_capex_charge_eur(pv_capex, 0.0, years, dr),
            mrk_contract_kw=mrk_kw,
            fee_eur_per_kw_month=fee_m,
            excess_penalty_eur_per_kw=pen,
            feed_in_eur_per_kwh=feed_in,
            distribution_eur_per_kwh=distribution,
            cycles=0.0,
        )
        if use_pv
        else None
    )
    battery_only = (
        _scenario_from_precomputed(
            name="battery_only",
            ts=df["datetime"],
            price=price,
            dt_h=dt_h,
            grid_kw=battery_only_grid,
            export_kw=battery_only_export,
            ann_capex=annual_capex_charge_eur(0.0, bat_capex, years, dr),
            mrk_contract_kw=mrk_kw,
            fee_eur_per_kw_month=fee_m,
            excess_penalty_eur_per_kw=pen,
            feed_in_eur_per_kwh=feed_in,
            distribution_eur_per_kwh=distribution,
            cycles=equivalent_full_cycles(pd.Series(battery_only_soc)),
        )
        if use_bat
        else None
    )
    pv_and_battery = (
        _scenario_from_precomputed(
            name="pv_and_battery",
            ts=df["datetime"],
            price=price,
            dt_h=dt_h,
            grid_kw=pv_battery_grid,
            export_kw=pv_battery_export,
            ann_capex=annual_capex_charge_eur(pv_capex, bat_capex, years, dr),
            mrk_contract_kw=mrk_kw,
            fee_eur_per_kw_month=fee_m,
            excess_penalty_eur_per_kw=pen,
            feed_in_eur_per_kwh=feed_in,
            distribution_eur_per_kwh=distribution,
            cycles=equivalent_full_cycles(pd.Series(pv_battery_soc)),
        )
        if (use_pv and use_bat)
        else None
    )
    enable_trading = bool(an_cfg.get("enable_trading_only_scenario", True)) and use_bat
    trading_cycles = float(summary_row.get("trading_only_cycles_equivalent", 0.0) or 0.0)
    trading_only = (
        _scenario_from_precomputed(
            name="battery_trading_only",
            ts=df["datetime"],
            price=price,
            dt_h=dt_h,
            grid_kw=trading_grid,
            export_kw=trading_export,
            ann_capex=annual_capex_charge_eur(0.0, bat_capex, years, dr),
            mrk_contract_kw=mrk_kw,
            fee_eur_per_kw_month=fee_m,
            excess_penalty_eur_per_kw=pen,
            feed_in_eur_per_kwh=feed_in,
            distribution_eur_per_kwh=distribution,
            cycles=trading_cycles,
            trading_only=True,
        )
        if enable_trading
        else None
    )

    days_in_sample = float(n) * dt_h / 24.0
    pvals = price.astype(float).to_numpy()
    p_low = summary_row.get("strategy_charge_below_eur_per_kwh")
    p_high = summary_row.get("strategy_discharge_above_eur_per_kwh")
    try:
        p_low = float(p_low) if p_low is not None else float(np.quantile(pvals, 0.30))
    except (TypeError, ValueError):
        p_low = float(np.quantile(pvals, 0.30)) if len(pvals) else 0.0
    try:
        p_high = float(p_high) if p_high is not None else float(np.quantile(pvals, 0.75))
    except (TypeError, ValueError):
        p_high = float(np.quantile(pvals, 0.75)) if len(pvals) else 0.0
    net_for_reserve = (load - pv_kw) if use_pv else load
    mrk_peak_reserve = decide_mrk_peak_reserve(
        net_for_reserve,
        dt_h,
        energy_kwh=e_kwh,
        max_c_rate=float(bat_cfg.get("max_c_rate", 0.5)),
        eta_c=eta_c,
        eta_d=float(bat_cfg.get("discharge_efficiency", 0.95)),
        mrk_contract_kw=mrk_kw,
        peak_shaving_reserve_pct=float(bat_cfg.get("peak_shaving_reserve_pct", 30.0)),
        excess_penalty_eur_per_kw=pen,
        fee_eur_per_kw_month=fee_m,
        wear=battery_dispatch_wear_eur_per_kwh(bat_cfg),
        price_low=p_low,
        price_high=p_high,
        timestamps=df["datetime"],
    )
    if pv_and_battery is not None:
        pv_and_battery["mrk_peak_reserve"] = mrk_peak_reserve
    elif battery_only is not None:
        battery_only["mrk_peak_reserve"] = mrk_peak_reserve
    profiles_kw = {
        "baseline_no_storage": baseline_grid,
        "pv_only": pv_only_grid,
        "battery_only": battery_only_grid,
        "pv_and_battery": pv_battery_grid,
    }
    if trading_only is not None:
        profiles_kw["battery_trading_only"] = trading_grid

    bundle = {
        "baseline": baseline,
        "pv_only": pv_only,
        "battery_only": battery_only,
        "pv_and_battery": pv_and_battery,
        "battery_trading_only": trading_only,
        "econ_global": econ_global,
        "pv_capex": pv_capex,
        "battery_capex": bat_capex,
        "use_pv": use_pv,
        "use_bat": use_bat,
        "days_in_sample": days_in_sample,
        "years": years,
        "discount_rate": dr,
        "profiles_kw": profiles_kw,
        "installed_kwp": installed_kwp,
        "energy_kwh": e_kwh,
        "annual_om_eur": plant_om_eur_per_year(cfg, installed_kwp if use_pv else 0.0, e_kwh if use_bat else 0.0),
        "degradation_frac": plant_degradation_frac(cfg),
    }

    optimized = primary_optimized_scenario(bundle)
    price_quality = analyze_price_input_quality(df, price, load)
    safety_rv = float(mrk_cfg.get("rv_downsizing_safety_margin_pct", 8.0))
    mrk_rv_block: dict[str, Any] | None = None
    if optimized is not None:
        mrk_rv_block = mrk_peak_reduction_and_rv_opportunity(
            baseline["monthly_peak_detail"],
            optimized["monthly_peak_detail"],
            contract_kw=mrk_kw,
            fee_eur_per_kw_month=fee_m,
            safety_margin_pct=safety_rv,
        )
    executive_summary = build_executive_summary(
        baseline,
        optimized,
        days_in_sample=days_in_sample,
        price_quality=price_quality,
        mrk_rv=mrk_rv_block or {},
        econ=econ_global,
    )
    uncertainty = build_uncertainty_assessment(bundle, optimized=optimized)
    battery_soh = None
    if optimized is not None and bundle.get("use_bat"):
        battery_soh = build_battery_soh_assessment(
            equivalent_cycles_period=float(optimized.get("equivalent_full_cycles", 0.0)),
            days_in_sample=days_in_sample,
            battery_cfg=cfg.get("battery") or {},
        )
    c_rate_sweep = []
    if bool((cfg.get("analysis") or {}).get("enable_c_rate_sweep", True)):
        c_rates = (cfg.get("analysis") or {}).get("c_rate_sweep_values", [0.25, 0.5, 1.0])
        c_rate_vals = [float(x) for x in c_rates if float(x) > 0]
        if c_rate_vals:
            c_rate_sweep = run_c_rate_sweep(cfg, df, c_rate_vals)

    optimized_label = str((optimized or {}).get("label") or "")
    prof = bundle.get("profiles_kw") or {}
    base_profile = prof.get("baseline_no_storage")
    opt_profile = prof.get(optimized_label)
    profile_csv_path = out / "baseline_vs_optimized_profile.csv"
    if base_profile is not None and opt_profile is not None:
        pd.DataFrame(
            {
                "datetime": pd.to_datetime(df["datetime"]),
                "baseline_grid_kw": np.asarray(base_profile, dtype=float),
                "optimized_grid_kw": np.asarray(opt_profile, dtype=float),
                "baseline_energy_kwh_interval": np.asarray(base_profile, dtype=float) * dt_h,
                "optimized_energy_kwh_interval": np.asarray(opt_profile, dtype=float) * dt_h,
            }
        ).to_csv(profile_csv_path, index=False)

    eq_obj = str((eq.get("auto") or {}).get("objective", "shortest_payback")).lower()
    _, fin_metrics = _score_financials(bundle, dr=dr, years=years, objective=eq_obj)

    pv_mods, bt_prods, inv_items = _load_hardware_catalogs(
        battery_catalog_path=battery_catalog_json,
        inverter_catalog_path=inverter_catalog_json,
    )
    hardware = build_hardware_recommendation(
        cfg,
        installed_kwp=float(bundle["installed_kwp"]),
        energy_kwh=float(bundle["energy_kwh"]),
        pv_modules=pv_mods,
        battery_products=bt_prods,
        inverter_items=inv_items,
    )

    equipment_block: dict[str, Any] = {
        "selection_mode": selection_mode,
        "system_scope": eq.get("system_scope"),
        "resolved": {
            "use_pv": bundle["use_pv"],
            "use_battery": bundle["use_bat"],
            "installed_kwp": round(float(bundle["installed_kwp"]), 3),
            "energy_kwh": round(float(bundle["energy_kwh"]), 3),
        },
        "investment_metrics": {
            **{k: v for k, v in fin_metrics.items() if k != "reason"},
            "analysis_horizon_years": years,
            "discount_rate": dr,
        },
        "hardware_recommendation": hardware,
        "auto_optimization": auto_log,
        "catalog_paths": {
            "pv_modules": str(_project_root() / "catalog" / "pv_modules_catalog.json"),
            "battery_products": str(_project_root() / "catalog" / "battery_catalog.json"),
            "battery_products_online": battery_catalog_json or "",
            "inverters_online": inverter_catalog_json or "",
        },
        "upstream_sources": {
            "virtual_solar_csv": str(virtual_solar_csv),
            "battery_dispatch_csv": str(battery_dispatch_csv),
            "battery_summary_csv": str(battery_summary_csv),
        },
    }

    def savings(alt: dict | None, base: dict) -> dict | None:
        if alt is None:
            return None
        return {
            "operating_savings_eur_vs_baseline": round(base["total_operating_eur"] - alt["total_operating_eur"], 2),
            "net_after_capex_savings_eur_vs_baseline": round(
                base["total_with_capex_eur"] - alt["total_with_capex_eur"]
                if alt.get("total_with_capex_eur")
                else base["total_operating_eur"] - alt["total_operating_eur"],
                2,
            ),
        }

    result: dict[str, Any] = {
        "meta": {
            "schema_version": "mrk_report_v2",
            "run_id": str(uuid.uuid4()),
            "csv": str(csv_path),
            "scenario": str(scenario_path),
            "intervals": n,
            "dt_hours": round(dt_h, 6),
            "input_fingerprint_sha256": hashlib.sha256(
                (
                    str(csv_path)
                    + str(scenario_path)
                    + str(n)
                    + str(round(dt_h, 6))
                    + str(virtual_solar_csv)
                    + str(battery_dispatch_csv)
                ).encode("utf-8")
            ).hexdigest(),
        },
        "economics": {
            "pv_lcoe_eur_per_kwh": round(float(econ_global["pv_lcoe_eur_per_kwh"]), 6),
            "pv_annual_kwh_est": round(float(econ_global["pv_annual_kwh_est"]), 1),
            "battery_marginal_eur_per_kwh_throughput": round(
                float(econ_global["battery_marginal_eur_per_kwh_throughput"]), 6
            ),
            "battery_eur_per_kwh_at_grid_effective": round(
                float(econ_global["battery_eur_per_kwh_at_grid_effective"]), 6
            ),
            "opportunity_pv_to_battery_eur_per_kwh_stored": round(
                float(econ_global["opportunity_pv_to_battery_eur_per_kwh_stored"]), 6
            ),
            "round_trip_efficiency": round(float(econ_global["round_trip_efficiency"]), 4),
            "distribution_eur_per_kwh": round(distribution, 6),
            "feed_in_surplus_eur_per_kwh": round(feed_in, 6),
            "dispatch_note": "Battery dispatch is consumed from upstream BatterySimPiece; SimulatePiece aggregates only.",
        },
        "scenarios": {
            "baseline": baseline,
            "pv_only": pv_only,
            "battery_only": battery_only,
            "pv_and_battery": pv_and_battery,
            "battery_trading_only": trading_only,
            "optimized": optimized,
        },
        "savings_vs_baseline": {
            "pv_only": savings(pv_only, baseline),
            "battery_only": savings(battery_only, baseline),
            "pv_and_battery": savings(pv_and_battery, baseline),
            "optimized": savings(optimized, baseline),
        },
        "capex_inputs": {
            "pv_capex_eur": round(pv_capex, 2),
            "battery_capex_eur": round(bat_capex, 2),
            "amortization_years": years,
            "specific_capex_eur_per_kwp": round(float(pv_cfg.get("specific_capex_eur_per_kwp", 800.0)), 2),
            "specific_capex_eur_per_kwh": round(float(bat_cfg.get("specific_capex_eur_per_kwh", 400.0)), 2),
        },
        "executive_summary": executive_summary,
        "uncertainty_assessment": uncertainty,
        "data_contracts": {
            "validation": validation,
            "compatible_consumer_schema_min": "mrk_report_v1",
            "report_schema": "mrk_report_v2",
        },
        "mrk_and_rv": mrk_rv_block,
        "mrk_peak_reserve": (optimized or {}).get("mrk_peak_reserve")
        or (pv_and_battery or {}).get("mrk_peak_reserve")
        or {},
        "input_quality": price_quality,
        "equipment": equipment_block,
        "battery_lifetime_assessment": battery_soh,
        "c_rate_sweep": c_rate_sweep,
        "artifacts": {
            "baseline_vs_optimized_profile_csv": str(profile_csv_path),
            "optimized_scenario_label": optimized_label,
            "virtual_solar_csv": str(virtual_solar_csv),
            "battery_dispatch_csv": str(battery_dispatch_csv),
            "battery_summary_csv": str(battery_summary_csv),
        },
    }
    result["battery_strategy"] = {
        "source_json": str(summary_row.get("strategy_source_json", "") or ""),
        "thresholds_eur_per_kwh": {
            "charge_below": summary_row.get("strategy_charge_below_eur_per_kwh"),
            "discharge_above": summary_row.get("strategy_discharge_above_eur_per_kwh"),
            "expensive_hour": summary_row.get("strategy_expensive_hour_threshold_eur_per_kwh"),
        },
        "applied_in_dispatch": bool(summary_row.get("strategy_source_json")) or bool(
            summary_row.get("strategy_charge_below_eur_per_kwh") is not None
        ),
        "dispatch_owner_piece": "BatterySimPiece",
    }
    if trading_only is not None:
        base_trade = {
            "label": "battery_trading_idle",
            "total_operating_eur": 0.0,
            "equivalent_full_cycles": 0.0,
        }
        days_trade = max(days_in_sample, 1e-9)
        trade_annual = (0.0 - float(trading_only.get("total_operating_eur", 0.0))) * (365.0 / days_trade)
        result["trading_only_analysis"] = {
            "scenario": trading_only,
            "annual_margin_eur_estimate": round(float(trade_annual), 2),
            "note": "Trading-only battery scenario ignores site load and MRK; it reflects pure buy-low/sell-high arbitrage potential.",
            "relative_to_idle_baseline": base_trade,
        }

    if optimized is not None:
        annual_savings = (float(baseline["total_operating_eur"]) - float(optimized["total_operating_eur"])) * (
            365.0 / max(days_in_sample, 1e-9)
        )
        total_capex = float(pv_capex + bat_capex)
        om_from_plant = float(pv_cfg.get("om_eur_per_kwp_year", 0.0) or 0.0) * float(installed_kwp)
        om_from_plant += float(bat_cfg.get("om_eur_per_kwh_year", 0.0) or 0.0) * float(e_kwh)
        finance_cfg = dict(cfg.get("finance") or {})
        if not finance_cfg.get("o_and_m_eur_per_year"):
            finance_cfg["o_and_m_eur_per_year"] = om_from_plant
        result["finance_layer"] = apply_finance_layer(
            annual_operating_savings_eur=annual_savings,
            total_capex_eur=total_capex,
            analysis_years=years,
            discount_rate=dr,
            finance_cfg=finance_cfg,
        )
        result["finance_layer"]["assumptions"] = {
            "enabled": bool(finance_cfg.get("enabled", False)),
            "o_and_m_eur_per_year": float(finance_cfg.get("o_and_m_eur_per_year", 0.0)),
            "o_and_m_pct_of_capex": float(finance_cfg.get("o_and_m_pct_of_capex", 0.0)),
            "debt_ratio_of_capex": float(finance_cfg.get("debt_ratio_of_capex", 0.0)),
            "debt_interest_rate": float(finance_cfg.get("debt_interest_rate", 0.0)),
            "debt_years": int(finance_cfg.get("debt_years", max(1, years))),
            "tax_rate_pct": float(finance_cfg.get("tax_rate_pct", 0.0)),
            "ancillary_revenue_eur_per_year": float(finance_cfg.get("ancillary_revenue_eur_per_year", 0.0)),
        }

    _write_report(out / "mrk_savings_report.json", result)
    return result


class SimulatePiece(BasePiece):
    """Run MRK+PV+battery simulation and write mrk_savings_report.json."""

    def piece_function(self, input_data: InputModel, secrets_data=None) -> OutputModel:
        _stage = None
        _piece_out = None
        _run_id = None
        if od is not None:
            input_data, _stage = od.stage_inputs(input_data, secrets_data)
            _run_id = od.resolve_run_id(input_data, secrets_data, generate=False)
        csv_path = Path(input_data.load_csv)
        scenario_path = Path(input_data.scenario_yaml)
        if self.results_path:
            out_dir = Path(self.results_path)
        else:
            out_dir = Path("/tmp/simulate_piece_results")
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "simulate.log"
        (out_dir / "simulate_started.txt").write_text(
            f"load_csv={csv_path}\nscenario_yaml={scenario_path}\nresults_path={self.results_path}\n",
            encoding="utf-8",
        )

        def _log(msg: str) -> None:
            text = f"[SimulatePiece] {msg}"
            print(text, flush=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")

        _log(f"Input load_csv={csv_path}")
        _log(f"Input scenario_yaml={scenario_path}")
        _log(f"Output dir={out_dir}")
        _log(f"Input virtual_solar_csv={input_data.virtual_solar_csv}")
        _log(f"Input battery_dispatch_csv={input_data.battery_dispatch_csv}")
        _log(f"Input battery_summary_csv={input_data.battery_summary_csv}")
        if not csv_path.is_file():
            raise FileNotFoundError(f"Load CSV not found: {csv_path}")
        if not scenario_path.is_file():
            raise FileNotFoundError(f"Scenario YAML not found: {scenario_path}")

        try:
            run_analysis(
                csv_path,
                scenario_path,
                output_dir=out_dir,
                virtual_solar_csv=input_data.virtual_solar_csv,
                battery_dispatch_csv=input_data.battery_dispatch_csv,
                battery_summary_csv=input_data.battery_summary_csv,
                battery_catalog_json=input_data.battery_catalog_json,
                inverter_catalog_json=input_data.inverter_catalog_json,
            )
        except Exception as exc:
            (out_dir / "simulate_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            _log(f"ERROR during run_analysis: {exc}")
            if od is not None:
                od.cleanup_on_error(self.results_path, secrets_data, "SimulatePiece", _stage, run_id=_run_id)
            raise
        report_path = out_dir / "mrk_savings_report.json"
        if not report_path.is_file():
            raise RuntimeError(f"Report was not written: {report_path}")

        ranked = Path(input_data.ranked_catalog_json) if input_data.ranked_catalog_json else None
        if ranked and ranked.is_file():
            rep = json.loads(report_path.read_text(encoding="utf-8"))
            eq = rep.setdefault("equipment", {})
            hw = eq.setdefault("hardware_recommendation", {})
            rank_payload = json.loads(ranked.read_text(encoding="utf-8"))
            top = (rank_payload.get("top_recommendations") or [])
            if top:
                hw["pv_online_ranking_top10"] = top
                hw["pv_online_selected_rank1"] = top[0]
            eq["catalog_ranker_source"] = str(ranked)
            report_path.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")

        inverter_catalog = Path(input_data.inverter_catalog_json) if input_data.inverter_catalog_json else None
        if inverter_catalog and inverter_catalog.is_file():
            rep = json.loads(report_path.read_text(encoding="utf-8"))
            eq = rep.setdefault("equipment", {})
            hw = eq.setdefault("hardware_recommendation", {})
            inv_items = (json.loads(inverter_catalog.read_text(encoding="utf-8")) or {}).get("items") or []
            installed_kwp = float((((eq.get("resolved") or {}).get("installed_kwp")) or 0.0))
            inv_rec = recommend_inverters_for_pv(installed_kwp, inv_items)
            if inv_rec:
                hw["inverter"] = inv_rec
            eq["inverter_catalog_source"] = str(inverter_catalog)
            report_path.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")

        manifest = Path(input_data.catalog_manifest_json) if input_data.catalog_manifest_json else None
        if manifest and manifest.is_file():
            rep = json.loads(report_path.read_text(encoding="utf-8"))
            eq = rep.setdefault("equipment", {})
            sync = json.loads(manifest.read_text(encoding="utf-8"))
            eq["catalog_sync_status"] = {
                "source_mode": sync.get("source_mode"),
                "url_outage_detected": bool(sync.get("url_outage_detected", False)),
                "warnings": sync.get("warnings") or [],
                "manifest_path": str(manifest),
            }
            report_path.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")

        rep = json.loads(report_path.read_text(encoding="utf-8"))
        base = rep["scenarios"]["baseline"]["total_operating_eur"]
        both = rep["scenarios"].get("optimized") or rep["scenarios"].get("pv_and_battery") or {}
        summary = pd.DataFrame(
            [
                {
                    "baseline_operating_eur": float(base),
                    "pv_battery_operating_eur": float(both.get("total_operating_eur", 0.0)),
                }
            ]
        )
        scenarios = rep.get("scenarios") or {}
        sim_rows: list[dict[str, Any]] = []
        for key in ("baseline", "pv_only", "battery_only", "pv_and_battery", "optimized"):
            sc = scenarios.get(key)
            if not isinstance(sc, dict):
                continue
            sim_rows.append(
                {
                    "scenario": key,
                    "label": sc.get("label", key),
                    "energy_cost_eur": sc.get("energy_cost_eur", 0.0),
                    "mrk_cost_period_eur": sc.get("mrk_cost_period_eur", 0.0),
                    "feed_in_revenue_eur": sc.get("feed_in_revenue_eur", 0.0),
                    "total_operating_eur": sc.get("total_operating_eur", 0.0),
                    "total_with_capex_eur": sc.get("total_with_capex_eur", 0.0),
                }
            )
        simulated = pd.DataFrame(sim_rows)
        summary.to_csv(out_dir / "summary.csv", index=False)
        simulated.to_csv(out_dir / "simulated_results.csv", index=False)

        report_path = out_dir / "mrk_savings_report.json"
        _log(f"Wrote outputs: {report_path}, {out_dir / 'summary.csv'}, {out_dir / 'simulated_results.csv'}")
        _piece_out = OutputModel(message="Simulation finished", report_json=str(report_path))
        if od is not None and _piece_out is not None:
            return od.finish_piece(
                _piece_out, self.results_path, secrets_data, "SimulatePiece", _stage, run_id=_run_id
            )
        if _stage is not None:
            _stage.cleanup()
        return _piece_out
