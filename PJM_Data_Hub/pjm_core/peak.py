"""PJM peak & 5CP analytics shared across screens and the alert digest.

PJM sets each load's capacity obligation (Peak Load Contribution) from its
demand during the **five highest RTO peak-load hours of the summer** — the "5
Coincident Peaks" (5CP). Those hours land on the hottest, most humid summer
afternoons, so the system peak is largely a function of weather.

This module centralises the logic the 5CP screen, the **peak predictor** and the
**alert digest** all need:

* the RTO hourly load series and its per-day summer peaks,
* the *current* 5CP threshold — the load a new day must beat to enter the top 5,
* a load-vs-apparent-temperature model fit on history, and
* a forecast of the coming days' peaks with the probability each cracks the 5CP.

Keeping it here (not inside a screen) means the CLI digest and the Streamlit
screens compute identical numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from datasets.load import pjm_load
from datasets.weather import pjm_weather

SUMMER_START = (6, 1)    # June 1
SUMMER_END = (9, 30)     # Sept 30 — PJM planning-period summer for 5CP
N_CP = 5


# ---------------------------------------------------------------------------
# Load & summer peaks
# ---------------------------------------------------------------------------

def rto_hourly_load() -> pd.DataFrame:
    """RTO system load, one row per hour (summed across load areas)."""
    df = pjm_load.load_store()
    if df.empty:
        return df
    df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
    rto = df[df["zone"] == "RTO"]
    return (rto.groupby("datetime_beginning_ept", as_index=False)["mw"].sum()
            .sort_values("datetime_beginning_ept").reset_index(drop=True))


def summer_bounds(year: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    return (pd.Timestamp(year, *SUMMER_START),
            pd.Timestamp(year, SUMMER_END[0], SUMMER_END[1], 23, 59, 59))


def daily_peaks(load: pd.DataFrame, year: int) -> pd.DataFrame:
    """RTO peak-load hour per day across the summer window of ``year``.

    Columns: peak_hour (timestamp of the day's peak), peak_mw, day (date).
    """
    lo, hi = summer_bounds(year)
    sub = load[(load["datetime_beginning_ept"] >= lo)
               & (load["datetime_beginning_ept"] <= hi)].copy()
    if sub.empty:
        return pd.DataFrame(columns=["peak_hour", "peak_mw", "day"])
    sub["day"] = sub["datetime_beginning_ept"].dt.date
    peaks = sub.loc[sub.groupby("day")["mw"].idxmax()].copy()
    peaks = peaks.rename(columns={"datetime_beginning_ept": "peak_hour", "mw": "peak_mw"})
    peaks["eligible"] = peaks["day"].map(is_eligible_5cp)
    return peaks[["peak_hour", "peak_mw", "day", "eligible"]].reset_index(drop=True)


def five_cp(load: pd.DataFrame, year: int) -> pd.DataFrame:
    """The five coincident peaks so far this summer, ranked (rank 1 = highest).

    Only eligible days (non-holiday weekdays) can be a 5CP, per PJM rule.
    """
    p = daily_peaks(load, year)
    p = p[p["eligible"]].sort_values("peak_mw", ascending=False).head(N_CP)
    p = p.reset_index(drop=True)
    p.insert(0, "rank", range(1, len(p) + 1))
    return p


def current_threshold(load: pd.DataFrame, year: int) -> float | None:
    """Load a new day must beat to enter the current top-5.

    The Nth-highest *eligible* daily peak so far this summer. If fewer than 5
    eligible days exist yet, any new eligible day makes the board, so 0.
    """
    p = daily_peaks(load, year)
    peaks = p.loc[p["eligible"], "peak_mw"].sort_values(ascending=False)
    if peaks.empty:
        return None
    return float(peaks.iloc[N_CP - 1]) if len(peaks) >= N_CP else 0.0


# ---------------------------------------------------------------------------
# Load-vs-temperature model
# ---------------------------------------------------------------------------

@dataclass
class LoadTempModel:
    """Quadratic fit of daily RTO peak (MW) on daily-max apparent temp (°F),
    with an additive shift for reduced-load days (weekends & NERC holidays).

    ``coeffs`` describe a *weekday*; ``offday_offset`` (typically negative) is the
    MW the peak drops on a weekend or holiday at the same temperature. This lets
    a hot Saturday be scored correctly — 5CPs are the summer's highest RTO peaks
    and, because non-work-day load runs lower, they land on weekdays in practice.
    """
    coeffs: np.ndarray          # degree-2 temp coefficients (weekday baseline)
    offday_offset: float        # MW shift applied on weekend/holiday days
    resid_std: float            # residual standard deviation (MW)
    n: int                      # sample size
    t_min: float                # temp range the fit was trained on
    t_max: float

    def predict(self, apparent_f, is_offday=False):
        arr = np.asarray(apparent_f, dtype=float)
        base = np.polyval(self.coeffs, arr)
        return base + np.asarray(is_offday, dtype=float) * self.offday_offset

    def prob_above(self, apparent_f, threshold_mw: float, is_offday=False):
        """P(actual daily peak > threshold) given forecast apparent temp.

        Normal around the model prediction using the residual spread — a rough
        but honest read on how likely the day cracks the current 5CP.
        """
        from math import erf, sqrt
        mu = self.predict(apparent_f, is_offday)
        sd = max(self.resid_std, 1.0)
        z = (np.asarray(mu, dtype=float) - threshold_mw) / (sd * sqrt(2.0))
        # 1 - CDF(threshold) = 0.5 * (1 + erf(z))
        vfunc = np.vectorize(lambda x: 0.5 * (1.0 + erf(x)))
        return vfunc(z)


def _observed_summer_holidays(year: int) -> dict:
    """PJM-observed holidays inside the 5CP window (Jun 1–Sep 30), by date.

    Only Independence Day and Labor Day land in summer (Memorial Day is May).
    Independence Day is **observed** on the nearest weekday when Jul 4 falls on
    a weekend — e.g. in 2026 Jul 4 is a Saturday, so PJM observes it Friday
    Jul 3, and *that Friday* is the ineligible day (confirmed by PJM's member
    notice). Labor Day is the first Monday of September.
    """
    from datetime import date
    hols = {}
    j4 = date(year, 7, 4)
    if j4.weekday() == 5:        # Saturday → observed Friday
        j4 = date(year, 7, 3)
    elif j4.weekday() == 6:      # Sunday → observed Monday
        j4 = date(year, 7, 5)
    hols[j4] = "Independence Day"
    sep1 = date(year, 9, 1)
    labor = date(year, 9, 1 + ((0 - sep1.weekday()) % 7))  # first Monday
    hols[labor] = "Labor Day"
    return hols


def holiday_name(d) -> str | None:
    """Name of the PJM-observed holiday on ``d``, or None."""
    return _observed_summer_holidays(d.year).get(d)


def is_eligible_5cp(d) -> bool:
    """True if ``d`` can be an RTO 5CP: a weekday that is not a PJM holiday.

    Per PJM, the 5 CPs are drawn only from non-holiday weekdays — weekends and
    observed holidays are never eligible, regardless of how hot they run.
    """
    return d.weekday() < 5 and holiday_name(d) is None


def _is_offday(d) -> bool:
    """True for reduced-load days — weekends and observed holidays.

    Equivalent to *not eligible*; used both to gate 5CP eligibility and as the
    load-model's weekend/holiday term.
    """
    return not is_eligible_5cp(d)


def _daily_max_apparent(wx: pd.DataFrame) -> pd.DataFrame:
    """Daily maximum apparent (and, when present, actual air) temperature (°F).

    Always returns ``tmax_apparent_f``; also returns ``tmax_f`` (daily-max air
    temperature) when the frame carries a ``temp_f`` column.
    """
    if wx.empty:
        return pd.DataFrame(columns=["day", "tmax_apparent_f"])
    w = wx.copy()
    w["day"] = pd.to_datetime(w["datetime_beginning_ept"]).dt.date
    aggs = {"apparent_f": "max"}
    if "temp_f" in w.columns:
        aggs["temp_f"] = "max"
    g = (w.groupby("day").agg(aggs).reset_index()
         .rename(columns={"apparent_f": "tmax_apparent_f", "temp_f": "tmax_f"}))
    return g


def fit_load_temp_model(load: pd.DataFrame | None = None,
                        wx: pd.DataFrame | None = None) -> LoadTempModel | None:
    """Fit daily RTO peak vs. daily-max apparent temp across all summers.

    Uses only summer (Jun–Sep) days — the cooling-driven regime that governs
    5CP — so the curve isn't blended with winter heating load.
    """
    if load is None:
        load = rto_hourly_load()
    if load.empty:
        return None
    if wx is None:
        wx = pjm_weather.weighted_temp()
    if wx.empty:
        return None

    years = sorted({d.year for d in load["datetime_beginning_ept"].dt.date})
    frames = [daily_peaks(load, y) for y in years]
    peaks = pd.concat([f for f in frames if not f.empty], ignore_index=True) \
        if any(not f.empty for f in frames) else pd.DataFrame()
    if peaks.empty:
        return None

    tmax = _daily_max_apparent(wx)
    df = peaks.merge(tmax, on="day", how="inner").dropna(subset=["tmax_apparent_f", "peak_mw"])
    if len(df) < 10:
        return None

    t = df["tmax_apparent_f"].to_numpy(dtype=float)
    y = df["peak_mw"].to_numpy(dtype=float)
    offday = df["day"].map(_is_offday).to_numpy(dtype=float)

    # Least-squares fit of peak ~ a·t² + b·t + c + d·offday. The offday column
    # captures the weekend/holiday load drop so the temp curve isn't biased and
    # a hot weekend isn't scored like a hot weekday. Fall back to a plain
    # quadratic if history has no reduced-load days to identify the offset.
    if offday.any() and not offday.all():
        X = np.column_stack([t ** 2, t, np.ones_like(t), offday])
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        coeffs = beta[:3]
        offday_offset = float(beta[3])
        resid = y - X @ beta
    else:
        coeffs = np.polyfit(t, y, 2)
        offday_offset = 0.0
        resid = y - np.polyval(coeffs, t)

    return LoadTempModel(coeffs=coeffs, offday_offset=offday_offset,
                         resid_std=float(resid.std(ddof=1)),
                         n=len(df), t_min=float(t.min()), t_max=float(t.max()))


# ---------------------------------------------------------------------------
# Forecast → upcoming-peak prediction
# ---------------------------------------------------------------------------

def predict_upcoming(days: int = 16,
                     load: pd.DataFrame | None = None,
                     model: LoadTempModel | None = None,
                     forecast_wx: pd.DataFrame | None = None) -> pd.DataFrame:
    """Rank the coming ``days`` days by 5CP risk.

    Returns one row per forecast day with the forecast daily-max apparent temp,
    the model's predicted RTO peak, the current 5CP threshold, the margin over
    it, and the probability the day beats the threshold. Empty if inputs are
    missing (no load, no model, or no forecast).
    """
    if load is None:
        load = rto_hourly_load()
    if load.empty:
        return pd.DataFrame()
    if model is None:
        model = fit_load_temp_model(load=load)
    if model is None:
        return pd.DataFrame()
    if forecast_wx is None:
        forecast_wx = pjm_weather.forecast_weighted(days=days)
    if forecast_wx.empty:
        return pd.DataFrame()

    year = pd.Timestamp.now().year
    threshold = current_threshold(load, year)
    if threshold is None:
        threshold = 0.0

    fc = _daily_max_apparent(forecast_wx)
    # Restrict to summer days — 5CP can only be set Jun 1–Sep 30.
    fc = fc[fc["day"].map(lambda d: SUMMER_START <= (d.month, d.day) <= SUMMER_END)]
    if fc.empty:
        return pd.DataFrame()

    fc = fc.sort_values("day").reset_index(drop=True)
    t = fc["tmax_apparent_f"].to_numpy()
    fc["eligible"] = fc["day"].map(is_eligible_5cp)
    fc["holiday"] = fc["day"].map(lambda d: holiday_name(d) or "")
    off = (~fc["eligible"]).to_numpy()
    fc["predicted_peak_mw"] = model.predict(t, is_offday=off)
    fc["threshold_mw"] = threshold
    fc["margin_mw"] = fc["predicted_peak_mw"] - threshold
    # Ineligible days (weekends / observed holidays) cannot be a 5CP → prob 0.
    prob = model.prob_above(t, threshold, is_offday=off)
    fc["prob_5cp"] = np.where(fc["eligible"].to_numpy(), prob, 0.0)
    fc["extrapolated"] = (fc["tmax_apparent_f"] > model.t_max) | \
                         (fc["tmax_apparent_f"] < model.t_min)
    return fc


def risk_label(prob: float) -> str:
    """Human 5CP-risk band for a probability."""
    if prob >= 0.66:
        return "🔴 High"
    if prob >= 0.33:
        return "🟠 Elevated"
    if prob >= 0.10:
        return "🟡 Watch"
    return "🟢 Low"
