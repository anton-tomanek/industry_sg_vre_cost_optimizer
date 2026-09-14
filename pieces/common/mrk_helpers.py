"""Lightweight MRK CSV/YAML helpers (no SimulatePiece / xgboost import)."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd


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


def build_price_series(df: pd.DataFrame, cfg: dict) -> pd.Series:
    if "price_eur_per_kwh" in df.columns:
        s = pd.to_numeric(df["price_eur_per_kwh"], errors="coerce")
        if s.notna().any():
            med = float(s.median())
            return s.fillna(med).astype(float)

    tariff = (cfg.get("tariff") or {}) if isinstance(cfg, dict) else {}
    mrk = (cfg.get("mrk") or {}) if isinstance(cfg, dict) else {}
    default_price = float(
        tariff.get("default_price_eur_per_kwh")
        or mrk.get("default_price_eur_per_kwh")
        or 0.10
    )
    return pd.Series(default_price, index=df.index, dtype=float)


def synthetic_pv_kw(dt: pd.Series, installed_kwp: float) -> pd.Series:
    if installed_kwp <= 0:
        return pd.Series(0.0, index=dt.index, name="pv_kw")

    t = pd.DatetimeIndex(pd.to_datetime(dt))
    hours = (t.hour + t.minute / 60.0).astype(float)
    day_of_year = t.dayofyear.values.astype(float)
    seasonal = 0.85 + 0.15 * np.cos(2 * math.pi * (day_of_year - 172) / 365.0)
    solar_elev = np.clip(np.sin((hours - 6.0) / 12.0 * np.pi), 0.0, 1.0) ** 1.2
    raw = np.asarray(seasonal * solar_elev * installed_kwp, dtype=float)
    return pd.Series(np.clip(raw, 0.0, installed_kwp * 1.15), index=dt.index, name="pv_kw")
