"""Full retail bill estimator for a large C&I customer in a PJM zone.

Wholesale LMP is only the energy slice of a bill. This screen composes the whole
thing — **supply** (energy + capacity) plus **delivery** (distribution,
transmission/NITS, customer charge, riders) — into an itemized monthly bill and
an all-in $/kWh, using:

  • energy      — zone/hub LMP from the local price store (pjm_core.prices),
                  editable, × monthly kWh
  • capacity    — RPM clearing price (pjm_core.capacity) × PLC, monthly
  • delivery    — the EDC's C&I tariff (pjm_core.delivery)

Delivery figures come from a seeded, user-editable reference table
(data/delivery_rates/edc_ci_delivery_rates.csv). The seed rows are
order-of-magnitude placeholders (flagged "unverified") — replace them with each
utility's filed tariff, or pull structured tariffs from NREL's URDB.
"""

from __future__ import annotations

import sys
import pathlib
import calendar

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _common  # noqa: F401

import pandas as pd
import plotly.express as px
import streamlit as st

from pjm_core import paths, delivery, capacity
from pjm_core import prices as PX
from pjm_core.settlement_points import ZONES

st.title("🧮 Full Bill Estimator")
st.caption("Build a complete monthly bill for a large C&I customer — supply "
           "(energy + capacity) plus utility delivery — and an all-in $/kWh.")

st.info(
    "Delivery charges come from a **seeded reference table** of C&I tariffs "
    "(`data/delivery_rates/edc_ci_delivery_rates.csv`). Seed rows are "
    "order-of-magnitude placeholders — replace them with each EDC's filed "
    "tariff, or pull structured tariffs from the "
    "[NREL URDB](https://openei.org/wiki/Utility_Rate_Database). Rows still "
    "marked *unverified* are flagged below.",
    icon="ℹ️")

drates = delivery.load()
del_zones = sorted(drates["zone"].unique().tolist())

# ── Inputs ────────────────────────────────────────────────────────────────
with st.container(border=True):
    c1, c2, c3 = st.columns(3)
    with c1:
        zone = st.selectbox("PJM zone", del_zones,
                            index=del_zones.index("DOM") if "DOM" in del_zones else 0)
        # A zone can carry several filed rate schedules (voltage tiers) — let the
        # user pick when more than one exists (e.g. PSE&G LPL-Primary vs HTS).
        _zone_rows = drates[drates["zone"] == zone]
        rate_class = None
        if len(_zone_rows) > 1:
            _classes = _zone_rows["rate_class"].tolist()
            rate_class = st.selectbox(
                "Rate schedule / voltage", _classes, index=0,
                help="Filed rate schedule for this zone. Higher-voltage classes "
                     "(subtransmission / transmission) carry lower $/kW demand but "
                     "higher fixed charges — pick the one the customer takes service on.")
        kwh = st.number_input("Monthly usage (kWh)", min_value=0.0,
                              value=1_000_000.0, step=50_000.0, format="%.0f")
    with c2:
        peak_kw = st.number_input("Billing demand — metered peak (kW)",
                                  min_value=0.0, value=2_000.0, step=100.0, format="%.0f")
        nspl_kw = st.number_input("NSPL for transmission (kW)", min_value=0.0,
                                  value=peak_kw, step=100.0, format="%.0f",
                                  help="Network Service Peak Load — bills the "
                                       "transmission (NITS) charge. Defaults to "
                                       "the metered peak.")
    with c3:
        plc_mw = st.number_input("Capacity obligation — PLC (MW)", min_value=0.0,
                                 value=peak_kw / 1000.0, step=0.1, format="%.3f",
                                 help="Peak Load Contribution — the tag PJM "
                                      "capacity is billed on ($/MW-day × days).")
        month = st.selectbox("Billing month", list(range(1, 13)), index=6,
                             format_func=lambda m: calendar.month_name[m])

# Load factor readout — the single biggest driver of $/kWh for C&I.
hours = 730.0
if peak_kw > 0:
    lf = kwh / (peak_kw * hours)
    st.caption(f"Load factor ≈ **{lf:.0%}**  ·  {kwh/1000:,.0f} MWh / "
               f"{peak_kw:,.0f} kW peak")

# ── Energy (supply) ───────────────────────────────────────────────────────
def _default_energy_price() -> float:
    """Recent average zone LMP ($/MWh) if the store has it, else a fallback."""
    try:
        df = PX.load_hub_prices(market="RT")
        if df is not None and not df.empty and "total_lmp" in df:
            return round(float(df["total_lmp"].tail(720).mean()), 2)
    except Exception:
        pass
    return 40.0

