"""Walk-forward backtest of the DOM Hub price forecast.

At each historical as-of date we have a strip vintage for, run the full model
constrained to only see data on or before that date, then compare each
forecasted month's P50/P10/P90 to what actually cleared. Persists to a parquet
so the Streamlit view loads fast and only recomputes when the archive grows.

Two data limits worth knowing:

  * The gas-strip archive is reconstructed from *currently-trading* NYMEX
    contracts. Yahoo delists expired contracts, so any vintage more than a
    few days old is missing its near months — the front year comes from
    _extend_to_horizon's mean-reversion fallback rather than the actual
    market price of those contracts at the time. `gas_from_strip` marks per
    row whether the forecast month's own contract was in the vintage (True)
    or filled in (False). Today the flag is False on every backtested row;
    it will start populating a few months from now, as forecasts against
    still-live contracts from live daily pulls land in the realized window.
    Until then the backtest measures (model + strip-extrapolation) as a
    bundle rather than the model alone.
  * The DOM Hub LMP store starts 2020-01-01, so as-ofs before ~2021 have too
    little history for the recency-weighted heat rate to be meaningful.
    Backtests below 12 months of prior LMPs are skipped, not silently biased.
"""

from __future__ import annotations

import unittest.mock as mock
from typing import Iterable

import numpy as np
import pandas as pd

from pjm_core import credentials, gas_strip, paths, price_forecast as pf
from pjm_core.settlement_points import PRIMARY_HUB


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------

def _monthly_asofs(vintages: list[pd.Timestamp]) -> list[pd.Timestamp]:
    """One as-of per calendar month — the earliest vintage in that month.

    Daily granularity would make the backtest ~30× more expensive and the
    marginal information per extra as-of is small (a strip moves modestly
    over a week). Monthly gives a clean walk-forward and one row per (asof,
    forecast-month) pair per calendar.
    """
    s = pd.Series(pd.to_datetime(vintages)).sort_values()
    return list(s.groupby(s.dt.to_period("M")).min())


def run_backtest(
    hub: str = PRIMARY_HUB,
    horizon_months: int = 12,
    asofs: Iterable[pd.Timestamp] | None = None,
    n_sims: int = 2_000,
    eia_api_key: str | None = None,
    log=print,
) -> pd.DataFrame:
    """Run the walk-forward and return one row per (as-of, forecast-month).

    Filters the model's own inputs to `< asof` before each run — no lookahead.
    Returned columns:
        asof, month, horizon (months), gas_fwd, hr_median,
        p10, p50, p90, actual, err (p50 - actual), ape, in_band,
        vintage_incomplete, gas_source, vol_source.

    `asofs` defaults to one per calendar month from the vintage archive.
    """
    key = eia_api_key if eia_api_key is not None else credentials.get_eia_api_key()

    lmp_full = pf._load_dom_hub_monthly(hub)
    lmp_full["month"] = pd.to_datetime(lmp_full["month"])
    gas_hist_full = pf._gas_from_eia(key)
    actuals = lmp_full.set_index("month")["lmp"]

    if asofs is None:
        asofs = _monthly_asofs(gas_strip.vintages())
    else:
        asofs = [pd.Timestamp(a).normalize() for a in asofs]
    # Nothing to compare against for as-ofs newer than the last realised month.
    last_realised = actuals.index.max()
    asofs = [a for a in asofs if a <= last_realised - pd.offsets.MonthBegin(1)]

    rows: list[pd.DataFrame] = []
    for asof in asofs:
        lmp_seen = lmp_full[lmp_full["month"] < asof]
        gas_seen = gas_hist_full[gas_hist_full.index < asof]
        # Skip as-ofs with too little LMP history — the heat-rate anchor needs
        # a few years to be meaningful, and pretending otherwise would spoil
        # the summary with garbage bias numbers.
        if lmp_seen["month"].nunique() < 12:
            log(f"  {asof.date()}: only {lmp_seen['month'].nunique()}mo of LMP, skipping")
            continue
        with mock.patch.object(pf, "_load_dom_hub_monthly", lambda hub=None: lmp_seen), \
             mock.patch.object(pf, "_gas_from_eia", lambda *a, **k: gas_seen):
            try:
                df = pf.run(hub=hub, horizon_months=horizon_months,
                            asof=asof, gas_asof=asof, n_sims=n_sims,
                            eia_api_key=key)
            except Exception as e:
                log(f"  {asof.date()}: {e}")
                continue

        # Per-row honesty check: was this forecast month's gas contract
        # actually in the reconstructed vintage, or filled in by
        # _extend_to_horizon's mean-reversion fallback? Yahoo delists expired
        # contracts, so older as-ofs are missing their near months and the
        # gas_fwd for those rows is an extrapolation, not a market print.
        # A backtest metric that mixes real-strip and extrapolated-strip rows
        # is measuring two different models — split them so the summary can
        # exclude the extrapolated ones and be about the model alone.
        got = gas_strip.strip_asof(asof)
        vintage_months = (set(pd.to_datetime(got[0]["month"]))
                          if got is not None else set())

        df["asof"] = asof
        df["horizon"] = ((df["month"].dt.year - asof.year) * 12
                         + (df["month"].dt.month - asof.month))
        df["actual"] = df["month"].map(actuals)
        df["gas_from_strip"] = df["month"].isin(vintage_months)
        rows.append(df[df["actual"].notna()])

    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    out["err"] = out["p50"] - out["actual"]
    out["ape"] = out["err"].abs() / out["actual"]
    out["in_band"] = (out["actual"] >= out["p10"]) & (out["actual"] <= out["p90"])
    keep = ["asof", "month", "horizon", "gas_fwd", "hr_median",
            "p10", "p50", "p90", "actual", "err", "ape", "in_band",
            "gas_from_strip", "gas_source", "vol_source"]
    return out[[c for c in keep if c in out.columns]].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _fingerprint(df: pd.DataFrame) -> tuple[int, int]:
    """(count of as-ofs, count of rows) — cheap way to detect that a rerun
    would produce different results because new vintages or LMPs landed."""
    return (int(df["asof"].nunique()) if len(df) else 0, len(df))


