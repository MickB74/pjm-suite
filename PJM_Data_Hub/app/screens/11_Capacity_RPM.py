"""PJM capacity market (RPM) — Base Residual Auction clearing prices.

PJM has no Data Miner API for capacity clearing prices, so this screen reads a
user-maintained reference table (data/capacity/rpm_bra_clearing_prices.csv),
seeded with published BRA results. Shows the clearing-price history by LDA and
a capacity-cost calculator for a given MW obligation.
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

from pjm_core import paths, capacity

st.title("🏛️ PJM Capacity Market (RPM)")
st.caption("Reliability Pricing Model Base Residual Auction clearing prices "
           "($/MW-day) by delivery year & Locational Deliverability Area (LDA).")

st.info(
    "PJM publishes no capacity-price API — this is a **reference table** seeded "
    "with published BRA results. Verify & extend it against PJM's posted "
    "[RPM auction results](https://www.pjm.com/markets-and-operations/rpm). "
    f"Edit `{paths.CAPACITY_CSV}` to update as new auctions clear.",
    icon="ℹ️")

df = capacity.load()
if df.empty:
    st.warning("No capacity reference data.")
    st.stop()

ldas = sorted(df["lda"].unique())
with st.container(border=True):
    sel_ldas = st.multiselect("LDAs", ldas,
                              default=[l for l in ("RTO", "DOM") if l in ldas] or ldas)

sub = df[df["lda"].isin(sel_ldas)] if sel_ldas else df

# Clearing-price history.
fig = px.bar(sub, x="delivery_year", y="clearing_price_mw_day", color="lda",
             barmode="group",
             labels={"delivery_year": "Delivery year", "clearing_price_mw_day": "$/MW-day", "lda": "LDA"},
             title="RPM BRA clearing price by delivery year")
fig.update_layout(height=420, margin=dict(t=40))
st.plotly_chart(fig, use_container_width=True)

# Latest-year snapshot.
latest_year = df["delivery_year"].max()
latest = df[df["delivery_year"] == latest_year]
st.subheader(f"Latest delivery year: {latest_year}")
cols = st.columns(min(4, len(latest)) or 1)
for col, (_, row) in zip(cols, latest.iterrows()):
    col.metric(f"{row['lda']} $/MW-day", f"${row['clearing_price_mw_day']:,.2f}",
               help=f"≈ ${row['clearing_price_mw_day']*365:,.0f} /MW-year")

# --- Capacity-cost calculator ------------------------------------------------
st.divider()
st.subheader("Capacity cost calculator")
st.caption("Estimate the annual capacity charge for a peak-load (obligation) MW "
           "amount. Capacity Obligation ≈ your coincident peak × zonal scaling "
           "factors — use your actual UCAP obligation for precision.")
c1, c2, c3 = st.columns(3)
mw = c1.number_input("Obligation (MW)", min_value=0.0, value=100.0, step=10.0)
dy = c2.selectbox("Delivery year", sorted(df["delivery_year"].unique(), reverse=True))
lda = c3.selectbox("LDA", sorted(df[df["delivery_year"] == dy]["lda"].unique()))

price = capacity.price_for(dy, lda)
if price is not None:
    annual = price * mw * 365.0
    m1, m2, m3 = st.columns(3)
    m1.metric("Clearing price", f"${price:,.2f} /MW-day")
    m2.metric("Daily cost", f"${price*mw:,.0f}")
    m3.metric("Annual capacity cost", f"${annual:,.0f}")
else:
    st.warning("No clearing price for that delivery year / LDA.")

# Reference table + download.
st.subheader("Reference table")
st.dataframe(
    df[["delivery_year", "lda", "auction", "clearing_price_mw_day", "clearing_price_mw_year"]]
    .style.format({"clearing_price_mw_day": "${:,.2f}", "clearing_price_mw_year": "${:,.0f}"}),
    use_container_width=True, height=380)
st.download_button(
    "⬇ Download RPM clearing prices (CSV)",
    df.to_csv(index=False).encode(),
    file_name="pjm_rpm_bra_clearing_prices.csv", mime="text/csv")
