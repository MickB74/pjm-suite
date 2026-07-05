"""PJM 5 Coincident Peaks (5CP) with weather.

PJM sets each load's **capacity obligation** (Peak Load Contribution) from its
demand during the **five highest PJM RTO peak-load hours of the summer**
(June 1 – Sept 30) — the "5 Coincident Peaks". Those five hours land on the
hottest, most humid afternoons, so this screen finds them and shows the ERA5
weather that drove them: population-weighted PJM temperature, apparent (heat-
index) temperature, and the Dominion-zone temperature.

Reads the local load store (RTO zone, required) and the ERA5 weather store
(optional — enriches with temperature). 5CP directly drives the capacity cost
on the Capacity (RPM) screen.
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

from pjm_core import paths
from datasets.load import pjm_load
from datasets.weather import pjm_weather

SUMMER_START = (6, 1)   # June 1
SUMMER_END = (9, 30)    # Sept 30 — PJM planning-period summer for 5CP
N_CP = 5

st.title("🌡️ PJM 5 Coincident Peaks (5CP) & Weather")
st.caption("The five highest RTO summer peak hours set capacity obligations "
           "(Peak Load Contribution). Here they are, with the ERA5 weather that "
           "drove them.")


@st.cache_data(show_spinner=True)
def _rto_load() -> pd.DataFrame:
    df = pjm_load.load_store()
    if df.empty:
        return df
    df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
    rto = df[df["zone"] == "RTO"]
    return (rto.groupby("datetime_beginning_ept", as_index=False)["mw"].sum()
            .sort_values("datetime_beginning_ept").reset_index(drop=True))


@st.cache_data(show_spinner=True)
def _weather(zone=None) -> pd.DataFrame:
    return pjm_weather.weighted_temp(zone=zone)


load = _rto_load()
if load.empty:
    _common.empty_state(
        st, "No RTO load data yet — needed for 5CP.",
        hint="Run 'Update System Load' on the API Keys page.",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

wx = _weather()          # PJM population-weighted
wx_dom = _weather("DOM")  # Dominion zone
has_wx = not wx.empty

dmin, dmax = load["datetime_beginning_ept"].min().date(), load["datetime_beginning_ept"].max().date()
_common.data_status(st, path=paths.LOAD_PARQUET, rows=len(load), span=(dmin, dmax))
if not has_wx:
    st.warning("No weather data yet — 5CP will show without temperature. "
               "Run **Update Weather (ERA5)** on the API Keys page to enrich it.")


def _summer_bounds(year: int):
    return (pd.Timestamp(year, *SUMMER_START),
            pd.Timestamp(year, SUMMER_END[0], SUMMER_END[1], 23, 59, 59))


def _daily_peaks(year: int) -> pd.DataFrame:
    """RTO peak-load hour per day across the summer window of `year`."""
    lo, hi = _summer_bounds(year)
    sub = load[(load["datetime_beginning_ept"] >= lo) & (load["datetime_beginning_ept"] <= hi)].copy()
    if sub.empty:
        return sub
    sub["day"] = sub["datetime_beginning_ept"].dt.date
    peaks = sub.loc[sub.groupby("day")["mw"].idxmax()].copy()
    return peaks.rename(columns={"datetime_beginning_ept": "peak_hour", "mw": "peak_mw"})


def _enrich_weather(df: pd.DataFrame) -> pd.DataFrame:
    """Attach PJM & DOM temperature at each row's peak_hour."""
    out = df.copy()
    if has_wx:
        w = wx.rename(columns={"datetime_beginning_ept": "peak_hour",
                               "temp_f": "pjm_temp_f", "apparent_f": "pjm_apparent_f",
                               "rh_pct": "pjm_rh"})
        out = out.merge(w[["peak_hour", "pjm_temp_f", "pjm_apparent_f", "pjm_rh"]],
                        on="peak_hour", how="left")
    if not wx_dom.empty:
        wd = wx_dom.rename(columns={"datetime_beginning_ept": "peak_hour", "temp_f": "dom_temp_f"})
        out = out.merge(wd[["peak_hour", "dom_temp_f"]], on="peak_hour", how="left")
    return out


# --- Year selection ---------------------------------------------------------
summers = sorted({d.year for d in pd.to_datetime(load["datetime_beginning_ept"]).dt.date
                  if (d.month, d.day) >= SUMMER_START and (d.month, d.day) <= SUMMER_END},
                 reverse=True)
# A summer counts only if the window has meaningful coverage.
summers = [y for y in summers if not _daily_peaks(y).empty]
if not summers:
    st.warning("No summer (Jun–Sep) load data in the store yet.")
    st.stop()

year = st.selectbox("Summer", summers, index=0)

peaks = _daily_peaks(year)
peaks = peaks.sort_values("peak_mw", ascending=False).reset_index(drop=True)
cp = _enrich_weather(peaks.head(N_CP).copy())
cp.insert(0, "rank", range(1, len(cp) + 1))

# --- 5CP KPI + table --------------------------------------------------------
plc_basis = cp["peak_mw"].mean()
k = st.columns(3)
k[0].metric(f"{year} 5CP average (PLC basis)", f"{plc_basis:,.0f} MW",
            help="Average RTO load across the 5 coincident peaks — the basis for "
                 "Peak Load Contribution / capacity obligations.")
k[1].metric("Highest single peak", f"{cp['peak_mw'].max():,.0f} MW",
            help=str(cp.loc[cp['peak_mw'].idxmax(), 'peak_hour']))
if has_wx and cp["pjm_apparent_f"].notna().any():
    k[2].metric("Avg apparent temp @ 5CP", f"{cp['pjm_apparent_f'].mean():.0f} °F",
                help="Population-weighted PJM heat-index across the 5 peaks.")