def load_cached() -> pd.DataFrame | None:
    p = paths.FORECAST_BACKTEST_PARQUET
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    for c in ("asof", "month", "vintage_first_month"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c])
    return df


def save_cached(df: pd.DataFrame) -> None:
    paths.PRICE_FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(paths.FORECAST_BACKTEST_PARQUET, index=False)


def refresh_if_stale(**kw) -> pd.DataFrame:
    """Rerun only when the vintage or LMP store has grown since last cache."""
    cached = load_cached()
    n_vintages = len(gas_strip.vintages())
    last_lmp = pf._load_dom_hub_monthly()["month"].max()
    if cached is not None and len(cached):
        cached_vintages = cached["asof"].nunique()
        cached_last = pd.to_datetime(cached["month"]).max()
        # Rough heuristic: as-ofs are one per calendar month, so cache is fresh
        # if we haven't rolled into a new month with an available vintage AND
        # the LMP store hasn't added a new realised month.
        n_expected = len(_monthly_asofs(gas_strip.vintages()))
        if cached_vintages >= n_expected and pd.Timestamp(cached_last) >= pd.Timestamp(last_lmp):
            return cached
    fresh = run_backtest(**kw)
    if len(fresh):
        save_cached(fresh)
    return fresh


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

SUMMARY_METRICS = ("bias", "mae", "mape_pct", "coverage_pct", "n")


def summarise(bt: pd.DataFrame,
              real_strip_only: bool = False) -> dict:
    """Overall bias/MAE/MAPE/coverage on the backtest table."""
    df = bt if not real_strip_only else bt[bt["gas_from_strip"]]
    if df.empty:
        return {k: float("nan") for k in SUMMARY_METRICS[:-1]} | {"n": 0}
    return {
        "bias": float(df["err"].mean()),
        "mae": float(df["err"].abs().mean()),
        "mape_pct": float(df["ape"].mean() * 100),
        "coverage_pct": float(df["in_band"].mean() * 100),
        "n": int(len(df)),
    }


def by_group(bt: pd.DataFrame, col: str,
             real_strip_only: bool = False) -> pd.DataFrame:
    """Same as summarise() but grouped by a column (e.g. 'horizon', 'season').

    Rows sorted by the group key. Ideal for feeding straight into st.dataframe.
    """
    df = bt if not real_strip_only else bt[bt["gas_from_strip"]]
    if df.empty:
        return pd.DataFrame(columns=[col, *SUMMARY_METRICS])
    g = df.assign(abs_err=df["err"].abs()).groupby(col, observed=True)
    out = pd.DataFrame({
        "bias": g["err"].mean(),
        "mae": g["abs_err"].mean(),
        "mape_pct": g["ape"].mean() * 100,
        "coverage_pct": g["in_band"].mean() * 100,
        "n": g.size(),
    }).reset_index()
    return out


def attach_season(bt: pd.DataFrame) -> pd.DataFrame:
    """Add a 'season' column keyed off the forecast month."""
    df = bt.copy()
    df["season"] = pd.cut(pd.to_datetime(df["month"]).dt.month,
                          bins=[0, 3, 6, 9, 12],
                          labels=["Q1 winter", "Q2 spring",
                                  "Q3 summer", "Q4 fall"])
    return df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    horizon = 12
    for a in sys.argv[1:]:
        if a.startswith("--horizon="):
            horizon = int(a.split("=", 1)[1])
    print(f"Running backtest (horizon={horizon}mo)…")
    bt = run_backtest(horizon_months=horizon)
    if bt.empty:
        print("No results — check that the vintage archive and LMP store exist.")
        sys.exit(1)
    save_cached(bt)
    all_rows = summarise(bt, real_strip_only=False)
    honest = summarise(bt, real_strip_only=True)
    print(f"Saved {len(bt):,} (as-of, forecast-month) pairs "
          f"to {paths.FORECAST_BACKTEST_PARQUET}")
    print(f"  ALL rows           : bias {all_rows['bias']:+.2f}  "
          f"mae {all_rows['mae']:.2f}  mape {all_rows['mape_pct']:.1f}%  "
          f"coverage {all_rows['coverage_pct']:.1f}%  n={all_rows['n']}")
    print(f"  Real-strip only    : bias {honest['bias']:+.2f}  "
          f"mae {honest['mae']:.2f}  mape {honest['mape_pct']:.1f}%  "
          f"coverage {honest['coverage_pct']:.1f}%  n={honest['n']}")
    print(f"  {len(bt) - honest['n']:,} rows used mean-reversion-extrapolated gas.")
