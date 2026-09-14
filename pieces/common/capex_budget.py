"""CAPEX ceilings for the recommended-size search (heatmap axes stay technical).

Two modes:

- **joint** (default): one pool ``max_total_capex_eur``. The optimiser may spend
  it on PV, battery, or any mix that fits ``pv + battery ≤ total``. Unused money
  on one asset can go to the other.
- **split**: independent ``max_pv_capex_eur`` / ``max_battery_capex_eur``. Unused
  battery budget never rolls into PV.

Empty = no euro cap on that pool (roof / site area still apply).
0 = do not spend on that pool.
Legacy YAML ``max_capex_eur`` without split fields is a joint total, not two
independent envelopes (that used to double the budget).
"""

from __future__ import annotations

from typing import Any


def optional_capex(constraints: dict[str, Any], key: str) -> float | None:
    if key not in constraints:
        return None
    val = constraints.get(key)
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def estimate_pair_capex(
    kwp: float,
    kwh: float,
    eur_per_kwp: float,
    eur_per_kwh: float,
) -> float:
    return max(0.0, float(kwp)) * max(0.0, float(eur_per_kwp)) + max(0.0, float(kwh)) * max(
        0.0, float(eur_per_kwh)
    )


def resolve_capex_mode(constraints: dict[str, Any]) -> str:
    raw = str(constraints.get("capex_mode") or "").strip().lower()
    if raw in {"joint", "total", "shared", "pool"}:
        return "joint"
    if raw in {"split", "separate", "axis", "independent"}:
        return "split"
    pv_cap = optional_capex(constraints, "max_pv_capex_eur")
    bat_cap = optional_capex(constraints, "max_battery_capex_eur")
    if pv_cap is not None or bat_cap is not None:
        return "split"
    return "joint"


def apply_capex_axis_budgets(
    constraints: dict[str, Any],
    *,
    max_kwp: float,
    max_kwh: float,
    eur_per_kwp: float,
    eur_per_kwh: float,
) -> dict[str, Any]:
    """Return recommended-size budgets without changing technical heatmap axes."""
    mode = resolve_capex_mode(constraints)
    pv_cap = optional_capex(constraints, "max_pv_capex_eur")
    bat_cap = optional_capex(constraints, "max_battery_capex_eur")
    total = optional_capex(constraints, "max_total_capex_eur")
    legacy = optional_capex(constraints, "max_capex_eur")
    used_legacy = False

    budget_kwp = float(max_kwp)
    budget_kwh = float(max_kwh)

    if mode == "joint":
        if total is None and legacy is not None:
            total = legacy
            used_legacy = True
        pv_cap = None
        bat_cap = None
        if total is not None:
            if total <= 1e-9:
                budget_kwp = 0.0
                budget_kwh = 0.0
            else:
                budget_kwp = min(budget_kwp, total / max(float(eur_per_kwp), 1e-9))
                budget_kwh = min(budget_kwh, total / max(float(eur_per_kwh), 1e-9))
        return {
            "capex_mode": "joint",
            "max_kwp_budget": max(0.0, budget_kwp),
            "max_kwh_budget": max(0.0, budget_kwh),
            "max_total_capex_eur": total,
            "max_pv_capex_eur": None,
            "max_battery_capex_eur": None,
            "max_capex_eur": total,
            "used_legacy_joint": used_legacy,
        }

    if pv_cap is not None:
        if pv_cap <= 1e-9:
            budget_kwp = 0.0
        else:
            budget_kwp = min(budget_kwp, pv_cap / max(float(eur_per_kwp), 1e-9))
    if bat_cap is not None:
        if bat_cap <= 1e-9:
            budget_kwh = 0.0
        else:
            budget_kwh = min(budget_kwh, bat_cap / max(float(eur_per_kwh), 1e-9))

    return {
        "capex_mode": "split",
        "max_kwp_budget": max(0.0, budget_kwp),
        "max_kwh_budget": max(0.0, budget_kwh),
        "max_total_capex_eur": None,
        "max_pv_capex_eur": pv_cap,
        "max_battery_capex_eur": bat_cap,
        "max_capex_eur": None,
        "used_legacy_joint": False,
    }


