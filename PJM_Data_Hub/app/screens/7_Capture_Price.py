"""Capture price by source — what each fuel actually earns at a PJM hub.

Capture price = Σ(fuel_gen_MW × hub_LMP) / Σ(fuel_gen_MW), i.e. the
generation-weighted price a fuel realizes given *when* it produces. Compared
to the time-weighted ("round-the-clock", RTC) hub price, the capture *ratio*
shows renewable cannibalization: wind/solar push price down in the very hours
they generate, so they capture less than the flat average; dispatchable fuels
capture more.

Joins the hourly system fuel mix (System Generation screen) to the hourly hub
LMP (Hub Prices). Needs both datasets populated.
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _common  # noqa: F401

import pandas as pd
import plotly.express as px
import streamlit as st

from pjm_core import paths, prices as PX
from pjm_core.settlement_points import HUBS, PRIMARY_HUB
from datasets.system_gen_by_fuel.pjm_gen import load as load_gen, FUEL_MAP

FUEL_COLORS = {
    "gas": "#f4a460", "nuclear": "#9370db", "coal": "#696969",
    "hydro": "#4169e1", "wind": "#87ceeb", "solar": "#ffd700",
    "oil": "#8b4513", "other_renewables": "#3cb371", "other": "#aaa",
    "storage": "#ff69b4",
}

st.title("🎯 Capture Price by Source")
st.caption("Generation-weighted price each fuel realizes at a hub, vs the flat "
           "round-the-clock (RTC) price. Capture ratio < 100% ⇒ the fuel earns "
           "less than average (renewable cannibalization); > 100% ⇒ more.")


@st.cache_data(show_spinner=True)
def load_gen_cached() -> pd.DataFrame:
    g = load_gen()
    if not g.empty:
        g["datetime_beginning_ept"] = pd.to_datetime(g["datetime_beginning_ept"])
    return g


@st.cache_data(show_spinner=True)
def load_price(hub: str) -> pd.DataFrame:
    p = PX.load_hub_prices([hub], market="RT")
    if not p.empty:
        p["datetime_beginning_ept"] = pd.to_datetime(p["datetime_beginning_ept"])
    return p[["datetime_beginning_ept", "total_lmp"]]


gen = load_gen_cached()
if gen.empty:
    _common.empty_state(
        st, "No system generation data yet.",
        hint="Populate it from the System Generation screen first.",
        page="screens/2_System_Generation.py", page_label="Go to System Generation")

fuel_cols = [c for c in FUEL_MAP.values() if c in gen.columns]
gmin, gmax = gen["datetime_beginning_ept"].min().date(), gen["datetime_beginning_ept"].max().date()

with st.container(border=True):
    st.header("Filters")
    hub = st.selectbox("Hub (price reference)", HUBS,
                       index=HUBS.index(PRIMARY_HUB) if PRIMARY_HUB in HUBS else 0)
    start, end = _common.period_picker(st, key="cap", min_year=gmin.year, default_mode="Year")
    sel_fuels = st.multiselect("Sources", fuel_cols,
                               default=[f for f in ("wind", "solar", "nuclear", "coal")
                                        if f in fuel_cols])

price = load_price(hub)
if price.empty:
    _common.empty_state(
        st, f"No RT price for {hub} yet.",
        hint="Run 'Update Hub Prices' on the API Keys page.",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

if not sel_fuels:
    st.warning("Select at least one source.")
    st.stop()

# Join hourly gen mix to hourly hub LMP over the window.
gmask = (gen["datetime_beginning_ept"].dt.date >= start) & (gen["datetime_beginning_ept"].dt.date <= end)
g = gen[gmask][["datetime_beginning_ept", *fuel_cols]]
merged = g.merge(price, on="datetime_beginning_ept", how="inner").dropna(subset=["total_lmp"])
if merged.empty:
    st.warning("No overlapping hours between generation and price in that window.")
    st.stop()

rtc = merged["total_lmp"].mean()  # time-weighted round-the-clock price
n_days = (end - start).days + 1
st.caption(f"**{start} → {end}** ({n_days} days) · {hub} · {len(merged):,} matched hours")

rows = []
for f in fuel_cols:
    w = merged[f].clip(lower=0)  # ignore negative (storage charging) for weighting
    tot = w.sum()
    if tot <= 0:
        continue
    capture = float((w * merged["total_lmp"]).sum() / tot)
    rows.append({"fuel": f, "capture_price": capture,
                 "capture_ratio": capture / rtc if rtc else float("nan"),
                 "avg_mw": merged[f].mean()})
cap = pd.DataFrame(rows)
if cap.empty:
    st.warning("No positive generation for the selected window.")
    st.stop()

st.metric("Round-the-clock (RTC) price", f"${rtc:,.2f}/MWh",
          help="Simple time-weighted average hub LMP — the benchmark every "
               "fuel's capture price is measured against.")

view = cap[cap["fuel"].isin(sel_fuels)].sort_values("capture_price")
color_map = {f: FUEL_COLORS.get(f, "#aaa") for f in view["fuel"]}
fig = px.bar(view, x="capture_price", y="fuel", orientation="h", color="fuel",
             color_discrete_map=color_map,
             text=view["capture_ratio"].map(lambda r: f"{r*100:.0f}% of RTC"),
             labels={"capture_price": "Capture price ($/MWh)", "fuel": ""},
             title=f"Capture price by source — {hub}")
fig.add_vline(x=rtc, line_dash="dot", line_color="#ccc",
              annotation_text="RTC", annotation_position="top")
fig.update_layout(height=max(280, 46 * len(view)), margin=dict(t=40), showlegend=False)
st.plotly_chart(fig, width="stretch")

# Monthly capture price trend per selected fuel.
st.subheader("Monthly capture price")
merged["month"] = merged["datetime_beginning_ept"].dt.to_period("M").astype(str)
mrows = []
for (mth), grp in merged.groupby("month"):
    rtc_m = grp["total_lmp"].mean()
    for f in sel_fuels:
        w = grp[f].clip(lower=0)
        tot = w.sum()
        if tot <= 0:
            continue
        mrows.append({"month": mth, "fuel": f,
                      "capture_price": float((w * grp["total_lmp"]).sum() / tot)})
mdf = pd.DataFrame(mrows)
if not mdf.empty:
    fig2 = px.line(mdf, x="month", y="capture_price", color="fuel",
                   color_discrete_map={f: FUEL_COLORS.get(f, "#aaa") for f in sel_fuels},
                   markers=True,
                   labels={"month": "Month", "capture_price": "Capture price ($/MWh)",
                           "fuel": "Source"})
    fig2.update_layout(height=380, margin=dict(t=20))
    st.plotly_chart(fig2, width="stretch")

st.dataframe(
    cap.sort_values("capture_price", ascending=False).reset_index(drop=True)
    .style.format({"capture_price": "${:,.2f}", "capture_ratio": "{:.1%}", "avg_mw": "{:,.0f}"}),
    width="stretch")

st.caption("Fuel mix comes from PJM's `gen_by_fuel` feed (all 10 fuels incl. "
           "gas). Watch the capture *ratio*: wind/solar < 100% (they depress "
           "price when they run — cannibalization), gas/coal > 100% (they set "
           "price in the peak hours they run).")
