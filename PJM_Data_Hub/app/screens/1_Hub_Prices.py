"""PJM Hub LMPs (hourly RT) — explore the hub_prices dataset."""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _common  # noqa: F401

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from pjm_core import paths
from pjm_core.settlement_points import PRIMARY_HUB

HUB_COLORS = {
    "DOM HUB": "#1f77b4",
    "AEP-DAYTON HUB": "#ff7f0e",
    "COMED HUB": "#2ca02c",
    "NI HUB": "#d62728",
    "EASTERN HUB": "#9467bd",
    "WESTERN HUB": "#8c564b",
    "AECO HUB": "#e377c2",
}

st.title("💵 PJM Hub Prices (Hourly RT LMP)")
st.caption("Real-Time hourly LMPs from PJM Data Miner 2 (api.pjm.com). "
           "LMP = Energy + Congestion + Loss.")


@st.cache_data(show_spinner=True)
def load() -> pd.DataFrame:
    if paths.HUB_PRICES_PARQUET.exists():
        return pd.read_parquet(paths.HUB_PRICES_PARQUET)
    return pd.DataFrame()


df = load()
if df.empty:
    _common.empty_state(
        st, "No Hub Prices data yet.",
        hint="Set your PJM subscription key and run 'Update Hub Prices' from the API Keys page.",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
hubs = sorted(df["pnode_name"].unique())
dmin = df["datetime_beginning_ept"].min().date()
dmax = df["datetime_beginning_ept"].max().date()
_common.data_status(st, path=paths.HUB_PRICES_PARQUET, rows=len(df), span=(dmin, dmax))

with st.container(border=True):
    st.header("Filters")
    sel_hubs = st.multiselect("Hubs", hubs, default=[PRIMARY_HUB])
    start, end = _common.period_picker(st, key="hub", min_year=dmin.year, default_mode="Month")
    freq = st.selectbox("Resample", ["Hourly", "Daily", "Weekly"], index=1)
    component = st.selectbox("LMP Component", ["total_lmp", "energy", "congestion", "loss"], index=0)
    scarcity = st.number_input("Scarcity threshold ($/MWh)", min_value=0, value=200, step=50)
    logy = st.checkbox("Log price axis", value=False)

if not sel_hubs:
    st.warning("Select at least one hub.")
    st.stop()

mask = (
    df["pnode_name"].isin(sel_hubs)
    & (df["datetime_beginning_ept"].dt.date >= start)
    & (df["datetime_beginning_ept"].dt.date <= end)
)
sub = df[mask]
if sub.empty:
    st.warning("No rows for that selection.")
    st.stop()

price = sub[component]
n_days = (end - start).days + 1
spike = price >= scarcity
neg = price < 0

st.caption(f"**{start} → {end}** ({n_days} days) · {', '.join(sel_hubs)} · {component}")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Avg $/MWh", f"{price.mean():,.2f}")
c2.metric("Median $/MWh", f"{price.median():,.2f}")
c3.metric("Max $/MWh", f"{price.max():,.2f}")
c4.metric("Min $/MWh", f"{price.min():,.2f}")
c5.metric(f"Intervals ≥ ${scarcity:,}", f"{int(spike.sum()):,} ({spike.mean()*100:.1f}%)")

# Resample for chart
freq_map = {"Hourly": "1h", "Daily": "D", "Weekly": "W"}
rs_rule = freq_map[freq]

chart_frames = []
for hub in sel_hubs:
    h = sub[sub["pnode_name"] == hub].set_index("datetime_beginning_ept")[component]
    h_rs = h.resample(rs_rule).mean().reset_index()
    h_rs["hub"] = hub
    h_rs.columns = ["dt", "price", "hub"]
    chart_frames.append(h_rs)

chart_df = pd.concat(chart_frames, ignore_index=True) if chart_frames else pd.DataFrame()
if not chart_df.empty:
    color_map = {h: HUB_COLORS.get(h, "#333") for h in sel_hubs}
    fig = px.line(chart_df, x="dt", y="price", color="hub",
                  color_discrete_map=color_map,
                  labels={"dt": "Hour (EPT)", "price": f"{component} ($/MWh)", "hub": "Hub"},
                  log_y=logy)
    fig.update_layout(height=400, margin=dict(t=20))
    st.plotly_chart(fig, use_container_width=True)

# Monthly heatmap for DOM HUB
if PRIMARY_HUB in sel_hubs:
    st.subheader(f"Monthly average — {PRIMARY_HUB}")
    dom = sub[sub["pnode_name"] == PRIMARY_HUB].copy()
    dom["month"] = dom["datetime_beginning_ept"].dt.to_period("M").astype(str)
    dom["hour"] = dom["datetime_beginning_ept"].dt.hour
    pivot = dom.groupby(["month", "hour"])[component].mean().unstack("hour")
    if not pivot.empty:
        fig2 = px.imshow(pivot, labels={"x": "Hour (EPT)", "y": "Month", "color": "$/MWh"},
                         aspect="auto", color_continuous_scale="RdYlGn_r")
        fig2.update_layout(height=max(300, len(pivot) * 18), margin=dict(t=20))
        st.plotly_chart(fig2, use_container_width=True)
