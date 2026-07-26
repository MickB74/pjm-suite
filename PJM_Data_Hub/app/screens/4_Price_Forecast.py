"""PJM DOM Hub forward price forecast — implied heat rate × gas Monte Carlo."""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _common  # noqa: F401

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from pjm_core import credentials, gas_strip, paths, price_forecast
from pjm_core.settlement_points import HUBS, PRIMARY_HUB

st.title("📉 PJM Hub Price Forecast")
st.caption(
    "Forward monthly power price ($/MWh) for the selected hub. The band shows "
    "the range of outcomes: P50 is the median, P10–P90 the 10th–90th percentile. "
    "Built from the hub's implied heat rate × the Henry Hub gas forward, run "
    "through a Monte Carlo simulation. Pick a hub and horizon in the sidebar, "
    "then Run forecast."
)

if not paths.HUB_PRICES_PARQUET.exists():
    st.warning("No hub price data yet — run the update from **API Keys** first.")
    st.stop()

_LATEST = "Latest (live)"

with st.sidebar:
    st.header("Forecast settings")
    hub = st.selectbox("Hub", HUBS, index=HUBS.index(PRIMARY_HUB))
    horizon = st.slider("Horizon (months)", 3, 36, 12)
    n_sims = st.select_slider("Monte Carlo paths", [1_000, 2_000, 5_000, 10_000], value=5_000)
    _vintage_dates = gas_strip.vintages()
    vintage_pick = st.selectbox(
        "Gas strip vintage", [_LATEST] + [f"{d:%Y-%m-%d}" for d in reversed(_vintage_dates)],
        key="gas_vintage",
        help="Re-run the forecast using the Henry Hub strip exactly as it "
             "stood on a past date. One vintage is archived per daily pull, "
             "so this list grows over time.")
    run_btn = st.button("Run forecast", type="primary")

_vintage_kwargs = {}
if vintage_pick != _LATEST:
    _vintage_kwargs = {"asof": vintage_pick, "gas_asof": vintage_pick}

cfg = credentials.load_config()
eia_key = credentials.get_eia_api_key()

if run_btn:
    with st.spinner(f"Running Monte Carlo ({n_sims:,} paths × {horizon} months) …"):
        try:
            df = price_forecast.run(
                hub=hub, horizon_months=horizon,
                n_sims=n_sims, eia_api_key=eia_key or None,
                **_vintage_kwargs,
            )
            st.session_state["forecast_df"] = df
            st.session_state["forecast_hub"] = hub
            st.session_state["forecast_vintage"] = vintage_pick
        except Exception as e:
            st.error(f"Forecast failed: {e}")
            st.stop()
elif "forecast_df" not in st.session_state:
    with st.spinner("Loading cached forecast …"):
        cached = price_forecast.load_cached(hub)
        if cached is not None:
            st.session_state["forecast_df"] = cached
            st.session_state["forecast_hub"] = hub
        else:
            df = price_forecast.run(
                hub=hub, horizon_months=horizon,
                n_sims=n_sims, eia_api_key=eia_key or None,
            )
            st.session_state["forecast_df"] = df
            st.session_state["forecast_hub"] = hub

df = st.session_state["forecast_df"]
used_hub = st.session_state.get("forecast_hub", hub)
used_vintage = st.session_state.get("forecast_vintage", _LATEST)

st.subheader(f"{used_hub} — {len(df)}-month forward strip")
if used_vintage != _LATEST:
    st.info(
        f"🕰️ **Vintage re-run:** this forecast starts from **{used_vintage}** "
        f"and uses the Henry Hub strip archived on that date — what the model "
        "would have said then, not today's view."
    )

fig = go.Figure()
fig.add_trace(go.Scatter(
    x=df["month"], y=df["p90"], name="P90",
    line=dict(color="#d62728", dash="dot"), mode="lines",
))
fig.add_trace(go.Scatter(
    x=df["month"], y=df["p50"], name="P50",
    line=dict(color="#1f77b4", width=2), mode="lines+markers",
))
fig.add_trace(go.Scatter(
    x=df["month"], y=df["p10"], name="P10",
    line=dict(color="#2ca02c", dash="dot"), mode="lines",
    fill="tonexty", fillcolor="rgba(44,160,44,0.08)",
))
# P10–P90 band
fig.add_trace(go.Scatter(
    x=pd.concat([df["month"], df["month"][::-1]]),
    y=pd.concat([df["p90"], df["p10"][::-1]]),
    fill="toself", fillcolor="rgba(31,119,180,0.10)",
    line=dict(color="rgba(0,0,0,0)"), showlegend=False, name="P10–P90 band",
))
fig.update_layout(
    yaxis_title="$/MWh",
    xaxis_title="Month",
    height=420, margin=dict(t=20),
    legend=dict(orientation="h", yanchor="bottom", y=1.02),
)
st.plotly_chart(fig, use_container_width=True)

