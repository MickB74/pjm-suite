"""PJM DOM Hub forward price forecast: implied heat rate × gas strip + Monte Carlo.

Method (mirrors the ERCOT approach):
  1. Historical PJM DOM Hub hourly LMPs from the data lake.
  2. Henry Hub gas prices from EIA (monthly spot or STEO).
  3. Implied heat rate = LMP / gas price (MMBtu/MWh).
  4. Distribution pooled per calendar month across years → median P50 anchor.
  5. Monte Carlo: lognormal gas × lognormal heat rate → power price.
  6. Output: month / P10 / P50 / P90.

Price cap: $2,000/MWh (PJM market-wide offer cap).
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from pjm_core import paths
from pjm_core.settlement_points import PRIMARY_HUB

# Monte Carlo defaults
DEFAULT_N_SIMS = 5_000
DEFAULT_SEED = 42
GAS_LOG_VOL = 0.5          # annualised gas log-volatility, applied as vol·√t
HR_LOG_SIG_FLOOR = 0.10    # minimum heat-rate log-σ
LONG_RUN_GAS = 4.00        # $/MMBtu long-run mean reversion anchor
REVERSION_MONTHS = 24      # e-folding time for gas mean reversion (months)
PJM_PRICE_CAP = 2_000.0    # $/MWh


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


def _extend_to_horizon(
    partial: pd.Series,
    horizon_months: int,
    asof: pd.Timestamp,
) -> pd.Series:
    """Extend a partial monthly curve (STEO forecast, Yahoo strip, …) across the
    full horizon: use the source's values where available, then mean-revert to
    $4 for any months beyond the source's last month."""
    months = pd.date_range(asof, periods=horizon_months + 2, freq="MS")
    last_month = partial.index.max()
    last_val = float(partial.loc[last_month])
    alpha = np.exp(-1 / REVERSION_MONTHS)
    out = {}
    for m in months:
        if m in partial.index:
            out[m] = float(partial.loc[m])
        else:
            k = max((m.year - last_month.year) * 12 + (m.month - last_month.month), 0)
            out[m] = LONG_RUN_GAS + (last_val - LONG_RUN_GAS) * (alpha ** k)
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
        import json
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
) -> tuple[pd.Series, str]:
    """Gas forward curve and the source label describing where it came from.

    Priority: explicit override → manual override CSV → cached NYMEX strip
    (refreshed at launch) → live NYMEX strip (Yahoo) → EIA STEO forecast →
    EIA spot + mean-reversion → flat $4 anchor. Returns (series, source_label).

    `gas_asof` pins the curve to an archived strip vintage (the strip as it
    stood on that date) instead of the live chain — used to re-run forecasts
    from a past date. Raises ValueError if no vintage that old exists, rather
    than silently substituting today's strip."""
    # 0. Archived vintage requested — bypass the live chain entirely.
    if gas_asof is not None:
        from pjm_core import gas_strip  # local import; gas_strip imports us
        got = gas_strip.strip_asof(gas_asof)
        if got is None:
            raise ValueError(
                f"No archived gas-strip vintage on or before "
                f"{pd.Timestamp(gas_asof).date()} — vintages accumulate daily "
                "as the strip is pulled, so older dates can't be backtested.")
        snap, vintage = got
        strip = snap.set_index("month")["gas_price"]
        return (_extend_to_horizon(strip, horizon_months, asof),
                f"NYMEX strip (vintage {vintage:%Y-%m-%d})")

    # 1. Explicit override passed by the caller (used in tests).
    if csv_override and csv_override.exists():
        df = pd.read_csv(csv_override, parse_dates=["month"])
        return df.set_index("month")["gas_price"], "manual CSV override"

    # 2. Truly-manual override file on disk — wins over everything auto.
    if paths.GAS_OVERRIDE_CSV.exists():
        df = pd.read_csv(paths.GAS_OVERRIDE_CSV, parse_dates=["month"])
        return df.set_index("month")["gas_price"], "manual CSV override"

    # 3. Cached NYMEX strip written at launch by gas_strip.update() — this is the
    #    real traded strip pulled from a residential IP, so it works even when
    #    the app itself (e.g. a cloud host) can't reach Yahoo directly.
    if paths.GAS_STRIP_CSV.exists():
        df = pd.read_csv(paths.GAS_STRIP_CSV, parse_dates=["month"])
        strip = df.set_index("month")["gas_price"]
        if len(strip) >= 3:
            asof_label = _gas_strip_asof()
            return (_extend_to_horizon(strip, horizon_months, asof),
                    f"NYMEX strip (cached at launch{asof_label})")

    # 4. Best-effort live pull: the real traded NYMEX strip via Yahoo. Unofficial
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
        return pd.DataFrame(columns=["cal_month", "year", "heat_rate"])

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
    return df[["cal_month", "year", "heat_rate"]]


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
) -> pd.DataFrame:
    """Run the Monte Carlo price forecast.

    Returns a DataFrame with columns:
        month (Timestamp, first-of-month), cal_month (int 1–12),
        p10, p25, p50, p75, p90 ($/MWh), n_samples (heat-rate sample count),
        gas_fwd ($/MMBtu used for P50 anchor).

    Pass `gas_asof` (with a matching `asof`) to re-run the forecast from a
    past date using the archived gas-strip vintage from that date.
    """
    asof = pd.Timestamp(asof) if asof else pd.Timestamp.now().normalize()
    rng = np.random.default_rng(seed)

    gas_history = _gas_from_eia(eia_api_key)
    gas_fwd, gas_source = _gas_curve(eia_api_key, horizon_months, asof,
                                     gas_asof=gas_asof)
    monthly_lmp = _load_dom_hub_monthly(hub)
    hr_dist = _build_heat_rate_distribution(monthly_lmp, gas_history)

    months = pd.date_range(asof + pd.offsets.MonthBegin(1), periods=horizon_months, freq="MS")
    rows = []
    for i, month in enumerate(months):
        cal_m = month.month
        t_months = i + 1  # forward horizon in months

        # Gas forward for this month
        gas_f = float(gas_fwd.get(month, LONG_RUN_GAS))
        gas_vol = GAS_LOG_VOL * np.sqrt(t_months / 12)

        # Heat rate samples for this calendar month
        hr_samples = hr_dist[hr_dist["cal_month"] == cal_m]["heat_rate"].values
        n_hr = len(hr_samples)
        if n_hr < 2:
            hr_med = 8.0
            hr_log_sig = 0.20
        else:
            hr_med = float(np.median(hr_samples))
            hr_log_sig = max(float(np.std(np.log(hr_samples[hr_samples > 0]))), HR_LOG_SIG_FLOOR)

        # Simulate
        gas_sim = gas_f * np.exp(rng.normal(-0.5 * gas_vol**2, gas_vol, n_sims))
        hr_sim = hr_med * np.exp(rng.normal(-0.5 * hr_log_sig**2, hr_log_sig, n_sims))
        price_sim = np.minimum(gas_sim * hr_sim, PJM_PRICE_CAP)

        rows.append({
            "month": month,
            "cal_month": cal_m,
            "p10": float(np.percentile(price_sim, 10)),
            "p25": float(np.percentile(price_sim, 25)),
            "p50": float(np.percentile(price_sim, 50)),
            "p75": float(np.percentile(price_sim, 75)),
            "p90": float(np.percentile(price_sim, 90)),
            "n_samples": n_hr,
            "gas_fwd": gas_f,
        })

    out = pd.DataFrame(rows)
    out["gas_source"] = gas_source
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
) -> pd.DataFrame:
    """Return cached forecast or run fresh. Columns: month, p10, p50, p90."""
    asof = pd.Timestamp(asof) if asof else pd.Timestamp.now().normalize()
    cached = load_cached(hub, asof)
    if cached is not None:
        return cached
    df = run(hub=hub, asof=asof, horizon_months=horizon_months,
             n_sims=n_sims, eia_api_key=eia_api_key)
    try:
        save_cached(df, hub, asof)
    except Exception:
        pass
    return df
