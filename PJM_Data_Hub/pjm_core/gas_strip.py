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


if __name__ == "__main__":
    import sys

    force = "--force" in sys.argv[1:]
    if not force and refreshed_today():
        print("Gas strip: already refreshed today; skipping "
              "(use --force to override).")
    else:
        update()
