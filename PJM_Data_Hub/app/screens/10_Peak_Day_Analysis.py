"""PJM peak day analysis.

Find the system's highest-stress days and dissect them. Peak day is keyed off
metered load (true system peak) or the hub LMP, ranked over a chosen window.
Drill into any peak day to see the coincident picture: load shape, LMP, the
generation fuel mix that met the peak, and what reserves & regulation cleared —
i.e. everything that drove cost on the day it mattered most.

Reads the local load, hub-price, fuel-mix and ancillary stores. Load is
required; the rest enrich the view when present.
"""

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

from pjm_core import paths, prices as PX
from pjm_core.settlement_points import PRIMARY_HUB
from datasets.load import pjm_load
from datasets.ancillary import pjm_as
from datasets.weather import pjm_weather

try:
    from datasets.system_gen_by_fuel.pjm_gen import load as load_gen, FUEL_COLS
except Exception:  # dataset optional
    load_gen, FUEL_COLS = None, []

FUEL_COLORS = {
    "gas": "#f4a460", "nuclear": "#9370db", "coal": "#696969",
    "hydro": "#4169e1", "wind": "#87ceeb", "solar": "#ffd700",
    "oil": "#8b4513", "other_renewables": "#3cb371", "other": "#aaa",
    "storage": "#ff69b4",
}

st.title("⛰️ PJM Peak Day Analysis")
st.caption("Rank the highest-load (or highest-price) days, then dissect the "
           "coincident load shape, LMP, fuel mix, and ancillary prices.")


@st.cache_data(show_spinner=True)
def _load_load() -> pd.DataFrame:
    return pjm_load.load_store()


@st.cache_data(show_spinner=True)
def _load_prices() -> pd.DataFrame:
    return PX.load_hub_prices(market=None)


@st.cache_data(show_spinner=True)
def _load_gen() -> pd.DataFrame:
    if load_gen is None:
        return pd.DataFrame()
    try:
        return load_gen()
    except Exception:
        return pd.DataFrame()


@st.cache_data(show_spinner=True)
def _load_as() -> pd.DataFrame:
    return pjm_as.load_store()


@st.cache_data(show_spinner=True)
def _load_wx() -> pd.DataFrame:
    return pjm_weather.weighted_temp()


