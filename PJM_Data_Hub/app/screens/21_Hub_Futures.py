"""PJM Western Hub forward curve — exchange-traded power futures.

Western Hub is PJM's liquid traded hub. Monthly peak & off-peak calendar-month
futures settle in $/MWh (CME/NYMEX Globex JL1 peak, JN9 off-peak; also ICE).
There is no free forward-price API, so this screen reads a user-maintained
reference curve (data/futures/pjm_wh_forward_curve.csv) with a best-effort CME
settlement scraper, mirroring the Capacity (RPM) screen's design.
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

from pjm_core import paths, futures

st.title("📈 PJM Western Hub Forward Curve")
st.caption("Exchange-traded PJM power futures — peak & off-peak calendar-month "
           "settlements ($/MWh) for Western Hub, the liquid PJM trading hub.")

st.info(
    "Prices come from **ICE's free, ~15-minute-delayed** product-guide feed "
    "(PJM Western Hub Real-Time Peak / Off-Peak fixed-price futures) — no "
    "subscription. Use *Refresh from ICE* below to pull the latest curve, or "
    f"edit `{paths.FUTURES_CSV}` to override. Deferred months only appear once "
    "they've traded, so the curve is denser up front. Delayed data — verify "
    "against your broker before trading.",
    icon="ℹ️")

df = futures.load()
if df.empty:
    st.warning("No forward-curve data.")
    st.stop()

if futures.is_placeholder(df):
    st.warning(
        "⚠️ This curve is still the **seeded placeholder** — the price shape is "
        "illustrative, **not** real market marks. Replace it with real "
        "settlements (edit the CSV or use *Refresh from CME* below) before using "
        "these numbers for anything real.",
        icon="⚠️")

curve_asof = futures.asof(df)
st.caption(f"Marks as of **{curve_asof}** · {df['contract_month'].nunique()} "
           f"contract months · blocks: {', '.join(sorted(df['block'].unique()))}")

# --- Forward curve chart -----------------------------------------------------
months = sorted(df["contract_month"].unique())
label = {"peak": "Peak (5×16)", "offpeak": "Off-peak"}
plot_df = df.assign(Block=df["block"].map(label).fillna(df["block"]))
fig = px.line(
    plot_df, x="contract_month", y="price", color="Block", markers=True,
    category_orders={"contract_month": months},
    labels={"contract_month": "Contract month", "price": "$/MWh", "Block": "Block"},
    title="Western Hub forward curve by contract month")
fig.update_traces(hovertemplate="%{x}<br>$%{y:,.2f}/MWh<extra></extra>")
fig.update_layout(height=430, margin=dict(t=40), yaxis_tickprefix="$",
                  legend_title_text="")
st.plotly_chart(fig, use_container_width=True)

# --- Calendar strips ---------------------------------------------------------
strips = futures.calendar_strips(df)
if not strips.empty:
    st.subheader("Calendar strips (annual average $/MWh)")
    st.caption("A 'Cal strip' is the average of that year's monthly settlements — "
               "how full-year power blocks are quoted.")
    show_years = strips.tail(3)
    cols = st.columns(len(show_years) or 1)
    for col, (_, row) in zip(cols, show_years.iterrows()):
        peak = row.get("peak")
        off = row.get("offpeak")
        col.metric(
            f"Cal {row['year']} · Peak",
            f"${peak:,.2f}" if pd.notna(peak) else "—",
            help=(f"Off-peak ${off:,.2f}/MWh · "
                  f"ATC ≈ ${row.get('atc'):,.2f}/MWh"
                  if pd.notna(off) else None))

# --- Best-effort CME refresh -------------------------------------------------
st.divider()
c1, c2 = st.columns([1, 2])
with c1:
    do_refresh = st.button("🔄 Refresh from ICE")
with c2:
    st.caption("Pulls ICE's free ~15-min-delayed Western Hub curve (peak & "
               "off-peak) and overwrites the CSV. Falls back to the existing "
               "CSV on any failure — never crashes the page.")
if do_refresh:
    logs: list[str] = []
    with st.spinner("Contacting ICE…"):
        result = futures.update(log=logs.append)
    for line in logs:
        st.write(f"• {line}")
    if result is not None:
        st.success(f"Refreshed {len(result)} rows from ICE.")
        st.rerun()
    else:
        st.info("No refresh applied — the reference CSV is unchanged.")

# --- Reference table + download ----------------------------------------------
st.subheader("Reference curve")
show = (df.rename(columns={
    "asof": "As of", "contract_month": "Contract month", "block": "Block",
    "price": "$/MWh", "source": "Source"}))
st.dataframe(
    show.style.format({"$/MWh": "${:,.2f}"}),
    use_container_width=True, height=380, hide_index=True)
st.download_button(
    "⬇ Download Western Hub forward curve (CSV)",
    df.to_csv(index=False).encode(),
    file_name="pjm_wh_forward_curve.csv", mime="text/csv")

st.info(
    "**See also:** *Capacity (RPM)* for capacity-market clearing prices, and "
    "*Price Forecast* for a modeled energy price path.", icon="🔗")