def has_capex_limit(budget: dict[str, Any] | None) -> bool:
    if not budget:
        return False
    if str(budget.get("capex_mode") or "joint") == "split":
        return budget.get("max_pv_capex_eur") is not None or budget.get("max_battery_capex_eur") is not None
    if budget.get("max_total_capex_eur") is not None:
        return True
    alias = budget.get("max_capex_eur")
    return alias is not None and float(alias) > 1e-9


def configuration_over_budget(
    *,
    kwp: float,
    kwh: float,
    budget: dict[str, Any],
    total_capex_eur: float | None = None,
    eur_per_kwp: float = 0.0,
    eur_per_kwh: float = 0.0,
    slack: float = 1.0,
) -> bool:
    """True when this size must not be the recommended cell."""
    estimated = estimate_pair_capex(kwp, kwh, eur_per_kwp, eur_per_kwh)
    capex = estimated if total_capex_eur is None else float(total_capex_eur)
    mode = str(budget.get("capex_mode") or resolve_capex_mode(budget))
    if mode == "joint":
        total = budget.get("max_total_capex_eur")
        if total is None:
            alias = budget.get("max_capex_eur")
            if alias is None or float(alias) <= 1e-9:
                return False
            total = alias
        return capex > float(total) + slack

    pv_cap = budget.get("max_pv_capex_eur")
    bat_cap = budget.get("max_battery_capex_eur")
    pv_cost = max(0.0, float(kwp)) * max(0.0, float(eur_per_kwp))
    bat_cost = max(0.0, float(kwh)) * max(0.0, float(eur_per_kwh))
    if pv_cap is not None and pv_cost > float(pv_cap) + slack:
        return True
    if bat_cap is not None and bat_cost > float(bat_cap) + slack:
        return True
    return False


def copy_capex_bound_fields(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Copy numeric CAPEX bounds and the mode string from an upstream JSON."""
    for key in CAPEX_BOUND_KEYS:
        value = src.get(key)
        if value is None:
            continue
        try:
            dst[key] = float(value)
        except (TypeError, ValueError):
            pass
    mode = str(src.get("capex_mode") or "").strip().lower()
    if mode in {"joint", "split"}:
        dst["capex_mode"] = mode


def capex_limit_notes(cap: dict[str, Any], *, slovak: bool) -> list[str]:
    notes: list[str] = []
    mode = str(cap.get("capex_mode") or "joint")
    if mode == "joint":
        total = cap.get("max_total_capex_eur")
        if total is None:
            return notes
        if float(total) <= 1e-9:
            notes.append(
                "Celkový strop CAPEX je 0 € — odporúčanie bez investície. Mapa ostáva na technickom maxime."
                if slovak
                else "Total CAPEX cap is €0 — recommendation is no investment. The size map still uses technical limits."
            )
        else:
            notes.append(
                "Celkový strop CAPEX platí na súčet FVE + batéria; návrh rozdelí rozpočet podľa cieľa. "
                "Mapa veľkostí ostáva na technickom maxime."
                if slovak
                else "Total CAPEX cap applies to PV + battery together; the design allocates the pool by the objective. "
                "The size map still uses technical limits."
            )
        if cap.get("used_legacy_joint"):
            notes.append(
                "Starý max_capex_eur sa berie ako celkový rozpočet (FVE + batéria)."
                if slovak
                else "Legacy max_capex_eur is treated as a joint PV + battery pool."
            )
        return notes
    if cap.get("max_pv_capex_eur") is not None:
        notes.append(
            "Strop CAPEX FVE platí pre odporúčanie, mapa veľkostí ostáva na technickom maxime."
            if slovak
            else "PV CAPEX cap applies to the recommended size; the size map uses technical limits."
        )
    if cap.get("max_battery_capex_eur") is not None:
        notes.append(
            "Strop CAPEX batérie platí pre odporúčanie; nespotrebovaný strop sa nepresúva na FVE."
            if slovak
            else "Battery CAPEX cap applies to the recommended size; unused battery budget is not added to PV."
        )
    return notes


CAPEX_BOUND_KEYS = (
    "max_kwp",
    "max_kwh",
    "max_kwp_budget",
    "max_kwh_budget",
    "max_capex_eur",
    "max_total_capex_eur",
    "max_pv_capex_eur",
    "max_battery_capex_eur",
)
