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
from datasets.eia923.eia923 import load as load_eia, update as update_eia, fuel_group

SOURCE_COLORS = {
    "Gas": "#ff7f0e", "Coal": "#5d4037", "Nuclear": "#9467bd",
    "Oil": "#8c564b", "Wind": "#2ca02c", "Solar": "#fdd835",
    "Hydro": "#1f77b4", "Storage": "#17becf", "Biomass": "#827717",
    "Geothermal": "#e377c2", "Other": "#7f7f7f",
}

st.title("📅 EIA-923 Plant Generation (PJM Footprint)")
st.caption("Monthly plant-level net generation from EIA Form 923. "
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

df["source"] = df["fuel_type"].map(fuel_group)
st.caption(f"{len(df):,} rows · years: {sorted(df['year'].unique().tolist())}")

with st.container(border=True):
    years_avail = sorted(df["year"].unique().tolist(), reverse=True)
    year_sel = st.selectbox("Year", years_avail)
    source_opts = ["All"] + sorted(df["source"].dropna().unique().tolist())
    source_sel = st.selectbox("Source", source_opts,
                              help="Fuel codes rolled up: Gas, Coal, Nuclear, Wind, Solar, …")
    _fuel_pool = df if source_sel == "All" else df[df["source"] == source_sel]
    fuel_opts = ["All"] + sorted(_fuel_pool["fuel_type"].dropna().unique().tolist())
    fuel_sel = st.selectbox("Fuel code (detail)", fuel_opts)
    state_opts = ["All"] + sorted(df["state"].dropna().unique().tolist())
    state_sel = st.selectbox("State", state_opts)
    search = st.text_input("Plant name search")

sub = df[df["year"] == year_sel].copy()
if source_sel != "All":
    sub = sub[sub["source"] == source_sel]
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

# The store is one row per plant × fuel × month; aggregate to annual for ranking.
# (Pre-monthly parquets have month=<NA> and are already annual rows.)
annual = (sub.groupby(["plant_id", "plant_name", "state", "source", "fuel_type"],
                      as_index=False)["net_generation_mwh"].sum())

st.subheader("Generation by source")
by_source = (sub.groupby("source", as_index=False)["net_generation_mwh"].sum()
             .sort_values("net_generation_mwh"))
by_source["share"] = by_source["net_generation_mwh"] / by_source["net_generation_mwh"].sum()
fig0 = px.bar(by_source, x="net_generation_mwh", y="source", orientation="h",
              color="source", color_discrete_map=SOURCE_COLORS,
              text=by_source["share"].map(lambda s: f"{s:.1%}"),
              labels={"net_generation_mwh": "Net Generation (MWh)", "source": ""},
              title=f"Net generation by source ({year_sel})")
fig0.update_layout(height=max(280, 34 * len(by_source)), margin=dict(t=30),
                   showlegend=False)
st.plotly_chart(fig0, use_container_width=True)

top = annual.nlargest(20, "net_generation_mwh")[
    ["plant_name", "state", "source", "fuel_type", "net_generation_mwh"]]
fig = px.bar(top, x="net_generation_mwh", y="plant_name", orientation="h",
             color="source", color_discrete_map=SOURCE_COLORS,
             hover_data=["fuel_type", "state"],
             labels={"net_generation_mwh": "Net Generation (MWh)", "plant_name": ""},
             title=f"Top 20 plants by net generation ({year_sel})")
fig.update_layout(height=500, yaxis={"categoryorder": "total ascending"}, margin=dict(t=30))
st.plotly_chart(fig, use_container_width=True)

has_month = "month" in sub.columns and sub["month"].notna().any()
if has_month:
    st.subheader("Monthly generation")
    monthly = (sub.dropna(subset=["month"])
               .groupby(["month", "source"], as_index=False)["net_generation_mwh"].sum())
    fig2 = px.bar(monthly, x="month", y="net_generation_mwh",
                  color="source", color_discrete_map=SOURCE_COLORS,
                  labels={"month": "Month", "net_generation_mwh": "Net Generation (MWh)",
                          "source": "Source"},
                  title=f"Filtered plants by month × source ({year_sel})")
    fig2.update_layout(height=360, margin=dict(t=30), xaxis=dict(dtick=1),
                       barmode="stack")
    st.plotly_chart(fig2, use_container_width=True)

    # When narrowed to a handful of plants, show each plant's monthly profile.
    if annual["plant_name"].nunique() <= 30 and (search or fuel_sel != "All"):
        pivot = (sub.dropna(subset=["month"])
                 .pivot_table(index=["plant_name", "fuel_type"], columns="month",
                              values="net_generation_mwh", aggfunc="sum"))
        st.dataframe(pivot.style.format("{:,.0f}"), use_container_width=True)

st.subheader("Plant detail")
st.caption("One row per plant × fuel, annual net generation (MWh) for the selected year.")
_tbl = (annual.sort_values("net_generation_mwh", ascending=False)
        .reset_index(drop=True)
        .rename(columns={"plant_id": "Plant ID", "plant_name": "Plant",
                         "state": "State", "source": "Source",
                         "fuel_type": "Fuel code", "net_generation_mwh": "Net gen (MWh)"}))
st.dataframe(_tbl.style.format({"Net gen (MWh)": "{:,.0f}"}),
             use_container_width=True, hide_index=True)
