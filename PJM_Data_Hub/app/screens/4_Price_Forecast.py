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

from pjm_core import credentials, paths, price_forecast
from pjm_core.settlement_points import HUBS, PRIMARY_HUB

st.title("📉 PJM DOM Hub Price Forecast")
st.caption(
    "Monthly P10/P50/P90 forward power price via implied heat rate × Henry Hub gas strip "
    "with Monte Carlo (5,000 paths). Method mirrors the ERCOT price forecaster."
)

if not paths.HUB_PRICES_PARQUET.exists():
    st.warning("No hub price data yet — run the update from **API Keys** first.")
    st.stop()

with st.sidebar:
    st.header("Forecast settings")
    hub = st.selectbox("Hub", HUBS, index=HUBS.index(PRIMARY_HUB))
    horizon = st.slider("Horizon (months)", 3, 36, 12)
    n_sims = st.select_slider("Monte Carlo paths", [1_000, 2_000, 5_000, 10_000], value=5_000)
    run_btn = st.button("Run forecast", type="primary")

cfg = credentials.load_config()
eia_key = credentials.get_eia_api_key()

if run_btn:
    with st.spinner(f"Running Monte Carlo ({n_sims:,} paths × {horizon} months) …"):
        try:
            df = price_forecast.run(
                hub=hub, horizon_months=horizon,
                n_sims=n_sims, eia_api_key=eia_key or None,
            )
            st.session_state["forecast_df"] = df
            st.session_state["forecast_hub"] = hub
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

st.subheader(f"{used_hub} — {len(df)}-month forward strip")

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
col1.metric("P50 (first month)", f"${df['p50'].iloc[0]:.2f}/MWh")
col2.metric("P50 (avg over strip)", f"${df['p50'].mean():.2f}/MWh")
col3.metric("Gas fwd (first month)", f"${df['gas_fwd'].iloc[0]:.2f}/MMBtu")

st.subheader("Forecast table")
disp = df[["month", "gas_fwd", "p10", "p25", "p50", "p75", "p90", "n_samples"]].copy()
disp["month"] = disp["month"].dt.strftime("%Y-%m")
for col in ("gas_fwd", "p10", "p25", "p50", "p75", "p90"):
    disp[col] = disp[col].map(lambda x: f"${x:.2f}")
st.dataframe(disp, use_container_width=True, hide_index=True)

st.caption(
    "**Methodology:** P50 power price = gas forward × median implied heat rate (historical LMP ÷ HH gas). "
    "Monte Carlo: lognormal gas (σ = 0.5·√t annualised) × lognormal heat rate (σ from realized distribution). "
    "Price capped at $2,000/MWh. Gas mean-reverts to $4.00/MMBtu over 24 months beyond the EIA strip."
)
