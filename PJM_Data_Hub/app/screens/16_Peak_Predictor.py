"""PJM 5CP Peak Predictor — will the next two weeks set a coincident peak?

PJM's five coincident peaks (5CP) set capacity obligations, and they land on the
hottest afternoons. This screen turns that relationship into a look-ahead: it
fits daily RTO peak load against daily-max apparent temperature on all history,
then applies the Open-Meteo **forecast** (up to +16 days) to predict each coming
day's peak and the probability it cracks the current summer's top-5 threshold.

For a load that manages Peak Load Contribution, a high-risk day is the signal to
curtail — shaving load during a coincident peak lowers next year's capacity bill.

Reads the local RTO load store and the ERA5 weather store (for the fit); pulls
live forecast weather on the fly.
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _common  # noqa: F401

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from pjm_core import peak, prediction_log
from datasets.weather import pjm_weather

st.title("🔮 PJM 5CP Peak Predictor")
st.caption("Fits daily RTO peak load to apparent temperature, then applies the "
           "16-day weather forecast to flag days that may set a coincident peak "
           "(5CP) — your window to curtail and cut capacity cost.")


@st.cache_data(show_spinner=True)
def _load():
    return peak.rto_hourly_load()


@st.cache_data(show_spinner=True)
def _model_and_forecast(days: int):
    load = _load()
    if load.empty:
        return None, None, pd.DataFrame()
    model = peak.fit_load_temp_model(load=load)
    fc = pjm_weather.forecast_weighted(days=days)
    pred = peak.predict_upcoming(days=days, load=load, model=model, forecast_wx=fc)
    return load, model, pred


load = _load()
if load.empty:
    _common.empty_state(
        st, "No RTO load data yet — needed to predict peaks.",
        hint="Run 'Update System Load' on the API Keys page.",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

horizon = st.slider("Forecast horizon (days)", min_value=3, max_value=16, value=14,
                    help="Open-Meteo forecast reaches 16 days; skill fades past ~7.")
load, model, pred = _model_and_forecast(horizon)

if model is None:
    st.warning("Not enough overlapping load + weather history to fit the model. "
               "Run **Update Weather (ERA5)** on the API Keys page.")
    st.stop()

year = pd.Timestamp.now().year
threshold = peak.current_threshold(load, year) or 0.0

# --- Headline: current bar & the hottest upcoming day -----------------------
k = st.columns(3)
k[0].metric(f"{year} 5CP threshold", f"{threshold:,.0f} MW",
            help="The 5th-highest RTO daily peak so far this summer — a new day "
                 "must beat this to enter the top 5 and reshape capacity costs.")
if not pred.empty:
    top = pred.loc[pred["prob_5cp"].idxmax()]
    k[1].metric("Highest-risk upcoming day",
                pd.to_datetime(top["day"]).strftime("%a %b %d"),
                help=f"Forecast apparent {top['tmax_apparent_f']:.0f} °F → "
                     f"predicted {top['predicted_peak_mw']:,.0f} MW.")
    k[2].metric("Its 5CP probability", f"{top['prob_5cp']*100:.0f}%",
                help="Chance that day's peak beats the current threshold, from "
                     "the model's historical residual spread.")

if pred.empty:
    st.info("No summer days in the forecast horizon — the 5CP window is "
            "June 1 – Sept 30. Check back in season.")
    st.stop()

# Silently log today's forecast so it can be scored later. Idempotent per day
# (page refreshes don't double-log). Never fail the render if logging errors.
try:
    prediction_log.log_forecast(pred)
except Exception:
    pass

# --- Risk table -------------------------------------------------------------
st.subheader("Next days ranked by 5CP risk")
disp = pred.copy()
disp["Date"] = pd.to_datetime(disp["day"]).dt.strftime("%a %b %d")
disp["Risk"] = [
    "⚪ Not eligible" if not e else peak.risk_label(p)
    for e, p in zip(disp["eligible"], disp["prob_5cp"])]
disp["Air temp °F"] = disp.get("tmax_f", pd.NA)
disp["Apparent °F"] = disp["tmax_apparent_f"]
disp["Predicted peak (MW)"] = disp["predicted_peak_mw"]
disp["Margin vs 5CP (MW)"] = disp["margin_mw"]
disp["5CP prob"] = disp["prob_5cp"]
def _note(row):
    parts = []
    if not row["eligible"]:
        if row.get("holiday"):
            parts.append(f"Not eligible — {row['holiday']} (observed holiday)")
        else:
            parts.append("Not eligible — weekend")
    if row["extrapolated"]:
        parts.append("⚠ beyond training range")
    return " · ".join(parts)
disp["Note"] = disp.apply(_note, axis=1)
cols = ["Date", "Risk", "Air temp °F", "Apparent °F", "Predicted peak (MW)",
        "Margin vs 5CP (MW)", "5CP prob", "Note"]
st.dataframe(
    disp[cols].style.format({
        "Air temp °F": "{:.0f}", "Apparent °F": "{:.0f}",
        "Predicted peak (MW)": "{:,.0f}",
        "Margin vs 5CP (MW)": "{:+,.0f}", "5CP prob": "{:.0%}",
    }),
    use_container_width=True, hide_index=True)
st.caption("Risk bands: 🔴 High ≥66% · 🟠 Elevated ≥33% · 🟡 Watch ≥10% · 🟢 Low. "
           "Forecast skill fades past ~7 days; treat the far tail as directional.")

# --- Predicted peaks vs the current threshold -------------------------------
st.subheader("Predicted daily peak vs. the 5CP threshold")
fig = go.Figure()
colors = ["#555" if not e else "#d62728" if p >= 0.66 else "#ff7f0e" if p >= 0.33
          else "#e8c400" if p >= 0.10 else "#2ca02c"
          for e, p in zip(pred["eligible"], pred["prob_5cp"])]
fig.add_bar(x=pd.to_datetime(pred["day"]), y=pred["predicted_peak_mw"],
            marker_color=colors, name="Predicted peak",
            hovertemplate="%{x|%a %b %d}<br>%{y:,.0f} MW<extra></extra>")
fig.add_hline(y=threshold, line_dash="dash", line_color="#888",
              annotation_text=f"5CP threshold {threshold:,.0f} MW",
              annotation_position="top left")
fig.update_layout(height=420, margin=dict(t=30),
                  yaxis_title="RTO peak load (MW)", xaxis_title=None,
                  showlegend=False)
st.plotly_chart(fig, use_container_width=True)

# --- Model diagnostics ------------------------------------------------------
with st.expander("Model & method"):
    st.markdown(
        f"""
