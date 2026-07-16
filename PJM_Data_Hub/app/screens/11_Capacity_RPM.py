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

with st.expander("How PJM meets its capacity obligation — the two paths"):
    st.markdown(
        "PJM runs **one** capacity construct — the **Reliability Pricing Model "
        "(RPM)** — but a load-serving entity (LSE) can meet its obligation two "
        "ways:")
    p1, p2 = st.columns(2)
    with p1:
        st.markdown(
            "#### 1 · RPM auctions — *the default*\n"
            "The competitive, price-based path most of PJM uses.\n\n"
            "- **How:** PJM buys capacity in the **Base Residual Auction (BRA)**, "
            "~3 years ahead of the Delivery Year, plus **Incremental Auctions** "
            "to true up closer to real time.\n"
            "- **Pricing:** A downward-sloping **Variable Resource Requirement "
            "(VRR)** demand curve crosses supply to set a price in **$/MW-day**, "
            "priced separately by **LDA** so constrained zones clear higher.\n"
            "- **Who sells:** Generators, demand response, energy efficiency, "
            "storage.\n"
            "- **Applies to:** Merchant generators & competitive LSEs, which pay "
            "the **Locational Reliability Charge** at their zone's price.")
    with p2:
        st.markdown(
            "#### 2 · FRR — *the opt-out*\n"
            "Fixed Resource Requirement: self-supply instead of the auction.\n\n"
            "- **How:** The entity files an **FRR Capacity Plan** committing "
            "enough resources to cover its full **UCAP** obligation plus reserve "
            "margin for its zone.\n"
            "- **Commitment:** All-or-nothing and multi-year — once elected, "
            "**all load in that zone** is served under FRR (no dipping in and "
            "out of the auction to arbitrage price).\n"
            "- **Applies to:** Vertically integrated / regulated utilities, "
            "cooperatives, and municipal utilities that own generation and "
            "prefer price certainty over auction exposure.")
    st.caption(
        "In one line — **RPM:** market-price exposure, flexibility, competition. "
        "**FRR:** self-supply, price certainty, long-term commitment.")

df = capacity.load()
if df.empty:
    st.warning("No capacity reference data.")
    st.stop()

ldas = sorted(df["lda"].unique())
years_sorted = sorted(df["delivery_year"].unique())
with st.container(border=True):
    sel_ldas = st.multiselect(
        "LDAs", ldas,
        default=[l for l in ("RTO", "DOM", "BGE", "EMAAC", "MAAC") if l in ldas] or ldas)

sel_ldas = sel_ldas or ldas

# Build a complete grid so every selected LDA has a value for every year.
# When an LDA didn't separate, its price equals the RTO price for that year.
rto = df[df["lda"] == "RTO"].set_index("delivery_year")["clearing_price_mw_day"]
rows = []
for yr in years_sorted:
    for lda_name in sel_ldas:
        hit = df[(df["delivery_year"] == yr) & (df["lda"] == lda_name)]
        price = float(hit["clearing_price_mw_day"].iloc[0]) if not hit.empty else rto.get(yr)
        if price is not None:
            rows.append({"delivery_year": yr, "lda": lda_name, "clearing_price_mw_day": price})
sub = pd.DataFrame(rows)

# Clearing-price history.
fig = px.bar(sub, x="delivery_year", y="clearing_price_mw_day", color="lda",
             barmode="group",
             category_orders={"delivery_year": years_sorted},
             labels={"delivery_year": "Delivery year", "clearing_price_mw_day": "$/MW-day", "lda": "LDA"},
             title="RPM BRA clearing price by delivery year")
fig.update_layout(height=420, margin=dict(t=40))
st.plotly_chart(fig, use_container_width=True)

with st.expander("Why all LDAs clear at the same price now — the FERC price cap"):
    st.markdown(
        "From **2007/2008 through 2025/2026**, constrained LDAs like BGE, DOM, and "
        "EMAAC regularly cleared **above** the RTO price — sometimes dramatically "
        "(BGE hit $466/MW-day in 2025/2026). This reflected local supply shortages "
        "behind transmission-constrained boundaries.\n\n"
        "Starting with the **2026/2027 delivery year**, FERC approved a **price cap "
        "and floor** (a \"collar\") on the BRA. The cap has flattened all regional "
        "price differences — no zone can clear above it, so constrained zones that "
        "used to separate no longer do:\n\n"
        "| Delivery Year | Price Cap | Result |\n"
        "|---|---|---|\n"
        "| 2026/2027 | $329.17/MW-day | All LDAs uniform |\n"
        "| 2027/2028 | $333.44/MW-day | All LDAs uniform |\n"
        "| 2028/2029 | $325.00/MW-day | All LDAs uniform |\n\n"
        "**What prices would have been without the cap:** PJM's own simulations show "
        "the 2027/2028 auction would have cleared at ~$542/MW-day for Dominion, and "
        "the 2028/2029 auction at ~$777/MW-day for ComEd — more than double the "
        "capped price.\n\n"
        "**Why the cap exists:** Capacity prices spiked 9x between the 2024/2025 and "
        "2025/2026 auctions (from $29 to $270/MW-day RTO-wide) due to generator "
        "retirements, rising load forecasts, and new accreditation rules. The collar "
        "protects consumers from runaway prices while still signaling scarcity.\n\n"
        "**What's next:** PJM expects to return to the normal 3-year-ahead auction "
        "schedule by the **2030/2031 delivery year** (BRA in May 2027). The cap/floor "
        "structure may evolve as FERC reviews market design.")

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
