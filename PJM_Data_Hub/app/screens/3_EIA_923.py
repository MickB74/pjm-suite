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
from datasets.eia860 import eia860 as load_eia860

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

# ── Capacity join (EIA-860) ─────────────────────────────────────────────────
# EIA-860 lands later than 923 (typically ~1 year lag), so fall back to the
# newest 860 year available and note the vintage. Join on (plant_id,
# energy_source=fuel_type) — both feeds use the standard EIA fuel codes, and a
# per-fuel join is more accurate than plant-total when a plant runs several
# units on different fuels.
_available_860 = load_eia860.available_years()
_cap_year = year_sel if year_sel in _available_860 else (max(_available_860) if _available_860 else None)
cap = load_eia860.load(_cap_year) if _cap_year else pd.DataFrame()
if not cap.empty:
    cap = (cap.rename(columns={"energy_source": "fuel_type"})
           [["plant_id", "fuel_type", "nameplate_mw", "summer_mw", "winter_mw"]]
           .groupby(["plant_id", "fuel_type"], as_index=False).sum())

# The store is one row per plant × fuel × month; aggregate to annual for ranking.
# (Pre-monthly parquets have month=<NA> and are already annual rows.)
annual = (sub.groupby(["plant_id", "plant_name", "state", "source", "fuel_type"],
                      as_index=False)["net_generation_mwh"].sum())
if not cap.empty:
    annual = annual.merge(cap, on=["plant_id", "fuel_type"], how="left")
    # Capacity factor = actual MWh / (nameplate MW × hours-in-period). Hours
    # scale with how much of the year the store actually covers — the current
    # calendar year is typically only a few months in, and using 8760 there
    # gives artificially tiny CFs. `sub` carries a 'month' column when the
    # feed has monthly resolution (post-2020); count the distinct months and
    # scale by 730 hr/mo. Pre-monthly years fall back to full-year hours.
    _n_months = int(sub["month"].dropna().nunique()) if "month" in sub.columns else 0
    if _n_months == 0:
        _hrs = 8784 if pd.Timestamp(f"{year_sel}-01-01").is_leap_year else 8760
    else:
        _hrs = _n_months * (8784 // 12 if pd.Timestamp(f"{year_sel}-01-01").is_leap_year
                            else 8760 // 12)
    annual["capacity_factor"] = annual["net_generation_mwh"] / (annual["nameplate_mw"] * _hrs)
else:
    for _c in ("nameplate_mw", "summer_mw", "winter_mw", "capacity_factor"):
        annual[_c] = pd.NA
    _n_months, _hrs = 0, 0

total_mwh = sub["net_generation_mwh"].sum()
total_mw = float(annual["nameplate_mw"].sum(skipna=True)) if "nameplate_mw" in annual else 0.0
_matched = int(annual["nameplate_mw"].notna().sum()) if "nameplate_mw" in annual else 0

_c1, _c2, _c3 = st.columns(3)
_c1.metric("Total net generation (MWh)", f"{total_mwh:,.0f}")
_c2.metric("Nameplate capacity (MW)", f"{total_mw:,.0f}" if total_mw else "—",
           help=(f"Sum of nameplate MW across matched plants "
                 f"({_matched} of {len(annual)} rows). "
                 f"Source: EIA-860 {_cap_year}."
                 + (" Capacity is stated as of a different year than "
                    "generation — 860 lags 923 by ~1 year."
                    if _cap_year and _cap_year != year_sel else ""))
           if total_mw else None)
if total_mw and _hrs:
    _fleet_cf = total_mwh / (total_mw * _hrs)
    _cf_label = "Fleet capacity factor"
    _cf_help = ("Filtered fleet's actual generation as a share of what it "
                "could have produced running flat-out over the same period.")
    if _n_months and _n_months < 12:
        _cf_label += f" (YTD, {_n_months}mo)"
        _cf_help += (f" {year_sel} data covers only {_n_months} months so far; "
                     "the number reflects that partial-year period.")
    _c3.metric(_cf_label, f"{_fleet_cf*100:.1f}%", help=_cf_help)

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
st.plotly_chart(fig0, width="stretch")

top = annual.nlargest(20, "net_generation_mwh")[
    ["plant_name", "state", "source", "fuel_type", "net_generation_mwh"]]
fig = px.bar(top, x="net_generation_mwh", y="plant_name", orientation="h",
             color="source", color_discrete_map=SOURCE_COLORS,
             hover_data=["fuel_type", "state"],
             labels={"net_generation_mwh": "Net Generation (MWh)", "plant_name": ""},
             title=f"Top 20 plants by net generation ({year_sel})")
fig.update_layout(height=500, yaxis={"categoryorder": "total ascending"}, margin=dict(t=30))
st.plotly_chart(fig, width="stretch")

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
    st.plotly_chart(fig2, width="stretch")

    # When narrowed to a handful of plants, show each plant's monthly profile.
    if annual["plant_name"].nunique() <= 30 and (search or fuel_sel != "All"):
        pivot = (sub.dropna(subset=["month"])
                 .pivot_table(index=["plant_name", "fuel_type"], columns="month",
                              values="net_generation_mwh", aggfunc="sum"))
        st.dataframe(pivot.style.format("{:,.0f}"), width="stretch")

st.subheader("Plant detail")
st.caption(
    "One row per plant × fuel. **Net gen (MWh)** is annual generation for the "
    "selected year. **Nameplate MW** / **Summer MW** are from EIA-860 (which "
    "lags 923 by ~1 year, so far-back years may show blanks). "
    "**Cap factor** = MWh ÷ (Nameplate MW × hours in year); shows how hard the "
    "unit actually ran. Nuclear ~90%, coal 40-60%, gas peakers 5-15%, wind "
    "30-45%, solar 20-30% are typical.")
_tbl_cols = {"plant_id": "Plant ID", "plant_name": "Plant", "state": "State",
             "source": "Source", "fuel_type": "Fuel code",
             "nameplate_mw": "Nameplate MW", "summer_mw": "Summer MW",
             "net_generation_mwh": "Net gen (MWh)",
             "capacity_factor": "Cap factor"}
_tbl = (annual.sort_values("net_generation_mwh", ascending=False)
        .reset_index(drop=True)
        [[c for c in _tbl_cols if c in annual.columns]]
        .rename(columns=_tbl_cols))
_fmt = {"Net gen (MWh)": "{:,.0f}"}
if "Nameplate MW" in _tbl.columns: _fmt["Nameplate MW"] = "{:,.1f}"
if "Summer MW" in _tbl.columns:    _fmt["Summer MW"] = "{:,.1f}"
if "Cap factor" in _tbl.columns:   _fmt["Cap factor"] = "{:.1%}"
st.dataframe(_tbl.style.format(_fmt, na_rep="—"),
             width="stretch", hide_index=True)