# Key inputs
col1, col2, col3 = st.columns(3)
col1.metric("P50 (first month)", f"${df['p50'].iloc[0]:,.2f}/MWh")
col2.metric("P50 (avg over strip)", f"${df['p50'].mean():,.2f}/MWh")
col3.metric("Gas fwd (first month)", f"${df['gas_fwd'].iloc[0]:,.2f}/MMBtu")

gas_source = df["gas_source"].iloc[0] if "gas_source" in df.columns else "unknown"
if gas_source.startswith("NYMEX strip (vintage"):
    _source_note = ("Archived Henry Hub strip snapshot from the selected date; "
                    "months past the liquid strip mean-revert to $4.")
elif gas_source.startswith("NYMEX strip (cached"):
    _source_note = ("Real traded Henry Hub strip pulled from Yahoo at launch and "
                    "cached to CSV; months past the liquid strip mean-revert to $4.")
else:
    _source_note = {
    "NYMEX strip (Yahoo Finance, unofficial/delayed)":
        "Real traded Henry Hub futures strip from Yahoo Finance (CME-derived, "
        "unofficial & delayed); months past the liquid strip mean-revert to $4.",
    "EIA STEO Henry Hub forecast":
        "Real forward curve from EIA's Short-Term Energy Outlook (official gas "
        "price forecast); months beyond STEO's horizon mean-revert to $4.",
    "EIA Henry Hub spot + mean-reversion":
        "STEO unavailable — last EIA Henry Hub spot print extrapolated forward "
        "with mean-reversion to $4. Treat as a rough estimate, not a traded strip.",
    "mean-reversion fallback ($4 anchor)":
        "No EIA data (add your EIA key on the API Keys screen) — flat $4 anchor.",
    "manual CSV override":
        "Using your manual gas-price CSV override.",
}.get(gas_source, "")
st.caption(f"⛽ **Gas curve source:** {gas_source}. {_source_note}")

st.subheader("Forecast table")
st.caption(
    "One row per forward month. P10–P90 are percentile power prices ($/MWh) "
    "from the simulation. **History pts** is how many past heat-rate "
    "observations shaped that month — when it's below 2 the month falls back "
    "to a default heat rate, so treat those prices as rough until more price "
    "history accumulates.")
low_history = int((df["n_samples"] < 2).sum())
if low_history:
    st.warning(
        f"⚠️ {low_history:,} of {len(df):,} months have fewer than 2 historical "
        "heat-rate observations and use a default heat rate. The forecast will "
        "sharpen as the hub price store builds up more months of history.")
disp = df[["month", "gas_fwd", "p10", "p25", "p50", "p75", "p90", "n_samples"]].copy()
disp["month"] = disp["month"].dt.strftime("%Y-%m")
for col in ("gas_fwd", "p10", "p25", "p50", "p75", "p90"):
    disp[col] = disp[col].map(lambda x: f"${x:,.2f}")
disp["n_samples"] = disp["n_samples"].map(lambda x: f"{x:,.0f}")
disp = disp.rename(columns={
    "month": "Month", "gas_fwd": "Gas fwd $/MMBtu",
    "p10": "P10", "p25": "P25", "p50": "P50 (median)", "p75": "P75", "p90": "P90",
    "n_samples": "History pts"})
st.dataframe(disp, use_container_width=True, hide_index=True)

st.caption(
    "**Methodology:** P50 power price = gas forward × median implied heat rate (historical LMP ÷ HH gas). "
    "Monte Carlo: lognormal gas (σ = 0.5·√t annualised) × lognormal heat rate (σ from realized distribution). "
    "Price capped at $2,000/MWh. Gas forward comes from EIA STEO's Henry Hub forecast where available, "
    "then mean-reverts to $4.00/MMBtu over 24 months beyond STEO's horizon."
)
