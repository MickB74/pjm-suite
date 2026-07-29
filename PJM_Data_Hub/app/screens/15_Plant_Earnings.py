"""Estimated PJM plant earnings — EIA-923 generation × zonal LMP.

NOT actual settlement (PJM does not publish per-plant revenue). This is an
*energy-only estimate*: monthly net generation (EIA-923) valued at the plant's
PJM zone monthly-average LMP. See pjm_core/plant_earnings.py for caveats.
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

from pjm_core import paths, plant_earnings, plant_zones
from pjm_core.settlement_points import ZONE_PNODE_IDS
from datasets.zone_prices import pjm_zone_prices
from datasets.eia860 import eia860

SOURCE_COLORS = {
    "Gas": "#ff7f0e", "Coal": "#5d4037", "Nuclear": "#9467bd",
    "Oil": "#8c564b", "Wind": "#2ca02c", "Solar": "#fdd835",
    "Hydro": "#1f77b4", "Storage": "#17becf", "Biomass": "#827717",
    "Geothermal": "#e377c2", "Other": "#7f7f7f",
}

st.title("💰 Estimated Plant Earnings (PJM Footprint)")
st.caption(
    "**Estimate, not actuals.** PJM does not publish per-plant revenue. This "
    "values each plant's EIA-923 monthly net generation at its PJM **zone's** "
    "monthly-average LMP. Only true PJM units (EIA balancing-authority code "
    "`PJM`) are included. Energy revenue only unless capacity is toggled on — "
    "excludes ancillary, RECs/PTC, and any PPA or hedge.")

# --- data readiness ---------------------------------------------------------
zone_sum = pjm_zone_prices.store_summary()
eia_files = sorted(paths.EIA_DIR.glob("eia923_pjm_*.parquet")) if paths.EIA_DIR.exists() else []

if not eia_files:
    _common.empty_state(
        st, "No EIA-923 generation data yet.",
        "Build it on the EIA-923 page first.",
        page="screens/3_EIA_923.py", page_label="Go to EIA-923")

if not zone_sum.get("exists") or zone_sum.get("rows", 0) == 0:
    st.warning("**No zone LMP data yet.** Fetch it to price the generation.")
    if st.button("Fetch zone LMPs from PJM", type="primary"):
        with st.spinner("Fetching zone LMPs from PJM (monthly averages)…"):
            try:
                pjm_zone_prices.update(progress_callback=st.write)
                st.cache_data.clear()
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(str(e))
    st.stop()


@st.cache_data(show_spinner=False)
def _estimate(market: str, _sig: str) -> pd.DataFrame:
    return plant_earnings.estimate(market=market)


have_860 = bool(eia860.available_years())

# --- controls ---------------------------------------------------------------
with st.container(border=True):
    c1, c2, c3, c4 = st.columns(4)
    market = c1.radio("Market", ["DA", "RT"], horizontal=True,
                      help="Day-Ahead (most generation self-schedules here) or Real-Time.")
    _sig = f"{zone_sum.get('end')}-{zone_sum.get('rows')}-{len(eia_files)}"
    est_all = _estimate(market, _sig)
    if est_all.empty:
        st.info("No overlapping generation + price months yet.")
        st.stop()
    years_avail = sorted(est_all["year"].unique().tolist(), reverse=True)
    year_sel = c2.selectbox("Year", years_avail)
    src_opts = ["All"] + sorted(est_all["source"].dropna().unique().tolist())
    src_sel = c3.selectbox("Source", src_opts)
    add_cap = c4.toggle("+ Capacity (RPM)", value=have_860, disabled=not have_860,
                        help="Add estimated annual capacity revenue "
                             "(EIA-860 MW × capacity credit × RPM clearing price). "
                             "Requires EIA-860 capacity data.")
    search = st.text_input("Plant name search")
    if not have_860:
        cap_c1, cap_c2 = st.columns([3, 1])
        cap_c1.caption("💡 Add **capacity revenue** too: fetch EIA-860 plant "
                       "capacity (annual, no key).")
        if cap_c2.button("Fetch EIA-860"):
            with st.spinner("Downloading EIA-860 capacity data…"):
                try:
                    eia860.update(log=st.write)
                    st.cache_data.clear()
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(str(e))

est = est_all[est_all["year"] == year_sel].copy()
if src_sel != "All":
    est = est[est["source"] == src_sel]
if search:
    est = est[est["plant_name"].str.contains(search, case=False, na=False)]

if est.empty:
    st.info("No plants match the filters.")
    st.stop()

# --- roll up to plants (optionally with capacity) ---------------------------
plants = plant_earnings.by_plant(est, cal_year=int(year_sel), with_capacity=add_cap)
has_cap = add_cap and "est_capacity_rev" in plants.columns

# --- headline metrics + coverage -------------------------------------------
cov = plant_earnings.coverage(est)
total_rev = est["est_revenue"].sum(min_count=1)
total_mwh = est["net_generation_mwh"].sum()
blended = (total_rev / cov["mwh_priced"]) if cov["mwh_priced"] else float("nan")

n_months = int(est["month"].nunique())
if has_cap:
    cap_rev = plants["est_capacity_rev"].sum(min_count=1)
    tot_rev = plants["est_total_rev"].sum(min_count=1)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Est. energy revenue", f"${total_rev/1e9:,.2f}B" if pd.notna(total_rev) else "—")
    m2.metric("Est. capacity revenue", f"${cap_rev/1e9:,.2f}B" if pd.notna(cap_rev) else "—",
              help=f"RPM capacity revenue for delivery year "
                   f"{plant_earnings._delivery_year(int(year_sel))} (EIA-860 MW × "
                   f"capacity credit × clearing price), prorated to the "
                   f"{n_months}-month generation window shown.")
    m3.metric("Est. total revenue", f"${tot_rev/1e9:,.2f}B" if pd.notna(tot_rev) else "—")
    m4.metric("Priced coverage", f"{cov['priced_share']:.0%}",
              help="Share of MWh that mapped to a zone with LMP data for that month.")
else:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Est. energy revenue", f"${total_rev/1e9:,.2f}B" if pd.notna(total_rev) else "—")
    m2.metric("Net generation", f"{total_mwh/1e6:,.2f} TWh")
    m3.metric("Blended $/MWh", f"${blended:,.2f}" if pd.notna(blended) else "—")
    m4.metric("Priced coverage", f"{cov['priced_share']:.0%}",
              help="Share of MWh that mapped to a zone with LMP data for that month.")

if cov["priced_share"] < 0.999:
    unpriced = total_mwh - cov["mwh_priced"]
    st.caption(
        f"⚠️ {unpriced/1e6:,.2f} TWh ({1 - cov['priced_share']:.0%}) unpriced — "
        "either outside the PJM footprint or a month without zone LMP data. "
        "Those MWh are excluded from the revenue figure.")
if has_cap:
    window = "full year" if n_months == 12 else f"{n_months} of 12 months"
    st.caption(
        f"ℹ️ **Capacity** is RPM revenue prorated to the generation window "
        f"({window}). **Ancillary** (reserves/regulation) revenue is *not* shown "
        "per plant — PJM does not publish which units cleared, so it cannot be "
        "honestly allocated to individual plants.")

# --- charts -----------------------------------------------------------------
st.subheader("Estimated revenue by source")
by_src = (est.dropna(subset=["est_revenue"])
          .groupby("source", as_index=False)["est_revenue"].sum()
          .sort_values("est_revenue"))
if not by_src.empty:
    by_src["share"] = by_src["est_revenue"] / by_src["est_revenue"].sum()
    figs = px.bar(by_src, x="est_revenue", y="source", orientation="h",
                  color="source", color_discrete_map=SOURCE_COLORS,
                  text=by_src["share"].map(lambda s: f"{s:.1%}"),
                  labels={"est_revenue": "Est. energy revenue ($)", "source": ""},
                  title=f"Estimated energy revenue by source ({year_sel}, {market})")
    figs.update_layout(height=max(280, 34 * len(by_src)), margin=dict(t=30),
                       showlegend=False)
    st.plotly_chart(figs, width="stretch")

rank_col = "est_total_rev" if has_cap else "est_revenue"
st.subheader("Top plants by estimated " + ("total" if has_cap else "energy") + " revenue")
top = plants.dropna(subset=[rank_col]).nlargest(20, rank_col).copy()
if has_cap:
    # Stack energy vs capacity so the split is visible per plant.
    melted = top.melt(id_vars=["plant_name", "zone", "state"],
                      value_vars=["est_revenue", "est_capacity_rev"],
                      var_name="stream", value_name="revenue")
    melted["stream"] = melted["stream"].map({"est_revenue": "Energy",
                                             "est_capacity_rev": "Capacity"})
    figt = px.bar(melted, x="revenue", y="plant_name", orientation="h",
                  color="stream",
                  color_discrete_map={"Energy": "#1f77b4", "Capacity": "#ff7f0e"},
                  hover_data={"zone": True, "state": True, "revenue": ":$,.0f"},
                  labels={"revenue": "Est. revenue ($)", "plant_name": "", "stream": ""},
                  title=f"Top 20 plants — energy + capacity ({year_sel}, {market} · "
                        f"DY {plant_earnings._delivery_year(int(year_sel))})")
    figt.update_layout(height=580, barmode="stack",
                       yaxis={"categoryorder": "total ascending"}, margin=dict(t=40))
else:
    src_by_plant = (est.dropna(subset=["est_revenue"])
                    .groupby("plant_id")["source"]
                    .agg(lambda s: s.value_counts().index[0]))
    top["source"] = top["plant_id"].map(src_by_plant).fillna("Other")
    figt = px.bar(top, x="est_revenue", y="plant_name", orientation="h",
                  color="source", color_discrete_map=SOURCE_COLORS,
                  hover_data={"zone": True, "state": True, "net_generation_mwh": ":,.0f",
                              "est_rev_per_mwh": ":$,.2f", "est_revenue": ":$,.0f",
                              "plant_name": False},
                  labels={"est_revenue": "Est. energy revenue ($)", "plant_name": ""},
                  title=f"Top 20 plants by estimated energy revenue ({year_sel}, {market})")
    figt.update_layout(height=560, yaxis={"categoryorder": "total ascending"}, margin=dict(t=30))
st.plotly_chart(figt, width="stretch")

# --- plant table + export ---------------------------------------------------
st.subheader("Plant detail")
st.caption("One row per plant. $/MWh is energy revenue ÷ priced MWh."
           + (f" Capacity is RPM revenue prorated to the {n_months}-month window."
              if has_cap else ""))
rename = {
    "plant_id": "Plant ID", "plant_name": "Plant", "state": "State",
    "zone": "Zone", "zone_source": "Zone via",
    "net_generation_mwh": "Net gen (MWh)", "est_revenue": "Est. energy rev ($)",
    "est_rev_per_mwh": "Est. $/MWh"}
fmt = {"Net gen (MWh)": "{:,.0f}", "Est. energy rev ($)": "${:,.0f}",
       "Est. $/MWh": "${:,.2f}"}
if has_cap:
    rename.update({"summer_mw": "Summer MW", "ucap_mw": "UCAP MW",
                   "est_capacity_rev": "Est. capacity rev ($)",
                   "est_total_rev": "Est. total rev ($)"})
    fmt.update({"Summer MW": "{:,.0f}", "UCAP MW": "{:,.0f}",
                "Est. capacity rev ($)": "${:,.0f}", "Est. total rev ($)": "${:,.0f}"})
    drop_cols = ["delivery_year"]
    tbl = plants.drop(columns=[c for c in drop_cols if c in plants.columns]).rename(columns=rename)
else:
    tbl = plants.rename(columns=rename)
# Direct link to each plant's EIA Electricity Data Browser page.
tbl["EIA page"] = tbl["Plant ID"].map(plant_earnings.eia_plant_url)
st.dataframe(
    tbl.style.format(fmt), width="stretch", hide_index=True,
    column_config={
        "EIA page": st.column_config.LinkColumn(
            "EIA page", display_text="View ↗",
            help="Open this plant's generation & fuel history on eia.gov"),
    })
st.download_button(
    "⬇️ Download plant earnings (CSV)",
    tbl.to_csv(index=False).encode(),
    file_name=f"pjm_plant_earnings_{year_sel}_{market}.csv", mime="text/csv")

# --- plant → zone overrides -------------------------------------------------
with st.expander("🔧 Refine plant → zone mapping"):
    st.markdown(
        "Each plant is priced at its **PJM zone's** LMP. Plants are mapped by "
        "their state's *dominant* zone by default — pin individual plants to "
        "their true zone here (this changes which zone LMP values them). "
        "Overrides are saved to `data/zone_prices/plant_zone_overrides.csv`.")
    overrides = plant_zones.load_overrides()
    st.caption(f"{len(overrides)} override(s) set · state defaults: "
               + ", ".join(f"{k}→{v}" for k, v in plant_zones.STATE_DEFAULT_ZONE.items()))
    oc1, oc2 = st.columns([2, 1])
    pid_options = (plants[["plant_id", "plant_name", "state", "zone"]]
                   .drop_duplicates("plant_id"))
    pid_options["label"] = (pid_options["plant_name"] + " (" + pid_options["state"]
                            + ", id " + pid_options["plant_id"].astype(str) + ")")
    pick = oc1.selectbox("Plant", pid_options["label"].tolist(),
                         index=None, placeholder="Choose a plant to re-zone…")
    zone_pick = oc2.selectbox("Zone", sorted(ZONE_PNODE_IDS.keys()))
    if pick and st.button("Save override"):
        pid = pid_options.loc[pid_options["label"] == pick, "plant_id"].iloc[0]
        overrides[str(pid)] = zone_pick
        plant_zones.save_overrides(overrides)
        st.cache_data.clear()
        st.success(f"{pick} → {zone_pick}. Recomputing…")
        st.rerun()