st.subheader(f"{year} — Five Coincident Peaks")
disp = cp.copy()
disp["date"] = pd.to_datetime(disp["peak_hour"]).dt.strftime("%a %b %d, %Y")
disp["hour_ept"] = pd.to_datetime(disp["peak_hour"]).dt.strftime("%H:00")
cols = ["rank", "date", "hour_ept", "peak_mw"]
rename = {"rank": "Rank", "date": "Date", "hour_ept": "Peak hr (EPT)", "peak_mw": "RTO peak (MW)"}
fmts = {"RTO peak (MW)": "{:,.0f}"}
if has_wx:
    cols += ["pjm_temp_f", "pjm_apparent_f", "pjm_rh"]
    rename |= {"pjm_temp_f": "PJM temp °F", "pjm_apparent_f": "Apparent °F", "pjm_rh": "RH %"}
    fmts |= {"PJM temp °F": "{:.0f}", "Apparent °F": "{:.0f}", "RH %": "{:.0f}"}
if "dom_temp_f" in disp.columns:
    cols += ["dom_temp_f"]; rename |= {"dom_temp_f": "DOM temp °F"}; fmts |= {"DOM temp °F": "{:.0f}"}
st.dataframe(disp[cols].rename(columns=rename).style.format(fmts),
             use_container_width=True, hide_index=True)

# --- Load vs temperature scatter (the driver) -------------------------------
if has_wx:
    st.subheader("Why these days: daily peak load vs. temperature")
    st.caption("Each dot is a summer day (its RTO peak hour). The 5CP sit in the "
               "hot, high-load corner — weather drives the capacity-setting peaks.")
    dp = _enrich_weather(peaks.copy())
    dp["is_5cp"] = False
    dp.loc[dp.index[:N_CP], "is_5cp"] = True
    dp["Day type"] = dp["is_5cp"].map({True: "5CP day", False: "Other summer day"})
    xcol = "pjm_apparent_f" if dp["pjm_apparent_f"].notna().any() else "pjm_temp_f"
    fig = px.scatter(dp.dropna(subset=[xcol]), x=xcol, y="peak_mw", color="Day type",
                     color_discrete_map={"5CP day": "#d62728", "Other summer day": "#8c9bab"},
                     hover_data={"peak_hour": True},
                     labels={xcol: "PJM apparent temp at daily peak (°F)", "peak_mw": "RTO daily peak (MW)"},
                     title=f"{year} summer: peak load vs. apparent temperature")
    fig.update_traces(marker=dict(size=9, line=dict(width=0.5, color="#fff")))
    fig.update_layout(height=440, margin=dict(t=40))
    st.plotly_chart(fig, use_container_width=True)

# --- Intraday load + temperature on the 5CP days ----------------------------
st.subheader(f"{year} 5CP days — intraday load & temperature")
cp_days = pd.to_datetime(cp["peak_hour"]).dt.date.tolist()
lo, hi = _summer_bounds(year)
day_mask = load["datetime_beginning_ept"].dt.date.isin(cp_days)
ld = load[day_mask].copy()
ld["day"] = ld["datetime_beginning_ept"].dt.date.astype(str)
ld["hour"] = ld["datetime_beginning_ept"].dt.hour
figl = px.line(ld, x="hour", y="mw", color="day",
               labels={"hour": "Hour of day (EPT)", "mw": "RTO load (MW)", "day": "5CP day"},
               title="Load shape on each 5CP day")
figl.update_layout(height=380, margin=dict(t=40))
st.plotly_chart(figl, use_container_width=True)

# --- Historical 5CP across all summers --------------------------------------
st.divider()
st.subheader("Historical 5CP by summer")
last_load_day = pd.to_datetime(load["datetime_beginning_ept"]).max().date()
hist_rows = []
for y in summers:
    p = _daily_peaks(y).sort_values("peak_mw", ascending=False).head(N_CP)
    p = _enrich_weather(p)
    # A summer is provisional if load coverage stops before Sept 30 of that year.
    partial = last_load_day < pd.Timestamp(y, SUMMER_END[0], SUMMER_END[1]).date()
    hist_rows.append({
        "Summer": f"{y} ⚠ partial" if partial else str(y),
        "5CP avg (MW)": p["peak_mw"].mean(),
        "Top peak (MW)": p["peak_mw"].max(),
        "5CP dates": ", ".join(pd.to_datetime(p["peak_hour"]).dt.strftime("%b %d").tolist()),
        "Avg apparent °F": p["pjm_apparent_f"].mean() if "pjm_apparent_f" in p and p["pjm_apparent_f"].notna().any() else float("nan"),
    })
hist = pd.DataFrame(hist_rows)
st.dataframe(
    hist.style.format({"5CP avg (MW)": "{:,.0f}", "Top peak (MW)": "{:,.0f}", "Avg apparent °F": "{:.0f}"}),
    use_container_width=True, hide_index=True)
if (last_load_day.month, last_load_day.day) < SUMMER_END:
    st.caption("⚠ *partial* = summer still in progress (or load data ends before "
               "Sep 30); its 5CP is provisional and will shift as more days settle.")

st.download_button(
    "⬇ Download 5CP table (CSV)",
    disp[cols].rename(columns=rename).to_csv(index=False).encode(),
    file_name=f"pjm_5cp_{year}.csv", mime="text/csv")

st.caption("5CP window: Jun 1 – Sep 30 (PJM planning-period summer). Weather is "
           "ERA5 reanalysis (Open-Meteo), population-weighted across PJM load "
           "centers. PLC/obligation math uses your metered share of these hours.")