- **Model:** quadratic fit of *daily RTO peak (MW)* on *daily-max apparent
  temperature (°F)*, trained on **{model.n:,} summer days** across all history,
  plus a **weekend/holiday term** ({model.offday_offset:+,.0f} MW at equal
  temperature) so a hot Saturday isn't scored like a hot weekday.
- **5CP eligibility:** per PJM, the 5 CPs are drawn **only from non-holiday
  weekdays** — weekends and observed holidays are *never* eligible, however hot
  they run, so they're gated to 0% here and excluded from the threshold.
  Independence Day is observed on the nearest weekday when Jul 4 is a weekend
  (e.g. **Fri Jul 3, 2026** is the ineligible day, per PJM's member notice);
  Labor Day is the first Monday of September.
- **Residual spread:** ±{model.resid_std:,.0f} MW (1σ) — used to turn a
  predicted peak into a 5CP probability against the current threshold.
- **Training temp range:** {model.t_min:.0f}–{model.t_max:.0f} °F apparent;
  days hotter than that are extrapolation (flagged ⚠ above).
- **Weather:** live Open-Meteo forecast, population-weighted across PJM load
  centers — same weighting as the 5CP & Weather screen.
- **Caveat:** this is weather-driven only; it doesn't model economic load
  growth, large new interconnections, or demand response already dispatched.
"""
    )

st.download_button(
    "⬇ Download prediction (CSV)",
    disp[cols].to_csv(index=False).encode(),
    file_name=f"pjm_5cp_prediction_{pd.Timestamp.now().date()}.csv",
    mime="text/csv")
