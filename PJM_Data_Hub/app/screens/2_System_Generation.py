"""PJM system generation by fuel."""

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
from datasets.system_gen_by_fuel.pjm_gen import load as load_gen, update as update_gen, FUEL_MAP

FUEL_COLORS = {
    "gas": "#f4a460", "nuclear": "#9370db", "coal": "#696969",
    "hydro": "#4169e1", "wind": "#87ceeb", "solar": "#ffd700",
    "oil": "#8b4513", "other_renewables": "#3cb371", "other": "#aaa",
    "storage": "#ff69b4",
}

st.title("🔥 PJM System Generation by Fuel")
st.caption("Hourly system-wide generation (MW) for each fuel, from PJM Data "
           "Miner 2's gen_by_fuel feed. Pick a period to see the daily mix and "
           "each fuel's average output and share.")

gen_dir = paths.SYSTEM_GEN_DIR
files = sorted(gen_dir.glob("pjm_gen_by_fuel_*.parquet")) if gen_dir.exists() else []

if not files:
    st.warning("No generation data yet.")
    if st.button("Fetch generation data now"):
        with st.spinner("Downloading from gridstatus …"):
            try:
                update_gen(log=st.write)
                st.rerun()
            except Exception as e:
                st.error(str(e))
    st.stop()

df = load_gen()
df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
dmin = df["datetime_beginning_ept"].min().date()
dmax = df["datetime_beginning_ept"].max().date()
st.caption(f"{len(df):,} rows · {dmin} → {dmax}")

start, end = _common.period_picker(st, key="gen", min_year=dmin.year, default_mode="Month")

mask = (df["datetime_beginning_ept"].dt.date >= start) & (df["datetime_beginning_ept"].dt.date <= end)
sub = df[mask]

fuel_cols = [c for c in FUEL_MAP.values() if c in sub.columns]
if sub.empty or not fuel_cols:
    st.warning("No data for that period.")
    st.stop()

# Daily stack
sub2 = sub.set_index("datetime_beginning_ept")[fuel_cols]
daily = sub2.resample("D").mean().reset_index()
daily_long = daily.melt(id_vars="datetime_beginning_ept", var_name="fuel", value_name="gen_mw")
daily_long = daily_long[daily_long["fuel"].isin(fuel_cols)]

color_map = {f: FUEL_COLORS.get(f, "#aaa") for f in fuel_cols}
fig = px.area(daily_long, x="datetime_beginning_ept", y="gen_mw", color="fuel",
              color_discrete_map=color_map,
              labels={"datetime_beginning_ept": "Date", "gen_mw": "MW", "fuel": "Fuel"},
              title="Daily average generation by fuel")
fig.update_layout(height=420, margin=dict(t=30))
st.plotly_chart(fig, use_container_width=True)

# Summary table
st.subheader("Average output by fuel")
st.caption("Average MW over the selected period, and each fuel's share of total generation.")
summary = sub[fuel_cols].mean().rename("avg_mw").reset_index()
summary.columns = ["Fuel", "Avg MW", ]
summary["Share"] = summary["Avg MW"] / summary["Avg MW"].sum()
summary = summary.sort_values("Avg MW", ascending=False).reset_index(drop=True)
st.dataframe(
    summary.style.format({"Avg MW": "{:,.0f}", "Share": "{:.1%}"}),
    use_container_width=True, hide_index=True)
