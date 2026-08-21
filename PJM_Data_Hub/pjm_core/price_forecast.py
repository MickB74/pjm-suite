"""PJM DOM Hub forward price forecast: implied heat rate × gas strip + Monte Carlo.

Method (mirrors the ERCOT approach):
  1. Historical PJM DOM Hub hourly LMPs from the data lake.
  2. Henry Hub gas prices from EIA (monthly spot or STEO).
  3. Implied heat rate = LMP / gas price (MMBtu/MWh).
  4. Distribution per calendar month, recency-weighted → median P50 anchor.
  5. Monte Carlo: correlated lognormal gas × lognormal heat rate → power price.
  6. Output: month / P10 / P50 / P90, plus the strip-average distribution.

How gas swings are handled (see the constants below for the calibration):
  * The anchor is the per-contract *median of the last few strip vintages*,
    which rejects bad ticks from the unofficial feed without lagging real moves.
  * Volatility is seasonal — a January contract is roughly twice as volatile as
    a July one — and saturates with horizon instead of growing as √t forever.
  * Gas is drawn as a correlated path across the horizon (an Ornstein-Uhlenbeck
    covariance), not independently per month, so multi-month aggregates carry
    realistic regime risk rather than diversifying it away.
  * Gas and heat rate are drawn jointly: power price does not move 1:1 with gas.

Price cap: $2,000/MWh (PJM market-wide offer cap).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from pjm_core import paths
from pjm_core.settlement_points import PRIMARY_HUB

# Monte Carlo defaults
DEFAULT_N_SIMS = 5_000
DEFAULT_SEED = 42
HR_LOG_SIG_FLOOR = 0.10    # minimum heat-rate log-σ
LONG_RUN_GAS = 4.00        # $/MMBtu long-run mean reversion anchor
REVERSION_MONTHS = 24      # e-folding time for gas mean reversion (months)
PJM_PRICE_CAP = 2_000.0    # $/MWh

# ── Gas volatility model ────────────────────────────────────────────────────
# Fitted to EIA Henry Hub monthly spot, 2016-2026, by least squares against the
# realised dispersion of log gas at 1/3/6/12/24-month horizons. The old model
# used a flat 0.5·√t, which tracked the 12-month point but ran ~25% wide by
# month 24 (0.88 vs 0.69 realised) because √t grows without bound while gas
# mean-reverts. The OU form below saturates at σ/√(2κ) ≈ 0.82.
#
# Caveat: this is calibrated on *spot* dispersion, which is an upper bound on
# forward-vs-realised error since the strip already prices the seasonal shape.
# It is deliberately the conservative choice until `gas_strip.forward_vol()`
# has enough vintages to measure true forward vol directly.
GAS_SIGMA_ANN = 0.62       # annualised log-vol of the gas shock
GAS_KAPPA = 0.29           # /yr mean reversion; sets both term structure and
                           #   the cross-month correlation of the gas path

# Per-delivery-month volatility multipliers (mean 1.0), averaged over two
# measures on the same EIA history: month-over-month log-return σ, and the
# cross-year dispersion of each month's deviation from its own year's level.
# Winter carries genuine tail risk (polar vortex); summer gas is quiet and the
# risk in a July power price lives in the heat rate instead.
SEASONAL_VOL_SHAPE = {
    1: 1.90, 2: 1.69, 3: 1.20, 4: 0.82, 5: 0.77, 6: 0.68,
    7: 0.57, 8: 0.78, 9: 0.76, 10: 0.88, 11: 0.83, 12: 1.10,
}
MIN_YEARS_FOR_SEASONAL_FIT = 5   # below this, use the baked-in shape above

# Seasonality is *delivery-month* risk: a January contract is exposed to winter
# weather during the weeks around delivery, not during the preceding summer.
# So the seasonal multiplier scales only the variance accumulated in the final
# DELIVERY_WINDOW_YEARS; the diffusion before that is seasonally neutral.
# Multiplying the whole accumulated path instead would put σ ≈ 1.2 on an
# 18-month-out January, which implies a P50 half the forward — not credible.
DELIVERY_WINDOW_YEARS = 2.0 / 12.0
GAS_SIGMA_MAX = 1.0     # hard guard on terminal log-σ from any vol source

# ── Heat rate model ─────────────────────────────────────────────────────────
# Measured within-calendar-month on DOM Hub, seasonality removed: a 10% move in
# gas moves the power price about 9%, not 10% (β ≈ 0.90, corr(log gas, log HR)
# ≈ -0.18). Coal, nuclear and renewables set the margin often enough that the
# implied heat rate compresses when gas rallies.
#
# This is applied as a *structural* pass-through rather than a correlation:
# log HR is shifted by -(1-β)·(log gas - log forward), so the simulated β is
# exactly this number at every horizon. Fixing a correlation instead would let
# β drift with the ratio of the two σ's, which has no economic basis. A plain
# product of independent lognormals — the old model — implies β = 1.0 and
# overstates how much a gas swing reaches the power price.
GAS_PASS_THROUGH_BETA = 0.90

# Lag-1 autocorrelation of the HR anomaly (vs. its calendar-month norm) is
# 0.27 on DOM Hub — weather and outage shocks persist somewhat, but far less
# than gas regimes do. Applied as an equicorrelation across horizon months.
HR_TERM_CORR = 0.30

# The DOM Hub fleet has shifted materially (coal retirement, solar build, and
# data-centre load growth in the Dominion zone). July implied HR by year runs
# 15.9, 9.4, 12.8, 18.0, 25.4, 20.6 — pooling 2020 with 2025 anchors the P50
# too low. Exponential recency weights on the observation year fix that; a
# 3-year half-life keeps an effective sample of ~3.6 out of ~6 observations.
HR_RECENCY_HALFLIFE_YEARS = 3.0

# ── Pooled heat-rate model ──────────────────────────────────────────────────
# The recency-weighted median above estimates each calendar month from its own
# ~6 observations, and a median of a weighted window *lags* a drifting series
# rather than extrapolating it. Implied HR on DOM Hub trends +7.5%/yr (t=5.8)
# on coal retirement and Dominion-zone data-centre load, so that lag showed up
# as a one-sided under-forecast worth ~+20 $/MWh of the backtest error.
# Pooling all ~80 months into one fit — trend + calendar-month effects +
# weather — estimates the trend off every month instead of six, and predicts
# forward along it. Walk-forward on the point forecast: log-HR bias -0.205 →
# -0.037, MAE 0.247 → 0.192.
HR_MODEL_MIN_MONTHS = 36     # below this the trend is not identifiable
# Extrapolating a fitted trend indefinitely is how a linear model runs away;
# past this many years beyond the last fitted month the trend is held flat.
HR_TREND_MAX_YEARS = 2.0

# Weather enters as *extreme-day* degree sums, not monthly HDD/CDD totals.
# Scarcity is made on the few days the fleet is stretched, and a monthly total
# averages those away: on monthly totals CDD is insignificant (t=0.8), while
# the hot-day sum carries t=1.9 and the cold-day sum t=2.8, for a lower
# residual σ (0.202 vs 0.209) and higher adjusted R² (0.574 vs 0.545).
HR_COLD_DAY_F = 35.0         # daily mean below this counts toward xcold
HR_HOT_DAY_F = 80.0          # daily mean above this counts toward xhot

# ── Regional gas basis ──────────────────────────────────────────────────────
# DOM Hub burns Mid-Atlantic gas, not Henry Hub gas. February 2026 settled at
# $3.62 Henry Hub and $7.09 delivered to Virginia power burners; priced off
# Henry Hub alone that month implies a heat rate of 24.1 against a normal 12.3
# on the gas actually burned. The heat rate is therefore fitted on *delivered*
# gas, and the forward carries a basis of its own.
# ...but off by default, because it does not pay for itself yet. Walk-forward
# over 138 (month, horizon) pairs, scored only on months EIA actually
# publishes: heat rate on Henry Hub gives MAE 7.67; on delivered gas with
# *perfect* basis foresight, 6.86 — so the signal is real; on delivered gas
# with the basis a forecaster can actually have (the seasonal mean), 8.44.
# Winter is where it inverts hardest: MAE 9.99 → 14.33. Splitting out a term
# you cannot forecast turns it into a multiplicative error the heat rate then
# scales by ~10. Conditioning basis on cold-day anomalies (corr +0.48 in
# winter, t=2.3) only trims the winter anomaly σ from 1.00 to 0.90, and the
# weather is not known ahead either.
#
# What would change the answer: a daily traded-hub feed (Platts/Argus Transco
# Z5, TETCO M3). That supplies a forward *basis quote* instead of a seasonal
# mean, which is exactly the missing piece — and removes the monthly
# averaging, the ~3-month publication lag, and the gaps (EIA has no January
# 2026 print for Virginia, the single most important month in the sample).
USE_DELIVERED_GAS = False

BASIS_STATE = "VA"           # the DOM Hub's own state
# Marcellus supply reaching the Mid-Atlantic reset this market: VA basis
# averaged ~$1.0-1.6 through 2011 and ~$0.4-0.7 since 2012. Fitting the whole
# 24-year record would price a pipeline configuration that no longer exists.
# Note the excluded window holds the largest basis event on record (Jan 2014,
# +$12.50, polar vortex), so the current-configuration tail here is, if
# anything, understated.
BASIS_HISTORY_YEARS = 10
# Per-calendar-month σ off ~8-10 observations is noisy, so it is shrunk toward
# the pooled σ with this many pseudo-observations of prior weight.
BASIS_SHRINK_K = 4.0
# Lag-1 autocorrelation of the basis anomaly is 0.28 — a cold winter widens
# December and January together. Applied as an equicorrelation, as for the
# heat rate.
BASIS_TERM_CORR = 0.30
# Delivered gas is a physical price; keep the simulated draw off zero.
MIN_DELIVERED_GAS = 0.25


# ---------------------------------------------------------------------------
# Gas price helpers
# ---------------------------------------------------------------------------

def _gas_from_eia(api_key: str | None = None) -> pd.Series:
    """Monthly Henry Hub spot from EIA API. Returns a price series indexed by
    the first-of-month date, or an empty Series on failure."""
    key = api_key or ""
    if not key:
        return pd.Series(dtype=float)
    try:
        import requests
        url = "https://api.eia.gov/v2/natural-gas/pri/fut/data/"
        params = {
            "api_key": key,
            "frequency": "monthly",
            "data[0]": "value",
            "facets[series][]": "RNGWHHD",
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": 120,
        }
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        rows = r.json()["response"]["data"]
        s = pd.Series(
            {pd.Timestamp(row["period"]): float(row["value"]) for row in rows
             if row.get("value") is not None}
        ).sort_index()
        return s
    except Exception:
        return pd.Series(dtype=float)


def _gas_from_steo(api_key: str | None = None) -> pd.Series:
    """Henry Hub natural gas price ($/MMBtu) from EIA's Short-Term Energy Outlook
    (STEO). Unlike the spot series, STEO carries EIA's official *forecast* months
    (~18–24 months forward), giving a real forward curve rather than an
    extrapolation. Returns a monthly series indexed by first-of-month, or an
    empty Series on failure."""
    key = api_key or ""
    if not key:
        return pd.Series(dtype=float)
    try:
        import requests
        url = "https://api.eia.gov/v2/steo/data/"
        params = {
            "api_key": key,
            "frequency": "monthly",
            "data[0]": "value",
            "facets[seriesId][]": "NGHHUUS",  # Henry Hub spot price, $/MMBtu
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "length": 300,
        }
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        rows = r.json()["response"]["data"]
        s = pd.Series(
            {pd.Timestamp(row["period"]): float(row["value"]) for row in rows
             if row.get("value") is not None}
        ).sort_index()
        return s
    except Exception:
        return pd.Series(dtype=float)


def _gas_forward_curve(
    gas_history: pd.Series,
    horizon_months: int,
    asof: pd.Timestamp,
) -> pd.Series:
    """Build a forward gas curve from last known price with mean reversion to $4."""
    last_price = float(gas_history.iloc[-1]) if not gas_history.empty else LONG_RUN_GAS
    months = pd.date_range(asof, periods=horizon_months, freq="MS")
    t = np.arange(1, horizon_months + 1)
    alpha = np.exp(-1 / REVERSION_MONTHS)
    fwd = LONG_RUN_GAS + (last_price - LONG_RUN_GAS) * (alpha ** t)
    return pd.Series(fwd, index=months)


def _price_seasonal_shape(curve: pd.Series) -> dict[int, float]:
    """Per-calendar-month multiplicative price level of `curve`, mean 1.

    Distinct from `_seasonal_shape`, which is a *volatility* multiplier — this
    is the level shape: Henry Hub January trades ~1.26× the strip average and
    April ~0.89×. Measured off the curve being extended, so a vintage supplies
    its own shape and no external series (or later data) is consulted.

    Deviations are taken against a centred 12-month rolling mean rather than a
    calendar-year mean, so a curve that starts mid-year is not distorted by its
    partial first and last years.
    """
    s = curve.dropna()
    s = s[s > 0]
    if len(s) < 12:
        return {}
    lg = np.log(s.sort_index())
    level = lg.rolling(12, center=True, min_periods=8).mean()
    dev = (lg - level).dropna()
    if dev.empty:
        return {}
    by_month = dev.groupby(dev.index.month).mean()
    if len(by_month) < 12:
        return {}
    shape = np.exp(by_month)
    shape = shape / shape.mean()
    if not np.isfinite(shape.to_numpy()).all() or (shape <= 0).any():
        return {}
    return {int(m): float(v) for m, v in shape.items()}


def _month_gap(a: pd.Timestamp, b: pd.Timestamp) -> int:
    return (a.year - b.year) * 12 + (a.month - b.month)


def _extend_to_horizon(
    partial: pd.Series,
    horizon_months: int,
    asof: pd.Timestamp,
    spot: tuple[pd.Timestamp, float] | None = None,
) -> pd.Series:
    """Extend a partial monthly curve across the full horizon.

    Months the source covers are used as-is. Months it does not are filled by
    mean-reverting toward $4 from the *nearest* real price, with the curve's
    own seasonal shape re-applied, so a filled January is not priced like a
    filled April.

    Gaps run in both directions. Forward gaps (horizon past the last liquid
    contract) are the obvious case. Backward gaps matter just as much for
    backtesting: a vintage reconstructed from currently-trading contracts has
    no near months at all, because Yahoo delists contracts once they expire —
    a vintage stamped 2025-07-29 can start at 2026-08. `spot`, when given, is
    the (month, price) of the most recent *realised* spot before the as-of and
    anchors those backward gaps; it is far closer to the missing months than
    the first surviving contract is.

    The previous version clamped the month gap at zero, which silently treated
    every backward gap as "zero months past the end of the curve" and returned
    the most deferred contract's price — filling a 2025-08 slot with January
    2029 gas, flat, for the whole horizon.
    """
    months = pd.date_range(asof, periods=horizon_months + 2, freq="MS")
    first_month, last_month = partial.index.min(), partial.index.max()
    first_val = float(partial.loc[first_month])
    last_val = float(partial.loc[last_month])
    shape = _price_seasonal_shape(partial)
    alpha = np.exp(-1 / REVERSION_MONTHS)

    def _deseason(v: float, m: pd.Timestamp) -> float:
        return v / shape.get(m.month, 1.0)

    out = {}
    for m in months:
        if m in partial.index:
            out[m] = float(partial.loc[m])
            continue
        if m < first_month:
            anchor_m, anchor_v = ((spot[0], float(spot[1])) if spot is not None
                                  else (first_month, first_val))
        else:
            anchor_m, anchor_v = last_month, last_val
        k = abs(_month_gap(m, anchor_m))
        level = LONG_RUN_GAS + (_deseason(anchor_v, anchor_m) - LONG_RUN_GAS) * (alpha ** k)
        out[m] = level * shape.get(m.month, 1.0)
    return pd.Series(out)


_YF_MONTH_CODE = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
                  7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}


def _gas_from_yahoo(horizon_months: int, asof: pd.Timestamp) -> pd.Series:
    """Best-effort *real* NYMEX Henry Hub strip from Yahoo Finance monthly
    contracts (ticker ``NG<monthcode><yy>.NYM``, e.g. ``NGQ26.NYM`` = Aug-2026).

    Yahoo's quotes are CME-derived but **unofficial and delayed**, and Yahoo
    rate-limits/blocks aggressively — so this returns whatever contract months
    resolve, or an empty Series on any failure. Deferred contracts are illiquid
    and often missing; callers should fall back when too few months come back."""
    try:
        import logging
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # hush blocked-fetch spam
    except Exception:
        return pd.Series(dtype=float)
    n = min(horizon_months, 24)  # deferred NG contracts get too thin past ~2y
    start = asof.to_period("M").to_timestamp()
    months = pd.date_range(start, periods=n, freq="MS")
    tk_map = {f"NG{_YF_MONTH_CODE[m.month]}{str(m.year)[2:]}.NYM": m for m in months}
    try:
        # timeout caps the per-request socket wait so a stalled/rate-limited
        # Yahoo can't hang the caller (this runs at launch and per-request).
        data = yf.download(list(tk_map), period="7d", progress=False,
                           threads=True, timeout=8)
    except Exception:
        return pd.Series(dtype=float)
    if data is None or len(data) == 0:
        return pd.Series(dtype=float)
    close = data["Close"] if "Close" in getattr(data, "columns", []) else data
    out = {}
    for tk, m in tk_map.items():
        try:
            if hasattr(close, "columns") and tk in close.columns:
                col = close[tk]
            elif len(tk_map) == 1:
                col = close
            else:
                continue
            vals = col.dropna()
            if len(vals):
                out[m] = float(vals.iloc[-1])
        except Exception:
            continue
    return pd.Series(out).sort_index()


def _gas_strip_asof() -> str:
    """`, YYYY-MM-DD` date the cached strip was pulled, or '' if unknown."""
    try:
        state = json.loads(paths.GAS_STRIP_STATE.read_text())
        return f", {state['asof']}"
    except Exception:
        return ""


def _gas_curve(
    api_key: str | None,
    horizon_months: int,
    asof: pd.Timestamp,
    csv_override: Path | None = None,
    gas_asof=None,
    anchor_vintages: int = 5,
    gas_history: pd.Series | None = None,
) -> tuple[pd.Series, str]:
    """Gas forward curve and the source label describing where it came from.

    Priority: explicit override → manual override CSV → vintage-median strip →
    cached NYMEX strip (refreshed at launch) → live NYMEX strip (Yahoo) →
    EIA STEO forecast → EIA spot + mean-reversion → flat $4 anchor.
    Returns (series, source_label).

    `anchor_vintages` sets how many recent strip vintages are collapsed to a
    per-contract median before use. This denoises the unofficial Yahoo feed
    without lagging the market — set it to 1 for the raw single-day settle.

    `gas_asof` pins the curve to the archived strip as it stood on that date —
    used to re-run forecasts from a past date. Raises ValueError if no vintage
    that old exists, rather than silently substituting today's strip.

    `gas_history` is realised Henry Hub spot; its last observation anchors any
    months the strip is missing *before* its first contract. The caller passes
    the same series it already loaded, which in a backtest is truncated to the
    as-of — so this cannot leak the future."""
    from pjm_core import gas_strip  # local import; gas_strip imports us

    spot = None
    if gas_history is not None and not gas_history.empty:
        h = gas_history.dropna()
        h = h[h > 0]
        if not h.empty:
            spot = (pd.Timestamp(h.index.max()), float(h.iloc[-1]))

    # 0. Archived vintage requested — bypass the live chain entirely.
    if gas_asof is not None:
        if anchor_vintages > 1:
            got = gas_strip.strip_median(gas_asof, n=anchor_vintages)
            if got is not None:
                med, vintage, n_used = got
                strip = med.set_index("month")["gas_price"]
                label = (f"NYMEX strip (vintage {vintage:%Y-%m-%d}"
                         + (f", {n_used}-vintage median)" if n_used > 1 else ")"))
                return _extend_to_horizon(strip, horizon_months, asof, spot=spot), label
        got = gas_strip.strip_asof(gas_asof)
        if got is None:
            raise ValueError(
                f"No archived gas-strip vintage on or before "
                f"{pd.Timestamp(gas_asof).date()} — vintages accumulate daily "
                "as the strip is pulled, so older dates can't be backtested.")
        snap, vintage = got
        strip = snap.set_index("month")["gas_price"]
        return (_extend_to_horizon(strip, horizon_months, asof, spot=spot),
                f"NYMEX strip (vintage {vintage:%Y-%m-%d})")

    # 1. Explicit override passed by the caller (used in tests).
    if csv_override and csv_override.exists():
        df = pd.read_csv(csv_override, parse_dates=["month"])
        return df.set_index("month")["gas_price"], "manual CSV override"

    # 2. Truly-manual override file on disk — wins over everything auto.
    if paths.GAS_OVERRIDE_CSV.exists():
        df = pd.read_csv(paths.GAS_OVERRIDE_CSV, parse_dates=["month"])
        return df.set_index("month")["gas_price"], "manual CSV override"

    # 3. Median of the last few archived vintages — same real traded strip as
    #    the cached CSV below, but robust to a single bad Yahoo print.
    if anchor_vintages > 1:
        got = gas_strip.strip_median(n=anchor_vintages)
        if got is not None:
            med, vintage, n_used = got
            strip = med.set_index("month")["gas_price"]
            if len(strip) >= 3 and n_used > 1:
                return (_extend_to_horizon(strip, horizon_months, asof, spot=spot),
                        f"NYMEX strip ({n_used}-vintage median, "
                        f"latest {vintage:%Y-%m-%d})")

    # 4. Cached NYMEX strip written at launch by gas_strip.update() — this is the
    #    real traded strip pulled from a residential IP, so it works even when
    #    the app itself (e.g. a cloud host) can't reach Yahoo directly.
    if paths.GAS_STRIP_CSV.exists():
        df = pd.read_csv(paths.GAS_STRIP_CSV, parse_dates=["month"])
        strip = df.set_index("month")["gas_price"]
        if len(strip) >= 3:
            asof_label = _gas_strip_asof()
            return (_extend_to_horizon(strip, horizon_months, asof, spot=spot),
                    f"NYMEX strip (cached at launch{asof_label})")

    # 5. Best-effort live pull: the real traded NYMEX strip via Yahoo. Unofficial
    #    /flaky, so only trust it when enough contract months resolve.
    yahoo = _gas_from_yahoo(horizon_months, asof)
    if len(yahoo) >= 3:
        return (_extend_to_horizon(yahoo, horizon_months, asof),
                "NYMEX strip (Yahoo Finance, unofficial/delayed)")

    # Licensed auto-source: EIA STEO carries real forward (forecast) months.
    steo = _gas_from_steo(api_key)
    if not steo.empty:
        return _extend_to_horizon(steo, horizon_months, asof), "EIA STEO Henry Hub forecast"

    # Fallback: extrapolate the last EIA spot print with mean reversion to $4.
    history = _gas_from_eia(api_key)
    fwd = _gas_forward_curve(history, horizon_months, asof)
    if not history.empty:
        fwd.update(history)  # patch forward months already in history
        return fwd, "EIA Henry Hub spot + mean-reversion"
    return fwd, "mean-reversion fallback ($4 anchor)"


# ---------------------------------------------------------------------------
# Implied heat rate from historical LMPs
# ---------------------------------------------------------------------------

def _load_dom_hub_monthly(hub: str = PRIMARY_HUB) -> pd.DataFrame:
    """Monthly average DOM Hub LMP from the parquet store."""
    if not paths.HUB_PRICES_PARQUET.exists():
        return pd.DataFrame()
    df = pd.read_parquet(paths.HUB_PRICES_PARQUET)
    df = df[df["pnode_name"] == hub].copy()
    if df.empty:
        return pd.DataFrame()
    df["dt"] = pd.to_datetime(df["datetime_beginning_ept"])
    df["month"] = df["dt"].dt.to_period("M").dt.to_timestamp()
    monthly = df.groupby("month")["total_lmp"].mean().reset_index()
    monthly.columns = ["month", "lmp"]
    return monthly


def _build_heat_rate_distribution(
    monthly_lmp: pd.DataFrame,
    gas_history: pd.Series,
) -> pd.DataFrame:
    """Monthly implied heat rate distribution.

    Returns a DataFrame with columns: cal_month (1–12), year, heat_rate.
    Only months where both LMP and gas are available are included.
    Negative or extreme heat rates are dropped (price ≤ $0 or gas ≤ $0.5).
    """
    if monthly_lmp.empty or gas_history.empty:
        return pd.DataFrame(columns=["month", "cal_month", "year", "heat_rate"])

    gas_monthly = gas_history.resample("MS").mean()
    df = monthly_lmp.copy()
    df = df.merge(
        gas_monthly.rename("gas").reset_index().rename(columns={"index": "month"}),
        on="month", how="inner",
    )
    df = df[(df["lmp"] > 0) & (df["gas"] > 0.5)]
    df["heat_rate"] = df["lmp"] / df["gas"]
    df = df[(df["heat_rate"] > 2) & (df["heat_rate"] < 40)]  # sanity bounds
    df["cal_month"] = df["month"].dt.month
    df["year"] = df["month"].dt.year
    # `month` rides along for the pooled model's trend clock and weather join;
    # the per-calendar-month estimator ignores it.
    return df[["month", "cal_month", "year", "heat_rate"]]


# ---------------------------------------------------------------------------
# Volatility term structure and correlation
# ---------------------------------------------------------------------------

def _seasonal_shape(gas_history: pd.Series) -> dict[int, float]:
    """Per-delivery-month volatility multipliers, mean 1.0.

    Recalibrated from `gas_history` when it spans enough years, so the shape
    tracks the market as it evolves; otherwise the baked-in SEASONAL_VOL_SHAPE
    is used. Averages two views of the same history — month-over-month return
    σ, and cross-year dispersion of each month's deviation from its own year's
    mean — because neither alone is stable on ~10 years of data.
    """
    if gas_history is None or gas_history.empty:
        return dict(SEASONAL_VOL_SHAPE)
    s = gas_history.dropna()
    s = s[s > 0]
    if s.index.year.nunique() < MIN_YEARS_FOR_SEASONAL_FIT:
        return dict(SEASONAL_VOL_SHAPE)
    lg = np.log(s)
    df = pd.DataFrame({"lg": lg, "y": lg.index.year, "m": lg.index.month})
    # View 1: month-over-month log-return σ, grouped by the arriving month.
    ret = lg.diff().dropna()
    v1 = ret.groupby(ret.index.month).std()
    # View 2: cross-year σ of each month's deviation from its year's level.
    df["dev"] = df["lg"] - df.groupby("y")["lg"].transform("mean")
    v2 = df.groupby("m")["dev"].std()
    if v1.isna().any() or v2.isna().any() or len(v1) < 12 or len(v2) < 12:
        return dict(SEASONAL_VOL_SHAPE)
    shape = ((v1 / v1.mean()) + (v2 / v2.mean())) / 2.0
    shape = shape / shape.mean()
    if not np.isfinite(shape.values).all() or (shape <= 0).any():
        return dict(SEASONAL_VOL_SHAPE)
    return {int(m): float(v) for m, v in shape.items()}


def _ou_horizon_vol(t_years: np.ndarray, sigma_ann: float, kappa: float) -> np.ndarray:
    """Terminal log-σ of a mean-reverting gas shock at horizons `t_years`.

    ``σ·√((1-e^(-2κt))/(2κ))`` — equals σ·√t as κ→0 and saturates at σ/√(2κ)
    for long horizons, which is why a 24-month band no longer implies a 2.5×
    P50→P90 on gas alone.
    """
    t = np.asarray(t_years, dtype=float)
    if kappa <= 0:
        return sigma_ann * np.sqrt(t)
    return sigma_ann * np.sqrt((1.0 - np.exp(-2.0 * kappa * t)) / (2.0 * kappa))


def _gas_variance_parts(
    t_years: np.ndarray,
    cal_months: np.ndarray,
    shape: dict[int, float],
    sigma_ann: float = GAS_SIGMA_ANN,
    kappa: float = GAS_KAPPA,
) -> tuple[np.ndarray, np.ndarray]:
    """Gas log-variance at each horizon month, split by how it correlates.

      * `shared_var` — the seasonally-neutral OU diffusion out to delivery.
        This is a common factor: it moves the whole strip together, and it is
        what makes a gas regime shift show up in every month at once.
      * `total_var` — that plus the *seasonal excess* of the delivery month,
        ``(mult² - 1)·σ²`` accumulated over the final DELIVERY_WINDOW_YEARS.
        Winter months add variance here (polar-vortex risk, idiosyncratic to
        that month's weather); quiet summer months subtract it.

    The seasonal term is an excess over the base rather than a replacement for
    it, so the near months stay tied to the common factor — treating their
    whole variance as delivery-specific would leave the front of the strip
    uncorrelated with everything, which is the opposite of how gas behaves.

    `shared_var` is capped at `total_var` so the implied correlation can never
    exceed the OU correlation (and the covariance stays PSD).
    """
    t = np.asarray(t_years, dtype=float)
    window = np.minimum(t, DELIVERY_WINDOW_YEARS)
    mult = np.array([shape.get(int(m), 1.0) for m in cal_months], dtype=float)
    shared_var = _ou_horizon_vol(t, sigma_ann, kappa) ** 2
    seasonal_excess = (mult ** 2 - 1.0) * sigma_ann ** 2 * window
    # Floor keeps a quiet summer month positive without letting it collapse.
    total_var = np.maximum(shared_var + seasonal_excess, 0.10 * shared_var)
    return np.minimum(shared_var, total_var), total_var


def _gas_terminal_sigma(
    t_years: np.ndarray,
    cal_months: np.ndarray,
    shape: dict[int, float],
    sigma_ann: float = GAS_SIGMA_ANN,
    kappa: float = GAS_KAPPA,
) -> np.ndarray:
    """Terminal log-σ of gas at each horizon month."""
    _, total_var = _gas_variance_parts(
        t_years, cal_months, shape, sigma_ann, kappa)
    return np.minimum(np.sqrt(total_var), GAS_SIGMA_MAX)


def _gas_corr_matrix(
    t_years: np.ndarray,
    cal_months: np.ndarray,
    shape: dict[int, float],
    sigma_ann: float = GAS_SIGMA_ANN,
    kappa: float = GAS_KAPPA,
) -> np.ndarray:
    """Correlation of the gas shock across horizon months.

    Built consistently with the variance split: the shared OU diffusion
    correlates across months at the OU rate, while the delivery month's
    seasonal excess is that month's own weather and is treated as
    idiosyncratic. Applying the OU correlation to the whole variance would
    imply January's polar-vortex risk moves in lockstep with July's — it
    doesn't, and that would overstate the spread on any multi-month average.

    PSD by construction: an OU-weighted Gram matrix plus a non-negative
    diagonal.
    """
    shared_var, total_var = _gas_variance_parts(
        t_years, cal_months, shape, sigma_ann, kappa)
    shared_sd = np.sqrt(shared_var)
    cov = np.outer(shared_sd, shared_sd) * _ou_corr_matrix(t_years, kappa)
    cov[np.diag_indices_from(cov)] = total_var
    sd = np.sqrt(np.diag(cov))
    sd[sd <= 0] = 1.0
    corr = cov / np.outer(sd, sd)
    np.fill_diagonal(corr, 1.0)
    return corr


def _ou_corr_matrix(t_years: np.ndarray, kappa: float) -> np.ndarray:
    """Correlation of the gas shock between horizon months.

    For an OU process, ``corr(X_s, X_t) = e^(-κ(t-s))·√((1-e^(-2κs))/(1-e^(-2κt)))``
    for s ≤ t. No extra free parameter: the same κ that sets the vol term
    structure sets how tightly the months move together, so a gas regime shift
    hits the whole strip at once instead of diversifying away across months.
    """
    t = np.asarray(t_years, dtype=float)
    if kappa <= 0:
        # Pure random walk: corr = √(min/max).
        lo = np.minimum.outer(t, t)
        hi = np.maximum.outer(t, t)
        return np.sqrt(np.divide(lo, hi, out=np.ones_like(lo), where=hi > 0))
    lo_i = np.minimum.outer(t, t)
    hi_i = np.maximum.outer(t, t)
    var_lo = 1.0 - np.exp(-2.0 * kappa * lo_i)
    var_hi = 1.0 - np.exp(-2.0 * kappa * hi_i)
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.exp(-kappa * (hi_i - lo_i)) * np.sqrt(
            np.divide(var_lo, var_hi, out=np.ones_like(var_lo), where=var_hi > 0))
    corr[~np.isfinite(corr)] = 0.0
    np.fill_diagonal(corr, 1.0)
    return corr


def _safe_cholesky(corr: np.ndarray) -> np.ndarray:
    """Cholesky factor of a correlation matrix, repaired if numerically
    non-PSD (eigenvalue clipping then renormalising the diagonal)."""
    try:
        return np.linalg.cholesky(corr)
    except np.linalg.LinAlgError:
        vals, vecs = np.linalg.eigh(corr)
        vals = np.clip(vals, 1e-10, None)
        fixed = vecs @ np.diag(vals) @ vecs.T
        d = np.sqrt(np.diag(fixed))
        fixed = fixed / np.outer(d, d)
        np.fill_diagonal(fixed, 1.0)
        return np.linalg.cholesky(fixed + 1e-10 * np.eye(len(fixed)))


def _equicorr(n: int, rho: float) -> np.ndarray:
    """n×n equicorrelation matrix (rho off-diagonal, 1 on the diagonal)."""
    rho = float(np.clip(rho, -1.0 / max(n - 1, 1) + 1e-9, 1.0 - 1e-9))
    m = np.full((n, n), rho, dtype=float)
    np.fill_diagonal(m, 1.0)
    return m


# ---------------------------------------------------------------------------
# Recency-weighted heat-rate statistics
# ---------------------------------------------------------------------------

def _weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    """Interpolated weighted quantile."""
    order = np.argsort(values)
    v, w = values[order], weights[order]
    cum = np.cumsum(w) - 0.5 * w
    total = w.sum()
    if total <= 0:
        return float(np.median(v))
    return float(np.interp(q, cum / total, v))


def _hr_stats(hr_samples: np.ndarray, years: np.ndarray,
              ref_year: int) -> tuple[float, float, float]:
    """Recency-weighted (median, log-σ, effective sample size) of implied HR.

    Weights decay exponentially in the observation's age with
    HR_RECENCY_HALFLIFE_YEARS, so the current fleet dominates the anchor while
    older years still inform the spread. The log-σ carries a small-sample
    correction against the *effective* sample size — with ~6 observations per
    calendar month and downweighting, the naive σ is biased low.
    """
    good = hr_samples > 0
    hr, yr = hr_samples[good], years[good]
    if len(hr) < 2:
        return 8.0, 0.20, float(len(hr))
    age = np.maximum(ref_year - yr, 0).astype(float)
    w = 0.5 ** (age / HR_RECENCY_HALFLIFE_YEARS)
    if w.sum() <= 0:
        w = np.ones_like(hr, dtype=float)
    n_eff = float(w.sum() ** 2 / np.square(w).sum())
    median = _weighted_quantile(hr, w, 0.5)
    lv = np.log(hr)
    mu = float(np.average(lv, weights=w))
    var = float(np.average((lv - mu) ** 2, weights=w))
    if n_eff > 1:
        var *= n_eff / (n_eff - 1.0)      # unbiased against effective n
    sigma = max(float(np.sqrt(max(var, 0.0))), HR_LOG_SIG_FLOOR)
    return float(median), sigma, n_eff


# ---------------------------------------------------------------------------
# Regional gas basis
# ---------------------------------------------------------------------------

def _load_gas_basis(state: str = BASIS_STATE) -> pd.Series:
    """Monthly delivered-to-power gas minus Henry Hub, $/MMBtu, for `state`.

    Empty series if the store is missing, in which case the whole basis
    channel collapses to zero and the model reduces to its Henry Hub form.

    Module-level and argument-light so the backtest can monkey-patch it to a
    pre-as-of slice, as it does for LMP, gas, and weather.
    """
    if not paths.GAS_BASIS_PARQUET.exists():
        return pd.Series(dtype=float)
    try:
        df = pd.read_parquet(paths.GAS_BASIS_PARQUET)
    except Exception:
        return pd.Series(dtype=float)
    df = df[df["state"] == state]
    if df.empty:
        return pd.Series(dtype=float)
    s = pd.Series(df["basis"].to_numpy(dtype=float),
                  index=pd.to_datetime(df["month"]))
    return s[~s.index.duplicated(keep="last")].sort_index()


def _basis_stats(basis: pd.Series,
                 asof: pd.Timestamp) -> tuple[dict[int, float], dict[int, float]]:
    """Per-calendar-month (mean, σ) of basis, from recent history only.

    σ is shrunk toward the pooled σ — with ~8-10 observations a month, a raw
    per-month σ swings between 0.18 and 2.18 and would make the band lurch
    from month to month for no defensible reason.
    """
    if basis is None or basis.empty:
        return {}, {}
    s = basis[basis.index < asof].dropna()
    cutoff = asof - pd.DateOffset(years=BASIS_HISTORY_YEARS)
    s = s[s.index >= cutoff]
    if len(s) < 24:
        return {}, {}
    cal = s.index.month
    mu = s.groupby(cal).mean()
    anomaly = s - cal.map(mu)
    pooled_var = float(np.square(anomaly).mean())
    mus, sds = {}, {}
    for m, sub in s.groupby(cal):
        n = len(sub)
        var = float(np.square(sub - sub.mean()).sum() / max(n - 1, 1)) if n > 1 else pooled_var
        blended = (n * var + BASIS_SHRINK_K * pooled_var) / (n + BASIS_SHRINK_K)
        mus[int(m)] = float(sub.mean())
        sds[int(m)] = float(np.sqrt(max(blended, 0.0)))
    return mus, sds


def _delivered_gas(gas_history: pd.Series, basis: pd.Series,
                   basis_mu: dict[int, float]) -> pd.Series:
    """Henry Hub plus realised basis, falling back to the seasonal normal.

    The EIA basis series lags roughly three months, so the most recent months
    of history have no print. Filling those with that calendar month's normal
    keeps the heat-rate panel complete — dropping them would cost exactly the
    recent months the trend leans on — and is the same estimate the forward
    uses, so history and forecast stay on one definition.
    """
    if gas_history is None or gas_history.empty:
        return gas_history
    if (basis is None or basis.empty) and not basis_mu:
        return gas_history
    b = basis.reindex(gas_history.index) if basis is not None and not basis.empty \
        else pd.Series(index=gas_history.index, dtype=float)
    fill = pd.Series([basis_mu.get(m, 0.0) for m in gas_history.index.month],
                     index=gas_history.index)
    return gas_history + b.fillna(fill)


# ---------------------------------------------------------------------------
# Pooled heat-rate model
# ---------------------------------------------------------------------------

def _load_weather_monthly(zone: str = "DOM") -> pd.DataFrame:
    """Monthly extreme-day degree sums for `zone` from the weather store.

    Columns: month, xcold, xhot. Empty frame if the store is missing — the
    caller falls back to the per-calendar-month estimator.

    Kept as a module-level function with no arguments beyond the zone so the
    backtest can monkey-patch it to a pre-as-of slice, exactly as it does for
    `_load_dom_hub_monthly` and `_gas_from_eia`. Without that the weather
    channel would leak the future into every walk-forward run.
    """
    if not paths.WEATHER_PARQUET.exists():
        return pd.DataFrame(columns=["month", "xcold", "xhot"])
    try:
        w = pd.read_parquet(
            paths.WEATHER_PARQUET,
            columns=["datetime_beginning_ept", "zone", "temp_f"],
        )
    except Exception:
        return pd.DataFrame(columns=["month", "xcold", "xhot"])
    w = w[w["zone"] == zone]
    if w.empty:
        return pd.DataFrame(columns=["month", "xcold", "xhot"])
    dt = pd.to_datetime(w["datetime_beginning_ept"])
    # Average the zone's cities first, then reduce to one temperature per day:
    # a degree-day is a property of the day, not of an hour.
    daily = (pd.DataFrame({"day": dt.dt.normalize(), "temp_f": w["temp_f"].to_numpy()})
             .groupby("day")["temp_f"].mean())
    out = pd.DataFrame({
        "month": daily.index.to_period("M").to_timestamp(),
        "xcold": (HR_COLD_DAY_F - daily.to_numpy()).clip(min=0),
        "xhot": (daily.to_numpy() - HR_HOT_DAY_F).clip(min=0),
    })
    return out.groupby("month", as_index=False)[["xcold", "xhot"]].sum()


class _HeatRateModel:
    """log(HR) ~ trend + calendar-month effects + extreme-weather anomalies.

    `predict(months)` returns the per-month (anchor, log-σ) the Monte Carlo
    engine consumes. σ is the *predictive* spread and carries three pieces:

      * residual variance — what the fit does not explain;
      * parameter variance x'(X'X)⁻¹x·s² — the fitted line's own uncertainty,
        which grows as the trend is extrapolated past the data. Without this
        the bands are visibly overconfident near-in (h=1 coverage 42%);
      * weather variance — anomalies are unknowable ahead, so the point
        forecast sets them to their zero expectation and their spread moves
        into σ. This is what makes σ seasonal: a January carries the spread of
        unknown cold-day risk, a September carries almost none.
    """

    def __init__(self, beta, cals, cal_effect, resid_var, xtxi, wx_var,
                 t0, t_max, n_obs):
        self.beta = beta
        self.cals = cals                # calendar months, first is the base
        self.cal_effect = cal_effect
        self.resid_var = resid_var
        self.xtxi = xtxi                # (X'X)⁻¹, for parameter uncertainty
        self.wx_var = wx_var
        self.t0 = t0
        self.t_max = t_max
        self.n_obs = n_obs

    def _t(self, month: pd.Timestamp) -> float:
        t = (month - self.t0).days / 365.25
        # Hold the trend flat past the extrapolation horizon rather than
        # letting a linear fit compound out to an implausible level.
        return min(t, self.t_max + HR_TREND_MAX_YEARS)

    def _design_row(self, month: pd.Timestamp) -> np.ndarray:
        """The design row for a forecast month, weather anomalies at zero."""
        row = [1.0, self._t(month)]
        row += [1.0 if month.month == c else 0.0 for c in self.cals[1:]]
        row += [0.0, 0.0]
        return np.asarray(row, dtype=float)

    def predict(self, months) -> tuple[np.ndarray, np.ndarray]:
        anchor = np.empty(len(months))
        sigma = np.empty(len(months))
        for i, m in enumerate(months):
            x = self._design_row(m)
            mu = (self.beta[0] + self.beta[1] * self._t(m)
                  + self.cal_effect.get(m.month, 0.0))
            par_var = float(x @ self.xtxi @ x) * self.resid_var
            var = self.resid_var + par_var + self.wx_var.get(m.month, 0.0)
            sig = max(float(np.sqrt(max(var, 0.0))), HR_LOG_SIG_FLOOR)
            # The engine draws hr * exp(-½σ² + σz), whose median is
            # hr * exp(-½σ²). Carrying the +½σ² here makes the *simulated*
            # median land on exp(mu), the model's actual central estimate.
            anchor[i] = float(np.exp(mu + 0.5 * sig ** 2))
            sigma[i] = sig
        return anchor, sigma


def _fit_heat_rate_model(hr_dist: pd.DataFrame,
                         wx: pd.DataFrame) -> "_HeatRateModel | None":
    """Fit the pooled model, or None if there is too little to fit it on.

    Returning None (rather than a degraded fit) keeps the caller on the
    per-calendar-month estimator for short histories, where a trend estimated
    off two years of data would be worse than no trend at all.

    Deliberately *not* included: a trend×season interaction. Peak months do
    trend faster than mild ones in-sample (corr 0.48 between a month's slope
    and its seasonal level), but the per-month slopes come off 6–7 points
    each and the interaction lost out-of-sample at every horizon tested
    (log-HR MAE 0.225 vs 0.211 flat). It is overfitting, not signal.
    """
    if hr_dist.empty or "month" not in hr_dist.columns:
        return None
    df = hr_dist.dropna(subset=["heat_rate"]).copy()
    df = df[df["heat_rate"] > 0]
    if len(df) < HR_MODEL_MIN_MONTHS:
        return None
    if wx is not None and not wx.empty:
        df = df.merge(wx, on="month", how="left")
    for c in ("xcold", "xhot"):
        if c not in df.columns:
            df[c] = 0.0
        df[c] = df[c].fillna(df.groupby("cal_month")[c].transform("mean")).fillna(0.0)
    # Anomalies against each calendar month's own normal, so the seasonal
    # dummies carry the climate and the weather terms carry only the surprise.
    for c in ("xcold", "xhot"):
        df[c + "_a"] = (df[c] - df.groupby("cal_month")[c].transform("mean")) / 100.0

    t0 = df["month"].min()
    df["t"] = (df["month"] - t0).dt.days / 365.25
    cals = sorted(int(c) for c in df["cal_month"].unique())
    if len(cals) < 2:
        return None
    cols = [np.ones(len(df)), df["t"].to_numpy(float)]
    cols += [(df["cal_month"] == c).to_numpy(float) for c in cals[1:]]
    cols += [df["xcold_a"].to_numpy(float), df["xhot_a"].to_numpy(float)]
    X = np.column_stack(cols)
    y = np.log(df["heat_rate"].to_numpy(float))
    dof = len(df) - X.shape[1]
    if dof < 6:
        return None
    try:
        b, *_ = np.linalg.lstsq(X, y, rcond=None)
        xtxi = np.linalg.pinv(X.T @ X)
    except np.linalg.LinAlgError:
        return None
    resid = y - X @ b
    resid_var = float(resid @ resid / dof)

    cal_effect = {cals[0]: 0.0}
    for j, c in enumerate(cals[1:], start=2):
        cal_effect[int(c)] = float(b[j])
    g_cold, g_hot = float(b[-2]), float(b[-1])
    wx_var = {}
    for c, sub in df.groupby("cal_month"):
        wx_var[int(c)] = float(g_cold ** 2 * sub["xcold_a"].var(ddof=0)
                               + g_hot ** 2 * sub["xhot_a"].var(ddof=0))
    return _HeatRateModel(
        beta=(float(b[0]), float(b[1]), g_cold, g_hot),
        cals=cals, cal_effect=cal_effect, resid_var=resid_var, xtxi=xtxi,
        wx_var=wx_var, t0=t0, t_max=float(df["t"].max()), n_obs=len(df),
    )


# ---------------------------------------------------------------------------
# Monte Carlo engine
# ---------------------------------------------------------------------------

def run(
    hub: str = PRIMARY_HUB,
    asof=None,
    horizon_months: int = 12,
    n_sims: int = DEFAULT_N_SIMS,
    seed: int = DEFAULT_SEED,
    eia_api_key: str | None = None,
    gas_asof=None,
    anchor_vintages: int = 5,
    return_paths: bool = False,
    use_basis: bool = USE_DELIVERED_GAS,
) -> pd.DataFrame:
    """Run the Monte Carlo price forecast.

    Returns a DataFrame with columns:
        month (Timestamp, first-of-month), cal_month (int 1–12),
        p10, p25, p50, p75, p90 ($/MWh), n_samples (heat-rate observations),
        n_eff (effective sample after recency weighting), gas_fwd ($/MMBtu),
        gas_sigma (terminal log-σ applied to gas that month), hr_median,
        gas_source, vol_source, and strip_p10/p50/p90 — the distribution of
        the *average* price across the whole horizon, repeated on every row.

    The strip_* columns are the payoff of simulating gas as a correlated path:
    a whole-horizon average built from independent monthly draws would shed
    most of its regime risk, understating the spread on any annual number.

    Pass `gas_asof` (with a matching `asof`) to re-run the forecast from a
    past date using the archived gas-strip vintage from that date.
    `anchor_vintages` sets the strip-median window (1 = raw single-day settle).

    `use_basis` measures the heat rate against delivered gas (Henry Hub plus
    regional basis) instead of Henry Hub, and gives the forward a basis leg of
    its own. Off by default — see USE_DELIVERED_GAS for the walk-forward that
    says why.

    With `return_paths=True`, returns ``(df, {"gas", "delivered", "basis",
    "hr", "price"})``, each an (n_sims × horizon) array — for diagnostics
    and tests.
    """
    asof = pd.Timestamp(asof) if asof else pd.Timestamp.now().normalize()
    rng = np.random.default_rng(seed)

    gas_history = _gas_from_eia(eia_api_key)
    gas_fwd, gas_source = _gas_curve(eia_api_key, horizon_months, asof,
                                     gas_asof=gas_asof,
                                     anchor_vintages=anchor_vintages,
                                     gas_history=gas_history)
    monthly_lmp = _load_dom_hub_monthly(hub)
    # With the basis channel on, the heat rate is measured against the gas the
    # fleet actually burns; otherwise regional basis has nowhere to go but the
    # heat rate. See USE_DELIVERED_GAS for why the default is off.
    if use_basis:
        basis_hist = _load_gas_basis()
        basis_mu, basis_sd = _basis_stats(basis_hist, asof)
        gas_for_hr = _delivered_gas(gas_history, basis_hist, basis_mu)
    else:
        basis_mu, basis_sd = {}, {}
        gas_for_hr = gas_history
    hr_dist = _build_heat_rate_distribution(monthly_lmp, gas_for_hr)

    months = pd.date_range(asof + pd.offsets.MonthBegin(1),
                           periods=horizon_months, freq="MS")
    n = len(months)
    t_years = (np.arange(1, n + 1)) / 12.0

    # ── Gas volatility per horizon month ────────────────────────────────────
    # Preferred: forward vol measured straight off the vintage archive, which
    # already embeds the seasonal shape and the Samuelson effect. Until enough
    # vintages exist, fall back to the OU model fitted to EIA spot history.
    try:
        from pjm_core import gas_strip
        fwd_vol = gas_strip.forward_vol(gas_asof)
    except Exception:
        fwd_vol = None

    shape = _seasonal_shape(gas_history)
    model_sigma = _gas_terminal_sigma(
        t_years, np.array([m.month for m in months]), shape)

    if fwd_vol:
        # A forward is a martingale, so its terminal dispersion accumulates as
        # √t at the measured vol. Seasonality and the Samuelson effect are
        # already inside fwd_vol[m], so no shape multiplier is applied here.
        gas_sigma = np.array([
            min(fwd_vol[m] * np.sqrt(t), GAS_SIGMA_MAX) if m in fwd_vol
            else model_sigma[i]
            for i, (m, t) in enumerate(zip(months, t_years))
        ])
        n_measured = sum(1 for m in months if m in fwd_vol)
        vol_source = (f"vintage forward vol ({n_measured}/{n} months measured, "
                      "rest OU-modelled)")
    else:
        gas_sigma = model_sigma
        vol_source = "OU model on EIA spot (seasonal, σ=%.2f κ=%.2f)" % (
            GAS_SIGMA_ANN, GAS_KAPPA)

    # ── Heat-rate anchor and spread per horizon month ───────────────────────
    # Preferred: one pooled fit over every month of history, which estimates
    # the fleet's trend off ~80 observations and extrapolates along it. The
    # per-calendar-month recency median is the fallback for short histories,
    # where a trend fitted on two years would be worse than none.
    hr_model = _fit_heat_rate_model(hr_dist, _load_weather_monthly())
    ref_year = int(hr_dist["year"].max()) if not hr_dist.empty else asof.year
    hr_med = np.empty(n)
    hr_sig = np.empty(n)
    n_obs = np.empty(n, dtype=int)
    n_eff = np.empty(n)
    if hr_model is not None:
        hr_med, hr_sig = hr_model.predict(months)
        n_obs[:] = hr_model.n_obs
        n_eff[:] = hr_model.n_obs
        hr_source = (f"pooled trend+season+weather on {hr_model.n_obs} months "
                     f"({(np.exp(hr_model.beta[1]) - 1) * 100:+.1f}%/yr trend)")
    else:
        for i, month in enumerate(months):
            sub = hr_dist[hr_dist["cal_month"] == month.month]
            n_obs[i] = len(sub)
            hr_med[i], hr_sig[i], n_eff[i] = _hr_stats(
                sub["heat_rate"].to_numpy(dtype=float),
                sub["year"].to_numpy(dtype=int),
                ref_year,
            )
        hr_source = (f"per-calendar-month recency median "
                     f"({HR_RECENCY_HALFLIFE_YEARS:.0f}yr half-life)")

    # ── Correlated draws ────────────────────────────────────────────────────
    # Gas across months follows the OU covariance, so a regime shift hits the
    # whole strip together. The heat-rate shock is equicorrelated across months
    # and independent of gas; the gas→power link is the structural pass-through
    # below, not a correlation between the two shocks.
    z_g = rng.standard_normal((n_sims, n)) @ _safe_cholesky(
        _gas_corr_matrix(t_years, np.array([m.month for m in months]), shape)).T
    z_e = rng.standard_normal((n_sims, n)) @ _safe_cholesky(
        _equicorr(n, HR_TERM_CORR)).T
    z_b = rng.standard_normal((n_sims, n)) @ _safe_cholesky(
        _equicorr(n, BASIS_TERM_CORR)).T

    gas_f = np.array([float(gas_fwd.get(m, LONG_RUN_GAS)) for m in months])
    # -½σ² keeps E[gas] on the forward, so the anchor stays mark-consistent.
    gas_dev = -0.5 * gas_sigma ** 2 + gas_sigma * z_g   # log(gas / forward)
    gas_sim = gas_f * np.exp(gas_dev)

    # Basis is additive in $/MMBtu and, once its seasonal mean is removed, close
    # to symmetric (skew +0.14) — the fat right tail people associate with it is
    # the January/February *mean*, not the anomaly. So: seasonal level plus a
    # normal shock, not a lognormal. Basis anomaly is mildly negatively
    # correlated with the Henry Hub level (-0.28) and that link is left out;
    # summing the two independently makes the delivered band slightly wider
    # than reality, which is the safe direction to be wrong in.
    b_mu = np.array([basis_mu.get(m.month, 0.0) for m in months])
    b_sd = np.array([basis_sd.get(m.month, 0.0) for m in months])
    basis_sim = b_mu + b_sd * z_b
    delivered_f = gas_f + b_mu
    delivered_sim = np.maximum(gas_sim + basis_sim, MIN_DELIVERED_GAS)

    # Pass-through applies to *delivered* fuel cost, which is what a unit's
    # offer actually responds to. β was measured against Henry Hub, so reusing
    # it here is an approximation — the two differ only where basis moves
    # independently of Henry Hub, which is winter.
    dev_d = np.log(delivered_sim / delivered_f)
    # Heat rate compresses as gas rallies: the -(1-β)·dev term makes
    # log(price) = β·log(gas) + noise, i.e. exactly the measured pass-through.
    hr_sim = hr_med * np.exp(-(1.0 - GAS_PASS_THROUGH_BETA) * dev_d
                             - 0.5 * hr_sig ** 2 + hr_sig * z_e)
    price_sim = np.minimum(delivered_sim * hr_sim, PJM_PRICE_CAP)

    qs = np.percentile(price_sim, [10, 25, 50, 75, 90], axis=0)
    strip_q = np.percentile(price_sim.mean(axis=1), [10, 50, 90])

    out = pd.DataFrame({
        "month": months,
        "cal_month": [m.month for m in months],
        "p10": qs[0], "p25": qs[1], "p50": qs[2], "p75": qs[3], "p90": qs[4],
        "n_samples": n_obs,
        "n_eff": n_eff.round(2),
        "gas_fwd": gas_f,
        "gas_sigma": gas_sigma,
        "basis_fwd": b_mu,
        "basis_sigma": b_sd,
        "delivered_fwd": delivered_f,
        "hr_median": hr_med,
    })
    out["gas_source"] = gas_source
    out["vol_source"] = vol_source
    out["hr_source"] = hr_source
    out["basis_source"] = (
        f"EIA {BASIS_STATE} delivered-to-power vs Henry Hub "
        f"({BASIS_HISTORY_YEARS}yr seasonal)" if basis_mu
        else "none — heat rate measured against Henry Hub")
    out["basis_source"] = out["basis_source"].astype(str)
    out["strip_p10"] = float(strip_q[0])
    out["strip_p50"] = float(strip_q[1])
    out["strip_p90"] = float(strip_q[2])
    if return_paths:
        return out, {"gas": gas_sim, "delivered": delivered_sim,
                     "basis": basis_sim, "hr": hr_sim, "price": price_sim}
    return out


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------

def _cache_path(hub: str, asof: pd.Timestamp) -> Path:
    return paths.PRICE_FORECAST_DIR / f"pjm_forecast_{hub.replace(' ', '_')}_{asof.date()}.parquet"


def load_cached(hub: str, asof=None) -> pd.DataFrame | None:
    asof = pd.Timestamp(asof) if asof else pd.Timestamp.now().normalize()
    p = _cache_path(hub, asof)
    return pd.read_parquet(p) if p.exists() else None


def save_cached(df: pd.DataFrame, hub: str, asof=None) -> None:
    asof = pd.Timestamp(asof) if asof else pd.Timestamp.now().normalize()
    paths.PRICE_FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(_cache_path(hub, asof), index=False)


def monthly_band(
    hub: str = PRIMARY_HUB,
    asof=None,
    horizon_months: int = 12,
    n_sims: int = DEFAULT_N_SIMS,
    eia_api_key: str | None = None,
    anchor_vintages: int = 5,
) -> pd.DataFrame:
    """Return cached forecast or run fresh. Columns: month, p10, p50, p90."""
    asof = pd.Timestamp(asof) if asof else pd.Timestamp.now().normalize()
    cached = load_cached(hub, asof)
    if cached is not None:
        return cached
    df = run(hub=hub, asof=asof, horizon_months=horizon_months,
             n_sims=n_sims, eia_api_key=eia_api_key,
             anchor_vintages=anchor_vintages)
    try:
        save_cached(df, hub, asof)
    except Exception:
        pass
    return df