load_df = _load_load()
if load_df.empty:
    _common.empty_state(
        st, "No load data yet — needed for peak-day ranking.",
        hint="Run 'Update System Load' on the API Keys page.",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

load_df["datetime_beginning_ept"] = pd.to_datetime(load_df["datetime_beginning_ept"])
zones = sorted(load_df["zone"].dropna().unique())
prices_df = _load_prices()
gen_df = _load_gen()
as_df = _load_as()
wx_df = _load_wx()
if not prices_df.empty:
    prices_df["datetime_beginning_ept"] = pd.to_datetime(prices_df["datetime_beginning_ept"])
if not wx_df.empty:
    wx_df["datetime_beginning_ept"] = pd.to_datetime(wx_df["datetime_beginning_ept"])

dmin = load_df["datetime_beginning_ept"].min().date()
dmax = load_df["datetime_beginning_ept"].max().date()
_common.data_status(st, path=paths.LOAD_PARQUET, rows=len(load_df), span=(dmin, dmax))

hubs = sorted(prices_df["pnode_name"].unique()) if not prices_df.empty else []

# --- Controls ---------------------------------------------------------------
with st.container(border=True):
    st.header("Peak selection")
    c1, c2, c3 = st.columns(3)
    basis = c1.selectbox(
        "Rank days by", ["Load peak", "Price peak"],
        help="Load peak = highest hourly metered load. Price peak = highest hourly RT LMP.")
    if basis == "Load peak":
        metric_zone = c2.selectbox(
            "Load zone", zones,
            index=zones.index("RTO") if "RTO" in zones else 0)
        metric_hub = None
    else:
        metric_hub = c2.selectbox(
            "Hub", hubs,
            index=hubs.index(PRIMARY_HUB) if PRIMARY_HUB in hubs else 0) if hubs else None
        metric_zone = None
    top_n = c3.slider("Top N days", 3, 20, 8)
    start, end = _common.period_picker(st, key="peak", min_year=dmin.year, default_mode="Year")

# --- Build the ranking metric (one value per hour) --------------------------
if basis == "Load peak":
    m = load_df[(load_df["zone"] == metric_zone)
                & (load_df["datetime_beginning_ept"].dt.date >= start)
                & (load_df["datetime_beginning_ept"].dt.date <= end)]
    metric = (m.groupby("datetime_beginning_ept", as_index=False)["mw"].sum()
              .rename(columns={"mw": "value"}))
    unit, label = "MW", f"{metric_zone} load"
else:
    if prices_df.empty or not metric_hub:
        st.warning("No hub price data — pick 'Load peak' or update hub prices.")
        st.stop()
    m = prices_df[(prices_df["pnode_name"] == metric_hub)
                  & (prices_df["market"] == "RT")
                  & (prices_df["datetime_beginning_ept"].dt.date >= start)
                  & (prices_df["datetime_beginning_ept"].dt.date <= end)]
    metric = (m[["datetime_beginning_ept", "total_lmp"]]
              .rename(columns={"total_lmp": "value"}))
    unit, label = "$/MWh", f"{metric_hub} RT LMP"

if metric.empty:
    st.warning("No data in that window.")
    st.stop()

metric["day"] = metric["datetime_beginning_ept"].dt.date
daily_peak = (metric.loc[metric.groupby("day")["value"].idxmax()]
              .rename(columns={"datetime_beginning_ept": "peak_hour",
                               "value": "peak_value"}))
daily_mean = metric.groupby("day", as_index=False)["value"].mean().rename(columns={"value": "avg_value"})
ranked = (daily_peak.merge(daily_mean, on="day")
          .sort_values("peak_value", ascending=False).head(top_n).reset_index(drop=True))
ranked["peak_hour_ept"] = pd.to_datetime(ranked["peak_hour"]).dt.strftime("%H:00")

st.subheader(f"Top {len(ranked)} {basis.lower()} days · {label}")
show = ranked[["day", "peak_hour_ept", "peak_value", "avg_value"]].copy()
fmt = "${:,.2f}" if unit == "$/MWh" else "{:,.0f}"
st.dataframe(
    show.rename(columns={"day": "Date", "peak_hour_ept": "Peak hr (EPT)",
                         "peak_value": f"Peak ({unit})", "avg_value": f"Day avg ({unit})"})
    .style.format({f"Peak ({unit})": fmt, f"Day avg ({unit})": fmt}),
    use_container_width=True)

# --- Drill into one peak day ------------------------------------------------
st.divider()
day_choices = ranked["day"].tolist()
sel_day = st.selectbox("Dissect a peak day", day_choices,
                       format_func=lambda d: f"{d}  (peak {ranked.loc[ranked['day']==d,'peak_value'].iloc[0]:,.0f} {unit})")
peak_hour = pd.to_datetime(ranked.loc[ranked["day"] == sel_day, "peak_hour"].iloc[0])

day_start = pd.Timestamp(sel_day)
day_end = day_start + pd.Timedelta(days=1)


def _day_slice(df, tcol="datetime_beginning_ept"):
    if df is None or df.empty:
        return pd.DataFrame()
    return df[(df[tcol] >= day_start) & (df[tcol] < day_end)]


# Coincident-peak KPIs at the peak hour.
st.subheader(f"Coincident peak — {sel_day} {peak_hour.strftime('%H:00')} EPT")
k = st.columns(5)

load_hr = _day_slice(load_df)
if not load_hr.empty:
    rto_hr = load_hr[(load_hr["zone"] == "RTO") & (load_hr["datetime_beginning_ept"] == peak_hour)]["mw"].sum()
    dom_hr = load_hr[(load_hr["zone"] == "DOM") & (load_hr["datetime_beginning_ept"] == peak_hour)]["mw"].sum()
    k[0].metric("RTO load @ peak", f"{rto_hr:,.0f} MW" if rto_hr else "—")
    k[1].metric("DOM load @ peak", f"{dom_hr:,.0f} MW" if dom_hr else "—")

price_hr = _day_slice(prices_df)
if not price_hr.empty:
    hub_for_price = metric_hub or PRIMARY_HUB
    ph = price_hr[(price_hr["pnode_name"] == hub_for_price)
                  & (price_hr["market"] == "RT")
                  & (price_hr["datetime_beginning_ept"] == peak_hour)]
    if not ph.empty:
        k[2].metric(f"{hub_for_price} RT @ peak", f"${ph['total_lmp'].iloc[0]:,.2f}")

as_hr = _day_slice(as_df)
if not as_hr.empty:
    reg = as_hr[(as_hr["locale"] == "PJM_RTO") & (as_hr["service"] == "REG")
                & (as_hr["datetime_beginning_ept"] == peak_hour)]
    if not reg.empty:
        k[3].metric("Regulation MCP @ peak", f"${reg['mcp'].iloc[0]:,.2f}")

wx_hr = _day_slice(wx_df)
if not wx_hr.empty:
    wh = wx_hr[wx_hr["datetime_beginning_ept"] == peak_hour]
    if not wh.empty and pd.notna(wh["apparent_f"].iloc[0]):
        k[4].metric("PJM apparent temp @ peak", f"{wh['apparent_f'].iloc[0]:.0f} °F",
                    help=f"Population-weighted heat index · actual {wh['temp_f'].iloc[0]:.0f} °F"
                         + (f", RH {wh['rh_pct'].iloc[0]:.0f}%" if pd.notna(wh['rh_pct'].iloc[0]) else ""))

# Intraday load + price (dual axis).
load_day = _day_slice(load_df)
fig = go.Figure()
if not load_day.empty:
    for z in [z for z in ("RTO", "DOM") if z in load_day["zone"].unique()]:
        g = (load_day[load_day["zone"] == z]
             .groupby("datetime_beginning_ept", as_index=False)["mw"].sum())
        fig.add_trace(go.Scatter(x=g["datetime_beginning_ept"], y=g["mw"],
                                 name=f"{z} load (MW)", mode="lines"))
price_day = _day_slice(prices_df)
if not price_day.empty:
    hub_for_price = metric_hub or PRIMARY_HUB
    pg = (price_day[(price_day["pnode_name"] == hub_for_price) & (price_day["market"] == "RT")]
          .sort_values("datetime_beginning_ept"))
    if not pg.empty:
        fig.add_trace(go.Scatter(x=pg["datetime_beginning_ept"], y=pg["total_lmp"],
                                 name=f"{hub_for_price} RT ($/MWh)", mode="lines",
                                 yaxis="y2", line=dict(dash="dot", color="#d62728")))
fig.add_vline(x=peak_hour, line_dash="dash", line_color="#888")
fig.update_layout(
    title="Intraday load & price",
    height=380, margin=dict(t=40),
    yaxis=dict(title="Load (MW)"),
    yaxis2=dict(title="LMP ($/MWh)", overlaying="y", side="right"),
    legend=dict(orientation="h", y=-0.2))
st.plotly_chart(fig, use_container_width=True)

# Intraday temperature (ERA5) — the weather behind the peak.
wx_day = _day_slice(wx_df)
if not wx_day.empty and wx_day[["temp_f", "apparent_f"]].notna().any().any():
    wd = wx_day.sort_values("datetime_beginning_ept")
    figw = go.Figure()
    figw.add_trace(go.Scatter(x=wd["datetime_beginning_ept"], y=wd["temp_f"],
                              name="PJM temp (°F)", mode="lines", line=dict(color="#ff7f0e")))
    if wd["apparent_f"].notna().any():
        figw.add_trace(go.Scatter(x=wd["datetime_beginning_ept"], y=wd["apparent_f"],
                                  name="Apparent / heat index (°F)", mode="lines",
                                  line=dict(color="#d62728", dash="dot")))
    figw.add_vline(x=peak_hour, line_dash="dash", line_color="#888")
    figw.update_layout(title="Intraday temperature (ERA5, population-weighted PJM)",
                       height=280, margin=dict(t=40), yaxis=dict(title="°F"),
                       legend=dict(orientation="h", y=-0.25))
    st.plotly_chart(figw, use_container_width=True)

# Intraday fuel mix stack.
gen_day = _day_slice(gen_df)
if not gen_day.empty:
    fuel_cols = [c for c in FUEL_COLS if c in gen_day.columns]
    gl = (gen_day.set_index("datetime_beginning_ept")[fuel_cols]
          .reset_index()
          .melt(id_vars="datetime_beginning_ept", var_name="fuel", value_name="mw"))
    fig2 = px.area(gl, x="datetime_beginning_ept", y="mw", color="fuel",
                   color_discrete_map={f: FUEL_COLORS.get(f, "#aaa") for f in fuel_cols},
                   labels={"datetime_beginning_ept": "Hour (EPT)", "mw": "MW", "fuel": "Fuel"},
                   title="Intraday generation by fuel")
    fig2.add_vline(x=peak_hour, line_dash="dash", line_color="#333")
    fig2.update_layout(height=360, margin=dict(t=40))
    st.plotly_chart(fig2, use_container_width=True)

    # Fuel mix at the peak hour.
    peak_gen = gen_day[gen_day["datetime_beginning_ept"] == peak_hour]
    if not peak_gen.empty:
        mixp = (peak_gen[fuel_cols].iloc[0].rename("mw").reset_index()
                .rename(columns={"index": "fuel"}))
        mixp["share"] = mixp["mw"] / mixp["mw"].sum()
        st.caption("Fuel mix at the peak hour")
        st.dataframe(
            mixp.sort_values("mw", ascending=False).reset_index(drop=True)
            .rename(columns={"fuel": "Fuel", "mw": "MW", "share": "Share"})
            .style.format({"MW": "{:,.0f}", "Share": "{:.1%}"}),
            use_container_width=True, height=300, hide_index=True)

# Ancillary prices across the peak day.
as_day = _day_slice(as_df)
if not as_day.empty:
    ad = as_day[as_day["locale"] == "PJM_RTO"]
    if not ad.empty:
        ad = ad.assign(product=ad["service"].map(lambda s: f"{s} — {pjm_as.SERVICE_LABELS.get(s, s)}"))
        fig3 = px.line(ad.sort_values("datetime_beginning_ept"),
                       x="datetime_beginning_ept", y="mcp", color="product",
                       labels={"datetime_beginning_ept": "Hour (EPT)", "mcp": "MCP ($/MWh)", "product": "Product"},
                       title="Intraday ancillary clearing prices (PJM_RTO)")
        fig3.add_vline(x=peak_hour, line_dash="dash", line_color="#333")
        fig3.update_layout(height=340, margin=dict(t=40))
        st.plotly_chart(fig3, use_container_width=True)