with st.container(border=True):
    st.subheader("Supply")
    s1, s2 = st.columns(2)
    with s1:
        energy_price = st.number_input(
            "Energy / supply price ($/MWh)", min_value=0.0,
            value=_default_energy_price(), step=1.0,
            help="Retail supply / Price-to-Compare in $/MWh. Defaults to the "
                 "recent average hub LMP — add your supplier's adder on top.")
    with s2:
        dy_options = sorted(capacity.load()["delivery_year"].unique(), reverse=True)
        delivery_year = st.selectbox("Capacity delivery year", dy_options,
                                     index=0 if dy_options else None)
        lda = "DOM" if zone == "DOM" else "RTO"

# ── Delivery (needs the billing month to pick the summer vs winter demand rate) ─
deliv = delivery.estimate_delivery(zone, kwh=kwh, billing_demand_kw=peak_kw,
                                   nspl_kw=nspl_kw, month=month, rate_class=rate_class)
if deliv is None:
    st.error(f"No delivery tariff for zone {zone}.")
    st.stop()

days = calendar.monthrange(2026, month)[1]
plc_kw = plc_mw * 1000.0
ciep_kw_day = float(deliv.get("capacity_kw_day") or 0.0)

# ── Capacity billing ───────────────────────────────────────────────────────
# How PJM generation capacity reaches the customer depends on the tariff:
#
#   • BGS/CIEP states (all of NJ) bill it YEAR-ROUND as $/kW-day on the PLC tag.
#     The summer seasonality lives in the delivery DEMAND charge, not here. When
#     the delivery row carries a filed ``capacity_kw_day``, use it directly.
#
#   • Otherwise fall back to the RPM table, with an optional summer-only
#     capacity-demand-charge treatment for tariffs that recover it that way.
SUMMER_WINDOWS = {"Jun–Sept": (6, 7, 8, 9), "May–Sept": (5, 6, 7, 8, 9),
                  "Jun–Oct": (6, 7, 8, 9, 10)}
_dy_price = capacity.price_for(delivery_year, lda) if delivery_year else None
with st.container(border=True):
    st.subheader("Capacity billing")
    if ciep_kw_day > 0:
        cap_cost = ciep_kw_day * plc_kw * days
        cap_label = f"Capacity — BGS-CIEP ({zone})"
        _cap_note = (f"Capacity: BGS-CIEP ${ciep_kw_day:,.5f}/kW-day × {plc_kw:,.0f} kW "
                     f"PLC × {days} days = year-round on the capacity tag.")
        st.caption(
            f"✅ **{deliv['edc']}** bills generation capacity as **BGS-CIEP: "
            f"${ciep_kw_day:,.5f}/kW-day on the PLC tag, every day of the year**. "
            f"The summer premium is in the **distribution demand** charge, not here. "
            f"(This zone's filed rate overrides the RPM table.)")
    else:
        method = st.radio(
            "How does this tariff bill PJM capacity?",
            ["Summer capacity demand charge ($/kW-month, summer only)",
             "Year-round installments ($/MW-day × days, all 12 months)"],
            index=1,
            help="**Summer capacity demand charge** — some EDC tariffs recover the "
                 "annual RPM cost through a $/kW demand charge on the capacity tag "
                 "(PLC), billed only in the summer window. **Year-round installments** "
                 "— the straight wholesale pass-through: PLC × $/MW-day × days, every "
                 "month (June–May). NJ zones use neither — see BGS-CIEP above.")
        summer_only = method.startswith("Summer")
        cb1, cb2 = st.columns(2)
        window_label = cb1.selectbox(
            "Summer window", list(SUMMER_WINDOWS), index=0, disabled=not summer_only,
            help="The months your tariff assesses the summer capacity demand charge.")
        summer_months = set(SUMMER_WINDOWS[window_label])
        _auto_rate = ((_dy_price * 365.0 / 1000.0) / len(summer_months)
                      if _dy_price and summer_only else 0.0)
        cap_rate_kw = cb2.number_input(
            "Summer capacity demand rate ($/kW-month)", min_value=0.0,
            value=round(_auto_rate, 2), step=0.5, format="%.2f", disabled=not summer_only,
            help=(f"Applied to your capacity tag (PLC = {plc_kw:,.0f} kW) each summer "
                  f"month. Default derives from RPM {lda} (${_dy_price:,.2f}/MW-day × "
                  f"365 ÷ 1000 ÷ {len(summer_months)} months); replace with your "
                  f"filed rate." if _dy_price else "Enter the $/kW-month filed rate."))
        cap_label = (f"Capacity — summer demand charge ({lda})" if summer_only
                     else f"Capacity (RPM {lda})")
        if summer_only and month in summer_months:
            cap_cost = cap_rate_kw * plc_kw
            _cap_note = (f"Capacity: summer demand charge ${cap_rate_kw:,.2f}/kW-month × "
                         f"{plc_kw:,.0f} kW PLC ({window_label}).")
        elif summer_only:
            cap_cost = 0.0
            _cap_note = (f"Capacity: $0 — summer-only ({window_label}); "
                         f"{calendar.month_name[month]} is outside the window.")
        elif _dy_price is not None:
            cap_cost = _dy_price * plc_mw * days
            _cap_note = (f"Capacity: {lda} @ ${_dy_price:,.2f}/MW-day × {plc_mw:.3f} MW × "
                         f"{days} days (year-round installment, June–May).")
        else:
            cap_cost = 0.0
            _cap_note = "Capacity price unavailable."

