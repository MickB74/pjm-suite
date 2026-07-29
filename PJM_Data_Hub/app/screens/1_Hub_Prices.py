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
from pjm_core.settlement_points import (
    HUB_CLUSTERS, HUB_COORDS, HUB_HIERARCHY, PRIMARY_HUB,
)

HUB_COLORS = {
    "DOMINION HUB": "#1f77b4",
    "AEP-DAYTON HUB": "#ff7f0e",
    "WESTERN HUB": "#2ca02c",
    "EASTERN HUB": "#d62728",
    "N ILLINOIS HUB": "#9467bd",
    "CHICAGO HUB": "#8c564b",
    "NEW JERSEY HUB": "#e377c2",
    "OHIO HUB": "#17becf",
}

st.title("💵 PJM Hub Prices (Hourly LMP)")
st.caption("Real-Time and Day-Ahead hourly LMPs from PJM Data Miner 2 "
           "(api.pjm.com). LMP = Energy + Congestion + Loss.")


@st.cache_data(show_spinner=True)
def load() -> pd.DataFrame:
    if paths.HUB_PRICES_PARQUET.exists():
        df = pd.read_parquet(paths.HUB_PRICES_PARQUET)
        if not df.empty and "market" not in df.columns:
            df["market"] = "RT"  # pre-DA stores are all real-time
        return df
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
    markets = sorted(df["market"].unique())
    market = st.radio("Market", markets, index=markets.index("RT") if "RT" in markets else 0,
                      horizontal=True, help="RT = real-time hourly, DA = day-ahead hourly.")
    sel_hubs = st.multiselect("Hubs", hubs, default=[PRIMARY_HUB])
    start, end = _common.period_picker(st, key="hub", min_year=dmin.year, default_mode="Month")
    freq = st.selectbox("Resample", ["Hourly", "Daily", "Weekly"], index=1)
    component = st.selectbox("LMP Component", ["total_lmp", "energy", "congestion", "loss"],
                             index=0, help=_common.LMP_COMPONENT_HELP)
    with st.expander("What are the LMP components?"):
        st.markdown(_common.LMP_COMPONENT_HELP)
    scarcity = st.number_input("Scarcity threshold ($/MWh)", min_value=0, value=200, step=50)
    logy = st.checkbox("Log price axis", value=False)

if not sel_hubs:
    st.warning("Select at least one hub.")
    st.stop()

