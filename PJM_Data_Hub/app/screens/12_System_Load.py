"""PJM system load — hourly metered load by zone."""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _common  # noqa: F401

import pandas as pd
import plotly.express as px
import streamlit as st

from pjm_core import paths
from datasets.load import pjm_load

st.title("📈 PJM System Load")
st.caption("Hourly metered load by zone. RTO is the system-wide total; DOM is "
           "the Dominion (Virginia) zone.")


@st.cache_data(show_spinner=True)
def load_all() -> pd.DataFrame:
    return pjm_load.load_store()


df = load_all()
if df.empty:
    _common.empty_state(
        st, "No load data yet.",
        hint="Run 'Update System Load' on the API Keys page.",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
dmin, dmax = df["datetime_beginning_ept"].min().date(), df["datetime_beginning_ept"].max().date()
_common.data_status(st, path=paths.LOAD_PARQUET, rows=len(df), span=(dmin, dmax))

zones = sorted(df["zone"].dropna().unique())
default = [z for z in ("RTO", "DOM") if z in zones] or zones[:1]

with st.container(border=True):
    st.header("Filters")
    sel_zones = st.multiselect("Zones", zones, default=default)
    start, end = _common.period_picker(st, key="load", min_year=dmin.year, default_mode="Month")

if not sel_zones:
    st.warning("Select at least one zone.")
    st.stop()

mask = (df["zone"].isin(sel_zones)
        & (df["datetime_beginning_ept"].dt.date >= start)
        & (df["datetime_beginning_ept"].dt.date <= end))
sub = df[mask]
if sub.empty:
    st.warning("No rows for that selection.")
    st.stop()

# Aggregate across load_areas within each zone × hour.
hourly = (sub.groupby(["zone", "datetime_beginning_ept"], as_index=False)["mw"].sum())

c1, c2, c3 = st.columns(3)
peak_row = hourly.loc[hourly["mw"].idxmax()]
c1.metric("Peak load (MW)", f"{peak_row['mw']:,.0f}",
          help=f"{peak_row['zone']} · {peak_row['datetime_beginning_ept']}")
c2.metric("Avg load (MW)", f"{hourly['mw'].mean():,.0f}")
c3.metric("Peak hour (EPT)", str(peak_row["datetime_beginning_ept"]))

fig = px.line(hourly, x="datetime_beginning_ept", y="mw", color="zone",
              labels={"datetime_beginning_ept": "Hour (EPT)", "mw": "Load (MW)", "zone": "Zone"},
              title="Hourly metered load")
fig.update_layout(height=420, margin=dict(t=30))
st.plotly_chart(fig, use_container_width=True)

# Load-duration curve (sorted descending) for each zone.
st.subheader("Load-duration curve")
ldc_frames = []
for z, g in hourly.groupby("zone"):
    s = g["mw"].sort_values(ascending=False).reset_index(drop=True)
    ldc_frames.append(pd.DataFrame({
        "zone": z, "pct_hours": (s.index + 1) / len(s) * 100, "mw": s.values}))
ldc = pd.concat(ldc_frames, ignore_index=True)
fig2 = px.line(ldc, x="pct_hours", y="mw", color="zone",
               labels={"pct_hours": "% of hours at or above", "mw": "Load (MW)", "zone": "Zone"})
fig2.update_layout(height=360, margin=dict(t=20))
st.plotly_chart(fig2, use_container_width=True)

st.download_button(
    "⬇ Download hourly load (CSV)",
    hourly.to_csv(index=False).encode(),
    file_name=f"pjm_load_{start}_{end}.csv", mime="text/csv")
