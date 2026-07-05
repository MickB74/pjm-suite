"""PJM interconnection (New Services) queue — current state + changes over time."""

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
from datasets.queue import pjm_queue

st.title("🔌 PJM Interconnection Queue")
st.caption("Every generation & storage project that has applied to connect to the "
           "PJM grid — active, in-service, and withdrawn. Sourced from PJM's public "
           "New Services queue. Each refresh is archived as a dated snapshot, so the "
           "queue's evolution is tracked over time.")


@st.cache_data(show_spinner=True)
def load_all() -> pd.DataFrame:
    return pjm_queue.load_store()


store = load_all()
if store.empty:
    _common.empty_state(
        st, "No queue data yet.",
        hint="Run 'Update Interconnection Queue' on the API Keys page.",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

snaps = pjm_queue.snapshot_dates(store)
latest_snap = snaps[-1] if snaps else "—"
st.caption(f"**{len(snaps)} snapshot(s)** · latest {latest_snap} · "
           f"last modified {pd.Timestamp(paths.QUEUE_PARQUET.stat().st_mtime, unit='s').strftime('%Y-%m-%d %H:%M') if paths.QUEUE_PARQUET.exists() else 'never'}")

tab_now, tab_time = st.tabs(["📋 Current queue", "📈 Changes over time"])

# ===========================================================================
# CURRENT QUEUE — latest snapshot
# ===========================================================================
with tab_now:
    df = pjm_queue.latest(store)

    buckets = ["Active", "Operational", "Withdrawn"]
    fuels_all = sorted(df["fuel"].dropna().unique()) if "fuel" in df.columns else []
    states_all = sorted(df["state"].dropna().unique()) if "state" in df.columns else []

    with st.container(border=True):
        st.header("Filters")
        c1, c2, c3 = st.columns(3)
        sel_buckets = c1.multiselect(
            "Lifecycle", buckets, default=["Active"],
            help="**Active** = still working through studies/construction. "
                 "**Operational** = in service. **Withdrawn** = withdrawn, "
                 "retracted, or deactivated.")
        sel_fuels = c2.multiselect("Fuel / technology", fuels_all, default=[])
        sel_states = c3.multiselect("State", states_all, default=[])

    mask = pd.Series(True, index=df.index)
    if sel_buckets:
        mask &= df["bucket"].isin(sel_buckets)
    if sel_fuels:
        mask &= df["fuel"].isin(sel_fuels)
    if sel_states:
        mask &= df["state"].isin(sel_states)
    sub = df[mask].copy()

    if sub.empty:
        st.warning("No projects match that selection.")
        st.stop()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Projects", f"{len(sub):,}")
    c2.metric("Total MW", f"{sub['mw'].sum(skipna=True):,.0f}")
    n_active = int((sub["bucket"] == "Active").sum())
    c3.metric("Active projects", f"{n_active:,}")
    active_mw = sub.loc[sub["bucket"] == "Active", "mw"].sum(skipna=True)
    c4.metric("Active MW", f"{active_mw:,.0f}")

    st.subheader("Capacity by fuel / technology")
    by_fuel = (sub.dropna(subset=["mw"]).groupby("fuel", as_index=False)
               .agg(mw=("mw", "sum"), projects=("project_id", "count"))
               .sort_values("mw", ascending=False))
    if not by_fuel.empty:
        fig = px.bar(by_fuel, x="fuel", y="mw", text="projects",
                     labels={"fuel": "Fuel", "mw": "Capacity (MW)", "projects": "Projects"},
                     title="MW by fuel (bar labels = project count)")
        fig.update_traces(textposition="outside")
        fig.update_layout(height=420, margin=dict(t=40))
        st.plotly_chart(fig, use_container_width=True)

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Capacity by state")
        by_state = (sub.dropna(subset=["mw"]).groupby("state", as_index=False)["mw"].sum()
                    .sort_values("mw", ascending=False).head(15))
        if not by_state.empty:
            figs = px.bar(by_state, x="mw", y="state", orientation="h",
                          labels={"mw": "Capacity (MW)", "state": "State"})
            figs.update_layout(height=420, margin=dict(t=20), yaxis=dict(autorange="reversed"))
            st.plotly_chart(figs, use_container_width=True)
    with col_b:
        st.subheader("Submissions by year")
        if "submitted_date" in sub.columns and sub["submitted_date"].notna().any():
            sy = sub.dropna(subset=["submitted_date"]).copy()
            sy["year"] = pd.to_datetime(sy["submitted_date"]).dt.year
            yr = (sy.groupby(["year", "fuel"], as_index=False)["mw"].sum())
            figy = px.bar(yr, x="year", y="mw", color="fuel",
                          labels={"year": "Submitted year", "mw": "Capacity (MW)", "fuel": "Fuel"})
            figy.update_layout(height=420, margin=dict(t=20))
            st.plotly_chart(figy, use_container_width=True)

    st.subheader("Projects")
    show_cols = [c for c in [
        "project_id", "name", "commercial_name", "state", "county", "status", "bucket",
        "fuel", "mw", "mw_energy", "mw_capacity", "mw_in_service", "transmission_owner",
        "submitted_date", "projected_in_service_date", "actual_in_service_date",
        "withdrawal_date",
    ] if c in sub.columns]
    table = sub[show_cols].sort_values("mw", ascending=False, na_position="last")
    st.dataframe(table, use_container_width=True, hide_index=True, height=460)
    st.download_button(
        "⬇ Download filtered queue (CSV)",
        table.to_csv(index=False).encode(),
        file_name=f"pjm_queue_{latest_snap}.csv", mime="text/csv")

# ===========================================================================
# CHANGES OVER TIME — across snapshots
# ===========================================================================
with tab_time:
    if len(snaps) < 2:
        st.info(
            f"Only **one snapshot** so far ({latest_snap}). Change-over-time views "
            "activate once a second snapshot is captured — PJM only serves the "
            "current queue state, so history builds forward each time you refresh "
            "(on the API Keys page or via auto-refresh). Check back after the next "
            "refresh on a later day.")
        st.stop()

    trend = pjm_queue.trend(store)

    st.subheader("Active queue over time")
    m1, m2 = st.columns(2)
    figt = px.line(trend, x="snapshot_date", y="active_mw", markers=True,
                   labels={"snapshot_date": "Snapshot", "active_mw": "Active MW"},
                   title="Active capacity in queue (MW)")
    figt.update_layout(height=360, margin=dict(t=40))
    m1.plotly_chart(figt, use_container_width=True)

    figc = px.line(trend, x="snapshot_date", y="active", markers=True,
                   labels={"snapshot_date": "Snapshot", "active": "Active projects"},
                   title="Active project count")
    figc.update_layout(height=360, margin=dict(t=40))
    m2.plotly_chart(figc, use_container_width=True)

    # Active MW by fuel across snapshots
    st.subheader("Active capacity by fuel over time")
    act_all = store[store["bucket"] == "Active"]
    fuel_trend = (act_all.dropna(subset=["mw"])
                  .groupby(["snapshot_date", "fuel"], as_index=False)["mw"].sum())
    figf = px.area(fuel_trend, x="snapshot_date", y="mw", color="fuel",
                   labels={"snapshot_date": "Snapshot", "mw": "Active MW", "fuel": "Fuel"})
    figf.update_layout(height=400, margin=dict(t=20))
    st.plotly_chart(figf, use_container_width=True)

    # Snapshot-to-snapshot diff
    st.subheader("What changed between two snapshots")
    d1, d2 = st.columns(2)
    date_a = d1.selectbox("From", snaps, index=0)
    date_b = d2.selectbox("To", snaps, index=len(snaps) - 1)
    if date_a >= date_b:
        st.warning("Pick an earlier 'From' date than 'To'.")
        st.stop()

    diff = pjm_queue.compare(date_a, date_b, store)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("New projects", f"{len(diff['added']):,}")
    k2.metric("Went operational", f"{len(diff['newly_operational']):,}")
    k3.metric("Withdrawn", f"{len(diff['newly_withdrawn']):,}")
    k4.metric("Status changed", f"{len(diff['changed']):,}")

    for label, key in [("🆕 New projects", "added"),
                       ("✅ Newly operational", "newly_operational"),
                       ("❌ Newly withdrawn", "newly_withdrawn"),
                       ("🔀 Status changed", "changed")]:
        frame = diff[key]
        with st.expander(f"{label} ({len(frame):,})"):
            if frame.empty:
                st.caption("None.")
            else:
                st.dataframe(frame, use_container_width=True, hide_index=True)