mask = (
    (df["market"] == market)
    & df["pnode_name"].isin(sel_hubs)
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

st.caption(f"**{start} → {end}** ({n_days} days) · {market} · {', '.join(sel_hubs)} · {component}")

st.subheader("Hub map")
map_color = st.radio(
    "Color hubs by", ["Average price", "Region cluster", "Hierarchy (nesting)"],
    horizontal=True,
    help="**Average price** shades each hub by its avg LMP over the period. "
         "**Region cluster** groups hubs that overlap the same geography. "
         "**Hierarchy (nesting)** draws lines from each broad regional hub down "
         "to the narrower trading and generation hubs nested inside it. "
         "See Markets Explained → Trading Hubs.")
all_mask = (
    (df["market"] == market)
    & (df["datetime_beginning_ept"].dt.date >= start)
    & (df["datetime_beginning_ept"].dt.date <= end)
)
map_avg = df[all_mask].groupby("pnode_name")[component].mean()
map_rows = [
    {"hub": hub, "lat": lat, "lon": lon, "price": map_avg.get(hub),
     "cluster": HUB_CLUSTERS.get(hub, "Other"),
     "level": HUB_HIERARCHY.get(hub, (0, None))[0]}
    for hub, (lat, lon) in HUB_COORDS.items()
    if hub in hubs
]
map_df = pd.DataFrame(map_rows).dropna(subset=["price"]).reset_index(drop=True)
if not map_df.empty:
    # Short label (drop the trailing "HUB") plus the price, shown next to each dot.
    map_df["short"] = map_df["hub"].str.replace(r"\s*HUB$", "", regex=True).str.title()
    map_df["label"] = map_df["short"] + "  $" + map_df["price"].round(0).astype(int).astype(str)
    common = dict(
        lat="lat", lon="lon", text="label",
        hover_name="hub", zoom=4.4, center={"lat": 39.5, "lon": -81.5},
    )
    hover = {"lat": False, "lon": False, "label": False, "short": False,
             "level": False, "cluster": True, "price": ":.2f"}
    edge = None  # optional hierarchy connector-line trace (added under markers)

    if map_color == "Region cluster":
        fig_map = px.scatter_mapbox(
            map_df, color="cluster", size=map_df["price"].abs(), size_max=34,
            hover_data=hover, color_discrete_sequence=px.colors.qualitative.Set2,
            labels={"cluster": "Region cluster"}, **common,
        )
        fig_map.update_layout(legend=dict(
            title="Region cluster", orientation="h", yanchor="bottom", y=0.01,
            xanchor="left", x=0.01, bgcolor="rgba(0,0,0,0.5)", font=dict(size=11)))
    elif map_color == "Hierarchy (nesting)":
        # Size markers by level: broad regional (0) biggest → generation node (2) smallest.
        map_df["hsize"] = map_df["level"].map({0: 30, 1: 19, 2: 12}).fillna(19)
        fig_map = px.scatter_mapbox(
            map_df, color="cluster", size="hsize", size_max=30,
            hover_data={**hover, "hsize": False},
            color_discrete_sequence=px.colors.qualitative.Set2,
            labels={"cluster": "Region cluster"}, **common,
        )
        # Build child→parent connector segments (only when both hubs are shown).
        coord = {r.hub: (r.lat, r.lon) for r in map_df.itertuples()}
        e_lat: list = []
        e_lon: list = []
        for hub in map_df["hub"]:
            parent = HUB_HIERARCHY.get(hub, (0, None))[1]
            if parent and parent in coord:
                (clat, clon), (plat, plon) = coord[hub], coord[parent]
                e_lat += [clat, plat, None]
                e_lon += [clon, plon, None]
        if e_lat:
            edge = go.Scattermapbox(
                lat=e_lat, lon=e_lon, mode="lines",
                line=dict(width=2, color="rgba(255,255,255,0.55)"),
                hoverinfo="skip", showlegend=False)
        fig_map.update_layout(legend=dict(
            title="Region cluster", orientation="h", yanchor="bottom", y=0.01,
            xanchor="left", x=0.01, bgcolor="rgba(0,0,0,0.5)", font=dict(size=11)))
    else:
        fig_map = px.scatter_mapbox(
            map_df, color="price", size=map_df["price"].abs(), size_max=34,
            hover_data=hover, color_continuous_scale="RdYlGn_r",
            labels={"price": f"Avg {component} ($/MWh)"}, **common,
        )

    # Style the marker traces (all traces so far are markers).
    fig_map.update_traces(
        mode="markers+text",
        textposition="top center",
        textfont=dict(size=13, color="white", family="Arial Black"),
        marker=dict(sizemin=12, opacity=0.95),
    )
    # Add connector lines beneath the markers (drawn first = underneath).
    if edge is not None:
        fig_map.add_trace(edge)
        fig_map.data = (fig_map.data[-1],) + fig_map.data[:-1]

    fig_map.update_layout(
        mapbox_style="carto-darkmatter", height=480,
        margin=dict(t=10, b=0, l=0, r=0),
        font=dict(color="white"),
    )
    st.plotly_chart(fig_map, width="stretch")
    if map_color == "Region cluster":
        _cap_extra = "Colors group hubs that overlap the same geography."
    elif map_color == "Hierarchy (nesting)":
        _cap_extra = ("Lines connect each broad regional hub to the narrower trading "
                      "and generation hubs nested inside it (bigger dot = broader hub).")
    else:
        _cap_extra = "Hub locations are approximate representative points, not exact nodes."
    st.caption(f"Average **{component}** by hub over {start} → {end} ({market}). {_cap_extra}")
else:
    st.caption("No data available for the map over this period.")

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
    st.plotly_chart(fig, width="stretch")

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
        # Month labels like "2026-06" parse as dates and garble the axis.
        fig2.update_yaxes(type="category")
        fig2.update_xaxes(type="category")
        fig2.update_layout(height=max(300, len(pivot) * 30), margin=dict(t=20))
        st.plotly_chart(fig2, width="stretch")
