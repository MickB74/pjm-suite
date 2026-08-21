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


# A month is only scoreable once it has essentially all its hours. The store
# grows through the current month, so `_load_dom_hub_monthly` would otherwise
# average a 20-day August and hand it to the backtest as that month's realised
# price. Stricter than the 0.90 the zone-price ETL uses for the same test: that
# threshold tolerates the archived↔live cutoff month losing a day, whereas here
# a 10%-short month is a materially wrong average being scored as truth. DST is
# safely inside the tolerance — March holds 743 of 744 hours (spring-forward),
# November 720 of 720 (the repeated hour collapses under one label).
MONTH_COMPLETE_FRAC = 0.98


def _complete_months(hub: str = PRIMARY_HUB) -> set[pd.Timestamp]:
    """Month-start timestamps whose hourly LMP coverage is effectively full."""
    if not paths.HUB_PRICES_PARQUET.exists():
        return set()
    try:
        df = pd.read_parquet(
            paths.HUB_PRICES_PARQUET,
            columns=["datetime_beginning_ept", "pnode_name"],
        )
    except Exception:
        return set()
    df = df[df["pnode_name"] == hub]
    if df.empty:
        return set()
    dt = pd.to_datetime(df["datetime_beginning_ept"])
    counted = dt.groupby(dt.dt.to_period("M")).nunique()
    return {
        per.to_timestamp() for per, n_hours in counted.items()
        if n_hours >= MONTH_COMPLETE_FRAC * per.days_in_month * 24
    }


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
        gas_from_strip, gas_source, vol_source.

    `asofs` defaults to one per calendar month from the vintage archive.
    """
    key = eia_api_key if eia_api_key is not None else credentials.get_eia_api_key()

    lmp_full = pf._load_dom_hub_monthly(hub)
    lmp_full["month"] = pd.to_datetime(lmp_full["month"])
    gas_hist_full = pf._gas_from_eia(key)
    # The pooled heat-rate model reads weather, so it needs the same per-as-of
    # truncation as LMP and gas — otherwise every walk-forward run would fit
    # its weather coefficients on months it is being scored against.
    wx_full = pf._load_weather_monthly()
    if not wx_full.empty:
        wx_full["month"] = pd.to_datetime(wx_full["month"])
    # Same for gas basis: `_basis_stats` already filters to < asof internally,
    # but patching the loader keeps every historical input on one rule.
    basis_full = pf._load_gas_basis()
    actuals = lmp_full.set_index("month")["lmp"]
    # Score only finished months — an in-progress month averages to a partial
    # price that is not what that month cleared at.
    complete = _complete_months(hub)
    dropped = sorted(m for m in actuals.index if m not in complete)
    actuals = actuals[actuals.index.isin(complete)]
    for m in dropped:
        log(f"  {m:%Y-%m}: incomplete month, not scored")

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
        wx_seen = (wx_full[wx_full["month"] < asof] if not wx_full.empty
                   else wx_full)
        basis_seen = (basis_full[basis_full.index < asof] if not basis_full.empty
                      else basis_full)
        with mock.patch.object(pf, "_load_dom_hub_monthly", lambda hub=None: lmp_seen), \
             mock.patch.object(pf, "_gas_from_eia", lambda *a, **k: gas_seen), \
             mock.patch.object(pf, "_load_weather_monthly", lambda *a, **k: wx_seen), \
             mock.patch.object(pf, "_load_gas_basis", lambda *a, **k: basis_seen):
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
            "gas_from_strip", "gas_source", "vol_source", "hr_source"]
    return out[[c for c in keep if c in out.columns]].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Heat-rate-only diagnostic
# ---------------------------------------------------------------------------

def run_hr_backtest(hub: str = PRIMARY_HUB, horizon_months: int = 12,
                    eia_api_key: str | None = None) -> pd.DataFrame:
    """Score the heat-rate estimators against *realised* gas, not the strip.

    Why this exists: every row `run_backtest` can currently produce uses a gas
    curve that `_extend_to_horizon` invented. Yahoo delists expired contracts,
    so a reconstructed vintage has no real near months and the fallback hands
    back the same number for all 12 — a dead-flat curve where the real NYMEX
    strip has a pronounced winter peak. Price error is then dominated by that
    fabricated input, and it moves in the opposite direction to heat-rate
    error, so the two partly cancel. A heat-rate change can therefore improve
    the model and make the end-to-end MAE worse, which is exactly what
    happened when the pooled model landed.

    Holding gas at its realised value removes the confound: what is left is
    the heat-rate forecast alone. Returns one row per (as-of, month) with
    `hr_pooled`/`hr_recency` predictions and the price each implies.

    This is a diagnostic, not a forecast-quality claim — it grants perfect
    foresight on gas, so its errors are smaller than a real forecast's. Use it
    to compare estimators, and `run_backtest` for the headline. Once real
    strip rows accumulate, `run_backtest(real_strip_only=True)` supersedes it.
    """
    key = eia_api_key if eia_api_key is not None else credentials.get_eia_api_key()
    lmp = pf._load_dom_hub_monthly(hub)
    gas = pf._gas_from_eia(key)
    wx = pf._load_weather_monthly()
    basis = pf._load_gas_basis()
    hr_all = pf._build_heat_rate_distribution(lmp, gas)   # vs Henry Hub
    actuals = lmp.set_index("month")["lmp"]
    actuals = actuals[actuals.index.isin(_complete_months(hub))]
    if hr_all.empty or actuals.empty:
        return pd.DataFrame()

    rows = []
    for asof in _monthly_asofs(gas_strip.vintages()):
        seen = hr_all[hr_all["month"] < asof]
        if len(seen) < pf.HR_MODEL_MIN_MONTHS:
            continue
        wx_seen = wx[wx["month"] < asof] if not wx.empty else wx
        model = pf._fit_heat_rate_model(seen, wx_seen)
        # Delivered-gas variant: the heat rate the live model actually fits.
        b_seen = basis[basis.index < asof] if not basis.empty else basis
        b_mu, _ = pf._basis_stats(b_seen, asof)
        deliv_hist = pf._delivered_gas(gas[gas.index < asof], b_seen, b_mu)
        seen_d = pf._build_heat_rate_distribution(
            lmp[lmp["month"] < asof], deliv_hist)
        model_d = pf._fit_heat_rate_model(seen_d, wx_seen)
        ref_year = int(seen["year"].max())
        for h in range(1, horizon_months + 1):
            m = asof + pd.offsets.MonthBegin(h)
            if m not in actuals.index or m not in gas.index:
                continue
            sub = seen[seen["cal_month"] == m.month]
            if len(sub) < 2:
                continue
            recency, _, _ = pf._hr_stats(sub["heat_rate"].to_numpy(float),
                                         sub["year"].to_numpy(int), ref_year)
            pooled = float(model.predict([m])[0][0]) if model else float("nan")
            pooled_d = float(model_d.predict([m])[0][0]) if model_d else float("nan")
            g, a = float(gas.loc[m]), float(actuals.loc[m])
            # The delivered variant forecasts a heat rate on delivered gas, so
            # it must be scored against realised *delivered* gas to be a
            # like-for-like price. Realised basis where published, seasonal
            # normal where it still lags.
            rb = float(basis.loc[m]) if (not basis.empty and m in basis.index) \
                else b_mu.get(m.month, 0.0)
            gd = max(g + rb, pf.MIN_DELIVERED_GAS)
            rows.append({"asof": asof, "month": m, "horizon": h, "gas_actual": g,
                         "delivered_actual": gd, "basis_actual": rb,
                         "actual": a, "hr_actual": a / g if g else float("nan"),
                         "hr_recency": recency, "hr_pooled": pooled,
                         "hr_pooled_delivered": pooled_d,
                         "p_recency": recency * g, "p_pooled": pooled * g,
                         "p_pooled_delivered": pooled_d * gd})
    return pd.DataFrame(rows)


def run_gas_backtest(horizon_months: int = 12,
                     eia_api_key: str | None = None) -> pd.DataFrame:
    """Score the *filled* gas curve against realised Henry Hub.

    A reconstructed vintage has no near months — Yahoo delists expired
    contracts — so the months a backtest scores are exactly the months
    `_extend_to_horizon` has to invent. This measures that fill on its own,
    away from the heat rate, which is the only way to tell a gas-side change
    from a power-side one.

    Returns one row per (as-of, month) with the filled and realised price.
    """
    key = eia_api_key if eia_api_key is not None else credentials.get_eia_api_key()
    spot_hist = pf._gas_from_eia(key)
    if spot_hist.empty:
        return pd.DataFrame()
    rows = []
    for asof in _monthly_asofs(gas_strip.vintages()):
        got = gas_strip.strip_asof(asof)
        if got is None:
            continue
        strip = got[0].set_index("month")["gas_price"]
        strip.index = pd.to_datetime(strip.index)
        seen = spot_hist[spot_hist.index < asof]
        if seen.empty:
            continue
        start = asof + pd.offsets.MonthBegin(1)
        curve = pf._extend_to_horizon(
            strip, horizon_months, start,
            spot=(pd.Timestamp(seen.index.max()), float(seen.iloc[-1])))
        for h, m in enumerate(pd.date_range(start, periods=horizon_months,
                                            freq="MS"), start=1):
            if m not in spot_hist.index or m not in curve.index:
                continue
            rows.append({"asof": asof, "month": m, "horizon": h,
                         "gas_fwd": float(curve.loc[m]),
                         "gas_actual": float(spot_hist.loc[m]),
                         "from_strip": m in strip.index})
    out = pd.DataFrame(rows)
    if not out.empty:
        out["err"] = out["gas_fwd"] - out["gas_actual"]
    return out


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
    hr_only = "--hr-only" in sys.argv[1:]
    for a in sys.argv[1:]:
        if a.startswith("--horizon="):
            horizon = int(a.split("=", 1)[1])
    if "--gas-only" in sys.argv[1:]:
        d = run_gas_backtest(horizon_months=horizon)
        if d.empty:
            print("No gas rows — check the vintage archive and the EIA key.")
            sys.exit(1)
        filled = d[~d["from_strip"]]
        print(f"Filled gas curve vs realised Henry Hub, n={len(d):,} "
              f"({len(filled):,} months invented by the fill)")
        for sub, lab in ((d, "all months"), (filled, "filled only")):
            if sub.empty:
                continue
            print("  %-12s bias %+.2f  mae %.2f  mape %.1f%%"
                  % (lab, sub["err"].mean(), sub["err"].abs().mean(),
                     (sub["err"].abs() / sub["gas_actual"]).mean() * 100))
        sys.exit(0)
    if hr_only:
        d = run_hr_backtest(horizon_months=horizon)
        if d.empty:
            print("No heat-rate rows — check the LMP store and vintage archive.")
            sys.exit(1)
        print(f"Heat-rate estimators vs realised gas, n={len(d):,} "
              f"(perfect gas foresight — a diagnostic, not a forecast score)")
        print("  %-10s %8s %8s %8s" % ("estimator", "bias", "mae", "mape"))
        for col, lab in (("p_recency", "recency"), ("p_pooled", "pooled"),
                         ("p_pooled_delivered", "pooled+basis")):
            e = d[col] - d["actual"]
            print("  %-10s %+8.2f %8.2f %7.1f%%"
                  % (lab, e.mean(), e.abs().mean(),
                     (e.abs() / d["actual"]).mean() * 100))
        sys.exit(0)
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
    if honest["n"]:
        print(f"  Real-strip only    : bias {honest['bias']:+.2f}  "
              f"mae {honest['mae']:.2f}  mape {honest['mape_pct']:.1f}%  "
              f"coverage {honest['coverage_pct']:.1f}%  n={honest['n']}")
    else:
        print("  Real-strip only    : no scoreable rows yet — every forecast "
              "month whose own gas contract was in the vintage is still open.")
    print(f"  {len(bt) - honest['n']:,} rows used mean-reversion-extrapolated gas.")
