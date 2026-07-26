"""5CP Predictor — accuracy scorecard.

The Peak Predictor logs its forecast every day it renders (see
``pjm_core.prediction_log``). Once each target day is in the past this screen
joins the log against realised RTO peaks and shows how well the model actually
did — MAPE on the peak MW forecast, the hit/false-alarm rate on high-probability
CP calls, and how skill decays with lead time.

Empty until the predictor has run for at least a few days; there's no
back-fill (past forecasts can't be reconstructed — the model changes as more
history accrues, and Open-Meteo doesn't archive its forecasts for us).
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


st.title("🎯 5CP Predictor — Accuracy Scorecard")
st.caption(
    "Every visit to the **5CP Peak Predictor** logs that day's forecast. "
    "Once a target day is in the past, we join the call against the realised "
    "RTO peak and score it here. Prediction accuracy earns trust — this screen "
    "makes the predictor's track record visible instead of taking it on faith."
)

load = peak.rto_hourly_load()
scored = prediction_log.scored_history(load=load)

if scored.empty:
    st.info(
        "**No scored predictions yet.** Predictions land here once their target "
        "day is in the past — visit the **5CP Peak Predictor** screen for a few "
        "days during the June–September window and check back."
    )
    st.stop()

card = prediction_log.scorecard(scored)

# ── Headline metrics ────────────────────────────────────────────────────────
st.subheader("Overall")
c = st.columns(4)
c[0].metric("Scored predictions", f"{card['n']:,}",
            help="Every (as_of, target_day) pair whose target day is now in the past.")
c[1].metric("Peak MW MAPE", f"{card['mape']:.2f}%",
            help="Mean absolute % error of the model's daily-peak MW forecast. "
                 "Lower is better.")
c[2].metric("Signed bias", f"{card['bias_mw']:+,.0f} MW",
            help="Mean of (predicted − actual). Positive → model runs hot; "
                 "negative → runs cold.")
c[3].metric("Brier score", f"{card['brier']:.3f}",
            help="Mean squared error of prob_5cp vs the 0/1 outcome. Lower "
                 "is better; 0.25 is what you'd get always guessing 50%.")

st.subheader("CP-day calls")
c = st.columns(3)
c[0].metric("High-prob calls (≥66%)", f"{card['high_calls']:,}",
            help="Days the model flagged with at least 66% CP probability.")
hit_rate = card.get("high_hit_rate")
c[1].metric("…of those, were 5CP",
            f"{hit_rate*100:.0f}%" if pd.notna(hit_rate) else "—",
            help="Hit rate on the model's most confident calls. High is good.")
recall = card.get("recall_5cp")
c[2].metric("Recall on actual 5CPs",
            f"{recall*100:.0f}%" if pd.notna(recall) else "—",
            help="Of days that ended up being real 5CPs, share the model had "
                 "flagged at ≥33% probability. High is good.")

# ── Skill vs lead time ──────────────────────────────────────────────────────
st.subheader("Skill vs forecast lead time")
st.caption("Weather forecasts lose skill past ~7 days. This shows whether the "
           "peak-MW error tracks that decay — if it does, the model's own "
           "response function is fine and the ceiling is Open-Meteo's.")

by_lead = (scored.groupby("lead_days").agg(
    n=("error_mw", "size"),
    mape=("abs_pct_error", "mean"),
    mae_mw=("error_mw", lambda s: s.abs().mean()),
).reset_index())

if not by_lead.empty:
    fig = go.Figure()
    fig.add_bar(x=by_lead["lead_days"], y=by_lead["mape"], name="MAPE (%)",
                marker_color="#3b82f6")
    fig.update_layout(
        xaxis_title="Lead time (days)",
        yaxis_title="Peak-MW MAPE (%)",
        height=320, margin=dict(l=10, r=10, t=10, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)
    st.dataframe(by_lead.rename(columns={
        "lead_days": "Lead (days)", "n": "n", "mape": "MAPE %", "mae_mw": "MAE (MW)"
    }).round(2), hide_index=True, use_container_width=True)

# ── Predicted vs actual scatter ─────────────────────────────────────────────
st.subheader("Predicted vs actual peak (MW)")
st.caption("Points on the 45° line = perfect calls. Systematic drift off the "
           "line reveals bias the headline metrics can average away.")

scatter = go.Figure()
scatter.add_scatter(
    x=scored["actual_peak_mw"], y=scored["predicted_peak_mw"],
    mode="markers",
    marker=dict(color=scored["prob_5cp"], colorscale="Reds",
                colorbar=dict(title="prob_5cp"),
                size=6, opacity=0.7),
    text=[
        f"as_of {r.as_of} → target {r.target_day} · lead {r.lead_days}d"
        for r in scored.itertuples()
    ],
    hoverinfo="text",
    name="calls",
)
lo = min(scored["actual_peak_mw"].min(), scored["predicted_peak_mw"].min())
hi = max(scored["actual_peak_mw"].max(), scored["predicted_peak_mw"].max())
scatter.add_scatter(x=[lo, hi], y=[lo, hi], mode="lines",
                    line=dict(dash="dash", color="gray"), name="perfect")
scatter.update_layout(
    xaxis_title="Actual daily peak (MW)",
    yaxis_title="Predicted daily peak (MW)",
    height=440, margin=dict(l=10, r=10, t=10, b=10),
)
st.plotly_chart(scatter, use_container_width=True)

# ── Raw scored table (for the curious) ──────────────────────────────────────
with st.expander("Scored calls (raw)"):
    show = scored[[
        "as_of", "target_day", "lead_days", "predicted_peak_mw",
        "actual_peak_mw", "error_mw", "abs_pct_error",
        "prob_5cp", "was_5cp", "eligible", "extrapolated",
    ]].copy().sort_values(["target_day", "as_of"])
    st.dataframe(show, hide_index=True, use_container_width=True)
