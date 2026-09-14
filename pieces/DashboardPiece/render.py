"""Render the investment dashboard as a single self-contained HTML file.

Charts are inline SVG rather than a JavaScript charting library. The dashboard is
handed to people who open it from a shared drive or an email attachment, often
inside a network that will not fetch a CDN, and a chart that silently fails to
draw is worse than no chart. Inline SVG also survives print-to-PDF, which is how
these end up in a board pack.
"""

from __future__ import annotations

import html
import json
import math
from datetime import datetime, timezone
from typing import Any, Sequence

from .i18n import EN, pack_json

# Sequential scale, dark for weak results through to strong. Readable in
# greyscale and safe for the most common form of colour blindness.
HEATMAP_COLOURS = [
    (13, 27, 62),
    (23, 62, 110),
    (26, 106, 140),
    (35, 148, 138),
    (95, 184, 111),
    (176, 210, 71),
    (253, 231, 55),
]

CSS = """
:root {
  --bg: #f5f6f8;
  --panel: #ffffff;
  --ink: #14181f;
  --muted: #5c6675;
  --line: #dfe3e9;
  --accent: #1a6a8c;
  --good: #2f8f5b;
  --warn: #b4761e;
  --bad: #b23a3a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 32px 24px 64px; }
header { border-bottom: 2px solid var(--ink); padding-bottom: 16px; margin-bottom: 14px; }
.meta-strip {
  display: flex; align-items: flex-start; gap: 10px;
  margin: 0 0 22px; padding: 7px 12px;
  background: #e8eef2; border: 1px solid #d3dce4; border-radius: 8px;
  font-size: 13px; line-height: 1.45; color: var(--muted);
}
.meta-strip .meta-k {
  flex: 0 0 auto; font-size: 11px; font-weight: 650;
  letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent);
  padding-top: 1px; white-space: nowrap;
}
.meta-strip p { margin: 0; }
.kicker {
  font-size: 11px; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--accent); font-weight: 650; margin: 0 0 8px;
}
h1 { font-size: 26px; margin: 0 0 6px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 14px; }
.source-box { border-left: 3px solid var(--accent); padding: 2px 0 2px 16px; margin-bottom: 16px; }
.source-box strong { display: block; font-size: 16px; margin-bottom: 4px; }
h2 {
  font-size: 15px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin: 36px 0 14px; font-weight: 600;
}
.tip {
  display: inline-flex; align-items: center; justify-content: center;
  width: 15px; height: 15px; margin-left: 6px; border-radius: 50%;
  border: 1px solid currentColor; font-size: 10px; font-weight: 700;
  line-height: 1; cursor: help; position: relative; vertical-align: 1px;
  flex: 0 0 auto; text-transform: none; letter-spacing: 0; opacity: .72;
}
.tip:hover, .tip:focus { opacity: 1; outline: none; z-index: 5; }
.tip .tip-text,
.card .label .tip .tip-text,
h2 .tip .tip-text,
th .tip .tip-text,
td .tip .tip-text {
  visibility: hidden; opacity: 0; pointer-events: none;
  position: absolute; z-index: 80; top: calc(100% + 8px); left: 0;
  bottom: auto; transform: none;
  width: max-content; max-width: min(360px, 70vw);
  padding: 10px 12px; border-radius: 6px; background: #1b212b; color: #f4f6f8;
  font-size: 12px; font-weight: 400; line-height: 1.45; text-transform: none;
  letter-spacing: 0; text-align: left; box-shadow: 0 8px 22px rgba(20,24,31,.22);
  white-space: normal;
}
.card .label .tip .tip-text {
  left: auto; right: 0;
}
.tip:hover .tip-text, .tip:focus .tip-text,
.card .label .tip:hover .tip-text, .card .label .tip:focus .tip-text {
  visibility: visible; opacity: 1;
}
.panel {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 20px 22px; margin-bottom: 18px; overflow: visible;
}
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; overflow: visible; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 16px 18px; overflow: visible; position: relative; z-index: 1;
}
.card:hover, .card:focus-within { z-index: 30; }
.card .label {
  display: flex; align-items: flex-start; gap: 2px;
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); line-height: 1.35;
}
.card .label > span:not(.tip) { flex: 1; min-width: 0; }
.card .label > .tip { flex: 0 0 auto; }
.card .value { font-size: 27px; font-weight: 650; margin-top: 6px; letter-spacing: -0.02em; }
.card .note { font-size: 12.5px; color: var(--muted); margin-top: 4px; }
.value.good { color: var(--good); }
.value.warn { color: var(--warn); }
.value.bad { color: var(--bad); }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line); }
th { font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.best td { background: #eaf5ef; font-weight: 600; }
.legend { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--muted); margin-top: 10px; }
.swatch { display: flex; }
.swatch span { width: 26px; height: 12px; display: block; }
.note-block { font-size: 13.5px; color: var(--muted); margin-top: 10px; }
.badge {
  display: inline-block; font-size: 12px; padding: 3px 9px; border-radius: 999px;
  border: 1px solid var(--line); background: #f0f3f6; color: var(--muted); margin-right: 6px;
}
.badge.ok { background: #eaf5ef; border-color: #bfe0cd; color: var(--good); }
.badge.warn { background: #fbf2e3; border-color: #ebd7b0; color: var(--warn); }
figure { margin: 0; }
figcaption { font-size: 13px; color: var(--muted); margin-top: 8px; }
.hm-cell { cursor: pointer; }
.hm-cell:focus { outline: none; }
.hm-cell:hover .hm-fill { stroke: #14181f; stroke-width: 1.2; }
.hm-chart-head {
  display: flex; justify-content: space-between; align-items: flex-start;
  gap: 12px; flex-wrap: wrap; margin: 0 0 10px;
}
.hm-chart-title { font-size: 13px; font-weight: 600; color: var(--ink); }
.hm-chart-axis { font-size: 12px; color: var(--muted); margin-top: 2px; }
.hm-rec-pill {
  display: inline-flex; align-items: center; gap: 8px;
  background: #c31f1f; color: #fff; font-weight: 700; font-size: 13px;
  padding: 7px 12px; border-radius: 6px; letter-spacing: 0.01em;
  box-shadow: 0 2px 8px rgba(195, 31, 31, .28);
}
.hm-rec-pill .k {
  font-size: 10px; font-weight: 700; letter-spacing: .08em;
  text-transform: uppercase; opacity: .88;
}
.hm-badge-rec {
  display: inline-block; background: #fdecec; border: 1px solid #e8b4b4;
  color: #9a2424; font-weight: 700; padding: 3px 10px; border-radius: 999px;
}
.hm-pick-head {
  display: flex; justify-content: space-between; align-items: baseline;
  gap: 12px; flex-wrap: wrap; margin: 18px 0 12px;
}
.hm-pick-head strong { font-size: 16px; }
.hm-figure { overflow-x: auto; }
.mrk-strip {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px; margin: 16px 0 14px;
}
.mrk-stat {
  background: #f7f9fb; border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px;
}
.mrk-stat .k { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.mrk-stat .v { font-size: 24px; font-weight: 650; margin-top: 4px; letter-spacing: -0.02em; }
.mrk-stat .v.good { color: var(--good); }
.mrk-copy { font-size: 14.5px; max-width: 78ch; margin: 0 0 8px; }
.bill-strip {
  display: grid; grid-template-columns: 1fr auto 1fr auto 1fr minmax(150px, 0.95fr);
  gap: 10px; align-items: stretch; margin: 0;
}
.bill-stat {
  background: #f7f9fb; border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px;
}
.bill-stat .k { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.bill-stat .v { font-size: 28px; font-weight: 650; margin-top: 6px; letter-spacing: -0.02em; }
.bill-stat .v.good { color: var(--good); }
.bill-stat .note { font-size: 12.5px; color: var(--muted); margin-top: 4px; }
.bill-arrow {
  display: flex; align-items: center; justify-content: center;
  color: var(--muted); font-size: 22px; font-weight: 600; padding: 0 2px;
}
@media (max-width: 980px) {
  .bill-strip { grid-template-columns: 1fr 1fr; }
  .bill-arrow { display: none; }
}
@media (max-width: 820px) {
  .bill-strip { grid-template-columns: 1fr; }
  .bill-arrow { display: none; }
}
.hw-strip {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px; margin-bottom: 14px;
}
.hw-stat {
  background: #f7f9fb; border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px;
}
.hw-stat .k { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.hw-stat .v { font-size: 18px; font-weight: 650; margin-top: 6px; letter-spacing: -0.02em; line-height: 1.25; }
.hw-stat .note { font-size: 12.5px; color: var(--muted); margin-top: 4px; }
@media (max-width: 820px) {
  .hw-strip { grid-template-columns: 1fr; }
}
.lang-switch {
  display: inline-flex; gap: 4px; margin-left: auto;
}
.lang-switch button {
  border: 1px solid var(--line); background: #fff; color: var(--muted);
  border-radius: 6px; padding: 4px 10px; font: inherit; font-size: 12px;
  letter-spacing: 0.06em; cursor: pointer;
}
.lang-switch button.on { background: var(--ink); color: #fff; border-color: var(--ink); }
header .head-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.day-pick { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 8px 0 12px; font-size: 14px; }
.day-pick input { font: inherit; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; }
.capex-table td:last-child, .capex-table th:last-child { text-align: right; font-variant-numeric: tabular-nums; }
.eq { font-variant-numeric: tabular-nums; }
.hm-capex-eq {
  margin-top: 10px; padding: 12px 16px; background: #f7f9fb;
  border: 1px solid var(--line); border-radius: 8px; position: relative; z-index: 4;
}
.hm-capex-eq .eq-head {
  display: inline-flex; align-items: center; gap: 2px;
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); margin-bottom: 6px;
}
.hm-capex-eq table { margin: 0; font-size: 13.5px; }
.hm-capex-eq th, .hm-capex-eq td { padding: 5px 8px; border-bottom: none; }
.hm-capex-eq tr.total td { font-weight: 650; border-top: 1px solid var(--line); padding-top: 8px; }
@media print {
  body { background: #fff; }
  .panel, .card { break-inside: avoid; }
  .hm-cell { cursor: default; }
  .lang-switch { display: none; }
}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def fmt_money(value: Any, decimals: int = 0) -> str:
    if not is_number(value):
        return "—"
    return f"{_group_num(value, decimals)} €"


def fmt_num(value: Any, decimals: int = 1, unit: str = "") -> str:
    if not is_number(value):
        return "—"
    text = _group_num(value, decimals)
    return f"{text} {unit}".strip()


def _group_num(value: float, decimals: int) -> str:
    """1 000 000,50 — space thousands, decimal comma."""
    sign = "-" if value < 0 else ""
    magnitude = abs(float(value))
    if decimals <= 0:
        return f"{sign}{int(round(magnitude)):,}".replace(",", " ")
    whole, frac = f"{magnitude:.{decimals}f}".split(".")
    return f"{sign}{int(whole):,}".replace(",", " ") + "," + frac


def fmt_years(value: Any) -> str:
    if not is_number(value):
        return "—"
    return f"{_group_num(value, 1)} r"


def fmt_mwh(value: Any, decimals: int = 1) -> str:
    if not is_number(value):
        return "—"
    return f"{_group_num(value, decimals)} €/MWh"


def fmt_dt(value: Any) -> str:
    if not value:
        return "—"
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return stamp.strftime("%d.%m.%Y %H:%M")


def colour_for(fraction: float) -> str:
    fraction = min(max(fraction, 0.0), 1.0)
    scaled = fraction * (len(HEATMAP_COLOURS) - 1)
    low = int(math.floor(scaled))
    high = min(low + 1, len(HEATMAP_COLOURS) - 1)
    t = scaled - low
    r = round(HEATMAP_COLOURS[low][0] + (HEATMAP_COLOURS[high][0] - HEATMAP_COLOURS[low][0]) * t)
    g = round(HEATMAP_COLOURS[low][1] + (HEATMAP_COLOURS[high][1] - HEATMAP_COLOURS[low][1]) * t)
    b = round(HEATMAP_COLOURS[low][2] + (HEATMAP_COLOURS[high][2] - HEATMAP_COLOURS[low][2]) * t)
    return f"rgb({r},{g},{b})"


PAYBACK_TIP_SK = (
    "Počet rokov, za ktoré úspora na faktúre (po O&M) vráti investíciu. "
    "Počíta sa ako CAPEX ÷ ročný čistý cashflow. "
    "Volá sa jednoduchá, lebo ignoruje časovú hodnotu peňazí: euro ušetrené neskôr berie rovnako ako euro dnes. "
    "Nezohľadňuje diskontnú sadzbu ani degradáciu panelov. "
    "Diskontovaná návratnosť v Podrobnostiach je preto vždy dlhšia."
)


def tip(text: str, i18n: str = "") -> str:
    if not text:
        return ""
    attr = f' data-i18n="{esc(i18n)}"' if i18n else ""
    return (
        f'<span class="tip" tabindex="0">?'
        f'<span class="tip-text"{attr}>{esc(text)}</span></span>'
    )


def labeled(label: str, hint: str = "", i18n: str = "", i18n_tip: str = "") -> str:
    text = f'<span data-i18n="{esc(i18n)}">{esc(label)}</span>' if i18n else esc(label)
    return f"{text}{tip(hint, i18n=i18n_tip)}"


def copy_el(sk: str, en: str, *, html: bool = False, tag: str = "span") -> str:
    extra = ' data-copy-html="1"' if html else ""
    inner = sk if html else esc(sk)
    return (
        f"<{tag}{extra} data-copy-sk=\"{esc(sk)}\" data-copy-en=\"{esc(en)}\">{inner}</{tag}>"
    )


def size_note_pair(kwp: Any, kwh: Any) -> tuple[str, str]:
    sk = (
        f"Simulácia {fmt_num(kwp, 0, 'kWp')} FVE + "
        f"{fmt_num(kwh, 0, 'kWh')} batéria"
    )
    en = (
        f"Simulation {fmt_num(kwp, 0, 'kWp')} PV + "
        f"{fmt_num(kwh, 0, 'kWh')} battery"
    )
    return sk, en


def section(title: str, hint: str = "", i18n: str = "", i18n_tip: str = "") -> str:
    label = f'<span data-i18n="{esc(i18n)}">{esc(title)}</span>' if i18n else esc(title)
    return f"<h2>{label}{tip(hint, i18n=i18n_tip)}</h2>"


def card(
    label: str,
    value: str,
    note: str = "",
    tone: str = "",
    live: str = "",
    rec_only: bool = False,
    hint: str = "",
    i18n: str = "",
    i18n_note: str = "",
    i18n_tip: str = "",
    note_html: str = "",
) -> str:
    tone_class = f" {tone}" if tone else ""
    note_attr = f' data-i18n="{esc(i18n_note)}"' if i18n_note else ""
    if note_html:
        note_html = f'<div class="note">{note_html}</div>'
    else:
        note_html = f'<div class="note"{note_attr}>{esc(note)}</div>' if (note or live) else ""
    attrs = ""
    if live:
        attrs += f' data-live="{esc(live)}"'
    if rec_only:
        attrs += ' data-rec-only="1"'
        attrs += f' data-original="{esc(value)}"'
        attrs += f' data-original-note="{esc(note)}"'
        attrs += f' data-original-tone="{esc(tone)}"'
    return (
        f'<div class="card"{attrs}><div class="label">{labeled(label, hint, i18n=i18n, i18n_tip=i18n_tip)}</div>'
        f'<div class="value{tone_class}">{esc(value)}</div>{note_html}</div>'
    )


def _mrk_copy(
    *,
    size_note: str,
    size_note_en: str,
    current: Any,
    proposed: Any,
    cut: Any,
    save_year: Any,
    peak: Any,
    fee: Any,
    safety: Any,
) -> tuple[str, str]:
    who_sk = (
        (size_note or "")
        .replace("Simulácia ", "")
        .replace(" + ", " a ")
        .replace(" batéria", " batérie")
        .strip()
        or "tejto veľkosti"
    )
    who_en = (
        (size_note_en or "")
        .replace("Simulation ", "")
        .strip()
        or "this size"
    )
    if not is_number(current) or current <= 0:
        return (
            "Zmluvná rezervovaná kapacita nebola zadaná, návrh zmeny MRK preto nie je k dispozícii.",
            "Contracted reserved capacity was not entered, so an MRK change proposal is not available.",
        )
    if not is_number(proposed):
        return (
            "Po nasadení systému ešte nemáme dostatok mesačných špičiek na návrh novej MRK.",
            "After deploying the plant we do not yet have enough monthly peaks to propose a new MRK.",
        )
    if not is_number(cut) or cut <= 1e-6:
        peak_sk = f" Najvyššia špička po nasadení je {fmt_num(peak, 0, 'kW')}." if is_number(peak) else ""
        peak_en = f" The highest peak after the plant is {fmt_num(peak, 0, 'kW')}." if is_number(peak) else ""
        return (
            f"Pri {who_sk} simulácia nezníži odber zo siete dosť na to, "
            f"aby sa oplatilo meniť zmluvu. Odporúčame ponechať {fmt_num(current, 0, 'kW')}."
            f"{peak_sk}",
            f"At {who_en} the simulation does not cut grid import enough to justify changing the contract. "
            f"We recommend keeping {fmt_num(current, 0, 'kW')}.{peak_en}",
        )
    fee_sk = f" Pri sadzbe {fmt_money(fee, 2)} za kW mesačne" if is_number(fee) and fee > 0 else ""
    fee_en = f" At {fmt_money(fee, 2)} per kW per month" if is_number(fee) and fee > 0 else ""
    safety_sk = (
        f" Návrh je o {fmt_num(safety, 0, '%')} nad najvyššou simulovanou špičkou "
        f"({fmt_num(peak, 0, 'kW')}) a zaokrúhlený na 5 kW."
        if is_number(safety) and is_number(peak)
        else ""
    )
    safety_en = (
        f" The proposal is {fmt_num(safety, 0, '%')} above the highest simulated peak "
        f"({fmt_num(peak, 0, 'kW')}) and rounded to 5 kW."
        if is_number(safety) and is_number(peak)
        else ""
    )
    return (
        f"Po nasadení {who_sk} klesnú mesačné špičky odberu zo siete. "
        f"Zmluvnú MRK preto navrhujeme znížiť z {fmt_num(current, 0, 'kW')} "
        f"na {fmt_num(proposed, 0, 'kW')} (−{fmt_num(cut, 0, 'kW')})."
        f"{fee_sk} to ušetrí približne {fmt_money(save_year)} ročne na fixnom poplatku, "
        f"navyše k úspore za silovú elektrinu a distribúciu.{safety_sk} "
        f"Zmena zmluvy s PDS nie je automatická — ide o podklad na rokovanie.",
        f"After deploying {who_en}, monthly grid peaks fall. "
        f"We therefore propose cutting contracted MRK from {fmt_num(current, 0, 'kW')} "
        f"to {fmt_num(proposed, 0, 'kW')} (−{fmt_num(cut, 0, 'kW')})."
        f"{fee_en} this saves about {fmt_money(save_year)} per year on the standing charge, "
        f"on top of commodity and distribution savings.{safety_en} "
        f"A contract change with the DSO is not automatic — this is a brief for negotiation.",
    )


def _plant_bill_label(has_battery: bool) -> str:
    return "Účet s FVE a batériou" if has_battery else "Účet s FVE"


def render_bill_compare(
    *,
    baseline: Any,
    plant: Any,
    savings: Any,
    size_note: str = "",
    size_note_en: str = "",
    has_battery: bool = True,
    co2: dict[str, Any] | None = None,
) -> str:
    plant_label = _plant_bill_label(has_battery)
    size_html = copy_el(size_note or "Odporúčaná veľkosť", size_note_en or "Recommended size")
    after_html = copy_el(size_note or "Po nasadení systému", size_note_en or "After deploying the plant")
    co2_tile = ""
    data = co2 or {}
    if is_number(data.get("t_co2")):
        saved_kwh = data.get("saved_kwh")
        factor = data.get("kg_co2_per_kwh")
        mwh_sk = (
            fmt_num(saved_kwh / 1000.0, 1, "MWh") if is_number(saved_kwh) else "—"
        )
        factor_txt = fmt_num(factor, 3, "kg/kWh") if is_number(factor) else "0,155 kg/kWh"
        co2_tile = (
            '<div class="bill-stat"><div class="k">'
            + labeled(
                "Úspora CO₂",
                data.get("method") or (
                    "Vyhnutá elektrina zo siete × emisný faktor. Nie je to LCA výroby panelov. "
                    "Počíta sa len pre odporúčanú veľkosť; klik na mapu toto číslo nemení."
                ),
                i18n="bill.co2",
                i18n_tip="bill.co2.tip",
            )
            + f'</div><div class="v good">{esc(fmt_num(data.get("t_co2"), 2, "t"))}</div>'
            + '<div class="note">'
            + copy_el(
                f"z {mwh_sk} menej nákupu · {factor_txt}",
                f"{EN['co2.from']} {mwh_sk} {EN['co2.less_grid']} · {factor_txt}",
            )
            + "</div></div>"
        )
    return (
        section(
            "Ročný účet za elektrinu",
            "Koľko zaplatíte za nákup zo siete a MRK dnes a koľko po nasadení navrhovaného systému. Rozdiel je ročná úspora.",
            i18n="section.bill",
            i18n_tip="section.bill.tip",
        )
        + '<div class="panel" id="bill-panel">'
        + f'<span class="badge" id="bill-size">{size_html}</span>'
        + '<div class="bill-strip">'
        + '<div class="bill-stat"><div class="k">'
        + labeled(
            "Bez FVE a batérie",
            "Ročný účet dnes: silová elektrina (DAM), distribúcia a poplatok za zmluvnú MRK. Žiadna FVE ani batéria.",
            i18n="bill.without",
            i18n_tip="bill.without.tip",
        )
        + f'</div><div class="v" id="bill-today">{esc(fmt_money(baseline))}</div>'
        + '<div class="note" data-i18n="bill.without.note">Dnešný nákup zo siete + MRK</div></div>'
        + '<div class="bill-arrow" aria-hidden="true">→</div>'
        + '<div class="bill-stat"><div class="k" id="bill-plant-label">'
        + labeled(
            plant_label,
            "Rovnaký účet po nasadení: menej nákupu zo siete, predaj prebytku a prípadne iné špičky. MRK v tomto čísle ostáva na dnešnej zmluve.",
            i18n="bill.with.pvbat" if has_battery else "bill.with.pv",
            i18n_tip="bill.with.tip",
        )
        + f'</div><div class="v" id="bill-plant">{esc(fmt_money(plant))}</div>'
        + f'<div class="note" id="bill-plant-note">{after_html}</div></div>'
        + '<div class="bill-arrow" aria-hidden="true">=</div>'
        + '<div class="bill-stat"><div class="k">'
        + labeled(
            "Ročný rozdiel",
            "Účet dnes mínus účet po nasadení. To isté číslo ako karta Ročná úspora. Nezahŕňa zmenu zmluvnej MRK.",
            i18n="bill.delta",
            i18n_tip="bill.delta.tip",
        )
        + f'</div><div class="v good" id="bill-delta">{esc(fmt_money(savings))}</div>'
        + '<div class="note" data-i18n="bill.delta.note">Toľko ušetríte za rok</div></div>'
        + co2_tile
        + "</div></div>"
    )


def render_mrk_proposal(
    proposal: dict[str, Any] | None,
    *,
    size_note: str = "",
    size_note_en: str = "",
    override: dict[str, Any] | None = None,
) -> str:
    src = dict(proposal or {})
    if override:
        src.update({k: v for k, v in override.items() if v is not None})
    current = src.get("current_contract_rv_kw") or src.get("current_mrk_kw")
    proposed = src.get("recommended_rv_kw_conservative") or src.get("proposed_mrk_kw")
    cut = src.get("rv_downsizing_potential_kw") or src.get("mrk_cut_kw")
    save_year = src.get("estimated_fixed_rv_fee_savings_eur_per_year") or src.get("mrk_savings_annual_eur")
    peak = src.get("max_monthly_peak_import_kw_after_optimization") or src.get("peak_after_kw")
    fee = src.get("fee_eur_per_kw_month")
    safety = src.get("safety_margin_pct")
    can_cut = is_number(cut) and cut > 1e-6
    badge = (
        '<span class="badge ok" id="mrk-badge" data-i18n="mrk.badge.cut">Návrh na zníženie zmluvy</span>'
        if can_cut
        else '<span class="badge" id="mrk-badge" data-i18n="mrk.badge.keep">Zmluvu ponechať</span>'
    )
    copy_sk, copy_en = _mrk_copy(
        size_note=size_note,
        size_note_en=size_note_en,
        current=current,
        proposed=proposed,
        cut=cut,
        save_year=save_year,
        peak=peak,
        fee=fee,
        safety=safety,
    )
    return (
        section(
            "Návrh zmluvnej MRK",
            "Koľko rezervovanej kapacity treba po nasadení FVE a batérie a koľko to ušetrí na mesačnom poplatku.",
            i18n="section.mrk",
            i18n_tip="section.mrk.tip",
        )
        + '<div class="panel" id="mrk-panel">'
        + f'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">{badge}'
        + f'<span class="badge" id="mrk-size">{copy_el(size_note or "Odporúčaná veľkosť", size_note_en or "Recommended size")}</span></div>'
        + '<div class="mrk-strip">'
        + f'<div class="mrk-stat"><div class="k">{labeled("Dnešná MRK", "Aktuálna zmluvná rezervovaná kapacita, ktorú platíte každý mesiac.", i18n="mrk.today", i18n_tip="mrk.today.tip")}</div>'
        + f'<div class="v" id="mrk-current">{esc(fmt_num(current, 0, "kW"))}</div></div>'
        + f'<div class="mrk-stat"><div class="k">{labeled("Navrhovaná MRK", "Konzervatívny návrh novej zmluvy: najvyššia simulovaná špička plus rezerva, zaokrúhlená na 5 kW.", i18n="mrk.proposed", i18n_tip="mrk.proposed.tip")}</div>'
        + f'<div class="v{" good" if can_cut else ""}" id="mrk-proposed">{esc(fmt_num(proposed, 0, "kW"))}</div></div>'
        + f'<div class="mrk-stat"><div class="k">{labeled("Zníženie", "Koľko kW možno zo zmluvy uvoľniť, ak PDS zmenu potvrdí.", i18n="mrk.cut", i18n_tip="mrk.cut.tip")}</div>'
        + f'<div class="v{" good" if can_cut else ""}" id="mrk-cut">{esc(fmt_num(cut, 0, "kW") if can_cut else "0 kW")}</div></div>'
        + f'<div class="mrk-stat"><div class="k">{labeled("Ročná úspora na MRK", "Zníženie × mesačná sadzba × 12. Pripočíta sa k úspore za silovú elektrinu, nie je v nej zahrnutá.", i18n="mrk.save", i18n_tip="mrk.save.tip")}</div>'
        + f'<div class="v{" good" if can_cut else ""}" id="mrk-save">{esc(fmt_money(save_year) if can_cut else "0 €")}</div></div>'
        + "</div>"
        + f'<p class="mrk-copy" id="mrk-copy">{copy_el(copy_sk, copy_en)}</p>'
        + f'<div class="note-block">{labeled("Špička po nasadení", "Najvyšší mesačný odber zo siete v simulácii po FVE a batérii.", i18n="mrk.peak", i18n_tip="mrk.peak.tip")} '
        + f'<span id="mrk-peak">{esc(fmt_num(peak, 0, "kW"))}</span>'
        + '<span data-i18n="mrk.peak.tail">. Ide o podklad na rokovanie s PDS, nie o automatickú zmenu zmluvy.</span></div>'
        + "</div>"
    )


def detail_row(
    label: str,
    value: str,
    live: str = "",
    rec_only: bool = False,
    hint: str = "",
    i18n: str = "",
    i18n_tip: str = "",
) -> str:
    attrs = ""
    if live:
        attrs += f' data-live="{esc(live)}"'
    if rec_only:
        attrs += ' data-rec-only="1"'
        attrs += f' data-original="{esc(value)}"'
    return (
        f"<tr{attrs}><td>{labeled(label, hint, i18n=i18n, i18n_tip=i18n_tip)}</td>"
        f"<td class='num'>{value if str(value).lstrip().startswith('<') else esc(value)}</td></tr>"
    )


def _axis_index(axis: Sequence[Any], value: Any) -> int:
    if not axis:
        return 0
    if not is_number(value):
        return 0
    return min(range(len(axis)), key=lambda i: abs(float(axis[i]) - float(value)))


def _grid_get(grid: Any, j: int, i: int) -> Any:
    if not isinstance(grid, list) or j >= len(grid):
        return None
    row = grid[j]
    if not isinstance(row, list) or i >= len(row):
        return None
    return row[i]


def unit_prices_from_heatmap(heatmap: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """€/kWp and €/kWh from the CAPEX grid, even when the recommended cell is 0 kWh."""
    data = heatmap or {}
    axes = data.get("axes") or {}
    pv_axis: list[Any] = list(axes.get("pv_kwp") or [])
    bat_axis: list[Any] = list(axes.get("battery_kwh") or [])
    cap = (data.get("grids") or {}).get("total_capex_eur") or []
    eur_kwp = None
    eur_kwh = None
    for j, kwh in enumerate(bat_axis):
        if not is_number(kwh) or float(kwh) > 1e-9:
            continue
        for i, kwp in enumerate(pv_axis):
            if not is_number(kwp) or float(kwp) <= 1e-9:
                continue
            val = _grid_get(cap, j, i)
            if is_number(val) and float(val) > 0:
                eur_kwp = float(val) / float(kwp)
                break
        if eur_kwp is not None:
            break
    for i, kwp in enumerate(pv_axis):
        if not is_number(kwp) or float(kwp) > 1e-9:
            continue
        for j, kwh in enumerate(bat_axis):
            if not is_number(kwh) or float(kwh) <= 1e-9:
                continue
            val = _grid_get(cap, j, i)
            if is_number(val) and float(val) > 0:
                eur_kwh = float(val) / float(kwh)
                break
        if eur_kwh is not None:
            break
    if eur_kwh is None:
        for i, _kwp in enumerate(pv_axis):
            pairs: list[tuple[float, float]] = []
            for j, kwh in enumerate(bat_axis):
                val = _grid_get(cap, j, i)
                if is_number(kwh) and is_number(val):
                    pairs.append((float(kwh), float(val)))
            for a, (k1, c1) in enumerate(pairs):
                for k2, c2 in pairs[a + 1 :]:
                    dk = k2 - k1
                    if abs(dk) <= 1e-9:
                        continue
                    cand = (c2 - c1) / dk
                    if cand > 0:
                        eur_kwh = cand
                        break
                if eur_kwh is not None:
                    break
            if eur_kwh is not None:
                break
    return eur_kwp, eur_kwh


def _objective_view(heatmap: dict[str, Any] | None) -> dict[str, Any]:
    obj = str((heatmap or {}).get("objective") or "max_npv").lower()
    if obj in {"shortest_payback", "min_payback", "payback"}:
        return {
            "id": "shortest_payback",
            "metric": "simple_payback_years",
            "invert": True,
            "title": "Jednoduchá návratnosť podľa veľkosti systému (roky)",
            "title_en": EN["hm.title.payback"],
            "title_i18n": "hm.title.payback",
            "cells": "Hodnoty v bunkách sú návratnosť v rokoch. Svetlejšia bunka = kratšia návratnosť.",
            "cells_en": EN["hm.cells.payback"],
            "cells_i18n": "hm.cells.payback",
            "legend": "years",
            "label": "najkratšia návratnosť",
            "label_en": EN["goal.payback"],
            "label_i18n": "goal.payback",
            "obj_note": "Tento beh hľadal najkratšiu návratnosť.",
            "obj_note_en": EN["kpi.obj.payback"],
            "obj_note_i18n": "kpi.obj.payback",
        }
    if obj in {"max_annual_savings", "max_savings", "annual_savings", "highest_savings"}:
        return {
            "id": "max_annual_savings",
            "metric": "annual_savings_eur",
            "invert": False,
            "title": "Ročná úspora podľa veľkosti systému (€)",
            "title_en": EN["hm.title.savings"],
            "title_i18n": "hm.title.savings",
            "cells": "Hodnoty v bunkách sú čistý ročný cashflow po O&M. Svetlejšia bunka = vyššia úspora.",
            "cells_en": EN["hm.cells.savings"],
            "cells_i18n": "hm.cells.savings",
            "legend": "money",
            "label": "najvyššia úspora za rok",
            "label_en": EN["goal.savings"],
            "label_i18n": "goal.savings",
            "obj_note": "Tento beh hľadal najvyššiu úsporu za rok.",
            "obj_note_en": EN["kpi.obj.savings"],
            "obj_note_i18n": "kpi.obj.savings",
        }
    return {
        "id": "max_npv",
        "metric": "npv_eur",
        "invert": False,
        "title": "Čistá súčasná hodnota podľa veľkosti systému (€)",
        "title_en": EN["hm.title.npv"],
        "title_i18n": "hm.title.npv",
        "cells": "Hodnoty v bunkách sú NPV v tisícoch eur. Svetlejšia bunka = vyššie NPV.",
        "cells_en": EN["hm.cells.npv"],
        "cells_i18n": "hm.cells.npv",
        "legend": "money",
        "label": "najvyššie NPV",
        "label_en": EN["goal.npv"],
        "label_i18n": "goal.npv",
        "obj_note": "Tento beh hľadal najvyššie NPV.",
        "obj_note_en": EN["kpi.obj.npv"],
        "obj_note_i18n": "kpi.obj.npv",
    }


def render_heatmap(
    heatmap: dict[str, Any],
    metric: str | None = None,
) -> str:
    """Draw the PV x battery grid; a click shows the economics of that size."""
    view = _objective_view(heatmap)
    metric = metric or view["metric"]
    axes = heatmap.get("axes") or {}
    pv_axis: list[float] = list(axes.get("pv_kwp") or [])
    bat_axis: list[float] = list(axes.get("battery_kwh") or [])
    grids = heatmap.get("grids") or {}
    grid = grids.get(metric) or []
    if not pv_axis or not bat_axis or not grid:
        return '<div class="panel" data-i18n="hm.empty">Mapa veľkostí v tomto behu nie je k dispozícii.</div>'

    values = [v for row in grid for v in row if is_number(v)]
    if not values:
        return '<div class="panel" data-i18n="hm.empty2">Mapa veľkostí nevrátila žiadne konečné výsledky.</div>'
    low, high = min(values), max(values)
    span = high - low if high > low else 1.0

    cell_w, cell_h = 62, 34
    left, top = 132, 22
    width = left + cell_w * len(pv_axis) + 24
    height = top + cell_h * len(bat_axis) + 8

    recommended = heatmap.get("recommended") or {}
    best_pv, best_bat = recommended.get("pv_kwp"), recommended.get("battery_kwh")
    rec_i = _axis_index(pv_axis, best_pv)
    rec_j = _axis_index(bat_axis, best_bat)

    parts = [
        f'<svg id="heatmap-svg" viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="Kliknite na kombináciu FVE a batérie" '
        f'style="font: 11px sans-serif; max-width:{width}px">'
    ]

    for i, kwp in enumerate(pv_axis):
        x = left + i * cell_w + cell_w / 2
        parts.append(
            f'<text x="{x:.1f}" y="{top - 6}" text-anchor="middle" fill="#5c6675">'
            f"{kwp:,.0f}</text>".replace(",", " ")
        )

    for j, kwh in enumerate(bat_axis):
        y = top + j * cell_h
        parts.append(
            f'<text x="{left - 10}" y="{y + cell_h / 2 + 4:.1f}" text-anchor="end" '
            f'fill="#5c6675">{kwh:,.0f} kWh</text>'.replace(",", " ")
        )
        for i, kwp in enumerate(pv_axis):
            x = left + i * cell_w
            value = _grid_get(grid, j, i)
            if not is_number(value):
                parts.append(
                    f'<rect x="{x}" y="{y}" width="{cell_w - 2}" height="{cell_h - 2}" '
                    f'fill="#eef0f3" stroke="#fff"/>'
                )
                continue
            fraction = (value - low) / span
            if view["invert"]:
                fraction = 1.0 - fraction
            fill = colour_for(fraction)
            text_fill = "#ffffff" if fraction < 0.55 else "#14181f"
            if view["legend"] == "years":
                title = f"{kwp:,.0f} kWp / {kwh:,.0f} kWh: {value:.1f}".replace(",", " ")
                label = f"{value:.1f}"
            else:
                title = f"{kwp:,.0f} kWp / {kwh:,.0f} kWh: {value:,.0f} €".replace(",", " ")
                label = (
                    f"{value / 1000:,.0f}k".replace(",", " ")
                    if abs(value) >= 1000
                    else f"{value:,.0f}".replace(",", " ")
                )
            parts.append(
                f'<g class="hm-cell" data-i="{i}" data-j="{j}" tabindex="0" role="button" '
                f'aria-label="{esc(title)}">'
                f'<rect class="hm-fill" x="{x}" y="{y}" width="{cell_w - 2}" height="{cell_h - 2}" '
                f'fill="{fill}" stroke="#fff"><title>{esc(title)}</title></rect>'
                f'<text x="{x + cell_w / 2 - 1:.1f}" y="{y + cell_h / 2 + 4:.1f}" '
                f'text-anchor="middle" fill="{text_fill}">{label}</text></g>'
            )

    if is_number(best_pv) and is_number(best_bat):
        x = left + rec_i * cell_w
        y = top + rec_j * cell_h
        rw, rh = cell_w - 2, cell_h - 2
        parts.append(
            f'<rect x="{x - 2:.1f}" y="{y - 2:.1f}" width="{rw + 4}" height="{rh + 4}" '
            f'fill="none" stroke="#fff" stroke-width="6" pointer-events="none"/>'
        )
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{rw}" height="{rh}" '
            f'fill="none" stroke="#c31f1f" stroke-width="3.5" pointer-events="none"/>'
        )
        parts.append(
            f'<polygon points="{x:.1f},{y:.1f} {x + 18:.1f},{y:.1f} {x:.1f},{y + 18:.1f}" '
            f'fill="#c31f1f" pointer-events="none"/>'
        )

    parts.append(
        f'<rect id="hm-cursor" x="0" y="0" width="{cell_w - 2}" height="{cell_h - 2}" '
        f'fill="none" stroke="#163a56" stroke-width="2.5" pointer-events="none"/>'
    )
    parts.append("</svg>")
    footer_sk = (
        f"Zvislá os je kapacita batérie (kWh). Červený rámček so štítkom je veľkosť, ktorú systém vybral "
        f"podľa cieľa „{view['label']}“. Modrá značka je bunka, na ktorú kliknete. {view['cells']}"
    )
    footer_en = (
        f"The vertical axis is battery capacity (kWh). The red frame with the corner mark is the size "
        f"the design picked for “{view['label_en']}”. The blue mark is the cell you click. {view['cells_en']}"
    )
    bounds = heatmap.get("bounds") or {}

    def _hm_capex(key: str) -> Any:
        val = heatmap.get(key)
        if not is_number(val):
            val = bounds.get(key)
        return val

    pv_capex = _hm_capex("max_pv_capex_eur")
    bat_capex = _hm_capex("max_battery_capex_eur")
    total_capex = _hm_capex("max_total_capex_eur")
    joint_capex = _hm_capex("max_capex_eur")
    mode = str(heatmap.get("capex_mode") or bounds.get("capex_mode") or "").lower()
    if not mode:
        if is_number(pv_capex) or is_number(bat_capex):
            mode = "split"
        elif is_number(total_capex) or is_number(joint_capex):
            mode = "joint"
    if mode == "joint" and not is_number(total_capex) and is_number(joint_capex) and float(joint_capex) > 1e-9:
        total_capex = joint_capex
    if mode == "joint" and is_number(total_capex):
        if float(total_capex) > 1e-9:
            footer_sk += (
                f" Červené odporúčanie ostáva v celkovom strope CAPEX ({fmt_money(total_capex)}): "
                "systém rozdelí rozpočet medzi FVE a batériu podľa cieľa. "
                "Drahšie kombinácie sú na mape, aby ste ich mohli porovnať."
            )
            footer_en += (
                f" The red recommendation stays within the total CAPEX cap ({fmt_money(total_capex)}); "
                "the design allocates the pool between PV and battery by the objective. "
                "More expensive combinations are on the map so you can compare them."
            )
        else:
            footer_sk += (
                " Červené odporúčanie je bez investície (celkový strop CAPEX je 0 €). "
                "Drahšie kombinácie sú na mape, aby ste ich mohli porovnať."
            )
            footer_en += (
                " The red recommendation is no investment (total CAPEX cap is €0). "
                "More expensive combinations are on the map so you can compare them."
            )
    else:
        bits_sk: list[str] = []
        bits_en: list[str] = []
        if is_number(pv_capex):
            if float(pv_capex) > 1e-9:
                bits_sk.append(f"FVE {fmt_money(pv_capex)}")
                bits_en.append(f"PV {fmt_money(pv_capex)}")
            else:
                bits_sk.append("FVE 0 €")
                bits_en.append("PV €0")
        if is_number(bat_capex):
            if float(bat_capex) > 1e-9:
                bits_sk.append(f"batéria {fmt_money(bat_capex)}")
                bits_en.append(f"battery {fmt_money(bat_capex)}")
            else:
                bits_sk.append("batéria 0 €")
                bits_en.append("battery €0")
        if bits_sk:
            footer_sk += (
                f" Červené odporúčanie ostáva v stropoch CAPEX ({', '.join(bits_sk)}). "
                "Nespotrebovaný strop batérie sa nepresúva na FVE. "
                "Drahšie kombinácie sú na mape, aby ste ich mohli porovnať."
            )
            footer_en += (
                f" The red recommendation stays within the CAPEX caps ({', '.join(bits_en)}). "
                "Unused battery budget is not added to PV. "
                "More expensive combinations are on the map so you can compare them."
            )
    rec_pill = ""
    if is_number(best_pv) and is_number(best_bat):
        rec_pill = (
            '<div class="hm-rec-pill">'
            f'<span class="k">{copy_el("Systém vybral", "Design pick")}</span>'
            f"<span>{esc(fmt_num(best_pv, 0, 'kWp'))} / {esc(fmt_num(best_bat, 0, 'kWh'))}</span>"
            "</div>"
        )
    chart_head = (
        f'<div class="hm-chart-head" style="padding-left:{left}px">'
        "<div>"
        f'<div class="hm-chart-title" data-i18n="{esc(view["title_i18n"])}">{esc(view["title"])}</div>'
        f'<div class="hm-chart-axis" data-i18n="hm.axis.pv">Veľkosť FVE (kWp) →</div>'
        "</div>"
        f"{rec_pill}"
        "</div>"
    )

    swatch = "".join(
        f'<span style="background:{colour_for(i / 6)}"></span>' for i in range(7)
    )
    if view["legend"] == "years":
        legend_left, legend_right = (fmt_years(high), fmt_years(low)) if view["invert"] else (fmt_years(low), fmt_years(high))
    else:
        legend_left, legend_right = fmt_money(low), fmt_money(high)
    legend = (
        f'<div class="legend"><span>{legend_left}</span>'
        f'<span class="swatch">{swatch}</span><span>{legend_right}</span></div>'
    )
    eur_kwp, eur_kwh = unit_prices_from_heatmap(heatmap)
    payload = {
        "pv_kwp": pv_axis,
        "battery_kwh": bat_axis,
        "grids": {
            key: grids.get(key) or []
            for key in (
                "annual_savings_eur",
                "npv_eur",
                "simple_payback_years",
                "discounted_payback_years",
                "total_capex_eur",
                "self_consumption_pct",
                "operating_cost_baseline_eur",
                "operating_cost_optimized_eur",
                "battery_cycles_per_year",
                "battery_life_years",
                "cashflow_after_om_eur",
                "proposed_mrk_kw",
                "mrk_cut_kw",
                "mrk_savings_annual_eur",
                "peak_after_kw",
            )
        },
        "recommended": {"i": rec_i, "j": rec_j},
        "cell": {"w": cell_w, "h": cell_h, "left": left, "top": top},
        "current": heatmap.get("current_scenario") or {},
        "objective_label": view["label"],
        "objective_label_en": view["label_en"],
        "objective_i18n": view["label_i18n"],
        "mrk": heatmap.get("mrk") or {},
        "eur_per_kwp": round(eur_kwp, 2) if is_number(eur_kwp) else None,
        "eur_per_kwh": round(eur_kwh, 2) if is_number(eur_kwh) else None,
    }
    data = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    source = heatmap.get("pv_profile_source")
    note_sk = (
        "Každá bunka je úplná ekonomická simulácia voči AI prognóze výroby. "
        "Úspora a NPV sú po O&M a s degradáciou panelov. "
        if source == "ai_forecast"
        else "Prognóza v tomto behu nebola k dispozícii; bunky používajú syntetický profil. "
    )
    note_key = "hm.note.ai" if source == "ai_forecast" else "hm.note.syn"
    return (
        '<div class="panel">'
        f"{chart_head}"
        '<figure class="hm-figure">' + "".join(parts) + "</figure>"
        f"{legend}"
        f'<div class="note-block" style="margin-top:8px">{copy_el(footer_sk, footer_en)}</div>'
        '<div class="hm-pick-head">'
        f'<strong id="hm-title">{labeled("Vybraná konfigurácia", "Kliknite na bunku mapy. Karty a Podrobnosti sa prepíšu na túto veľkosť.", i18n="hm.selected", i18n_tip="hm.selected.tip")}</strong>'
        f'<span class="note hm-badge-rec" id="hm-badge">{labeled("Odporúčanie", "Veľkosť, ktorú vybral cieľ návrhu. Červený rámček na mape.", i18n="hm.recommendation", i18n_tip="hm.rec.tip")}: '
        f'<span data-i18n="{esc(view["label_i18n"])}">{esc(view["label"])}</span></span>'
        "</div>"
        '<div class="cards" id="hm-cards"></div>'
        '<div class="hm-capex-eq" id="hm-capex-break"></div>'
        '<table id="hm-compare" style="margin-top:14px"></table>'
        f'<div class="note-block"><span data-i18n="{note_key}">{esc(note_sk)}</span> '
        f'<span data-i18n="hm.note.click">Kliknite na bunku — karty hore aj Podrobnosti sa prepíšu na túto veľkosť. '
        "Každá kombinácia má vlastnú úplnú simuláciu odberu, cien, FVE, batérie a MRK.</span></div>"
        f'<script type="application/json" id="heatmap-data">{data}</script></div>'
    )


COLOUR_WITHOUT = "#b23a3a"
COLOUR_WITH = "#1a6a8c"


def _axis_max(values: Sequence[float]) -> float:
    high = max((v for v in values if is_number(v)), default=0.0)
    if high <= 0:
        return 1.0
    magnitude = 10 ** max(0, int(math.floor(math.log10(high))))
    step = magnitude if high / magnitude > 2 else magnitude / 2
    return math.ceil(high / step) * step


def render_consumption(consumption: dict[str, Any] | None, fallback: dict[str, Any] | None) -> str:
    """Energy bought from the grid, with and without the proposed plant."""
    data = consumption or {}
    monthly = data.get("monthly") or {}
    daily = data.get("daily") or {}
    totals = data.get("totals") or {}

    if not monthly.get("x") and fallback:
        series = fallback.get("series") or []
        if len(series) >= 2 and fallback.get("x"):
            daily = {
                "x": fallback["x"],
                "without": series[0].get("values") or [],
                "with": series[1].get("values") or [],
                "unit": series[0].get("unit") or "kWh/deň",
            }

    if not monthly.get("x") and not daily.get("x"):
        return ""

    parts = ['<div class="panel">']
    if totals.get("without_kwh") is not None:
        parts.append(
            '<div class="cards" style="margin-bottom:18px">'
            + card(
                "Bez FVE a batérie",
                fmt_num(totals["without_kwh"] / 1000.0, 0, "MWh"),
                "Nákup zo siete za obdobie",
                hint="Koľko MWh by ste kúpili zo siete bez FVE a batérie za rovnaké obdobie.",
                i18n="card.without",
                i18n_note="card.without.note",
                i18n_tip="card.without.tip",
            )
            + card(
                "S FVE a batériou",
                fmt_num((totals.get("with_kwh") or 0) / 1000.0, 0, "MWh"),
                "Nákup zo siete po nasadení",
                "good",
                hint="Nákup zo siete po nasadení navrhnutej FVE a batérie.",
                i18n="card.with",
                i18n_note="card.with.note",
                i18n_tip="card.with.tip",
            )
            + card(
                "Ušetrená energia",
                fmt_num((totals.get("saved_kwh") or 0) / 1000.0, 0, "MWh"),
                note="",
                tone="good",
                hint="Rozdiel medzi nákupom zo siete dnes a po nasadení systému.",
                i18n="card.saved",
                i18n_tip="card.saved.tip",
                note_html=(
                    copy_el(
                        f"{fmt_num(totals.get('saved_pct'), 0, '%')} menej zo siete",
                        f"{fmt_num(totals.get('saved_pct'), 0, '%')} {EN['card.saved.less']}",
                    )
                    if totals.get("saved_pct") is not None
                    else ""
                ),
            )
            + "</div>"
        )

    if monthly.get("x") and monthly.get("without"):
        parts.append(_grouped_bars(monthly, title="Mesačná spotreba zo siete (kWh)", i18n="chart.monthly"))
    if daily.get("x") and daily.get("without"):
        parts.append(_line_pair(daily, title="Denná spotreba zo siete (kWh/deň)", i18n="chart.daily"))
    interval = data.get("interval") or {}
    if interval.get("without_kw"):
        parts.append(_day_picker(interval))

    parts.append(
        '<figcaption data-i18n="caption.consumption">Červená je nákup zo siete bez FVE a batérie. Modrá je ten istý odber '
        'po nasadení navrhnutého systému. Rozdiel medzi nimi je energia, ktorú už '
        'nezaplatíte dodávateľovi. Konkrétny deň otvorí 15-minútový priebeh.</figcaption>'
    )
    parts.append("</div>")
    return "".join(parts)


def _grouped_bars(series: dict[str, Any], *, title: str, i18n: str = "") -> str:
    labels = series.get("x") or []
    without = [float(v) if is_number(v) else 0.0 for v in (series.get("without") or [])]
    with_ = [float(v) if is_number(v) else 0.0 for v in (series.get("with") or [])]
    n = min(len(labels), len(without), len(with_))
    if n == 0:
        return ""
    without, with_, labels = without[:n], with_[:n], labels[:n]
    high = _axis_max(without + with_)

    width, height = 1080, 280
    pad_l, pad_r, pad_t, pad_b = 72, 16, 28, 48
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    group = plot_w / n
    bar_w = min(22.0, group * 0.32)

    i18n_attr = f' data-i18n="{esc(i18n)}"' if i18n else ""
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="{esc(title)}" style="font: 11px sans-serif">'
        f'<text x="{pad_l}" y="16" font-size="13" font-weight="600" fill="#14181f"'
        f"{i18n_attr}>{esc(title)}</text>"
    ]
    for k in range(5):
        y = pad_t + plot_h * k / 4
        value = high * (1 - k / 4)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#e6e9ee"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#5c6675">'
            f"{value:,.0f}</text>".replace(",", " ")
        )

    for i, (label, a, b) in enumerate(zip(labels, without, with_)):
        x0 = pad_l + i * group + group / 2
        ha = plot_h * (a / high)
        hb = plot_h * (b / high)
        parts.append(
            f'<rect x="{x0 - bar_w - 1:.1f}" y="{pad_t + plot_h - ha:.1f}" width="{bar_w:.1f}" '
            f'height="{ha:.1f}" fill="{COLOUR_WITHOUT}"><title>{esc(label)} bez: {a:,.0f} kWh</title></rect>'.replace(",", " ")
        )
        parts.append(
            f'<rect x="{x0 + 1:.1f}" y="{pad_t + plot_h - hb:.1f}" width="{bar_w:.1f}" '
            f'height="{hb:.1f}" fill="{COLOUR_WITH}"><title>{esc(label)} s FVE+bat: {b:,.0f} kWh</title></rect>'.replace(",", " ")
        )
        parts.append(
            f'<text x="{x0:.1f}" y="{height - 28}" text-anchor="middle" fill="#5c6675">{esc(label)}</text>'
        )

    parts.append(
        f'<rect x="{pad_l}" y="{height - 14}" width="11" height="11" fill="{COLOUR_WITHOUT}"/>'
        f'<text x="{pad_l + 16}" y="{height - 4}" fill="#14181f" data-i18n="legend.without">Bez FVE a batérie</text>'
        f'<rect x="{pad_l + 200}" y="{height - 14}" width="11" height="11" fill="{COLOUR_WITH}"/>'
        f'<text x="{pad_l + 216}" y="{height - 4}" fill="#14181f" data-i18n="legend.with">S FVE a batériou</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _line_pair(series: dict[str, Any], *, title: str, i18n: str = "") -> str:
    without = [float(v) if is_number(v) else 0.0 for v in (series.get("without") or [])]
    with_ = [float(v) if is_number(v) else 0.0 for v in (series.get("with") or [])]
    n = min(len(without), len(with_))
    if n < 2:
        return ""
    without, with_ = without[:n], with_[:n]
    high = _axis_max(without + with_)

    width, height = 1080, 260
    pad_l, pad_r, pad_t, pad_b = 72, 16, 28, 36
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def xy(i: int, value: float) -> tuple[float, float]:
        x = pad_l + plot_w * i / (n - 1)
        y = pad_t + plot_h * (1 - value / high)
        return x, y

    without_pts = [xy(i, v) for i, v in enumerate(without)]
    with_pts = [xy(i, v) for i, v in enumerate(with_)]
    fill = " ".join(
        [f"{x:.1f},{y:.1f}" for x, y in without_pts]
        + [f"{x:.1f},{y:.1f}" for x, y in reversed(with_pts)]
    )
    without_line = " ".join(f"{x:.1f},{y:.1f}" for x, y in without_pts)
    with_line = " ".join(f"{x:.1f},{y:.1f}" for x, y in with_pts)

    i18n_attr = f' data-i18n="{esc(i18n)}"' if i18n else ""
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="{esc(title)}" style="font: 11px sans-serif; margin-top:12px">'
        f'<text x="{pad_l}" y="16" font-size="13" font-weight="600" fill="#14181f"'
        f"{i18n_attr}>{esc(title)}</text>"
    ]
    for k in range(5):
        y = pad_t + plot_h * k / 4
        value = high * (1 - k / 4)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#e6e9ee"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#5c6675">'
            f"{value:,.0f}</text>".replace(",", " ")
        )
    parts.append(f'<polygon points="{fill}" fill="{COLOUR_WITH}" fill-opacity="0.12"/>')
    parts.append(
        f'<polyline fill="none" stroke="{COLOUR_WITHOUT}" stroke-width="1.5" points="{without_line}"/>'
    )
    parts.append(
        f'<polyline fill="none" stroke="{COLOUR_WITH}" stroke-width="1.7" points="{with_line}"/>'
    )
    parts.append(
        f'<rect x="{pad_l}" y="{height - 12}" width="11" height="11" fill="{COLOUR_WITHOUT}"/>'
        f'<text x="{pad_l + 16}" y="{height - 2}" fill="#14181f" data-i18n="legend.without">Bez FVE a batérie</text>'
        f'<rect x="{pad_l + 200}" y="{height - 12}" width="11" height="11" fill="{COLOUR_WITH}"/>'
        f'<text x="{pad_l + 216}" y="{height - 2}" fill="#14181f" data-i18n="legend.with">S FVE a batériou</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _day_picker(interval: dict[str, Any]) -> str:
    without = interval.get("without_kw") or []
    with_ = interval.get("with_kw") or []
    n = min(len(without), len(with_))
    if n < 2:
        return ""
    start = str(interval.get("start") or "")
    step = int(interval.get("step_minutes") or 15)
    payload = {
        "start": start,
        "step_minutes": step,
        "n": n,
        "without_kw": without[:n],
        "with_kw": with_[:n],
    }
    data = json.dumps(payload, ensure_ascii=False)
    return (
        '<div class="day-pick">'
        f'<label for="day-input">{labeled("Konkrétny deň", "Vyberte kalendárny deň. Graf nižšie ukáže 15-minútový nákup zo siete v ten deň, s FVE/batériou aj bez.", i18n="day.pick", i18n_tip="day.hint")}</label>'
        '<input id="day-input" type="date">'
        '<span class="note" id="day-label"></span>'
        "</div>"
        '<div id="day-chart"></div>'
        f'<script type="application/json" id="day-series">{data}</script>'
    )


def render_peaks(peaks: dict[str, Any] | None) -> str:
    data = peaks or {}
    labels = data.get("x") or []
    without = [float(v) if is_number(v) else 0.0 for v in (data.get("without") or [])]
    with_ = [float(v) if is_number(v) else 0.0 for v in (data.get("with") or [])]
    n = min(len(labels), len(without), len(with_))
    if n == 0:
        return ""
    without, with_, labels = without[:n], with_[:n], labels[:n]
    contract = data.get("contract_kw")
    proposed = data.get("proposed_kw")
    extras = [v for v in (contract, proposed) if is_number(v)]
    high = _axis_max(without + with_ + extras)

    width, height = 1080, 300
    pad_l, pad_r, pad_t, pad_b = 72, 16, 28, 56
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    group = plot_w / n
    bar_w = min(22.0, group * 0.32)

    def y_of(v: float) -> float:
        return pad_t + plot_h * (1 - v / high)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="Mesačné špičky odberu" style="font: 11px sans-serif">'
        f'<text x="{pad_l}" y="16" font-size="13" font-weight="600" fill="#14181f" data-i18n="chart.peaks">'
        f"Mesačná špička odberu zo siete (kW)</text>"
    ]
    for k in range(5):
        y = pad_t + plot_h * k / 4
        value = high * (1 - k / 4)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#e6e9ee"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#5c6675">'
            f"{_group_num(value, 0)}</text>"
        )
    for i, (label, a, b) in enumerate(zip(labels, without, with_)):
        x0 = pad_l + i * group + group / 2
        ha = plot_h * (a / high)
        hb = plot_h * (b / high)
        short = str(label)[5:] if len(str(label)) >= 7 else str(label)
        parts.append(
            f'<rect x="{x0 - bar_w - 1:.1f}" y="{pad_t + plot_h - ha:.1f}" width="{bar_w:.1f}" '
            f'height="{ha:.1f}" fill="{COLOUR_WITHOUT}">'
            f"<title>{esc(label)} bez: {_group_num(a, 0)} kW</title></rect>"
        )
        parts.append(
            f'<rect x="{x0 + 1:.1f}" y="{pad_t + plot_h - hb:.1f}" width="{bar_w:.1f}" '
            f'height="{hb:.1f}" fill="{COLOUR_WITH}">'
            f"<title>{esc(label)} so systémom: {_group_num(b, 0)} kW</title></rect>"
        )
        parts.append(
            f'<text x="{x0:.1f}" y="{height - 36}" text-anchor="middle" fill="#5c6675">{esc(short)}</text>'
        )
    if is_number(contract) and contract > 0:
        yc = y_of(float(contract))
        parts.append(
            f'<line x1="{pad_l}" y1="{yc:.1f}" x2="{width - pad_r}" y2="{yc:.1f}" '
            f'stroke="#b23a3a" stroke-dasharray="5 4" stroke-width="1.4"/>'
        )
        parts.append(
            f'<text x="{width - pad_r}" y="{yc - 4:.1f}" text-anchor="end" fill="#b23a3a">'
            f'<tspan data-i18n="legend.contract">dnešná MRK</tspan> {_group_num(float(contract), 0)} kW</text>'
        )
    if is_number(proposed) and proposed > 0:
        yp = y_of(float(proposed))
        parts.append(
            f'<line x1="{pad_l}" y1="{yp:.1f}" x2="{width - pad_r}" y2="{yp:.1f}" '
            f'stroke="#2f8f5b" stroke-dasharray="6 3" stroke-width="1.6"/>'
        )
        parts.append(
            f'<text x="{width - pad_r}" y="{yp + 12:.1f}" text-anchor="end" fill="#2f8f5b">'
            f'<tspan data-i18n="legend.proposed">navrhovaná MRK</tspan> {_group_num(float(proposed), 0)} kW</text>'
        )
    parts.append(
        f'<rect x="{pad_l}" y="{height - 14}" width="11" height="11" fill="{COLOUR_WITHOUT}"/>'
        f'<text x="{pad_l + 16}" y="{height - 4}" fill="#14181f" data-i18n="legend.peak.without">Špička bez systému</text>'
        f'<rect x="{pad_l + 200}" y="{height - 14}" width="11" height="11" fill="{COLOUR_WITH}"/>'
        f'<text x="{pad_l + 216}" y="{height - 4}" fill="#14181f" data-i18n="legend.peak.with">Špička so systémom</text>'
    )
    parts.append("</svg>")
    return (
        '<div class="panel"><figure>'
        + "".join(parts)
        + "</figure>"
        '<figcaption data-i18n="caption.peaks">Každý stĺpec je najvyšší 15-minútový nákup zo siete v danom mesiaci. '
        "Ak modré stĺpce ostávajú pod zelenou čiarkovanou čiarou, zmluvnú MRK je možné znížiť "
        "a stále pokryť špičku po nasadení FVE a batérie.</figcaption></div>"
    )


def render_co2(co2: dict[str, Any] | None) -> str:
    data = co2 or {}
    if not is_number(data.get("t_co2")):
        return ""
    return (
        '<div class="panel">'
        '<div class="cards" style="margin-bottom:12px">'
        + card(
            "Úspora CO₂ za obdobie",
            fmt_num(data.get("t_co2"), 2, "t"),
            note="",
            tone="good",
            hint="Vyhnutá elektrina zo siete × emisný faktor. Nie je to LCA výroby panelov.",
            i18n="co2.period",
            i18n_tip="co2.period.tip",
            note_html=copy_el(
                f"z {fmt_num(data.get('saved_kwh') / 1000.0, 1, 'MWh') if is_number(data.get('saved_kwh')) else '—'} menej nákupu zo siete",
                f"{EN['co2.from']} {fmt_num(data.get('saved_kwh') / 1000.0, 1, 'MWh') if is_number(data.get('saved_kwh')) else '—'} {EN['co2.less_grid']}",
            ),
        )
        + card(
            "Emisný faktor",
            fmt_num(data.get("kg_co2_per_kwh"), 3, "kg/kWh"),
            "EEA, Slovensko",
            hint="Intenzita skleníkových plynov pri výrobe elektriny, ktorú nahrádzate nákupom zo siete.",
            i18n="co2.factor",
            i18n_note="co2.factor.note",
            i18n_tip="co2.factor.tip",
        )
        + "</div>"
        f"<div class='note-block'>{copy_el(data.get('method') or '', EN['co2.method'])}</div></div>"
    )


def render_timestep_strip(timestep: dict[str, Any] | None) -> str:
    data = timestep or {}
    minutes = data.get("step_minutes")
    if minutes is None and is_number(data.get("step_hours")):
        minutes = int(round(float(data["step_hours"]) * 60.0))
    if not is_number(minutes):
        return ""
    minutes = int(minutes)
    intervals = data.get("intervals")
    count = f" ({fmt_num(intervals, 0)} intervalov)" if is_number(intervals) else ""
    count_en = f" ({fmt_num(intervals, 0)} intervals)" if is_number(intervals) else ""
    extra_sk = (
        " Štandardný slovenský elektromer pre firmy je 15-minútový, preto sedí tarifa aj MRK."
        if minutes == 15
        else ""
    )
    extra_en = (
        " A standard Slovak commercial meter is 15-minute, so the tariff and MRK line up."
        if minutes == 15
        else ""
    )
    body_sk = (
        f"Odber, ceny, FVE aj batéria v kroku meracieho súboru: "
        f"<strong>{minutes} min</strong>{count}.{extra_sk}"
    )
    body_en = (
        f"Load, prices, PV and battery on the meter-file interval: "
        f"<strong>{minutes} min</strong>{count_en}.{extra_en}"
    )
    badge = f"{minutes} min"
    return (
        '<div class="meta-strip">'
        f'<span class="meta-k">{esc(badge)}</span>'
        + copy_el(body_sk, body_en, html=True, tag="p")
        + "</div>"
    )


COLOUR_PRICE = "#1a6a8c"
COLOUR_PRICE_W = "#b4761e"


def _axis_span(values: Sequence[float]) -> tuple[float, float]:
    nums = [float(v) for v in values if is_number(v)]
    if not nums:
        return 0.0, 1.0
    lo, hi = min(nums), max(nums)
    if lo == hi:
        pad = abs(lo) * 0.15 or 10.0
        return lo - pad, hi + pad
    span = hi - lo
    return lo - 0.08 * span, hi + 0.08 * span


def _y_of(value: float, lo: float, hi: float, pad_t: float, plot_h: float) -> float:
    return pad_t + plot_h * (1 - (value - lo) / (hi - lo))


def _price_bars(labels: list[str], mean: list[float], weighted: list[float], *, title: str) -> str:
    n = min(len(labels), len(mean), len(weighted))
    if n == 0:
        return ""
    labels, mean, weighted = labels[:n], mean[:n], weighted[:n]
    lo, hi = _axis_span(mean + weighted)
    width, height = 1080, 280
    pad_l, pad_r, pad_t, pad_b = 72, 16, 28, 48
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    group = plot_w / n
    bar_w = min(20.0, group * 0.32)
    zero_y = _y_of(0.0, lo, hi, pad_t, plot_h) if lo < 0 < hi else pad_t + plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="{esc(title)}" style="font: 11px sans-serif">'
        f'<text x="{pad_l}" y="16" font-size="13" font-weight="600" fill="#14181f" data-i18n="chart.price.month">{esc(title)}</text>'
    ]
    for k in range(5):
        value = hi - (hi - lo) * k / 4
        y = pad_t + plot_h * k / 4
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#e6e9ee"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#5c6675">'
            f"{value:,.0f}</text>".replace(",", " ")
        )
    if lo < 0 < hi:
        parts.append(
            f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{width - pad_r}" y2="{zero_y:.1f}" '
            f'stroke="#9aa3ad" stroke-dasharray="3 3"/>'
        )
    for i, (label, a, b) in enumerate(zip(labels, mean, weighted)):
        x0 = pad_l + i * group + group / 2
        ya = _y_of(a, lo, hi, pad_t, plot_h)
        yb = _y_of(b, lo, hi, pad_t, plot_h)
        parts.append(
            f'<rect x="{x0 - bar_w - 1:.1f}" y="{min(ya, zero_y):.1f}" width="{bar_w:.1f}" '
            f'height="{abs(zero_y - ya):.1f}" fill="{COLOUR_PRICE}">'
            f"<title>{esc(label)} priemer: {a:,.1f} €/MWh</title></rect>".replace(",", " ")
        )
        parts.append(
            f'<rect x="{x0 + 1:.1f}" y="{min(yb, zero_y):.1f}" width="{bar_w:.1f}" '
            f'height="{abs(zero_y - yb):.1f}" fill="{COLOUR_PRICE_W}">'
            f"<title>{esc(label)} vážený: {b:,.1f} €/MWh</title></rect>".replace(",", " ")
        )
        parts.append(
            f'<text x="{x0:.1f}" y="{height - 28}" text-anchor="middle" fill="#5c6675">{esc(label)}</text>'
        )
    parts.append(
        f'<rect x="{pad_l}" y="{height - 14}" width="11" height="11" fill="{COLOUR_PRICE}"/>'
        f'<text x="{pad_l + 16}" y="{height - 4}" fill="#14181f" data-i18n="legend.mean">Jednoduchý priemer</text>'
        f'<rect x="{pad_l + 200}" y="{height - 14}" width="11" height="11" fill="{COLOUR_PRICE_W}"/>'
        f'<text x="{pad_l + 216}" y="{height - 4}" fill="#14181f" data-i18n="legend.weighted">Vážený odberom</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _price_line(labels: list[str], values: list[float], *, title: str) -> str:
    n = min(len(labels), len(values))
    if n < 2:
        return ""
    labels, values = labels[:n], [float(v) if is_number(v) else 0.0 for v in values[:n]]
    lo, hi = _axis_span(values)
    width, height = 1080, 240
    pad_l, pad_r, pad_t, pad_b = 72, 16, 28, 36
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    pts = []
    for i, value in enumerate(values):
        x = pad_l + plot_w * i / (n - 1)
        y = _y_of(value, lo, hi, pad_t, plot_h)
        pts.append((x, y))
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
        f'aria-label="{esc(title)}" style="font: 11px sans-serif; margin-top:12px">'
        f'<text x="{pad_l}" y="16" font-size="13" font-weight="600" fill="#14181f" data-i18n="chart.price.hour">{esc(title)}</text>'
    ]
    for k in range(5):
        value = hi - (hi - lo) * k / 4
        y = pad_t + plot_h * k / 4
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#e6e9ee"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#5c6675">'
            f"{value:,.0f}</text>".replace(",", " ")
        )
    if lo < 0 < hi:
        zy = _y_of(0.0, lo, hi, pad_t, plot_h)
        parts.append(
            f'<line x1="{pad_l}" y1="{zy:.1f}" x2="{width - pad_r}" y2="{zy:.1f}" '
            f'stroke="#9aa3ad" stroke-dasharray="3 3"/>'
        )
    parts.append(f'<polyline fill="none" stroke="{COLOUR_PRICE}" stroke-width="2" points="{line}"/>')
    step = max(1, n // 8)
    for i, (label, (x, _)) in enumerate(zip(labels, pts)):
        if i % step == 0 or i == n - 1:
            parts.append(
                f'<text x="{x:.1f}" y="{height - 8}" text-anchor="middle" fill="#5c6675">{esc(label)}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def render_prices(prices: dict[str, Any] | None) -> str:
    data = prices or {}
    PRICE_I18N = {
        "okte_dam": ("prices.src.okte", "prices.src.okte.detail"),
        "load_csv": ("prices.src.load", "prices.src.load.detail"),
        "prices_csv": ("prices.src.file", "prices.src.file.detail"),
    }
    src_key = str(data.get("source") or "")
    label_i18n, detail_i18n = PRICE_I18N.get(src_key, ("prices.src.merged", "prices.src.merged.detail"))
    if not data.get("available"):
        return (
            '<div class="panel"><span class="badge warn" data-i18n="prices.missing">Ceny sa nezobrazili</span>'
            f"{copy_el(data.get('source_detail') or 'Zlúčený profil odberu a cien chýba.', EN['prices.src.merged.detail'])}</div>"
        )
    p10_p50_p90 = data.get("p10_p50_p90_eur_per_mwh") or [None, None, None]
    while len(p10_p50_p90) < 3:
        p10_p50_p90.append(None)
    cards = "".join(
        [
            card(
                "Priemerná cena",
                fmt_mwh(data.get("mean_eur_per_mwh")),
                "Jednoduchý priemer intervalu",
                hint="Priemer komoditnej ceny (DAM) cez všetky intervaly. Nezahŕňa distribúciu.",
                i18n="prices.mean",
                i18n_note="prices.mean.note",
                i18n_tip="prices.mean.tip",
            ),
            card(
                "Vážená odberom",
                fmt_mwh(data.get("weighted_mean_eur_per_mwh")),
                "Cena, ktorú závod skutočne platí",
                hint="Priemer vážený odberom. Vyšší, ak závod berie viac v drahých hodinách.",
                i18n="prices.weighted",
                i18n_note="prices.weighted.note",
                i18n_tip="prices.weighted.tip",
            ),
            card(
                "Minimum / maximum",
                f"{fmt_mwh(data.get('min_eur_per_mwh'))} / {fmt_mwh(data.get('max_eur_per_mwh'))}",
                hint="Najnižšia a najvyššia komoditná cena v období. Ukazuje rozptyl na trhu.",
                i18n="prices.minmax",
                i18n_tip="prices.minmax.tip",
            ),
            card(
                "P10 / P50 / P90",
                f"{fmt_mwh(p10_p50_p90[0])} / {fmt_mwh(p10_p50_p90[1])} / {fmt_mwh(p10_p50_p90[2])}",
                hint=(
                    "Percentily hodinových DAM cien v tomto období, nie Monte Carlo úspor. "
                    "P10 = lacná hodina (len 10 % intervalov je lacnejších). "
                    "P50 = stred. P90 = drahá hodina (len 10 % intervalov je drahších). "
                    "P90 cien tu nie je to isté ako úspora pri P90 v zhrnutí."
                ),
                i18n="prices.pct",
                i18n_tip="prices.pct.tip",
            ),
        ]
    )
    source_badge = "ok" if data.get("source") in {"okte_dam", "load_csv", "prices_csv"} else "warn"
    monthly = data.get("monthly") or {}
    hourly = data.get("hourly") or {}
    month_rows = ""
    for label, mean, weighted in zip(
        monthly.get("x") or [],
        monthly.get("mean_eur_per_mwh") or [],
        monthly.get("weighted_eur_per_mwh") or [],
    ):
        month_rows += (
            f"<tr><td>{esc(label)}</td><td class='num'>{esc(fmt_mwh(mean))}</td>"
            f"<td class='num'>{esc(fmt_mwh(weighted))}</td></tr>"
        )
    table = ""
    if month_rows:
        table = (
            f"<table><thead><tr><th>{labeled('Mesiac', 'Kalendárny mesiac v vstupnom období.', i18n='prices.month', i18n_tip='prices.month.tip')}</th>"
            f"<th class='num'>{labeled('Priemer', 'Jednoduchý priemer DAM v mesiaci, v €/MWh.', i18n='prices.avg', i18n_tip='prices.avg.tip')}</th>"
            f"<th class='num'>{labeled('Vážený odberom', 'Priemer DAM vážený odberom závodu v mesiaci.', i18n='prices.wavg', i18n_tip='prices.wavg.tip')}</th>"
            "</tr></thead>"
            f"<tbody>{month_rows}</tbody></table>"
        )
    return (
        '<div class="panel">'
        f'<span class="badge {source_badge}" data-i18n="{esc(label_i18n)}">{esc(data.get("source_label") or "Ceny")}</span>'
        f'<div class="source-box" style="margin-top:12px">'
        f'<strong data-i18n="{esc(label_i18n)}">{esc(data.get("source_label"))}</strong>'
        f'<span data-i18n="{esc(detail_i18n)}">{esc(data.get("source_detail"))}</span>'
        f"<div class='note-block'><span data-i18n='prices.period'>Obdobie</span> {esc(fmt_dt(data.get('period_start')))} – "
        f"{esc(fmt_dt(data.get('period_end')))} · {esc(fmt_num(data.get('priced_rows'), 0))} "
        f"<span data-i18n='prices.priced'>intervalov s cenou</span> · "
        f"<span data-i18n='prices.coverage_of'>pokrytie</span> {esc(fmt_num(data.get('coverage_pct'), 1, '%'))}</div>"
        "</div>"
        f'<div class="cards">{cards}</div>'
        + _price_bars(
            list(monthly.get("x") or []),
            list(monthly.get("mean_eur_per_mwh") or []),
            list(monthly.get("weighted_eur_per_mwh") or []),
            title="Mesačná cena elektriny (€/MWh)",
        )
        + _price_line(
            list(hourly.get("x") or []),
            list(hourly.get("mean_eur_per_mwh") or []),
            title="Priemerný denný profil ceny (€/MWh)",
        )
        + (f'<div style="margin-top:16px">{table}</div>' if table else "")
        + '<figcaption data-i18n="caption.prices">Jednotka €/MWh je trhová kotácia denného trhu (komodita). '
        "K nákupu zo siete sa ešte pripočíta poplatok za distribúciu zadaný vo vstupe. "
        "Vážený priemer zohľadňuje, v ktorých hodinách závod skutočne odoberá — to je "
        "relevantnejšie číslo pre opex než jednoduchý priemer.</figcaption>"
        "</div>"
    )


def _hw_name(*parts: Any) -> str:
    return " ".join(str(p).strip() for p in parts if p not in (None, "")).strip() or "—"


def render_recommended_hardware(
    hardware: dict[str, Any] | None,
    ranking: dict[str, Any] | None,
    *,
    energy_kwh: Any = None,
    installed_kwp: Any = None,
) -> str:
    hw = hardware or {}
    pv = ((hw.get("pv") or {}).get("selected_rank_1") or {})
    if not pv:
        pv = ((ranking or {}).get("top_recommendations") or [{}])[0] or {}
    inv = hw.get("inverter") or {}
    if isinstance(inv.get("selected_rank_1"), dict):
        inv = inv["selected_rank_1"]
    bat = hw.get("battery") or {}

    pv_name = _hw_name(pv.get("manufacturer"), pv.get("model"))
    pv_notes: list[tuple[str, str]] = []
    if is_number(pv.get("power_wp")):
        n = fmt_num(pv.get("power_wp"), 0, "Wp")
        pv_notes.append((f"{n} modul", f"{n} module"))
    count = pv.get("module_count") or pv.get("module_count_estimate")
    if is_number(count):
        n = fmt_num(count, 0)
        pv_notes.append((f"{n} ks", f"{n} pcs"))
    if is_number(pv.get("efficiency_pct")):
        n = fmt_num(pv.get("efficiency_pct"), 1, "%")
        pv_notes.append((n, n))
    target_kwp = installed_kwp
    if ranking and is_number((ranking or {}).get("installed_kwp_target")):
        target_kwp = ranking.get("installed_kwp_target")
    if is_number(target_kwp) and target_kwp > 0:
        n = fmt_num(target_kwp, 0, "kWp")
        pv_notes.append((f"pre {n}", f"for {n}"))

    inv_name = _hw_name(inv.get("manufacturer"), inv.get("model"))
    inv_notes: list[tuple[str, str]] = []
    if is_number(inv.get("paco_w")):
        n = fmt_num(inv.get("paco_w") / 1000.0, 1, "kW")
        inv_notes.append((f"{n} AC / ks", f"{n} AC / unit"))
    if is_number(inv.get("count")):
        n = fmt_num(inv.get("count"), 0)
        inv_notes.append((f"{n} ks", f"{n} pcs"))
    if is_number(inv.get("total_ac_kw")):
        n = fmt_num(inv.get("total_ac_kw"), 0, "kW")
        inv_notes.append((f"spolu {n} AC", f"total {n} AC"))
    if is_number(inv.get("dc_ac_ratio")):
        n = fmt_num(inv.get("dc_ac_ratio"), 2)
        inv_notes.append((f"DC/AC {n}", f"DC/AC {n}"))
    if inv_name == "—":
        inv_notes = [(
            "V katalógu sa nenašiel vhodný menič pre túto veľkosť.",
            EN["hw.noinv"],
        )]

    no_battery = not is_number(energy_kwh) or float(energy_kwh) <= 1e-6
    if no_battery:
        bat_name_html = copy_el("Bez batérie", EN["hw.nobat"])
        bat_notes = [(EN["hw.nobat.note"] and "Návrh má 0 kWh, preto sa z katalógu nevyberá úložisko.", EN["hw.nobat.note"])]
    elif bat:
        bat_name_html = esc(_hw_name(bat.get("manufacturer"), bat.get("product_line") or bat.get("model")))
        bat_notes = []
        if is_number(bat.get("nominal_kwh")):
            n = fmt_num(bat.get("nominal_kwh"), 0, "kWh")
            bat_notes.append((n, n))
        if is_number(bat.get("max_power_kw")):
            n = fmt_num(bat.get("max_power_kw"), 0, "kW")
            bat_notes.append((n, n))
        if bat.get("chemistry"):
            chem = str(bat.get("chemistry"))
            bat_notes.append((chem, chem))
        if is_number(energy_kwh):
            n = fmt_num(energy_kwh, 0, "kWh")
            bat_notes.append((f"cieľ {n}", f"target {n}"))
    else:
        bat_name_html = copy_el("Katalóg batérií prázdny", EN["hw.emptybat"])
        bat_notes = [(
            f"Cieľ je {fmt_num(energy_kwh, 0, 'kWh')}, ale v katalógu nie je použiteľný produkt.",
            f"The target is {fmt_num(energy_kwh, 0, 'kWh')}, but the catalogue has no usable product.",
        )]

    def hw_card(title: str, hint: str, name_html: str, notes: list[tuple[str, str]], i18n: str, i18n_tip: str) -> str:
        note_html = "".join(
            f'<div class="note">{copy_el(sk, en)}</div>' for sk, en in notes if sk
        )
        return (
            f'<div class="hw-stat"><div class="k">{labeled(title, hint, i18n=i18n, i18n_tip=i18n_tip)}</div>'
            f'<div class="v">{name_html}</div>{note_html}</div>'
        )

    return (
        '<div class="panel">'
        '<div class="hw-strip">'
        + hw_card(
            "Modul",
            "Najlepší fotovoltický modul z katalógu pre túto lokalitu a navrhnutý výkon.",
            esc(pv_name),
            pv_notes,
            "hw.module",
            "hw.module.tip",
        )
        + hw_card(
            "Menič",
            "Katalógový menič s cieľovým pomerom DC/AC okolo 1,2 a najmenším prebytkom AC výkonu.",
            esc(inv_name),
            inv_notes,
            "hw.inverter",
            "hw.inverter.tip",
        )
        + hw_card(
            "Batéria",
            "Katalógové úložisko najbližšie k navrhnutej kapacite. Pri 0 kWh sa nevyberá.",
            bat_name_html,
            bat_notes,
            "hw.battery",
            "hw.battery.tip",
        )
        + "</div>"
        '<div class="note-block" data-i18n="hw.note">Orientačný výber z katalógu pre odporúčanú veľkosť. '
        "Pred objednávkou overte dostupnosť, záruku a cenu u dodávateľa.</div></div>"
    )


def render_equipment(ranking: dict[str, Any] | None) -> str:
    ranking = ranking or {}
    items = (
        ranking.get("top_recommendations")
        or ranking.get("top")
        or ranking.get("ranked")
        or []
    )
    if not items:
        return ""
    head = (
        f"<tr><th>{labeled('Výrobca', 'Výrobca fotovoltického modulu z katalógu.', i18n='eq.man', i18n_tip='eq.man.tip')}</th>"
        f"<th>{labeled('Model', 'Typové označenie modulu.', i18n='eq.model', i18n_tip='eq.model.tip')}</th>"
        f"<th class='num'>{labeled('Wp', 'Menovitý výkon jedného modulu v štandardných podmienkach.', i18n='eq.wp', i18n_tip='eq.wp.tip')}</th>"
        f"<th class='num'>{labeled('Účinnosť', 'Účinnosť premeny žiarenia na elektrinu.', i18n='eq.eff', i18n_tip='eq.eff.tip')}</th>"
        f"<th class='num'>{labeled('€/Wp', 'Orientačná cena modulu na watt špičkového výkonu.', i18n='eq.eur', i18n_tip='eq.eur.tip')}</th>"
        f"<th class='num'>{labeled('Moduly', 'Odhadovaný počet modulov pre navrhnutú FVE.', i18n='eq.count', i18n_tip='eq.count.tip')}</th>"
        f"<th class='num'>{labeled('Plocha', 'Odhadovaná plocha poľa pre túto sadu modulov.', i18n='eq.area', i18n_tip='eq.area.tip')}</th>"
        f"<th class='num'>{labeled('Skóre', 'Poradie pre túto lokalitu: hustota, cena, tienenie, účinnosť.', i18n='eq.score', i18n_tip='eq.score.tip')}</th></tr>"
    )
    rows = []
    for i, m in enumerate(items[:8]):
        rows.append(
            "<tr{cls}><td>{man}</td><td>{mod}</td><td class='num'>{wp}</td>"
            "<td class='num'>{eff}</td><td class='num'>{eur}</td>"
            "<td class='num'>{cnt}</td><td class='num'>{area}</td>"
            "<td class='num'>{score}</td></tr>".format(
                cls=' class="best"' if i == 0 else "",
                man=esc(m.get("manufacturer")),
                mod=esc(m.get("model")),
                wp=fmt_num(m.get("power_wp"), 0),
                eff=fmt_num(m.get("efficiency_pct"), 1, "%"),
                eur=fmt_num(m.get("eur_per_wp"), 3),
                cnt=fmt_num(m.get("module_count_estimate"), 0),
                area=fmt_num(m.get("array_area_m2_estimate"), 0, "m²"),
                score=fmt_num(m.get("score"), 3),
            )
        )
    return (
        f'<div class="panel"><table>{head}{"".join(rows)}</table>'
        '<div class="note-block" data-i18n="eq.note">Zoradené pre túto lokalitu podľa energetickej hustoty, '
        "ceny za watt, odolnosti voči tieneniu a účinnosti. Zvýraznený riadok je najlepšia zhoda.</div></div>"
    )


def render_heatmap_table(heatmap: dict[str, Any]) -> str:
    """Kept for callers; the interactive panel already shows this comparison."""
    return ""


_HEATMAP_JS = r"""
(function () {
  function init() {
  var node = document.getElementById("heatmap-data");
  if (!node) return;
  var data;
  try { data = JSON.parse(node.textContent); } catch (err) { return; }
  var grids = data.grids || {};
  var rec = data.recommended || { i: 0, j: 0 };
  var cell = data.cell || { w: 62, h: 34, left: 132, top: 22 };

  function tip(text) {
    if (!text) return "";
    return '<span class="tip" tabindex="0">?<span class="tip-text">' + text + '</span></span>';
  }
  function labeled(label, hint) { return label + tip(hint || ""); }
  function pack() {
    var node = document.getElementById("i18n-pack");
    if (!node) return {};
    try {
      var all = JSON.parse(node.textContent) || {};
      return all[window.UC32_LANG || "sk"] || {};
    } catch (err) { return {}; }
  }
  function t(key, fallback) {
    if ((window.UC32_LANG || "sk") !== "en") return fallback;
    return pack()[key] || fallback;
  }
  function en() { return (window.UC32_LANG || "sk") === "en"; }
  var lastI = rec.i, lastJ = rec.j;
  function num(v) { return typeof v === "number" && isFinite(v); }
  function groupNum(v, d) {
    if (!num(v)) return "—";
    var neg = v < 0 ? "-" : "";
    var abs = Math.abs(v);
    var parts = (d ? abs.toFixed(d) : String(Math.round(abs))).split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    var dec = (window.UC32_LANG === "en") ? "." : ",";
    return neg + (d ? parts[0] + dec + parts[1] : parts[0]);
  }
  function money(v) {
    return num(v) ? groupNum(v, 0) + " €" : "—";
  }
  function years(v) { return num(v) ? groupNum(v, 1) + (window.UC32_LANG === "en" ? " y" : " r") : "—"; }
  function pct(v) { return num(v) ? groupNum(v, 1) + " %" : "—"; }
  function kw(v, unit) { return num(v) ? groupNum(Math.round(v), 0) + " " + unit : "—"; }
  function dpbText(v) { return num(v) ? years(v) : (window.UC32_LANG === "en" ? "Does not pay back when discounted" : "Nevráti sa pri diskontovaní"); }
  function cyclesText(kwh, cycles) {
    if (!num(kwh) || kwh <= 0) return window.UC32_LANG === "en" ? "No battery" : "Bez batérie";
    return num(cycles) ? groupNum(Math.round(cycles), 0) : "—";
  }
  function lifeText(kwh, life) {
    if (!num(kwh) || kwh <= 0) return window.UC32_LANG === "en" ? "Not applicable" : "Neaplikuje sa";
    return years(life);
  }
  function pbTone(v) {
    if (!num(v)) return "";
    if (v <= 8) return "good";
    if (v <= 12) return "warn";
    return "bad";
  }
  function npvTone(v) { return num(v) && v > 0 ? "good" : "bad"; }
    function cellVal(key, i, j) {
    var g = grids[key] || [];
    return (g[j] && g[j][i] != null) ? g[j][i] : null;
  }
  function inferEurKwp() {
    if (num(data.eur_per_kwp) && data.eur_per_kwp > 0) return data.eur_per_kwp;
    var pvs = data.pv_kwp || [];
    var bats = data.battery_kwh || [];
    for (var j = 0; j < bats.length; j++) {
      if (!num(bats[j]) || bats[j] > 1e-9) continue;
      for (var i = 0; i < pvs.length; i++) {
        if (!num(pvs[i]) || pvs[i] <= 1e-9) continue;
        var c = cellVal("total_capex_eur", i, j);
        if (num(c) && c > 0) return c / pvs[i];
      }
    }
    return null;
  }
  function inferEurKwh() {
    if (num(data.eur_per_kwh) && data.eur_per_kwh > 0) return data.eur_per_kwh;
    var pvs = data.pv_kwp || [];
    var bats = data.battery_kwh || [];
    for (var i = 0; i < pvs.length; i++) {
      if (!num(pvs[i]) || pvs[i] > 1e-9) continue;
      for (var j = 0; j < bats.length; j++) {
        if (!num(bats[j]) || bats[j] <= 1e-9) continue;
        var c = cellVal("total_capex_eur", i, j);
        if (num(c) && c > 0) return c / bats[j];
      }
    }
    for (var i2 = 0; i2 < pvs.length; i2++) {
      for (var a = 0; a < bats.length; a++) {
        for (var b = a + 1; b < bats.length; b++) {
          if (!num(bats[a]) || !num(bats[b]) || Math.abs(bats[b] - bats[a]) <= 1e-9) continue;
          var ca = cellVal("total_capex_eur", i2, a);
          var cb = cellVal("total_capex_eur", i2, b);
          if (!num(ca) || !num(cb)) continue;
          var rate = (cb - ca) / (bats[b] - bats[a]);
          if (rate > 0) return rate;
        }
      }
    }
    return null;
  }
  function paintCapexBreak(kwp, kwh, capex) {
    var el = document.getElementById("hm-capex-break");
    if (!el) return;
    var eurKwp = inferEurKwp();
    var eurKwh = inferEurKwh();
    var pvAmt = (num(kwp) && num(eurKwp)) ? kwp * eurKwp : null;
    var batAmt = (num(kwh) && num(eurKwh)) ? kwh * eurKwh : ((num(kwh) && kwh <= 0) ? 0 : null);
    var pvEq = (num(kwp) && num(eurKwp))
      ? (kw(kwp, "kWp") + " × " + money(eurKwp) + "/kWp")
      : "—";
    var batEq = (num(kwh) && num(eurKwh))
      ? (kw(kwh, "kWh") + " × " + money(eurKwh) + "/kWh")
      : "—";
    var total = num(capex) ? capex : ((num(pvAmt) ? pvAmt : 0) + (num(batAmt) ? batAmt : 0));
    el.innerHTML =
      '<div class="eq-head">' + labeled(
        t("hm.capex.eq", "Výpočet CAPEX"),
        t("hm.capex.eq.tip", "Jednorazová investícia: kWp × €/kWp z formulára + kWh × €/kWh z formulára. Platí aj keď odporúčanie bolo 0 kWh.")
      ) + "</div><table><tbody>" +
      "<tr><td>" + t("capex.pv", "FVE") + "</td><td class='eq'>" + pvEq + "</td><td class='num'>" + money(pvAmt) + "</td></tr>" +
      "<tr><td>" + t("capex.battery", "Batéria") + "</td><td class='eq'>" + batEq + "</td><td class='num'>" + money(batAmt) + "</td></tr>" +
      "<tr class='total'><td>" + t("capex.total", "Spolu CAPEX") + "</td><td class='eq'>" + t("capex.together", "FVE + batéria") + "</td><td class='num'>" + money(total) + "</td></tr>" +
      "</tbody></table>";
  }
  function setCard(key, text, note, tone) {
    var el = document.querySelector('.card[data-live="' + key + '"]');
    if (!el) return;
    var val = el.querySelector(".value");
    if (val) {
      val.textContent = text;
      val.className = "value" + (tone ? " " + tone : "");
    }
    var n = el.querySelector(".note");
    if (n && note != null) n.textContent = note;
  }
  function setDetail(key, text) {
    var el = document.querySelector('#live-details [data-live="' + key + '"] td.num');
    if (el) el.textContent = text;
  }
  function restoreRecOnly() {
    document.querySelectorAll("[data-rec-only='1']").forEach(function (el) {
      var val = el.querySelector(".value") || el.querySelector("td.num");
      if (!val) return;
      val.textContent = el.getAttribute("data-original") || "—";
      if (el.classList.contains("card")) {
        var tone = el.getAttribute("data-original-tone") || "";
        val.className = "value" + (tone ? " " + tone : "");
        var n = el.querySelector(".note");
        if (n) {
          n.textContent = el.getAttribute("data-original-note") || "";
          var key = n.getAttribute("data-i18n");
          if (key && en()) {
            var tr = pack()[key];
            if (tr) n.textContent = tr;
          }
        }
      }
    });
  }
  function dashRecOnly() {
    document.querySelectorAll("[data-rec-only='1']").forEach(function (el) {
      var val = el.querySelector(".value") || el.querySelector("td.num");
      if (!val) return;
      val.textContent = "—";
      if (el.classList.contains("card")) {
        val.className = "value";
        var n = el.querySelector(".note");
        if (n) n.textContent = t("d.reconly", "Len pre odporúčanú veľkosť");
      }
    });
  }
  function paint(i, j) {
    var kwp = (data.pv_kwp || [])[i];
    var kwh = (data.battery_kwh || [])[j];
    var sav = cellVal("annual_savings_eur", i, j);
    var npv = cellVal("npv_eur", i, j);
    var pb = cellVal("simple_payback_years", i, j);
    var dpb = cellVal("discounted_payback_years", i, j);
    var capex = cellVal("total_capex_eur", i, j);
    var sc = cellVal("self_consumption_pct", i, j);
    var opexToday = cellVal("operating_cost_baseline_eur", i, j);
    var opexPlant = cellVal("operating_cost_optimized_eur", i, j);
    var cycles = cellVal("battery_cycles_per_year", i, j);
    var life = cellVal("battery_life_years", i, j);
    var cash = cellVal("cashflow_after_om_eur", i, j);
    var isRec = i === rec.i && j === rec.j;
    lastI = i; lastJ = j;
    var recSav = cellVal("annual_savings_eur", rec.i, rec.j);
    var recNpv = cellVal("npv_eur", rec.i, rec.j);
    var recPb = cellVal("simple_payback_years", rec.i, rec.j);
    var recCapex = cellVal("total_capex_eur", rec.i, rec.j);
    var sizeNote = en()
      ? ("Simulation " + kw(kwp, "kWp") + " PV + " + kw(kwh, "kWh") + " battery")
      : ("Simulácia " + kw(kwp, "kWp") + " FVE + " + kw(kwh, "kWh") + " batéria");
    var cfg = document.getElementById("live-config");
    if (cfg) cfg.textContent = sizeNote;
    var title = document.getElementById("hm-title");
    if (title) title.innerHTML = labeled(
      t("hm.selected", "Vybraná konfigurácia") + ": " + kw(kwp, "kWp") + (en() ? " PV + " : " FVE + ") + kw(kwh, "kWh") + (en() ? " battery" : " batéria"),
      t("hm.selected.tip", "Kliknite na bunku mapy. Karty a Podrobnosti sa prepíšu na túto veľkosť.")
    );
    var badge = document.getElementById("hm-badge");
    var objLabel = t(data.objective_i18n || "", data.objective_label || (en() ? "selected objective" : "vybraný cieľ"));
    if (en() && data.objective_label_en) objLabel = data.objective_label_en;
    if (badge) {
      badge.className = isRec ? "note hm-badge-rec" : "note";
      badge.innerHTML = isRec
      ? labeled(t("hm.recommendation", "Odporúčanie") + ": " + objLabel, t("hm.rec.tip", "Veľkosť, ktorú vybral cieľ návrhu. Červený rámček na mape."))
      : labeled(t("hm.compare", "Porovnanie voči odporúčaniu"), t("hm.compare.tip", "Táto bunka nie je odporúčanie. Tabuľka nižšie ukáže rozdiel."));
    }
    var cards = [
      [t("hm.pv", "FVE"), kw(kwp, "kWp"), "", t("hm.pv.tip", "Inštalovaný výkon fotovoltiky v tejto bunke mapy.")],
      [t("hm.bat", "Batéria"), kw(kwh, "kWh"), "", t("hm.bat.tip", "Kapacita úložiska v tejto bunke. 0 kWh = len FVE.")],
      [t("hm.sav", "Ročná úspora"), money(sav), t("hm.sav.note", "Simulácia tejto veľkosti"), t("hm.sav.tip", "Čistý ročný cashflow po O&M. Účet za elektrinu klesne o viac; tu je už odpočítaná prevádzka.")],
      [t("hm.capex", "Celkový CAPEX"), money(capex), t("hm.capex.note", "Investícia pri tejto veľkosti"), t("hm.capex.tip", "Jednorazová investícia: kWp × €/kWp + kWh × €/kWh z formulára, pri tejto bunke mapy.")],
      [t("hm.pb", "Jednoduchá návratnosť"), years(pb), "", t("hm.pb.tip", "__PAYBACK_TIP_SK__")],
      [t("hm.npv", "Čistá súčasná hodnota"), money(npv), t("hm.npv.note", "Za dobu amortizácie"), t("hm.npv.tip", "Súčasná hodnota úspor mínus investícia. Kladné NPV investíciu oplatí.")],
      [t("hm.sc", "Vlastná spotreba"), pct(sc), t("hm.sc.note", "Podiel vyrobenej energie spotrebovanej na mieste"), t("hm.sc.tip", "Koľko výroby FVE sa spotrebuje na objekte namiesto predaja do siete. Zatiaľ bez batérie.")]
    ];
    var hmCards = document.getElementById("hm-cards");
    if (hmCards) hmCards.innerHTML = cards.map(function (c) {
      return '<div class="card"><div class="label">' + labeled(c[0], c[3]) + '</div><div class="value">' + c[1] +
        '</div>' + (c[2] ? '<div class="note">' + c[2] + '</div>' : '') + '</div>';
    }).join("");
    paintCapexBreak(kwp, kwh, capex);
    function row(label, hint, a, b, d) {
      var tone = d && d.cls ? ' class="num value ' + d.cls + '"' : ' class="num"';
      return "<tr><td>" + labeled(label, hint) + "</td><td class='num'>" + a + "</td><td class='num'>" + b +
        "</td><td" + tone + ">" + (d ? d.text : "—") + "</td></tr>";
    }
    var dSav = (!isRec && num(sav) && num(recSav))
      ? { text: ((sav - recSav) >= 0 ? "+" : "") + money(sav - recSav), cls: sav >= recSav ? "good" : "bad" }
      : { text: "—", cls: "" };
    var dCap = (!isRec && num(capex) && num(recCapex))
      ? { text: ((capex - recCapex) >= 0 ? "+" : "") + money(capex - recCapex), cls: capex <= recCapex ? "good" : "" }
      : { text: "—", cls: "" };
    var dNpv = (!isRec && num(npv) && num(recNpv))
      ? { text: ((npv - recNpv) >= 0 ? "+" : "") + money(npv - recNpv), cls: npv >= recNpv ? "good" : "bad" }
      : { text: "—", cls: "" };
    var dPb = (!isRec && num(pb) && num(recPb))
      ? { text: ((pb - recPb) >= 0 ? "+" : "") + (pb - recPb).toFixed(1) + (en() ? " y" : " r"), cls: pb <= recPb ? "good" : "bad" }
      : { text: "—", cls: "" };
    var cmp = document.getElementById("hm-compare");
    if (cmp) cmp.innerHTML =
      "<thead><tr><th></th>" +
      "<th class='num'>" + labeled(t("hm.col.sel", "Vybrané"), t("hm.col.sel.tip", "Hodnota pre bunku, na ktorú ste klikli.")) + "</th>" +
      "<th class='num'>" + labeled(t("hm.col.rec", "Odporúčané"), t("hm.col.rec.tip", "Hodnota pre veľkosť, ktorú vybral cieľ návrhu.")) + "</th>" +
      "<th class='num'>" + labeled(t("hm.col.diff", "Rozdiel"), t("hm.col.diff.tip", "Vybrané mínus odporúčané. Zelená je pre vás výhodnejšia.")) + "</th></tr></thead><tbody>" +
      row(t("hm.row.bill0", "Účet bez FVE"), t("hm.row.bill0.tip", "Ročný nákup zo siete a MRK bez systému."), money(opexToday), money(cellVal("operating_cost_baseline_eur", rec.i, rec.j)), { text: "—", cls: "" }) +
      row(t("hm.row.bill1", "Účet so systémom"), t("hm.row.bill1.tip", "Rovnaký účet po nasadení vybranej veľkosti."), money(opexPlant), money(cellVal("operating_cost_optimized_eur", rec.i, rec.j)), { text: "—", cls: "" }) +
      row(t("hm.row.sav", "Ročná úspora"), t("hm.row.sav.tip", "O koľko klesnú ročné prevádzkové náklady voči stavu bez systému."), money(sav), money(recSav), dSav) +
      row(t("hm.row.capex", "CAPEX"), t("hm.row.capex.tip", "Jednorazová investícia do FVE a batérie."), money(capex), money(recCapex), dCap) +
      row(t("hm.row.npv", "NPV"), t("hm.row.npv.tip", "Čistá súčasná hodnota za dobu amortizácie."), money(npv), money(recNpv), dNpv) +
      row(t("hm.row.pb", "Návratnosť"), t("hm.row.pb.tip", "__PAYBACK_TIP_SK__"), years(pb), years(recPb), dPb) +
      "</tbody>";
    var cursor = document.getElementById("hm-cursor");
    if (cursor) {
      cursor.setAttribute("x", cell.left + i * cell.w);
      cursor.setAttribute("y", cell.top + j * cell.h);
    }
    setCard("savings", money(sav), sizeNote, "good");
    var billToday = document.getElementById("bill-today");
    if (billToday) billToday.textContent = money(opexToday);
    var billPlant = document.getElementById("bill-plant");
    if (billPlant) billPlant.textContent = money(opexPlant);
    var billDelta = document.getElementById("bill-delta");
    if (billDelta) billDelta.textContent = money(sav);
    var billSize = document.getElementById("bill-size");
    if (billSize) billSize.textContent = sizeNote;
    var billNote = document.getElementById("bill-plant-note");
    if (billNote) billNote.textContent = sizeNote || t("bill.with.pv", "Po nasadení systému");
    var billLab = document.getElementById("bill-plant-label");
    if (billLab) {
      var plantTitle = (num(kwh) && kwh > 0) ? t("bill.with.pvbat", "Účet s FVE a batériou") : t("bill.with.pv", "Účet s FVE");
      billLab.innerHTML = labeled(
        plantTitle,
        t("bill.with.tip", "Rovnaký účet po nasadení: menej nákupu zo siete, predaj prebytku a prípadne iné špičky. MRK v tomto čísle ostáva na dnešnej zmluve.")
      );
    }
    setCard("capex", money(capex), t("kpi.capex.note", "FVE, úložisko a inštalácia"), "");
    setCard("payback", years(pb), isRec ? null : t("kpi.payback.note", "Bez diskontovania"), pbTone(pb));
    setCard("npv", money(npv), isRec ? null : t("kpi.npv.note", "Za dobu amortizácie"), npvTone(npv));
    var mrkMeta = data.mrk || {};
    var currentMrk = num(mrkMeta.current_kw) ? mrkMeta.current_kw : null;
    var proposedMrk = cellVal("proposed_mrk_kw", i, j);
    var mrkCut = cellVal("mrk_cut_kw", i, j);
    var mrkSave = cellVal("mrk_savings_annual_eur", i, j);
    var peakAfter = cellVal("peak_after_kw", i, j);
    var canCut = num(mrkCut) && mrkCut > 0.5;
    setCard(
      "rv",
      kw(proposedMrk, "kW"),
      canCut ? ((en() ? "from " : "zo ") + kw(currentMrk, "kW") + ", −" + kw(mrkCut, "kW")) : t("kpi.mrk.note.keep", "Bez zmeny zmluvy"),
      canCut ? "good" : ""
    );
    function setMrk(id, text, good) {
      var el = document.getElementById(id);
      if (!el) return;
      el.textContent = text;
      if (el.classList && el.classList.contains("v")) {
        el.className = "v" + (good ? " good" : "");
      }
    }
    setMrk("mrk-current", kw(currentMrk, "kW"));
    setMrk("mrk-proposed", kw(proposedMrk, "kW"), canCut);
    setMrk("mrk-cut", canCut ? kw(mrkCut, "kW") : "0 kW", canCut);
    setMrk("mrk-save", canCut ? money(mrkSave) : "0 €", canCut);
    setMrk("mrk-peak", kw(peakAfter, "kW"));
    var sizeEl = document.getElementById("mrk-size");
    if (sizeEl) sizeEl.textContent = sizeNote;
    var badge = document.getElementById("mrk-badge");
    if (badge) {
      badge.textContent = canCut ? t("mrk.badge.cut", "Návrh na zníženie zmluvy") : t("mrk.badge.keep", "Zmluvu ponechať");
      badge.className = canCut ? "badge ok" : "badge";
    }
    var copyEl = document.getElementById("mrk-copy");
    if (copyEl) {
      var copy;
      if (!num(currentMrk) || currentMrk <= 0) {
        copy = t("mrk.none", "Zmluvná rezervovaná kapacita nebola zadaná, návrh zmeny MRK preto nie je k dispozícii.");
        if (en()) copy = "Contracted reserved capacity was not entered, so an MRK change proposal is not available.";
      } else if (!num(proposedMrk)) {
        copy = en()
          ? "After deploying the plant we do not yet have enough monthly peaks to propose a new MRK."
          : "Po nasadení systému ešte nemáme dostatok mesačných špičiek na návrh novej MRK.";
      } else if (!canCut) {
        var sizeShort = en()
          ? ((sizeNote || "").replace("Simulation ", "") || "this size")
          : ((sizeNote || "").replace("Simulácia ", "").replace(" + ", " a ").replace(" batéria", " batérie") || "tejto veľkosti");
        copy = en()
          ? ("At " + sizeShort + " the simulation does not cut grid import enough to justify changing the contract. We recommend keeping " + kw(currentMrk, "kW") + ".")
          : ("Pri " + sizeShort + " simulácia nezníži odber zo siete dosť na to, aby sa oplatilo meniť zmluvu. Odporúčame ponechať " + kw(currentMrk, "kW") + ".");
        if (num(peakAfter)) {
          copy += en()
            ? (" The highest peak after the plant is " + kw(peakAfter, "kW") + ".")
            : (" Najvyššia špička po nasadení je " + kw(peakAfter, "kW") + ".");
        }
      } else {
        var sizeShort = en()
          ? ((sizeNote || "").replace("Simulation ", "") || "this size")
          : ((sizeNote || "").replace("Simulácia ", "").replace(" + ", " a ").replace(" batéria", " batérie") || "navrhnutej FVE a batérie");
        copy = en()
          ? ("After deploying " + sizeShort + ", monthly grid peaks fall. We therefore propose cutting contracted MRK from "
            + kw(currentMrk, "kW") + " to " + kw(proposedMrk, "kW") + " (−" + kw(mrkCut, "kW") + ").")
          : ("Po nasadení " + sizeShort + " klesnú mesačné špičky odberu zo siete. Zmluvnú MRK preto navrhujeme znížiť z "
            + kw(currentMrk, "kW") + " na " + kw(proposedMrk, "kW") + " (−" + kw(mrkCut, "kW") + ").");
        if (num(mrkMeta.fee_eur_per_kw_month) && mrkMeta.fee_eur_per_kw_month > 0) {
          copy += en()
            ? (" At " + money(mrkMeta.fee_eur_per_kw_month) + " per kW per month this saves about "
              + money(mrkSave) + " per year on the standing charge, on top of commodity and distribution savings.")
            : (" Pri sadzbe " + money(mrkMeta.fee_eur_per_kw_month) + " za kW mesačne to ušetrí približne "
              + money(mrkSave) + " ročne na fixnom poplatku, navyše k úspore za silovú elektrinu a distribúciu.");
        } else {
          copy += en()
            ? (" This saves about " + money(mrkSave) + " per year on the standing charge.")
            : (" To ušetrí približne " + money(mrkSave) + " ročne na fixnom poplatku.");
        }
        if (num(mrkMeta.safety_margin_pct) && num(peakAfter)) {
          copy += en()
            ? (" The proposal is " + Math.round(mrkMeta.safety_margin_pct) + " % above the highest simulated peak ("
              + kw(peakAfter, "kW") + ") and rounded to 5 kW.")
            : (" Návrh je o " + Math.round(mrkMeta.safety_margin_pct) + " % nad najvyššou simulovanou špičkou ("
              + kw(peakAfter, "kW") + ") a zaokrúhlený na 5 kW.");
        }
        copy += en()
          ? " A contract change with the DSO is not automatic — this is a brief for negotiation."
          : " Zmena zmluvy s PDS nie je automatická — ide o podklad na rokovanie.";
      }
      copyEl.textContent = copy;
    }
    setDetail("opex-today", money(opexToday));
    setDetail("opex-plant", money(opexPlant));
    setDetail("dpb", dpbText(dpb));
    setDetail("cycles", cyclesText(kwh, cycles));
    setDetail("life", lifeText(kwh, life));
    setDetail("cashflow", money(cash));
    var note = document.getElementById("live-details-note");
    if (isRec) {
      restoreRecOnly();
      if (note) note.hidden = true;
    } else {
      dashRecOnly();
      if (note) note.hidden = false;
    }
  }
  document.querySelectorAll(".hm-cell").forEach(function (el) {
    function go() { paint(Number(el.getAttribute("data-i")), Number(el.getAttribute("data-j"))); }
    el.addEventListener("click", go);
    el.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); go(); }
    });
  });
  paint(rec.i, rec.j);
  window.UC32_repaint = function () { paint(lastI, lastJ); };
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
"""


_DASH_EXTRA_JS = r"""
(function () {
  window.UC32_LANG = window.UC32_LANG || "sk";
  function groupNum(v, d) {
    if (typeof v !== "number" || !isFinite(v)) return "—";
    var neg = v < 0 ? "-" : "";
    var abs = Math.abs(v);
    var parts = (d ? abs.toFixed(d) : String(Math.round(abs))).split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    var dec = (window.UC32_LANG === "en") ? "." : ",";
    return neg + (d ? parts[0] + dec + parts[1] : parts[0]);
  }
  function drawDay(dateStr) {
    var node = document.getElementById("day-series");
    var host = document.getElementById("day-chart");
    if (!node || !host) return;
    var data;
    try { data = JSON.parse(node.textContent); } catch (err) { return; }
    var start = new Date(data.start.replace(" ", "T"));
    if (isNaN(start.getTime())) return;
    var step = data.step_minutes || 15;
    var n = data.n || 0;
    var without = data.without_kw || [];
    var withP = data.with_kw || [];
    var dayStart = new Date(dateStr + "T00:00:00");
    var idx = Math.round((dayStart - start) / (step * 60000));
    var steps = Math.round(24 * 60 / step);
    if (idx < 0 || idx >= n) {
      host.innerHTML = "<p class='note-block'>" + (window.UC32_LANG === "en"
        ? "No data for this day in the run."
        : "Pre tento deň nie sú v behu dáta.") + "</p>";
      return;
    }
    var a = [], b = [];
    for (var i = 0; i < steps && (idx + i) < n; i++) {
      a.push(without[idx + i] || 0);
      b.push(withP[idx + i] || 0);
    }
    var high = Math.max.apply(null, a.concat(b).concat([1]));
    var mag = Math.pow(10, Math.max(0, Math.floor(Math.log10(high))));
    var stepY = high / mag > 2 ? mag : mag / 2;
    high = Math.ceil(high / stepY) * stepY;
    var w = 1080, h = 260, pl = 72, pr = 16, pt = 28, pb = 36;
    var pw = w - pl - pr, ph = h - pt - pb;
    function xy(i, v) {
      var x = pl + pw * i / Math.max(a.length - 1, 1);
      var y = pt + ph * (1 - v / high);
      return x.toFixed(1) + "," + y.toFixed(1);
    }
    var ptsA = a.map(function (v, i) { return xy(i, v); }).join(" ");
    var ptsB = b.map(function (v, i) { return xy(i, v); }).join(" ");
    var grid = "";
    for (var k = 0; k < 5; k++) {
      var y = pt + ph * k / 4;
      var val = high * (1 - k / 4);
      grid += '<line x1="'+pl+'" y1="'+y.toFixed(1)+'" x2="'+(w-pr)+'" y2="'+y.toFixed(1)+'" stroke="#e6e9ee"/>';
      grid += '<text x="'+(pl-8)+'" y="'+(y+4).toFixed(1)+'" text-anchor="end" fill="#5c6675">'+groupNum(val,0)+'</text>';
    }
    var title = (window.UC32_LANG === "en" ? "Grid import on " : "Nákup zo siete ") + dateStr + " (kW)";
    host.innerHTML = '<svg viewBox="0 0 '+w+' '+h+'" width="100%" style="font:11px sans-serif">'
      + '<text x="'+pl+'" y="16" font-size="13" font-weight="600" fill="#14181f">'+title+'</text>'
      + grid
      + '<polyline fill="none" stroke="#b23a3a" stroke-width="1.5" points="'+ptsA+'"/>'
      + '<polyline fill="none" stroke="#1a6a8c" stroke-width="1.7" points="'+ptsB+'"/>'
      + '</svg>';
    var lab = document.getElementById("day-label");
    if (lab) lab.textContent = dateStr;
  }
  function initDay() {
    var node = document.getElementById("day-series");
    var input = document.getElementById("day-input");
    if (!node || !input) return;
    var data;
    try { data = JSON.parse(node.textContent); } catch (err) { return; }
    var start = data.start ? data.start.slice(0, 10) : "";
    input.min = start;
    var endIdx = Math.max((data.n || 1) - 1, 0);
    var end = new Date(data.start.replace(" ", "T"));
    end.setMinutes(end.getMinutes() + endIdx * (data.step_minutes || 15));
    input.max = end.toISOString().slice(0, 10);
    var mid = start;
    if (start) {
      var s = new Date(start + "T00:00:00");
      s.setMonth(s.getMonth() + 5);
      mid = s.toISOString().slice(0, 10);
      if (input.max && mid > input.max) mid = input.max;
    }
    input.value = mid || start;
    input.addEventListener("change", function () { drawDay(input.value); });
    if (input.value) drawDay(input.value);
  }
  function applyLang(lang) {
    window.UC32_LANG = lang;
    document.documentElement.lang = lang;
    var pack = {};
    var node = document.getElementById("i18n-pack");
    if (node) {
      try { pack = JSON.parse(node.textContent) || {}; } catch (err) { pack = {}; }
    }
    var t = pack[lang] || {};
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      if (!el.getAttribute("data-sk")) el.setAttribute("data-sk", el.textContent);
      var key = el.getAttribute("data-i18n");
      if (lang === "en" && t[key]) el.textContent = t[key];
      if (lang === "sk") el.textContent = el.getAttribute("data-sk");
    });
    document.querySelectorAll("[data-copy-en]").forEach(function (el) {
      var next = lang === "en"
        ? (el.getAttribute("data-copy-en") || "")
        : (el.getAttribute("data-copy-sk") || "");
      if (!next) return;
      if (el.getAttribute("data-copy-html") === "1") el.innerHTML = next;
      else el.textContent = next;
    });
    var h1 = document.querySelector("h1[data-title-sk]");
    if (h1) {
      var next = lang === "en" ? (h1.getAttribute("data-title-en") || h1.textContent) : (h1.getAttribute("data-title-sk") || h1.textContent);
      h1.textContent = next;
      document.title = next;
    }
    document.querySelectorAll(".lang-switch button").forEach(function (btn) {
      btn.classList.toggle("on", btn.getAttribute("data-lang") === lang);
    });
    try { localStorage.setItem("uc32-lang", lang); } catch (err) {}
    var day = document.getElementById("day-input");
    if (day && day.value) drawDay(day.value);
    if (typeof window.UC32_repaint === "function") window.UC32_repaint();
  }
  document.querySelectorAll(".lang-switch button").forEach(function (btn) {
    btn.addEventListener("click", function () { applyLang(btn.getAttribute("data-lang")); });
  });
  var saved = "sk";
  try { saved = localStorage.getItem("uc32-lang") || "sk"; } catch (err) {}
  if (saved === "en") applyLang("en");
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initDay);
  } else {
    initDay();
  }
})();
"""


def build_html(
    payload: dict[str, Any],
    *,
    heatmap: dict[str, Any] | None,
    calibration: dict[str, Any] | None,
    ranking: dict[str, Any] | None,
    site_name: str = "",
) -> str:
    kpis = payload.get("decision_kpis") or {}
    generated = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    payback = kpis.get("simple_payback_years")
    payback_tone = "good" if is_number(payback) and payback <= 8 else (
        "warn" if is_number(payback) and payback <= 12 else "bad"
    )
    npv = kpis.get("npv_operating_eur")
    npv_tone = "good" if is_number(npv) and npv > 0 else "bad"
    view = _objective_view(heatmap)
    rec = (heatmap or {}).get("recommended") or {}
    if rec:
        kpis = dict(kpis)
        if rec.get("annual_savings_eur") is not None:
            kpis["operating_savings_annual_estimate_eur"] = rec.get("annual_savings_eur")
        if rec.get("total_capex_eur") is not None:
            kpis["total_capex_eur"] = rec.get("total_capex_eur")
        if rec.get("simple_payback_years") is not None:
            kpis["simple_payback_years"] = rec.get("simple_payback_years")
            payback = rec.get("simple_payback_years")
        if rec.get("npv_eur") is not None:
            kpis["npv_operating_eur"] = rec.get("npv_eur")
            npv = rec.get("npv_eur")
        if rec.get("operating_cost_baseline_eur") is not None:
            kpis["operating_cost_baseline_eur"] = rec.get("operating_cost_baseline_eur")
        if rec.get("operating_cost_optimized_eur") is not None:
            kpis["operating_cost_with_pv_battery_eur"] = rec.get("operating_cost_optimized_eur")
        if rec.get("discounted_payback_years") is not None:
            kpis["discounted_payback_years"] = rec.get("discounted_payback_years")
        if rec.get("battery_cycles_per_year") is not None:
            kpis["battery_annual_equivalent_cycles_est"] = rec.get("battery_cycles_per_year")
        if rec.get("battery_life_years") is not None:
            kpis["battery_estimated_life_years_effective"] = rec.get("battery_life_years")
        if rec.get("cashflow_after_om_eur") is not None:
            kpis["finance_annual_net_cashflow_after_finance_eur"] = rec.get("cashflow_after_om_eur")
        if rec.get("battery_kwh") is not None:
            kpis["battery_kwh"] = rec.get("battery_kwh")
        if rec.get("proposed_mrk_kw") is not None:
            kpis["proposed_mrk_kw"] = rec.get("proposed_mrk_kw")
        if rec.get("mrk_cut_kw") is not None:
            kpis["rv_downsizing_potential_kw"] = rec.get("mrk_cut_kw")
        if rec.get("mrk_savings_annual_eur") is not None:
            kpis["mrk_savings_annual_eur"] = rec.get("mrk_savings_annual_eur")
        if rec.get("peak_after_kw") is not None:
            kpis["mrk_peak_after_kw"] = rec.get("peak_after_kw")
        if rec.get("current_mrk_kw") is not None:
            kpis["current_mrk_kw"] = rec.get("current_mrk_kw")
    proposal = payload.get("mrk_proposal") or {}
    if not is_number(kpis.get("proposed_mrk_kw")):
        kpis["proposed_mrk_kw"] = proposal.get("recommended_rv_kw_conservative")
    if not is_number(kpis.get("current_mrk_kw")):
        kpis["current_mrk_kw"] = proposal.get("current_contract_rv_kw")
    if not is_number(kpis.get("mrk_savings_annual_eur")):
        kpis["mrk_savings_annual_eur"] = proposal.get("estimated_fixed_rv_fee_savings_eur_per_year")
    if not is_number(kpis.get("mrk_peak_after_kw")):
        kpis["mrk_peak_after_kw"] = proposal.get("max_monthly_peak_import_kw_after_optimization")
    if not is_number(kpis.get("mrk_fee_eur_per_kw_month")):
        kpis["mrk_fee_eur_per_kw_month"] = proposal.get("fee_eur_per_kw_month")
    if not is_number(kpis.get("mrk_safety_margin_pct")):
        kpis["mrk_safety_margin_pct"] = proposal.get("safety_margin_pct")
    payback_tone = "good" if is_number(payback) and payback <= 8 else (
        "warn" if is_number(payback) and payback <= 12 else "bad"
    )
    npv_tone = "good" if is_number(npv) and npv > 0 else "bad"
    rec_size_note = ""
    rec_size_note_en = ""
    if is_number(rec.get("pv_kwp")) or is_number(rec.get("battery_kwh")):
        rec_size_note, rec_size_note_en = size_note_pair(rec.get("pv_kwp"), rec.get("battery_kwh"))
    rec_size_html = copy_el(rec_size_note or "Odporúčaná veľkosť", rec_size_note_en or EN["size.rec"])

    kpi_cards = [
        card(
            "Ročná úspora",
            fmt_money(kpis.get("operating_savings_annual_estimate_eur")),
            "" if view["id"] == "max_annual_savings" or rec_size_note else "Úspora prevádzkových nákladov za rok",
            "good",
            live="savings",
            hint="Ročný cashflow po O&M: úspora na účte (DAM + distribúcia − výkup) mínus prevádzka FVE/batérie. Bez O&M by číslo vyzeralo príliš dobre.",
            i18n="kpi.savings",
            i18n_note="" if view["id"] == "max_annual_savings" or rec_size_note else "kpi.savings.note",
            i18n_tip="kpi.savings.tip",
            note_html=(
                copy_el(view["obj_note"], view["obj_note_en"])
                if view["id"] == "max_annual_savings"
                else (rec_size_html if rec_size_note else "")
            ),
        ),
        card(
            "Celkový CAPEX",
            fmt_money(kpis.get("total_capex_eur")),
            "FVE, úložisko a inštalácia",
            live="capex",
            hint="Jednorazová investícia do FVE a batérie pri zobrazenej veľkosti.",
            i18n="kpi.capex",
            i18n_note="kpi.capex.note",
            i18n_tip="kpi.capex.tip",
        ),
        card(
            "Jednoduchá návratnosť",
            fmt_years(payback),
            "" if view["id"] == "shortest_payback" else "Bez diskontovania",
            payback_tone,
            live="payback",
            hint=PAYBACK_TIP_SK,
            i18n="kpi.payback",
            i18n_note="" if view["id"] == "shortest_payback" else "kpi.payback.note",
            i18n_tip="kpi.payback.tip",
            note_html=(
                copy_el(view["obj_note"], view["obj_note_en"])
                if view["id"] == "shortest_payback"
                else ""
            ),
        ),
        card(
            "Čistá súčasná hodnota",
            fmt_money(npv),
            "" if view["id"] == "max_npv" else "Za dobu amortizácie",
            npv_tone,
            live="npv",
            hint="Súčasná hodnota budúcich úspor po O&M, s degradáciou panelov, mínus investícia.",
            i18n="kpi.npv",
            i18n_note="" if view["id"] == "max_npv" else "kpi.npv.note",
            i18n_tip="kpi.npv.tip",
            note_html=(
                copy_el(view["obj_note"], view["obj_note_en"])
                if view["id"] == "max_npv"
                else ""
            ),
        ),
    ]
    if view["id"] == "shortest_payback":
        kpi_cards[0], kpi_cards[2] = kpi_cards[2], kpi_cards[0]
    kpi_cards += [
        card(
            "Vnútorné výnosové percento",
            fmt_num(kpis.get("irr_pct"), 1, "%"),
            "Z neleverovaných peňažných tokov",
            "good" if is_number(kpis.get("irr_pct")) and kpis.get("irr_pct") > 8 else "",
            live="irr",
            rec_only=True,
            hint="IRR z ročných úspor voči CAPEX, bez úveru. Len pre odporúčanú veľkosť.",
            i18n="kpi.irr",
            i18n_note="kpi.irr.note",
            i18n_tip="kpi.irr.tip",
        ),
        card(
            "Úspora pri P90",
            fmt_money(kpis.get("p90_annual_savings_eur")),
            "Prekročená v 9 z 10 scenárov",
            live="p90",
            rec_only=True,
            hint=(
                "P90 je konzervatívny percentil z Monte Carlo: náhodne sa mení výroba FVE, "
                "úroveň cien a CAPEX (tisíce scenárov). Číslo znamená, že v 9 z 10 scenárov "
                "je ročná úspora aspoň taká vysoká; len v 1 z 10 je horšia. Nie je to priemer "
                "ani najlepší prípad. Platí len pre odporúčanú veľkosť — po kliknutí na mapu sa nemení."
            ),
            i18n="kpi.p90",
            i18n_note="kpi.p90.note",
            i18n_tip="kpi.p90.tip",
        ),
        card(
            "Navrhovaná MRK",
            fmt_num(kpis.get("proposed_mrk_kw"), 0, "kW"),
            (
                f"zo {fmt_num(kpis.get('current_mrk_kw'), 0, 'kW')}, "
                f"−{fmt_num(kpis.get('rv_downsizing_potential_kw'), 0, 'kW')}"
                if is_number(kpis.get("rv_downsizing_potential_kw"))
                and kpis.get("rv_downsizing_potential_kw") > 0
                else "Bez zmeny zmluvy"
            ),
            "good"
            if is_number(kpis.get("rv_downsizing_potential_kw")) and kpis.get("rv_downsizing_potential_kw") > 0
            else "",
            live="rv",
            hint="Nová zmluvná rezervovaná kapacita po nasadení tejto FVE a batérie. Podrobnosti sú v sekcii nižšie.",
            i18n="kpi.mrk",
            i18n_note="" if is_number(kpis.get("rv_downsizing_potential_kw")) and kpis.get("rv_downsizing_potential_kw") > 0 else "kpi.mrk.note.keep",
            i18n_tip="kpi.mrk.tip",
        ),
    ]
    cards = "".join(kpi_cards)
    baseline_bill = kpis.get("operating_cost_baseline_eur")
    plant_bill = kpis.get("operating_cost_with_pv_battery_eur")
    rec_battery = rec.get("battery_kwh")
    if rec_battery is None:
        rec_battery = kpis.get("battery_kwh")
    has_battery = is_number(rec_battery) and float(rec_battery) > 1e-6

    sections = [
        section(
            "Rozhodovacie zhrnutie",
            "Hlavné čísla pre schválenie investície. Klik na mapu veľkostí ich prepíše na vybranú kombináciu.",
            i18n="section.kpis",
            i18n_tip="section.kpis.tip",
        ),
        f'<div class="panel" style="margin-bottom:14px"><span class="badge ok">'
        f'<span data-i18n="goal.prefix">Cieľ návrhu:</span> '
        f'<span data-i18n="{esc(view["label_i18n"])}">{esc(view["label"])}</span>'
        f'{tip("Podľa tohto cieľa sa vybrala odporúčaná veľkosť FVE a batérie.", i18n="goal.tip")}</span>'
        f'<span class="badge" id="live-config">{rec_size_html}</span></div>',
        render_bill_compare(
            baseline=baseline_bill,
            plant=plant_bill,
            savings=kpis.get("operating_savings_annual_estimate_eur"),
            size_note=rec_size_note,
            size_note_en=rec_size_note_en,
            has_battery=has_battery,
            co2=payload.get("co2"),
        ),
        f'<div class="cards">{cards}</div>',
        render_mrk_proposal(
            proposal,
            size_note=rec_size_note,
            size_note_en=rec_size_note_en,
            override={
                "current_mrk_kw": kpis.get("current_mrk_kw"),
                "proposed_mrk_kw": kpis.get("proposed_mrk_kw"),
                "mrk_cut_kw": kpis.get("rv_downsizing_potential_kw"),
                "mrk_savings_annual_eur": kpis.get("mrk_savings_annual_eur"),
                "peak_after_kw": kpis.get("mrk_peak_after_kw"),
                "fee_eur_per_kw_month": kpis.get("mrk_fee_eur_per_kw_month"),
                "safety_margin_pct": kpis.get("mrk_safety_margin_pct"),
            },
        ),
        section(
            "Ceny elektriny použité vo výpočte",
            "Komoditná cena DAM. K nákupu zo siete sa ešte pripočíta poplatok za distribúciu.",
            i18n="section.prices",
            i18n_tip="section.prices.tip",
        ),
        render_prices(payload.get("prices")),
    ]

    consumption_html = render_consumption(
        payload.get("consumption"), payload.get("single_chart")
    )
    if consumption_html:
        sections += [
            section(
                "Spotreba energie zo siete",
                "Koľko energie kupujete zo siete pred a po nasadení FVE a batérie.",
                i18n="section.consumption",
                i18n_tip="section.consumption.tip",
            ),
            consumption_html,
        ]

    peaks_html = render_peaks(payload.get("peaks"))
    if peaks_html:
        sections += [
            section(
                "Mesačné špičky a MRK",
                "Najvyšší 15-minútový nákup zo siete v každom mesiaci. Dôkaz, či sa dá znížiť zmluvná rezervovaná kapacita.",
                i18n="section.peaks",
                i18n_tip="section.peaks.tip",
            ),
            peaks_html,
        ]

    if heatmap:
        sections += [
            section(
                "Veľkosť systému a návratnosť",
                "Každá bunka je úplná simulácia jednej kombinácie kWp a kWh. Kliknite na ňu.",
                i18n="section.heatmap",
                i18n_tip="section.heatmap.tip",
            ),
            render_heatmap(heatmap),
            render_heatmap_table(heatmap),
        ]

    hardware = payload.get("hardware_recommendation")
    rec_kwp = rec.get("pv_kwp")
    rec_kwh = rec.get("battery_kwh")
    if rec_kwh is None:
        rec_kwh = kpis.get("battery_kwh")
    hardware_html = render_recommended_hardware(
        hardware,
        ranking,
        energy_kwh=rec_kwh,
        installed_kwp=rec_kwp,
    )
    equipment = render_equipment(ranking)
    if hardware_html or equipment:
        sections += [
            section(
                "Odporúčané zariadenia",
                "Modul, menič a batéria vybrané z katalógu pre odporúčanú veľkosť.",
                i18n="section.hardware",
                i18n_tip="section.hardware.tip",
            ),
            hardware_html,
        ]
    if equipment:
        sections += [
            section(
                "Ďalšie moduly z katalógu",
                "Alternatívy zoradené pre túto lokalitu. Zvýraznený riadok je najlepšia zhoda.",
                i18n="section.modules",
                i18n_tip="section.modules.tip",
            ),
            equipment,
        ]

    baseline = kpis.get("operating_cost_baseline_eur")
    optimised = kpis.get("operating_cost_with_pv_battery_eur")
    dpb = kpis.get("discounted_payback_years")
    if not is_number(dpb):
        capex = rec.get("total_capex_eur") or kpis.get("total_capex_eur")
        annual = rec.get("annual_savings_eur") or kpis.get("operating_savings_annual_estimate_eur")
        dr = kpis.get("discount_rate") or (heatmap or {}).get("discount_rate") or 0.075
        years = int(kpis.get("analysis_horizon_years") or (heatmap or {}).get("amortization_years") or 15)
        if is_number(capex) and is_number(annual) and annual > 0 and capex > 0:
            cum = 0.0
            for t in range(1, max(int(years) + 10, 40) + 1):
                inc = annual / (1.0 + float(dr)) ** t
                prev = cum
                cum += inc
                if cum >= capex:
                    dpb = t - 1 + (capex - prev) / inc
                    break
    if not is_number(dpb):
        dpb_text = copy_el("Nevráti sa pri diskontovaní", EN["hm.nodiscount"])
    else:
        dpb_text = fmt_years(dpb)
    bat_kwh = rec.get("battery_kwh")
    if bat_kwh is None:
        bat_kwh = kpis.get("battery_kwh")
    no_battery = not is_number(bat_kwh) or float(bat_kwh) <= 1e-6
    cycles = kpis.get("battery_annual_equivalent_cycles_est")
    if no_battery:
        cycles_text = copy_el("Bez batérie", EN["hw.nobat"])
        life_text = copy_el("Neaplikuje sa", EN["hm.na"])
    else:
        cycles_text = fmt_num(cycles, 0)
        cal = kpis.get("battery_calendar_life_years")
        by_c = kpis.get("battery_estimated_life_years_by_cycles")
        cycle_life = kpis.get("battery_cycle_life_at_eol")
        life = kpis.get("battery_estimated_life_years_effective")
        if is_number(by_c):
            shown = min(float(cal), float(by_c)) if is_number(cal) else float(by_c)
        elif is_number(cycles) and float(cycles) > 0 and is_number(cycle_life):
            shown = float(cycle_life) / float(cycles)
            if is_number(cal):
                shown = min(float(cal), shown)
        else:
            shown = life
        life_text = fmt_years(shown)
    cash = kpis.get("finance_annual_net_cashflow_after_finance_eur")
    if not is_number(cash) and is_number(kpis.get("operating_savings_annual_estimate_eur")):
        cash = kpis.get("operating_savings_annual_estimate_eur")
    dist = kpis.get("distribution_eur_per_kwh")
    dist_text = f"{dist:.3f} €/kWh".replace(".", ",") if is_number(dist) else "—"
    reserve_on = kpis.get("mrk_peak_reserve_enabled")
    reserve_kwh_held = kpis.get("mrk_peak_reserve_kwh")
    if reserve_on is True:
        held = (
            f"{fmt_num(reserve_kwh_held, 0, 'kWh')} drží sa — zrezanie špičiek sa oplatí"
            if is_number(reserve_kwh_held) and float(reserve_kwh_held) > 0
            else "Drží sa — zrezanie špičiek sa oplatí"
        )
        reserve_text = copy_el(held, EN["d.reserve.on"])
    elif reserve_on is False:
        reserve_text = copy_el("Nedrží sa — energia z batérie sa oplatí viac", EN["d.reserve.off"])
    else:
        reserve_text = "—"
    assumptions = [
        (
            "Prevádzkové náklady dnes",
            fmt_money(baseline),
            "opex-today",
            False,
            "Ročný nákup zo siete a MRK bez FVE a batérie. Zahŕňa DAM aj distribúciu.",
            "d.opex0",
            "d.opex0.tip",
        ),
        (
            "Prevádzkové náklady s FVE a úložiskom",
            fmt_money(optimised),
            "opex-plant",
            False,
            "Rovnaké náklady po nasadení systému: menej nákupu, výnos z prebytku, prípadne nižšie špičky.",
            "d.opex1",
            "d.opex1.tip",
        ),
        (
            "Distribúcia v nákupe",
            dist_text,
            "distribution",
            False,
            "Poplatok za distribúciu pripočítaný ku každej kWh zo siete, nad rámec trhovej ceny DAM.",
            "d.dist",
            "d.dist.tip",
        ),
        (
            "Diskontovaná návratnosť",
            dpb_text,
            "dpb",
            False,
            "Kedy sa CAPEX vráti, ak budúce úspory diskontujete. Ak sa nevráti, NPV môže byť stále kladné neskôr.",
            "d.dpb",
            "d.dpb.tip",
        ),
        (
            "IRR pri P90",
            fmt_num(kpis.get("p90_irr_pct"), 1, "%"),
            "p90-irr",
            True,
            "Konzervatívne IRR z Monte Carlo: v 90 % scenárov je vnútorné výnosové percento aspoň také vysoké. Len v 1 z 10 je horšie. Odporúčaná veľkosť.",
            "d.p90irr",
            "d.p90irr.tip",
        ),
        (
            "NPV pri P50 / P90",
            f"{fmt_money(kpis.get('p50_npv_eur'))} / {fmt_money(kpis.get('p90_npv_eur'))}",
            "p50-p90-npv",
            True,
            "Dve NPV z toho istého Monte Carlo. P50 je medián: v polovici scenárov je NPV nižšie, v polovici vyššie. P90 je konzervatívny koniec: v 90 % scenárov je NPV aspoň také vysoké. Odporúčaná veľkosť.",
            "d.p5090",
            "d.p5090.tip",
        ),
        (
            "Pravdepodobnosť NPV > 0",
            fmt_num((kpis.get("probability_npv_positive") or 0) * 100, 0, "%")
            if is_number(kpis.get("probability_npv_positive"))
            else "—",
            "npv-prob",
            True,
            "Podiel Monte Carlo scenárov, v ktorých je investícia stále v pluse.",
            "d.npvprob",
            "d.npvprob.tip",
        ),
        (
            "Úspora pri P50",
            fmt_money(kpis.get("p50_annual_savings_eur")),
            "p50-sav",
            True,
            "P50 je medián toho istého Monte Carlo ako P90. V polovici scenárov je ročná úspora nižšia, v polovici vyššia. Je blízko hlavnej úspory na kartách, ale už zahŕňa neistotu výroby FVE, cien a CAPEX. Platí len pre odporúčanú veľkosť.",
            "d.p50",
            "d.p50.tip",
        ),
        (
            "Cyklov batérie za rok",
            cycles_text,
            "cycles",
            False,
            "Ekvivalent plných nabití a vybití za rok. Viac cyklov skráti životnosť.",
            "d.cycles",
            "d.cycles.tip",
        ),
        (
            "Očakávaná životnosť batérie",
            life_text,
            "life",
            False,
            "Odhad z cyklov: zadaná životnosť cyklov ÷ cykly za rok. Kalendárny strop (napr. záruka 10 r) sa použije len ak je v scenári zadaný; predvolene sa na 10 rokov neskracuje.",
            "d.life",
            "d.life.tip",
        ),
        (
            "Rezerva na špičky MRK",
            reserve_text,
            "mrk-reserve",
            True,
            "Podiel batérie, ktorý sa nenechá vybiť na arbitráž, aby ostal na zrezanie špičky odberu. Hodnota vo formulári je strop, nie príkaz. Beh ju drží len ak odber po FVE v niektorých mesiacoch prekročí zmluvnú MRK a penále plus mesačný poplatok za kW na zrezateľný výkon prevýšia to, čo by tých kWh zarobila arbitráž. Samotné prekročenie zmluvy sa stále zrezáva celým výkonom batérie.",
            "d.reserve",
            "d.reserve.tip",
        ),
        (
            "Obchodná marža za rok",
            fmt_money(kpis.get("trading_only_annual_margin_eur_estimate")),
            "trading",
            True,
            "Čistý zisk z čistého obchodovania batérie na DAM, bez odberu objektu. Len ilustrácia.",
            "d.trading",
            "d.trading.tip",
        ),
        (
            "Čistý cashflow (úspora − O&M)",
            fmt_money(cash),
            "cashflow",
            False,
            "Ročná úspora po odpočítaní údržby FVE a batérie.",
            "d.cash",
            "d.cash.tip",
        ),
    ]
    detail_rows = "".join(
        detail_row(label, value, live, rec_only, hint, i18n, i18n_tip)
        for label, value, live, rec_only, hint, i18n, i18n_tip in assumptions
    )
    sections += [
        section(
            "Podrobnosti",
            "Doplňujúce čísla k kartám hore. Pri kliknutí na mapu sa niektoré riadky prepíšu.",
            i18n="section.details",
            i18n_tip="section.details.tip",
        ),
        '<div class="panel"><table id="live-details">'
        f"{detail_rows}</table>"
        '<div class="note-block" id="live-details-note" hidden data-i18n="d.mcnote">'
        "P50/P90, IRR, rezervovaná kapacita a obchodná marža sú z Monte Carlo "
        "len pre odporúčanú veľkosť. Ostatné čísla sú z úplnej simulácie vybranej bunky."
        "</div></div>",
    ]

    flags = payload.get("quality_flags") or {}
    badges = []
    if flags.get("catalog_url_outage_detected"):
        badges.append('<span class="badge warn" data-i18n="q.catalog">Katalóg bol nedostupný, použité lokálne ceny zariadení</span>')
    source = flags.get("price_source")
    if source == "okte_dam":
        badges.append('<span class="badge ok" data-i18n="q.okte">Ceny z verejného trhu OKTE ISOT DAM</span>')
    elif source == "load_csv":
        badges.append('<span class="badge ok" data-i18n="q.load">Ceny zo stĺpca v profile odberu</span>')
    elif source == "prices_csv":
        badges.append('<span class="badge ok" data-i18n="q.file">Ceny zo samostatného cenníka</span>')
    elif flags.get("historical_prices_in_csv"):
        badges.append('<span class="badge ok" data-i18n="q.hist">Historické ceny v vstupnom súbore</span>')
    else:
        badges.append('<span class="badge warn" data-i18n="q.unknown">Zdroj cien sa nepodarilo potvrdiť</span>')
    if badges:
        sections += [
            section(
                "Kvalita vstupov",
                "Odkiaľ prišli ceny a či katalóg zariadení bol dostupný.",
                i18n="section.quality",
                i18n_tip="section.quality.tip",
            ),
            f'<div class="panel">{"".join(badges)}</div>',
        ]

    title = f"Investičný podklad: FVE a batériové úložisko{f' — {site_name}' if site_name else ''}"
    title_en = f"Investment brief: PV and battery storage{f' — {site_name}' if site_name else ''}"
    extra_js = _DASH_EXTRA_JS
    return (
        "<!doctype html><html lang='sk'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body><div class='wrap'>"
        "<header><div class='head-row'>"
        "<div><div class='kicker' data-i18n='kicker'>UC3.2. SEED</div>"
        f"<h1 data-i18n-title='1' data-title-sk='{esc(title)}' data-title-en='{esc(title_en)}'>{esc(title)}</h1>"
        f"<div class='sub'><span data-i18n='generated'>Vygenerované</span> {esc(generated)}</div></div>"
        "<div class='lang-switch' role='group' aria-label='Language'>"
        "<button type='button' data-lang='sk' class='on'>SK</button>"
        "<button type='button' data-lang='en'>EN</button>"
        "</div></div></header>"
        + render_timestep_strip(
            payload.get("timestep") or (payload.get("consumption") or {}).get("timestep")
        )
        + "".join(sections)
        + f"<script type='application/json' id='i18n-pack'>{pack_json()}</script>"
        + "<script>" + _HEATMAP_JS.replace("__PAYBACK_TIP_SK__", PAYBACK_TIP_SK) + "</script>"
        + f"<script>{extra_js}</script>"
        + "</div></body></html>"
    )
