"""EIA-923 plant-level generation for PJM-footprint plants."""

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
from datasets.eia923.eia923 import load as load_eia, update as update_eia

st.title("📅 EIA-923 Plant Generation (PJM Footprint)")
st.caption("Annual plant-level net generation from EIA Form 923. "
           "Covers all utility-scale plants in PJM-footprint states. "
           "Lag: ~6 months (prior-year final released ~October).")

files = sorted(paths.EIA_DIR.glob("eia923_pjm_*.parquet")) if paths.EIA_DIR.exists() else []

if not files:
    st.warning("No EIA-923 data yet.")
    if st.button("Download EIA-923 data"):
        with st.spinner("Downloading from EIA …"):
            try:
                update_eia(log=st.write)
                st.rerun()
            except Exception as e:
                st.error(str(e))
    st.stop()

df = load_eia()
if df.empty:
    st.warning("EIA-923 files exist but are empty.")
    st.stop()

st.caption(f"{len(df):,} rows · years: {sorted(df['year'].unique().tolist())}")

with st.container(border=True):
    years_avail = sorted(df["year"].unique().tolist(), reverse=True)
    year_sel = st.selectbox("Year", years_avail)
    fuel_opts = ["All"] + sorted(df["fuel_type"].dropna().unique().tolist())
    fuel_sel = st.selectbox("Fuel type", fuel_opts)
    state_opts = ["All"] + sorted(df["state"].dropna().unique().tolist())
    state_sel = st.selectbox("State", state_opts)
    search = st.text_input("Plant name search")

sub = df[df["year"] == year_sel].copy()
if fuel_sel != "All":
    sub = sub[sub["fuel_type"] == fuel_sel]
if state_sel != "All":
    sub = sub[sub["state"] == state_sel]
if search:
    sub = sub[sub["plant_name"].str.contains(search, case=False, na=False)]

if sub.empty:
    st.info("No plants match the filters.")
    st.stop()

total_mwh = sub["net_generation_mwh"].sum()
st.metric("Total net generation (MWh)", f"{total_mwh:,.0f}")

top = sub.nlargest(20, "net_generation_mwh")[["plant_name", "state", "fuel_type", "net_generation_mwh"]]
fig = px.bar(top, x="net_generation_mwh", y="plant_name", orientation="h",
             color="fuel_type",
             labels={"net_generation_mwh": "Net Generation (MWh)", "plant_name": ""},
             title=f"Top 20 plants by net generation ({year_sel})")
fig.update_layout(height=500, yaxis={"categoryorder": "total ascending"}, margin=dict(t=30))
st.plotly_chart(fig, use_container_width=True)

st.dataframe(sub.sort_values("net_generation_mwh", ascending=False).reset_index(drop=True),
             use_container_width=True)
