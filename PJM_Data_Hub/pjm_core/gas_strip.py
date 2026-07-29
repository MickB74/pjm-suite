"""Refresh the NYMEX Henry Hub gas strip from Yahoo Finance and cache it to CSV.

Yahoo blocks datacenter IPs, so this only reliably works from a residential IP
(i.e. your own machine). Run it at launch (see the launcher .command) so the app
reads a freshly-pulled real strip from the CSV instead of calling Yahoo per
request. Deployed on a cloud host it will simply be skipped and the app falls
back to EIA STEO.

The write is best-effort and never raises: if Yahoo returns too few contract
months, the existing CSV (if any) is left untouched and the app falls back on
its own (Yahoo → STEO → mean-reversion) chain.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from pjm_core import paths
from pjm_core.price_forecast import _gas_from_yahoo

MIN_MONTHS = 3        # need at least this many contracts to trust the pull
HORIZON_MONTHS = 24   # deferred NG contracts get too thin past ~2 years


def load() -> pd.DataFrame | None:
    """The cached strip as a DataFrame [month, gas_price], or None if absent."""
    if not paths.GAS_STRIP_CSV.exists():
        return None
    return pd.read_csv(paths.GAS_STRIP_CSV, parse_dates=["month"])


# ── Vintage history ──────────────────────────────────────────────────────────
# Every successful pull is also appended to a parquet of dated snapshots
# (asof, month, gas_price) so a forecast can be re-run against the strip
# exactly as it stood on a past date. One vintage per day; a same-day
# re-pull replaces that day's snapshot.

def history() -> pd.DataFrame | None:
    """All strip vintages as [asof, month, gas_price], or None if absent."""
    if not paths.GAS_STRIP_HISTORY_PARQUET.exists():
        return None
    return pd.read_parquet(paths.GAS_STRIP_HISTORY_PARQUET)


def vintages() -> list[pd.Timestamp]:
    """Sorted (oldest first) asof dates with an archived strip snapshot."""
    hist = history()
    if hist is None or hist.empty:
        return []
    return sorted(pd.to_datetime(hist["asof"]).unique())


def strip_asof(when) -> tuple[pd.DataFrame, pd.Timestamp] | None:
    """The most recent archived strip on or before `when`.

    Returns ([month, gas_price], vintage_date), or None if no vintage that
    old exists — callers should treat that as "can't backtest this date",
    not silently substitute today's strip.
    """
    hist = history()
    if hist is None or hist.empty:
        return None
    when = pd.Timestamp(when).normalize()
    asofs = pd.to_datetime(hist["asof"])
    eligible = asofs[asofs <= when]
    if eligible.empty:
        return None
    vintage = eligible.max()
    snap = (hist[asofs == vintage][["month", "gas_price"]]
            .assign(month=lambda d: pd.to_datetime(d["month"]))
            .sort_values("month").reset_index(drop=True))
    return snap, vintage


# ── Median anchor ────────────────────────────────────────────────────────────
# The strip source is an unofficial Yahoo feed and deferred NG contracts are
# illiquid enough to throw the occasional bad tick. Taking the per-contract
# median of the last few vintages rejects those outliers at almost no cost in
# lag — a forward is near-martingale, so a *long* trailing average would just
# lag genuine moves (Dec-25 $4.26 → Jan-26 $7.72). Hence a short window, and a
# max-age guard so a stale curve is never blended with a current one.

ANCHOR_VINTAGES = 5     # contract-month median taken over this many vintages
ANCHOR_MAX_AGE_DAYS = 14  # ignore vintages older than this vs. the newest used


def strip_median(
    when=None,
    n: int = ANCHOR_VINTAGES,
    max_age_days: int = ANCHOR_MAX_AGE_DAYS,
) -> tuple[pd.DataFrame, pd.Timestamp, int] | None:
    """Per-contract-month median of the last `n` vintages on or before `when`.

    Returns ([month, gas_price], newest_vintage_used, n_vintages_used), or
    None if there is no history at all / nothing on or before `when`. With a
    single eligible vintage this degrades to exactly `strip_asof`.

    Vintages more than `max_age_days` older than the newest eligible one are
    dropped, so a gap in the daily pull can't blend a months-old curve into
    today's anchor.
    """
    hist = history()
    if hist is None or hist.empty:
        return None
    asofs = pd.to_datetime(hist["asof"])
    eligible = asofs[asofs <= pd.Timestamp(when).normalize()] if when is not None else asofs
    if eligible.empty:
        return None
    newest = eligible.max()
    cutoff = newest - pd.Timedelta(float(max_age_days), unit="D")
    keep = sorted(eligible[eligible >= cutoff].unique())[-n:]
    snap = hist[asofs.isin(keep)].copy()
    snap["month"] = pd.to_datetime(snap["month"])
    med = (snap.groupby("month", as_index=False)["gas_price"].median()
           .sort_values("month").reset_index(drop=True))
    return med, newest, len(keep)


# ── Forward volatility from the archive ──────────────────────────────────────

MIN_VINTAGES_FOR_VOL = 30   # below this the per-contract vol estimate is noise
TRADING_DAYS = 252


def forward_vol(
    when=None,
    min_vintages: int = MIN_VINTAGES_FOR_VOL,
) -> dict[pd.Timestamp, float] | None:
    """Annualised log-return volatility of each contract month's forward price,
    measured directly from the vintage archive.

    This is the number the Monte Carlo actually wants: how much the *forward*
    for a given delivery month moves, which already embeds both the seasonal
    shape (winter contracts move more) and the Samuelson effect (near-dated
    contracts move more than deferred ones). Returns None until enough
    vintages have accumulated, in which case callers should fall back to the
    seasonal/OU model calibrated off spot history.

    Returned vols are *instantaneous* — the terminal dispersion of month m seen
    from now is roughly ``vol[m] * sqrt(years_to_delivery)``.
    """
    hist = history()
    if hist is None or hist.empty:
        return None
    asofs = pd.to_datetime(hist["asof"])
    hist = hist[asofs <= pd.Timestamp(when).normalize()] if when is not None else hist
    if hist.empty or hist["asof"].nunique() < min_vintages:
        return None
    wide = (hist.assign(asof=pd.to_datetime(hist["asof"]),
                        month=pd.to_datetime(hist["month"]))
            .pivot_table(index="asof", columns="month", values="gas_price")
            .sort_index())
    # Pulls can be missed, so vintages aren't necessarily consecutive days.
    # Normalise each return to a 1-day move before taking the std, otherwise a
    # gap in the archive reads as a volatility spike.
    gap_days = wide.index.to_series().diff().dt.days.clip(lower=1)
    rets = np.log(wide).diff().div(np.sqrt(gap_days), axis=0)
    out = {}
    for month in wide.columns:
        r = rets[month].dropna()
        if len(r) >= min_vintages - 1:
            sd = float(r.std())
            if sd > 0:
                out[pd.Timestamp(month)] = sd * (TRADING_DAYS ** 0.5)
    return out or None


def _append_history(df: pd.DataFrame, asof: pd.Timestamp, log=print) -> None:
    """Archive this pull as a vintage. Best-effort — never raises."""
    try:
        snap = df[["month", "gas_price"]].assign(asof=asof.normalize())
        snap = snap[["asof", "month", "gas_price"]]
        hist = history()
        if hist is not None:
            hist = hist[pd.to_datetime(hist["asof"]) != asof.normalize()]
            snap = pd.concat([hist, snap], ignore_index=True)
        snap = snap.sort_values(["asof", "month"]).reset_index(drop=True)
        paths.GAS_DIR.mkdir(parents=True, exist_ok=True)
        snap.to_parquet(paths.GAS_STRIP_HISTORY_PARQUET, index=False)
    except Exception as e:
        log(f"Gas strip: history append failed ({e}); latest CSV still written.")


def refreshed_today() -> bool:
    """True if the cached strip was already pulled today (per its state marker).

    Used to skip the launch-time Yahoo refresh when it's already run once today —
    the strip only moves on the daily NYMEX settle, so once a day is plenty.
    """
    try:
        state = json.loads(paths.GAS_STRIP_STATE.read_text())
        return state.get("asof") == pd.Timestamp.now().strftime("%Y-%m-%d")
    except Exception:
        return False


def update(log=print) -> pd.DataFrame | None:
    """Pull the Yahoo NYMEX strip and write it to GAS_STRIP_CSV.

    Returns the written DataFrame, or None if the pull was too thin (in which
    case any existing CSV is left in place). Never raises.
    """
    asof = pd.Timestamp.now().normalize()
    try:
        strip = _gas_from_yahoo(HORIZON_MONTHS, asof)
    except Exception as e:  # defensive — _gas_from_yahoo already swallows, but be safe
        log(f"Gas strip: Yahoo fetch failed ({e}); leaving cache untouched.")
        return None

    if strip is None or len(strip) < MIN_MONTHS:
        n = 0 if strip is None else len(strip)
        log(f"Gas strip: only {n} contract month(s) from Yahoo "
            f"(need {MIN_MONTHS}); leaving cache untouched, app will use STEO.")
        return None

    df = (strip.rename("gas_price").rename_axis("month").reset_index()
          .sort_values("month"))
    paths.GAS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(paths.GAS_STRIP_CSV, index=False)
    _append_history(df, asof, log=log)
    paths.GAS_STRIP_STATE.write_text(json.dumps({
        "asof": asof.strftime("%Y-%m-%d"),
        "last_success": pd.Timestamp.now(tz="UTC").isoformat(),
        "months": len(df),
        "first_month": df["month"].min().strftime("%Y-%m"),
        "last_month": df["month"].max().strftime("%Y-%m"),
    }, indent=2))
    log(f"Gas strip: wrote {len(df)} months "
        f"({df['month'].min():%Y-%m} → {df['month'].max():%Y-%m}) to {paths.GAS_STRIP_CSV}")
    return df


# ── Historical backfill from per-contract Yahoo history ─────────────────────
# Yahoo's snapshot endpoint (used by _gas_from_yahoo) only serves the strip as
# it stands right now. But each individual NYMEX NG contract ticker carries its
# own daily settle history, so downloading each contract separately and pivoting
# on trade date reconstructs a strip snapshot for every past trading day. That
# is what forward_vol() actually wants — it needs ~30 daily observations per
# delivery month before the estimator is stable, and one daily pull takes a
# month to accumulate that. A backfill lands the same data in a single run.

BACKFILL_HORIZON_MONTHS = 30    # how far past each snapshot date to include
BACKFILL_MAX_JUMP_FRAC = 0.5    # drop a single print > this vs. the prior kept


_NYMEX_MONTH_CODE = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
                     7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}


def _contract_ticker(m: pd.Timestamp) -> str:
    """NYMEX Henry Hub future ticker for delivery month m (e.g. Jan-2027 → NGF27.NYM)."""
    return f"NG{_NYMEX_MONTH_CODE[m.month]}{str(m.year)[2:]}.NYM"


def _reject_bad_ticks(series: pd.Series, max_jump: float) -> pd.Series:
    """Drop any print more than max_jump away (fractional) from the last kept
    value in the same series. Yahoo's deferred contracts throw the occasional
    outlier that would otherwise pump forward_vol()'s std estimate.
    """
    s = series.dropna().sort_index()
    if s.empty:
        return s
    keep = [True]
    last = float(s.iloc[0])
    for v in s.iloc[1:]:
        v = float(v)
        if last > 0 and abs(v / last - 1.0) > max_jump:
            keep.append(False)
        else:
            keep.append(True)
            last = v
    return s[keep]


def backfill_from_yahoo(
    days_back: int = 365,
    horizon_months: int = BACKFILL_HORIZON_MONTHS,
    min_contracts: int = MIN_MONTHS,
    max_jump_frac: float = BACKFILL_MAX_JUMP_FRAC,
    log=print,
) -> int:
    """Reconstruct daily strip vintages from per-contract Yahoo history and
    merge them into GAS_STRIP_HISTORY_PARQUET.

    Existing vintages (including today's live pull) are preserved — dedup on
    (asof, month) keeps the newer row on a same-day collision so the daily
    pipeline stays authoritative for its own dates.

    Returns the number of newly-added vintage days.
    """
    try:
        import logging
        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    except Exception as e:
        log(f"Gas backfill: yfinance unavailable ({e}).")
        return 0

    today = pd.Timestamp.now().normalize()
    window_start = today - pd.Timedelta(float(days_back), unit="D")
    # Every delivery month that could have been in-strip at any point in the
    # window: from the window-start front-month through today's deferred tail.
    first_delivery = window_start.to_period("M").to_timestamp()
    last_delivery = (today + pd.offsets.MonthBegin(horizon_months)).to_period("M").to_timestamp()
    months = pd.date_range(first_delivery, last_delivery, freq="MS")
    log(f"Gas backfill: pulling {len(months)} contracts "
        f"({months[0]:%b-%Y} → {months[-1]:%b-%Y}), {days_back}d window.")

    # per_contract[month] -> Series indexed by trade date, cleaned of bad ticks.
    per_contract: dict[pd.Timestamp, pd.Series] = {}
    for i, m in enumerate(months, 1):
        tk = _contract_ticker(m)
        try:
            h = yf.download(tk, period=f"{days_back + 60}d", progress=False,
                            threads=False, timeout=15, auto_adjust=False)
        except Exception as e:
            log(f"  [{i:2d}/{len(months)}] {tk}: fetch failed ({e})")
            continue
        if h is None or len(h) == 0 or "Close" not in getattr(h, "columns", []):
            log(f"  [{i:2d}/{len(months)}] {tk}: no data")
            continue
        close = h["Close"]
        if hasattr(close, "columns"):        # MultiIndex when tickers is a list
            close = close.iloc[:, 0]
        cleaned = _reject_bad_ticks(close, max_jump_frac)
        cleaned = cleaned[(cleaned.index >= window_start) & (cleaned.index <= today)]
        if len(cleaned):
            per_contract[m] = cleaned
            log(f"  [{i:2d}/{len(months)}] {tk}: {len(cleaned):>3} settles "
                f"({cleaned.index.min().date()} → {cleaned.index.max().date()})")

    if not per_contract:
        log("Gas backfill: no data pulled.")
        return 0

    # Pivot: for each trade date, the strip = every contract that printed then
    # and whose delivery month is still in the future on that date.
    wide = pd.DataFrame(per_contract).sort_index()
    wide.index = pd.to_datetime(wide.index).normalize()
    rows = []
    for trade_date, row in wide.iterrows():
        strip = row.dropna()
        # Drop contracts already delivered (a contract's own history keeps
        # printing past first-notice day, but that price is not a "forward").
        strip = strip[[m for m in strip.index if m >= trade_date.to_period("M").to_timestamp()]]
        if len(strip) < min_contracts:
            continue
        rows.append(pd.DataFrame({
            "asof": trade_date,
            "month": strip.index,
            "gas_price": strip.values,
        }))
    if not rows:
        log("Gas backfill: no vintages met the min-contracts threshold.")
        return 0
    new_snap = pd.concat(rows, ignore_index=True)

    existing = history()
    if existing is None or existing.empty:
        combined = new_snap
    else:
        existing = existing.assign(asof=pd.to_datetime(existing["asof"]),
                                   month=pd.to_datetime(existing["month"]))
        # Existing rows win on same-day collision — the live daily pull is the
        # canonical source for its own dates; reconstructed data is fallback.
        combined = pd.concat([new_snap, existing], ignore_index=True)
        combined = combined.drop_duplicates(subset=["asof", "month"], keep="last")

    combined = combined.sort_values(["asof", "month"]).reset_index(drop=True)
    paths.GAS_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(paths.GAS_STRIP_HISTORY_PARQUET, index=False)

    added = combined["asof"].nunique() - (existing["asof"].nunique()
                                          if existing is not None and not existing.empty else 0)
    log(f"Gas backfill: {len(new_snap):,} rows across "
        f"{new_snap['asof'].nunique()} vintages fetched; "
        f"archive now holds {combined['asof'].nunique()} distinct vintages "
        f"({added:+d}).")
    return int(added)


if __name__ == "__main__":
    import sys

    if "backfill" in sys.argv[1:]:
        days = 365
        for a in sys.argv[1:]:
            if a.startswith("--days="):
                days = int(a.split("=", 1)[1])
        backfill_from_yahoo(days_back=days)
        sys.exit(0)

    force = "--force" in sys.argv[1:]
    if not force and refreshed_today():
        print("Gas strip: already refreshed today; skipping "
              "(use --force to override).")
    else:
        update()
