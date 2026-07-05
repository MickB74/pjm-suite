"""PJM heating & cooling degree days (HDD / CDD).

Turn the stored ERA5 weather into a demand signal. Degree days measure how far
each day sits below (heating) or above (cooling) a 65 °F comfort base, in
°F-days — the standard way to weather-normalise load and to compare how warm or
cold one season ran versus another.

    HDD = max(0, base − Tavg)      CDD = max(0, Tavg − base)

with Tavg the NOAA daily mean (Tmin + Tmax)/2 of the population-weighted air
temperature. This screen charts daily and monthly HDD/CDD, relates them to
metered load (the payoff — cooling degree days are what drive PJM's summer
peak), and shows year-over-year seasonal totals so warm/cold years stand out.

Reads the local ERA5 weather store (required) and the system-load store (used
for the degree-days-vs-load analysis when present).
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _common  # noqa: F401

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from pjm_core import paths, weather_points
from datasets.weather import pjm_weather

try:
    from datasets.load import pjm_load
except Exception:  # dataset optional for the correlation section
    pjm_load = None

HDD_COLOR = "#4169e1"   # heating — blue
CDD_COLOR = "#d62728"   # cooling — red

st.title("🌤️ PJM Degree Days (HDD / CDD)")
st.caption("Heating & cooling degree days from the population-weighted ERA5 "
           "weather store — the exogenous driver behind seasonal load.")


# --- Data loaders (cached) --------------------------------------------------
@st.cache_data(show_spinner=True)
def _degree_days(zone: str | None, base: float) -> pd.DataFrame:
    dd = pjm_weather.degree_days(zone=zone, base=base)
    if not dd.empty:
        dd["date"] = pd.to_datetime(dd["date"])
    return dd


@st.cache_data(show_spinner=True)
def _daily_load(zone: str) -> pd.DataFrame:
    """Daily mean & peak metered load (MW) for one load zone."""
    if pjm_load is None:
        return pd.DataFrame()
    try:
        df = pjm_load.load_store()
    except Exception:
        return pd.DataFrame()
    if df.empty or "zone" not in df.columns:
        return pd.DataFrame()
    df = df[df["zone"] == zone].copy()
    if df.empty:
        return pd.DataFrame()
    df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
    # One system value per hour (sum sub-areas), then collapse to a day.
    hourly = df.groupby("datetime_beginning_ept", as_index=False)["mw"].sum()
    hourly["date"] = hourly["datetime_beginning_ept"].dt.floor("D")
    g = hourly.groupby("date")["mw"]
    return pd.DataFrame({"date": list(g.groups.keys()),
                         "load_mean_mw": g.mean().to_numpy(),
                         "load_peak_mw": g.max().to_numpy()})


# --- Zone selection ---------------------------------------------------------
# Weather zones (the cities we sample) map 1:1 to load-store zone codes, so the
# same code drives both the degree-day footprint and the load join. "PJM system"
# uses the whole pop-weighted footprint and the RTO (true system) load.
wx_zones = sorted(weather_points.points()["zone"].dropna().unique())
zone_labels = {
    "CE": "COMED (Chicago)", "PEP": "Pepco (DC)", "PE": "PECO (Philadelphia)",
    "PS": "PSEG (N. Jersey)", "BC": "BGE (Baltimore)", "DUQ": "Duquesne (Pittsburgh)",
    "DEOK": "DEOK (Cincinnati)", "AEP": "AEP (Columbus)", "ATSI": "ATSI (Cleveland)",
    "DOM": "Dominion (Virginia)", "PL": "PPL (Allentown)", "DAY": "Dayton",
    "JC": "JCP&L (Trenton)",
}
zone_options = ["PJM system"] + [f"{zone_labels.get(z, z)} · {z}" for z in wx_zones]
_option_to_zone = {"PJM system": None,
                   **{f"{zone_labels.get(z, z)} · {z}": z for z in wx_zones}}

with st.container(border=True):
    c1, c2, c3 = st.columns([2, 1, 2])
    zone_choice = c1.selectbox(
        "Footprint", zone_options,
        help="Which weather footprint to compute degree days over. "
             "'PJM system' is the whole pop-weighted region (joins to RTO load); "
             "each zone uses only that zone's load-center cities.")
    wx_zone = _option_to_zone[zone_choice]
    load_zone = "RTO" if wx_zone is None else wx_zone
    base = c2.number_input(
        "Base temp (°F)", min_value=50.0, max_value=75.0,
        value=pjm_weather.DEFAULT_BASE_F, step=1.0,
        help="Comfort base. HDD counts °F below it, CDD °F above it. "
             "US convention is 65 °F.")

# Full-history degree days (cached) — window is applied below.
dd_all = _degree_days(wx_zone, float(base))
if dd_all.empty:
    _common.empty_state(
        st, "No weather data yet — needed to compute degree days.",
        hint="Run the ERA5 weather update (auto-refreshes on app open, or use the API Keys page).",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

dmin = dd_all["date"].min().date()
dmax = dd_all["date"].max().date()
_common.data_status(st, path=paths.WEATHER_PARQUET, rows=len(dd_all), span=(dmin, dmax))

with st.container(border=True):
    start, end = _common.period_picker(st, key="dd", min_year=dmin.year, default_mode="Year")

win = dd_all[(dd_all["date"].dt.date >= start) & (dd_all["date"].dt.date <= end)].copy()
if win.empty:
    st.warning("No degree-day data in that window.")
    st.stop()

# --- KPIs -------------------------------------------------------------------
total_hdd = win["hdd"].sum()
total_cdd = win["cdd"].sum()
warmest = win.loc[win["cdd"].idxmax()]
coldest = win.loc[win["hdd"].idxmax()]

k = st.columns(4)
k[0].metric("Σ Cooling degree days", f"{total_cdd:,.0f} CDD",
            help="Total °F-days above base over the window — the summer cooling burden.")
k[1].metric("Σ Heating degree days", f"{total_hdd:,.0f} HDD",
            help="Total °F-days below base over the window — the winter heating burden.")
k[2].metric("Warmest day", f"{warmest['cdd']:.0f} CDD",
            help=f"{warmest['date'].date()} · Tavg {warmest['tavg_f']:.0f} °F "
                 f"(hi {warmest['tmax_f']:.0f})")
k[3].metric("Coldest day", f"{coldest['hdd']:.0f} HDD",
            help=f"{coldest['date'].date()} · Tavg {coldest['tavg_f']:.0f} °F "
                 f"(lo {coldest['tmin_f']:.0f})")

# --- Daily HDD/CDD ----------------------------------------------------------
st.subheader("Daily degree days")
figd = go.Figure()
figd.add_trace(go.Bar(x=win["date"], y=win["cdd"], name="CDD (cooling)",
                      marker_color=CDD_COLOR))
figd.add_trace(go.Bar(x=win["date"], y=-win["hdd"], name="HDD (heating)",
                      marker_color=HDD_COLOR,
                      customdata=win["hdd"],
                      hovertemplate="%{x|%Y-%m-%d}<br>HDD %{customdata:.0f}<extra></extra>"))
figd.update_layout(
    barmode="relative", height=360, margin=dict(t=30),
    yaxis=dict(title="°F-days  (cooling ▲ / heating ▼)"),
    legend=dict(orientation="h", y=-0.2))
st.plotly_chart(figd, use_container_width=True)

# --- Monthly totals ---------------------------------------------------------
st.subheader("Monthly totals")
mon = win.copy()
mon["month"] = mon["date"].dt.to_period("M").dt.to_timestamp()
monthly = mon.groupby("month", as_index=False)[["hdd", "cdd"]].sum()
figm = go.Figure()
figm.add_trace(go.Bar(x=monthly["month"], y=monthly["cdd"], name="CDD", marker_color=CDD_COLOR))
figm.add_trace(go.Bar(x=monthly["month"], y=monthly["hdd"], name="HDD", marker_color=HDD_COLOR))
figm.update_layout(barmode="group", height=320, margin=dict(t=30),
                   yaxis=dict(title="°F-days"), legend=dict(orientation="h", y=-0.2))
st.plotly_chart(figm, use_container_width=True)

# --- Degree days vs load ----------------------------------------------------
st.subheader("Degree days vs. load")
load_df = _daily_load(load_zone)
if load_df.empty:
    st.info(f"No metered load for zone **{load_zone}** yet — update System Load to see "
            "how degree days translate into MW. (Cooling degree days are the main "
            "driver of PJM's summer peak.)")
else:
    merged = win.merge(load_df, on="date", how="inner")
    if merged.empty:
        st.info("No overlapping days between the weather and load stores in this window.")
    else:
        cc1, cc2 = st.columns(2)
        mode = cc1.radio("Driver", ["Cooling (CDD)", "Heating (HDD)"], horizontal=True)
        ycol = cc2.radio("Load metric", ["Daily peak", "Daily mean"], horizontal=True)
        xcol = "cdd" if mode.startswith("Cooling") else "hdd"
        yname = "load_peak_mw" if ycol == "Daily peak" else "load_mean_mw"
        xcolor = CDD_COLOR if xcol == "cdd" else HDD_COLOR

        sub = merged[[xcol, yname, "date", "tavg_f"]].dropna()
        sub = sub[sub[xcol] > 0]   # only days where this degree-day type is active
        if len(sub) < 3:
            st.info("Not enough active days in this window for a fit "
                    f"(only {len(sub)} with {xcol.upper()} > 0).")
        else:
            r = float(np.corrcoef(sub[xcol], sub[yname])[0, 1])
            b1, b0 = np.polyfit(sub[xcol], sub[yname], 1)
            xs = np.linspace(sub[xcol].min(), sub[xcol].max(), 50)
            figs = go.Figure()
            figs.add_trace(go.Scatter(
                x=sub[xcol], y=sub[yname], mode="markers", name="Days",
                marker=dict(color=sub["tavg_f"], colorscale="RdYlBu_r", size=7,
                            showscale=True, colorbar=dict(title="Tavg °F")),
                customdata=np.stack([sub["date"].dt.strftime("%Y-%m-%d"), sub["tavg_f"]], axis=-1),
                hovertemplate=(f"%{{customdata[0]}}<br>{xcol.upper()} %{{x:.0f}}"
                               f"<br>Load %{{y:,.0f}} MW<br>Tavg %{{customdata[1]:.0f}} °F<extra></extra>")))
            figs.add_trace(go.Scatter(x=xs, y=b0 + b1 * xs, mode="lines",
                                      name=f"fit (r={r:.2f})", line=dict(color=xcolor, dash="dash")))
            figs.update_layout(
                height=420, margin=dict(t=30),
                xaxis=dict(title=f"{xcol.upper()} (°F-days)"),
                yaxis=dict(title=f"{load_zone} {ycol.lower()} load (MW)"),
                legend=dict(orientation="h", y=-0.2))
            st.plotly_chart(figs, use_container_width=True)
            st.caption(
                f"**r = {r:.2f}** · each additional {xcol.upper()} adds ≈ "
                f"**{b1:,.0f} MW** to {load_zone} {ycol.lower()} load "
                f"(fit over {len(sub):,} days, {start} → {end}). Points coloured by "
                "daily mean temperature.")

# --- Year-over-year seasonal totals (climatology, full history) -------------
st.subheader("Year-over-year seasonal totals")
st.caption("How each year's heating and cooling burden compares — weather "
           "normalisation at a glance. Uses the full weather history, not the "
           "window above.")
yr = dd_all.copy()
yr["year"] = yr["date"].dt.year
by_year = yr.groupby("year", as_index=False)[["hdd", "cdd"]].sum()
# Flag partial years (a still-accumulating current year, or a short first year).
days_per_year = yr.groupby("year")["date"].count()
partial = days_per_year[days_per_year < 360].index.tolist()
figy = go.Figure()
figy.add_trace(go.Bar(x=by_year["year"], y=by_year["cdd"], name="CDD (cooling)",
                      marker_color=CDD_COLOR))
figy.add_trace(go.Bar(x=by_year["year"], y=by_year["hdd"], name="HDD (heating)",
                      marker_color=HDD_COLOR))
figy.update_layout(barmode="group", height=340, margin=dict(t=30),
                   xaxis=dict(title="Year", dtick=1), yaxis=dict(title="°F-days"),
                   legend=dict(orientation="h", y=-0.2))
st.plotly_chart(figy, use_container_width=True)
if partial:
    st.caption(f"⚠️ Partial year(s) (incomplete data, not full-year comparable): "
               f"{', '.join(str(p) for p in partial)}.")

with st.expander("How degree days are computed"):
    st.markdown(
        f"""
- **Daily mean** — `Tavg = (Tmin + Tmax) / 2` of the population-weighted air
  temperature across {load_zone if wx_zone else 'all PJM'} load-center cities
  (NOAA convention).
- **Heating degree days** — `HDD = max(0, base − Tavg)`; how far the day fell
  *below* the {base:.0f} °F base.
- **Cooling degree days** — `CDD = max(0, Tavg − base)`; how far it rose
  *above* the base.
- Days with fewer than 20 hourly readings are dropped so the ERA5-lag frontier
  doesn't understate a partial day.

Degree days are exposed programmatically as
`datasets.weather.pjm_weather.degree_days(start, end_excl, zone, base)` and
`forecast_degree_days(days, zone, base)` for the upcoming-weather horizon.
        """)
