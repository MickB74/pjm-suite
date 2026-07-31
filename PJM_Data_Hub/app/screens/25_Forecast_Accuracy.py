"""DOM Hub price-forecast accuracy — walk-forward backtest, kept live.

For each historical as-of date with a gas-strip vintage, the model is re-run
against only the data available at that time and each forecasted month is
compared to what actually cleared. Results persist to a parquet cache so this
screen loads fast; the sidebar exposes a **Rerun** button and refreshes
automatically when the archive has grown enough for new pairs to be evaluated.
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

from pjm_core import forecast_backtest as fb, gas_strip, paths


st.title("🎯 Price Forecast Accuracy")
st.caption(
    "Walk-forward backtest of the DOM Hub forecast. At each historical as-of "
    "date, the model is re-run against only the data available then, and each "
    "12-month-ahead forecast is compared to what actually cleared.")

if not paths.HUB_PRICES_PARQUET.exists():
    st.warning("No hub price data yet — the backtest needs realised LMPs to compare against.")
    st.stop()
if not gas_strip.vintages():
    st.warning("No gas-strip vintages archived yet — nothing to backtest against.")
    st.stop()

with st.sidebar:
    st.header("Backtest settings")
    horizon = st.slider("Forecast horizon (months)", 6, 24, 12)
    n_sims = st.select_slider("Monte Carlo paths per as-of", [500, 1_000, 2_000, 5_000], value=2_000)
    rerun = st.button("Rerun backtest", type="primary",
                      help="Re-runs the model for every archived as-of. Takes "
                           "~5-30s depending on how many as-ofs there are and "
                           "the paths slider. Otherwise this loads a cached "
                           "result and only recomputes when the archive grew.")

if rerun:
    with st.spinner(f"Running backtest ({horizon}mo horizon × {len(gas_strip.vintages())} vintages)…"):
        bt = fb.run_backtest(horizon_months=horizon, n_sims=n_sims)
        if len(bt):
            fb.save_cached(bt)
else:
    bt = fb.refresh_if_stale(horizon_months=horizon, n_sims=n_sims)

if bt is None or bt.empty:
    st.warning("Backtest produced no rows — likely too few as-ofs meet the "
               "12-month-of-prior-LMPs minimum. Wait for more data to accumulate.")
    st.stop()

bt = fb.attach_season(bt)

# ── Data-limits banner ──────────────────────────────────────────────────────
# The 'real strip only' split is aspirational until the daily archive matures
# past a full delivery cycle. Being explicit here beats letting a reader
# assume the reported bias is 'the model' rather than model+extrapolation.
n_real = int(bt["gas_from_strip"].sum())
n_total = len(bt)
if n_real == 0:
    st.info(
        "**Note on what's measured.** Every row's forecast month lies before "
        "the earliest contract in its as-of's reconstructed vintage — Yahoo "
        "delists expired contracts, so those near-month gas prices came from "
        "the model's mean-reversion fallback rather than the actual traded "
        "strip. The bias / MAE numbers below therefore measure **model + "
        "strip-extrapolation as a bundle**. This will start splitting a few "
        "months from now, as forecasts against still-live contracts from live "
        "daily pulls land in the realised window.")
elif n_real < n_total:
    st.info(
        f"**Note.** {n_real:,} of {n_total:,} rows used the actual traded "
        "strip for their forecast month's gas price; the rest used the "
        "mean-reversion extrapolation. Toggle in the summary below to compare.")

# ── Headline metrics ────────────────────────────────────────────────────────
st.subheader("Overall accuracy")
show_real_only = False
if n_real > 0:
    show_real_only = st.toggle(
        "Real-strip rows only", value=True,
        help="Restrict to forecast months whose gas contract was in the "
             "vintage, i.e. no mean-reversion extrapolation.")
summary = fb.summarise(bt, real_strip_only=show_real_only)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Bias (P50 − actual)", f"${summary['bias']:+.2f}/MWh",
          help="Positive = model reads high on average. Negative = reads low.")
c2.metric("Absolute error", f"${summary['mae']:.2f}/MWh",
          help="Mean absolute error of the P50 vs. realised.")
c3.metric("MAPE", f"{summary['mape_pct']:.1f}%",
          help="Mean absolute percentage error — normalised so winter and "
               "summer are comparable.")
c4.metric("Actuals in P10–P90 band", f"{summary['coverage_pct']:.1f}%",
          delta=f"target 80%",
          help="Fraction of realised prices that landed inside the model's "
               "80% uncertainty band. Well-calibrated forecast ≈ 80%; over-"
               "confident < 80%, under-confident > 80%.")
st.caption(f"{summary['n']:,} (as-of, forecast-month) pairs evaluated. "
           f"Backtest covers {bt['asof'].min():%b %Y} → {bt['asof'].max():%b %Y}.")

# ── Breakdowns ──────────────────────────────────────────────────────────────
def _format_breakdown(g: pd.DataFrame) -> pd.DataFrame:
    d = g.copy()
    d["bias"] = d["bias"].map(lambda x: f"${x:+.2f}")
    d["mae"] = d["mae"].map(lambda x: f"${x:.2f}")
    d["mape_pct"] = d["mape_pct"].map(lambda x: f"{x:.1f}%")
    d["coverage_pct"] = d["coverage_pct"].map(lambda x: f"{x:.1f}%")
    return d.rename(columns={"bias": "Bias", "mae": "|Err|", "mape_pct": "MAPE",
                             "coverage_pct": "In-band", "n": "n"})

col_a, col_b = st.columns(2)
with col_a:
    st.markdown("**By horizon**")
    st.caption("How badly the forecast drifts the further out it looks.")
    st.dataframe(_format_breakdown(fb.by_group(bt, "horizon", real_strip_only=show_real_only))
                 .rename(columns={"horizon": "Months out"}),
                 hide_index=True, width="stretch")
with col_b:
    st.markdown("**By season of the forecasted month**")
    st.caption("Winter carries the tail risk; the model tends to lean low on it.")
    st.dataframe(_format_breakdown(fb.by_group(bt, "season", real_strip_only=show_real_only))
                 .rename(columns={"season": "Season"}),
                 hide_index=True, width="stretch")

# ── Forecast vs actual scatter ──────────────────────────────────────────────
st.subheader("Forecast vs realised (each (as-of, month) pair)")
st.caption(
    "Each dot is one (as-of, forecast-month) pair. Colour = months-out. "
    "Dots below the diagonal = model under-forecast; above = over-forecast. "
    "Vertical error bars are the P10–P90 band — the ones crossing the "
    "diagonal are the pairs where the actual landed inside the band.")
df_show = bt if not show_real_only else bt[bt["gas_from_strip"]]
if not df_show.empty:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df_show["actual"], y=df_show["p50"], mode="markers",
        marker=dict(size=8, color=df_show["horizon"], colorscale="Viridis",
                    colorbar=dict(title="Months out"), showscale=True),
        error_y=dict(type="data", symmetric=False,
                     array=df_show["p90"] - df_show["p50"],
                     arrayminus=df_show["p50"] - df_show["p10"]),
        name="Forecast",
        hovertemplate="Asof %{customdata[0]}<br>Month %{customdata[1]}"
                      "<br>Actual $%{x:.2f}<br>P50 $%{y:.2f}<extra></extra>",
        customdata=list(zip(df_show["asof"].dt.strftime("%Y-%m-%d"),
                            df_show["month"].dt.strftime("%Y-%m"))),
    ))
    lo, hi = min(df_show["actual"].min(), df_show["p10"].min()), \
             max(df_show["actual"].max(), df_show["p90"].max())
    fig.add_trace(go.Scatter(x=[lo, hi], y=[lo, hi], mode="lines",
                             line=dict(dash="dash", color="#888"),
                             name="Perfect", hoverinfo="skip"))
    fig.update_layout(height=440, margin=dict(t=20),
                      xaxis_title="Realised $/MWh", yaxis_title="Forecast P50 $/MWh",
                      legend=dict(orientation="h", y=1.05))
    st.plotly_chart(fig, width="stretch")

# ── Single as-of trace ──────────────────────────────────────────────────────
st.subheader("Trace a specific as-of")
st.caption("Pick one as-of and see the full 12-month forecast the model made "
           "then, plotted against what actually happened month by month.")
asof_labels = [f"{d:%Y-%m-%d}" for d in sorted(bt["asof"].unique())]
picked = st.selectbox("As-of", asof_labels, index=len(asof_labels) - 1)
sub = bt[bt["asof"] == pd.Timestamp(picked)].sort_values("month")
if not sub.empty:
    fig2 = go.Figure()
    fig2.add_trace(go.Scatter(x=sub["month"], y=sub["p90"], name="P90",
                              line=dict(color="#d62728", dash="dot")))
    fig2.add_trace(go.Scatter(x=sub["month"], y=sub["p10"], name="P10",
                              line=dict(color="#2ca02c", dash="dot"),
                              fill="tonexty", fillcolor="rgba(31,119,180,0.10)"))
    fig2.add_trace(go.Scatter(x=sub["month"], y=sub["p50"], name="P50 forecast",
                              line=dict(color="#1f77b4", width=2), mode="lines+markers"))
    fig2.add_trace(go.Scatter(x=sub["month"], y=sub["actual"], name="Actual",
                              line=dict(color="#ff7f0e", width=2), mode="lines+markers"))
    fig2.update_layout(height=380, margin=dict(t=20),
                       xaxis_title="Delivery month", yaxis_title="$/MWh",
                       legend=dict(orientation="h", y=1.05))
    st.plotly_chart(fig2, width="stretch")

# ── Raw pairs table ─────────────────────────────────────────────────────────
with st.expander("Raw pairs (all rows)"):
    disp = bt[["asof", "month", "horizon", "gas_fwd", "hr_median",
               "p10", "p50", "p90", "actual", "err", "ape",
               "in_band", "gas_from_strip"]].copy()
    disp["asof"] = pd.to_datetime(disp["asof"]).dt.strftime("%Y-%m-%d")
    disp["month"] = pd.to_datetime(disp["month"]).dt.strftime("%Y-%m")
    for c in ("gas_fwd", "p10", "p50", "p90", "actual", "err"):
        disp[c] = disp[c].map(lambda x: f"${x:.2f}")
    disp["hr_median"] = disp["hr_median"].map(lambda x: f"{x:.1f}")
    disp["ape"] = disp["ape"].map(lambda x: f"{x*100:.1f}%")
    st.dataframe(disp, hide_index=True, width="stretch")