# ── Compose the bill ──────────────────────────────────────────────────────
# If the delivery tariff is bundled (URDB had no unbundled class), its per-kWh
# charge already includes generation — don't add LMP energy on top.
is_bundled = str(deliv.get("verified", "")).endswith("bundled")
energy_cost = 0.0 if is_bundled else energy_price * (kwh / 1000.0)  # $/MWh × MWh
energy_label = ("Energy (bundled in delivery)" if is_bundled
                else "Energy (supply)")
_season = deliv.get("demand_season", "winter")
rows = [
    (energy_label,                 "Supply",   energy_cost),
    (cap_label,                    "Supply",   cap_cost),
    ("Customer charge",            "Delivery", deliv["customer_charge"]),
    (f"Distribution demand ({_season})", "Delivery", deliv["distribution_demand"]),
    ("Transmission (NITS)",        "Delivery", deliv["transmission_demand_nits"]),
    ("Distribution energy",        "Delivery", deliv["distribution_energy"]),
    ("Riders",                     "Delivery", deliv["riders"]),
]
bill = pd.DataFrame(rows, columns=["Component", "Group", "Amount ($)"])
total = float(bill["Amount ($)"].sum())
all_in_kwh = total / kwh if kwh else None

# ── Headline metrics ──────────────────────────────────────────────────────
supply_tot = bill[bill["Group"] == "Supply"]["Amount ($)"].sum()
deliv_tot = bill[bill["Group"] == "Delivery"]["Amount ($)"].sum()
m1, m2, m3, m4 = st.columns(4)
m1.metric("Total monthly bill", f"${total:,.0f}")
m2.metric("All-in", f"{all_in_kwh*100:.2f} ¢/kWh" if all_in_kwh else "—")
m3.metric("Supply", f"${supply_tot:,.0f}", f"{supply_tot/total:.0%}" if total else None)
m4.metric("Delivery", f"${deliv_tot:,.0f}", f"{deliv_tot/total:.0%}" if total else None)

verified = str(deliv.get("verified")).lower()
if verified == "no":
    st.warning(
        f"⚠️ Delivery rates for **{deliv['edc']}** ({deliv['rate_class']}) are "
        "seed placeholders — verify against the filed tariff before relying on "
        f"this total. Edit `{paths.DELIVERY_RATES_CSV}`.")
elif verified == "urdb-bundled":
    st.warning(
        f"ℹ️ **{deliv['edc']}** ({deliv['rate_class']}) is a **bundled** URDB "
        "tariff — generation is baked into its per-kWh rate, so the separate "
        "LMP energy line is set to $0 to avoid double-counting. Transmission "
        "(NITS) is still a manual estimate.")
elif verified == "urdb":
    st.caption(f"✅ Delivery from URDB: {deliv['rate_class']} — verify NITS "
               "(transmission) separately; it's a manual estimate.")
elif verified.startswith("filed"):
    _rate = deliv.get("demand_rate_kw")
    st.caption(
        f"✅ **{deliv['edc']}** — {deliv['rate_class']} — from the filed tariff "
        f"({verified}). Distribution demand this month ({calendar.month_name[month]}, "
        f"{deliv.get('demand_season')}): **${_rate:,.4f}/kW**. "
        f"Source: {deliv.get('source')}")

# ── Breakdown ─────────────────────────────────────────────────────────────
c_left, c_right = st.columns([3, 2])
with c_left:
    fig = px.bar(bill, x="Amount ($)", y="Component", color="Group",
                 orientation="h",
                 color_discrete_map={"Supply": "#1565c0", "Delivery": "#ef6c00"},
                 title="Monthly bill by component")
    fig.update_layout(yaxis={"categoryorder": "total ascending"},
                      height=380, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)
with c_right:
    show = bill.copy()
    show["¢/kWh"] = (show["Amount ($)"] / kwh * 100).round(3) if kwh else None
    show["Amount ($)"] = show["Amount ($)"].round(0)
    st.dataframe(
        show,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Amount ($)": st.column_config.NumberColumn(format="$%,.0f"),
            "¢/kWh": st.column_config.NumberColumn(format="%.3f ¢"),
        },
    )
    st.caption(_cap_note)

# ── The delivery reference row in play ────────────────────────────────────
with st.expander("Delivery tariff row used"):
    st.dataframe(drates[drates["zone"] == zone], use_container_width=True,
                 hide_index=True)
    st.caption("Edit the CSV to correct any EDC's charges; the estimate updates "
               "on the next rerun.")
