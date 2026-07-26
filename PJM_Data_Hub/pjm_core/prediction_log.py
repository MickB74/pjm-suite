"""Persist and score 5CP peak predictions.

The Peak Predictor screen shows the next two weeks' 5CP risk every time you
open it, but nothing has ever recorded whether those calls were right. This
module logs each day's forecast to a small parquet file and — once the target
day is in the past — joins those calls against realised RTO peaks so you can
score them.

    predictions/
      peak_predictor_log.parquet
        as_of              date the prediction was made
        target_day         date being predicted
        predicted_peak_mw  model's daily-peak prediction
        prob_5cp           P(target day beats the current 5CP threshold)
        threshold_mw       the threshold that applied *when the call was made*
        eligible           was that day a candidate 5CP (non-holiday weekday)
        tmax_apparent_f    forecast apparent temperature that drove the call
        extrapolated       forecast temp was outside the model's fit range

We store the entire forecast horizon each day (not just today+1). That lets the
scorecard show how forecast skill decays with lead time — the ~7-day skill
horizon Open-Meteo talks about is worth verifying on our own outcomes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from pjm_core import paths, peak


LOG_COLUMNS = [
    "as_of", "target_day", "predicted_peak_mw", "prob_5cp",
    "threshold_mw", "eligible", "tmax_apparent_f", "extrapolated",
]


def _load_log() -> pd.DataFrame:
    """Existing prediction log, or an empty typed frame."""
    if not paths.PEAK_PREDICTIONS_PARQUET.exists():
        return pd.DataFrame(columns=LOG_COLUMNS)
    df = pd.read_parquet(paths.PEAK_PREDICTIONS_PARQUET)
    for c in ("as_of", "target_day"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c]).dt.date
    return df


def log_forecast(pred_df: pd.DataFrame, *, as_of=None) -> dict:
    """Append the current predictor output to the log (idempotent per as_of).

    Called from the Peak Predictor screen every time it renders. If we've
    already logged an ``as_of`` today, the second call is a no-op — refreshing
    the page shouldn't double-count.

    Returns ``{"appended": <int>, "as_of": <date>}``.
    """
    if pred_df is None or pred_df.empty:
        return {"appended": 0, "as_of": None}

    as_of = as_of or pd.Timestamp.today().date()

    have = _load_log()
    if not have.empty and (have["as_of"] == as_of).any():
        # Already logged this as_of — leave it alone.
        return {"appended": 0, "as_of": as_of}

    row = pred_df.copy()
    row["as_of"] = as_of
    row = row.rename(columns={"day": "target_day"})
    # Fill any optional columns the caller might not have supplied.
    for c in ("extrapolated", "eligible"):
        if c not in row.columns:
            row[c] = False
    row = row[LOG_COLUMNS]

    paths.PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    out = pd.concat([have, row], ignore_index=True) if not have.empty else row
    out.to_parquet(paths.PEAK_PREDICTIONS_PARQUET, index=False)
    return {"appended": int(len(row)), "as_of": as_of}


def scored_history(load: pd.DataFrame | None = None) -> pd.DataFrame:
    """Join every logged prediction against the realised daily peak.

    Only rows whose ``target_day`` is in the past AND has an observed daily
    peak are returned. Adds:

        actual_peak_mw       from peak.daily_peaks for that day
        error_mw             predicted − actual
        abs_pct_error        |error| / actual (skip when actual is 0)
        was_5cp              did that day end up in its year's realised 5CP?
        lead_days            target_day − as_of

    ``load`` defaults to :func:`peak.rto_hourly_load` and can be passed in to
    avoid a second read when the caller already has it.
    """
    log = _load_log()
    if log.empty:
        return log
    if load is None:
        load = peak.rto_hourly_load()
    if load.empty:
        return pd.DataFrame(columns=list(log.columns) + [
            "actual_peak_mw", "error_mw", "abs_pct_error", "was_5cp", "lead_days"])

    today = pd.Timestamp.today().date()
    log = log[log["target_day"] < today].copy()
    if log.empty:
        return log

    # Realised daily peaks & the actual 5CP set for every year the log covers.
    years = sorted({d.year for d in log["target_day"]})
    actuals = pd.concat([peak.daily_peaks(load, y) for y in years], ignore_index=True) \
        if years else pd.DataFrame()
    if actuals.empty:
        return pd.DataFrame(columns=list(log.columns) + [
            "actual_peak_mw", "error_mw", "abs_pct_error", "was_5cp", "lead_days"])

    real_cp = set()
    for y in years:
        cp = peak.five_cp(load, y)
        real_cp.update((y, d) for d in cp["day"].tolist())

    merged = log.merge(
        actuals[["day", "peak_mw"]].rename(
            columns={"day": "target_day", "peak_mw": "actual_peak_mw"}),
        on="target_day", how="left",
    )
    merged = merged.dropna(subset=["actual_peak_mw"]).copy()
    merged["error_mw"] = merged["predicted_peak_mw"] - merged["actual_peak_mw"]
    merged["abs_pct_error"] = np.where(
        merged["actual_peak_mw"] > 0,
        (merged["error_mw"].abs() / merged["actual_peak_mw"]) * 100.0,
        np.nan,
    )
    merged["was_5cp"] = merged["target_day"].map(
        lambda d: (d.year, d) in real_cp)
    merged["lead_days"] = (pd.to_datetime(merged["target_day"])
                           - pd.to_datetime(merged["as_of"])).dt.days
    return merged.reset_index(drop=True)


def scorecard(scored: pd.DataFrame | None = None) -> dict:
    """Aggregate accuracy metrics over the scored history.

    - ``mape``            mean absolute percentage error on the daily peak
    - ``mae_mw``          mean absolute error, in MW
    - ``bias_mw``         mean signed error (model minus actual)
    - ``brier``           Brier score of prob_5cp against was_5cp (lower better)
    - ``high_hit_rate``   of days the model called ≥66% probability, how many were actual 5CPs
    - ``high_false_alarm``of those high-probability calls, how many were NOT 5CPs
    - ``recall_5cp``      of days that were actual 5CPs, how many did the model flag ≥33%
    - ``n``               scored observations
    """
    df = scored if scored is not None else scored_history()
    if df is None or df.empty:
        return {"n": 0}

    out = {"n": int(len(df))}
    out["mape"] = float(df["abs_pct_error"].mean(skipna=True))
    out["mae_mw"] = float(df["error_mw"].abs().mean())
    out["bias_mw"] = float(df["error_mw"].mean())
    # Brier score: mean squared error of probability vs 0/1 outcome.
    probs = df["prob_5cp"].astype(float).to_numpy()
    truth = df["was_5cp"].astype(float).to_numpy()
    out["brier"] = float(((probs - truth) ** 2).mean())

    high = df[df["prob_5cp"] >= 0.66]
    out["high_calls"] = int(len(high))
    out["high_hit_rate"] = (float(high["was_5cp"].mean()) if len(high) else float("nan"))
    out["high_false_alarm"] = (1.0 - out["high_hit_rate"]
                               if len(high) else float("nan"))

    actual_cp = df[df["was_5cp"]]
    out["actual_cp_days"] = int(len(actual_cp))
    out["recall_5cp"] = (float((actual_cp["prob_5cp"] >= 0.33).mean())
                         if len(actual_cp) else float("nan"))

    return out
