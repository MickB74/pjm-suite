#!/usr/bin/env python3
"""PJM zone LMP downloader → hourly rows plus a monthly average per zone × market.

Fetches hourly zone LMPs from PJM Data Miner 2 and writes two stores:

    data/zone_prices/pjm_zone_lmp_hourly.parquet    (hour × zone × market)
    data/zone_prices/pjm_zone_lmp_monthly.parquet   (zone × month × market)
    data/zone_prices/pjm_zone_lmp_monthly.csv

The monthly mean values EIA-923 *monthly* plant generation at a *locational*
price — one row per (zone, year, month, market) with avg total_lmp / energy /
congestion / loss and the hour count backing the average. The hourly store
backs anything that needs a zone's price at a *specific hour*, such as the
coincident-peak view: most PJM load zones have no namesake trading hub, so
there is no hub price that can stand in for them.

Incremental: past months are kept; the current and previous month are always
refreshed (they are still settling), and any missing months back to
``backfill_start`` are filled. A month is re-fetched when *either* store is
missing it, so adding the hourly store triggers its own backfill without
disturbing the monthly one.

Shares the PJM subscription key and the chunked / paginated / 429-retry request
pattern with datasets/hub_prices/pjm_api.py — kept parallel on purpose.

Usage:
    python pjm_zone_prices.py update
    python pjm_zone_prices.py status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pjm_core import credentials, paths, tz
from pjm_core.settlement_points import ZONE_PNODE_IDS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PJM_API_BASE = "https://api.pjm.com/api/v1"
_RT_ENDPOINT = f"{_PJM_API_BASE}/rt_hrl_lmps"
_DA_ENDPOINT = f"{_PJM_API_BASE}/da_hrl_lmps"

_PNODE_ID_STR = ";".join(str(v) for v in ZONE_PNODE_IDS.values())
_ID_TO_ZONE = {v: k for k, v in ZONE_PNODE_IDS.items()}

_EASTERN = ZoneInfo("America/New_York")
DEFAULT_BACKFILL_START = date(datetime.now(tz=_EASTERN).year - 1, 1, 1).isoformat()

# Always refresh this many trailing months (they are still settling / revised).
REFRESH_TRAILING_MONTHS = 2

VALUE_COLS = ["total_lmp", "energy", "congestion", "loss"]
KEY_COLS = ["zone", "year", "month", "market"]


class _SpansArchiveError(RuntimeError):
    """Query date range straddles PJM's rolling archived↔live cutoff."""


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------

def _make_logger(callback=None):
    def log(msg: str):
        line = str(msg)
        print(line, flush=True)
        if callback is not None:
            try:
                callback(line)
            except Exception:
                pass
    return log


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _api_key(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else credentials.load_config()
    key = cfg.get("subscription_key") or os.environ.get("PJM_API_KEY", "")
    if not key:
        raise RuntimeError(
            "Missing PJM API subscription key. Set it on the API Keys page or "
            "in config.json / the PJM_API_KEY environment variable.")
    return key


def _fetch_hourly(api_key: str, start: date, end: date,
                  market: str, log=print) -> pd.DataFrame:
    """Hourly zone LMPs for [start, end] inclusive, one market (RT or DA)."""
    endpoint = _RT_ENDPOINT if market == "RT" else _DA_ENDPOINT
    lmp_col = "total_lmp_rt" if market == "RT" else "total_lmp_da"
    energy_col = "system_energy_price_rt" if market == "RT" else "system_energy_price_da"
    cong_col = "congestion_price_rt" if market == "RT" else "congestion_price_da"
    loss_col = "marginal_loss_price_rt" if market == "RT" else "marginal_loss_price_da"

    headers = {"Ocp-Apim-Subscription-Key": api_key}

    # Filter by type=ZONE (not pnode_id): accepted for BOTH the live store and
    # PJM's "archived" store (>~24 months old), which rejects pnode_id/pnode_name
    # filtering. Returns all ~46 ZONE pnodes; we keep our 20 client-side via
    # _ID_TO_ZONE. Archive rules: a range must stay within ONE calendar month,
    # and must not straddle the rolling archived↔live cutoff (which sits mid-
    # month for ~24-months-ago). We build a same-month range and, if it straddles
    # the cutoff, fall back to day-by-day (only the single cutoff day is lost).
    def _paginate(dt_range: str) -> list:
        rows, start_row, row_count = [], 1, 50000
        while True:
            p = {"type": "ZONE", "row_is_current": "TRUE",
                 "datetime_beginning_ept": dt_range,
                 "startRow": start_row, "rowCount": row_count}
            for attempt in range(5):
                resp = requests.get(endpoint, params=p, headers=headers, timeout=60)
                if resp.status_code == 429:
                    wait = 10.0 * (2 ** attempt)
                    log(f"      rate-limited, waiting {wait:.0f}s …")
                    time.sleep(wait)
                    continue
                if resp.status_code == 400 and "spans over archived" in resp.text.lower():
                    raise _SpansArchiveError(dt_range)
                resp.raise_for_status()
                break
            else:
                log("      rate-limited after 5 retries, skipping page")
                break
            data = resp.json()
            items = data if isinstance(data, list) else data.get("items", [])
            rows.extend(items)
            if len(items) < row_count:
                break
            start_row += row_count
            time.sleep(1.0)
        return rows

    def _range(a: date, b: date) -> str:
        # Inclusive [a, b] within one month; end at the last hour of day b.
        return f"{a.strftime('%m/%d/%Y')} 00:00to{b.strftime('%m/%d/%Y')} 23:00"

    try:
        all_rows = _paginate(_range(start, end))
    except _SpansArchiveError:
        # This month crosses the archived↔live cutoff — fetch it day by day and
        # drop only the exact cutoff day (whose own range still straddles).
        log(f"      straddles archive cutoff — fetching {start:%b %Y} daily …")
        all_rows, d = [], start
        while d <= end:
            try:
                all_rows.extend(_paginate(_range(d, d)))
            except _SpansArchiveError:
                log(f"        {d} is the cutoff day — skipped.")
            d += timedelta(days=1)
            time.sleep(0.5)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["pnode_id"] = pd.to_numeric(df.get("pnode_id", pd.Series(dtype=int)), errors="coerce")
    df["zone"] = df["pnode_id"].map(_ID_TO_ZONE)
    df = df.rename(columns={
        lmp_col: "total_lmp", energy_col: "energy",
        cong_col: "congestion", loss_col: "loss",
    })
    df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"], errors="coerce")
    for col in VALUE_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["market"] = market
    keep = ["datetime_beginning_ept", "zone", "market", *VALUE_COLS]
    return df[[c for c in keep if c in df.columns]].dropna(subset=["datetime_beginning_ept", "zone"])


def _to_monthly(hourly: pd.DataFrame) -> pd.DataFrame:
    """Collapse hourly zone LMPs to one row per zone × month × market."""
    if hourly.empty:
        return pd.DataFrame(columns=[*KEY_COLS, *[f"{c}" for c in VALUE_COLS], "hours"])
    h = hourly.copy()
    h["year"] = h["datetime_beginning_ept"].dt.year
    h["month"] = h["datetime_beginning_ept"].dt.month
    agg = {c: "mean" for c in VALUE_COLS if c in h.columns}
    monthly = h.groupby(KEY_COLS, as_index=False).agg({**agg})
    hours = (h.groupby(KEY_COLS, as_index=False)
             .size().rename(columns={"size": "hours"}))
    return monthly.merge(hours, on=KEY_COLS, how="left")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def load_store() -> pd.DataFrame:
    p = paths.ZONE_PRICES_PARQUET
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def save_store(df: pd.DataFrame) -> None:
    paths.ZONE_PRICES_DIR.mkdir(parents=True, exist_ok=True)
    df = df.sort_values(["market", "year", "month", "zone"]).reset_index(drop=True)
    df.to_parquet(paths.ZONE_PRICES_PARQUET, index=False)
    df.to_csv(paths.ZONE_PRICES_CSV, index=False)


# ── Hourly store ─────────────────────────────────────────────────────────────
# The monthly file is an average; anything that asks "what did this zone cost at
# the peak hour" needs the hourly rows the fetch already returns. Kept as a
# separate parquet (no CSV — millions of rows) so the monthly store's shape and
# consumers are untouched.

HOURLY_KEY_COLS = ["datetime_beginning_ept", "zone", "market"]


def load_hourly(market: str | None = "RT", zones: list[str] | None = None) -> pd.DataFrame:
    """Hourly zone LMPs, optionally filtered to one market and/or a zone list.

    Columns: datetime_beginning_ept, zone, market, total_lmp, energy,
    congestion, loss. Empty DataFrame when the store does not exist yet.
    """
    p = paths.ZONE_PRICES_HOURLY_PARQUET
    if not p.exists():
        return pd.DataFrame()
    filters = []
    if market:
        filters.append(("market", "==", market))
    if zones:
        filters.append(("zone", "in", list(zones)))
    df = pd.read_parquet(p, filters=filters or None)
    if not df.empty:
        df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
    return df.reset_index(drop=True)


def save_hourly(df: pd.DataFrame) -> None:
    paths.ZONE_PRICES_DIR.mkdir(parents=True, exist_ok=True)
    # Sorted market → zone → time, not time-first: the dominant read is "one
    # zone's whole history" (load_hourly), and clustering a zone's rows into
    # contiguous row groups lets the parquet filter skip most of the file.
    df = (df.drop_duplicates(subset=HOURLY_KEY_COLS, keep="last")
          .sort_values(["market", "zone", "datetime_beginning_ept"])
          .reset_index(drop=True))
    df.to_parquet(paths.ZONE_PRICES_HOURLY_PARQUET, index=False)


HOURLY_COMPLETE_FRAC = 0.90


def _hourly_months() -> set[tuple[int, int, str]]:
    """(year, month, market) tuples the hourly store already holds *in full*.

    Completeness is judged on distinct hours against the calendar month, not on
    mere presence: a month left half-fetched by an interrupted backfill would
    otherwise be treated as done and never filled in. The threshold is below
    100% because the archived↔live cutoff month legitimately loses one day.
    """
    p = paths.ZONE_PRICES_HOURLY_PARQUET
    if not p.exists():
        return set()
    try:
        df = pd.read_parquet(p, columns=["datetime_beginning_ept", "market"])
    except Exception:
        return set()
    if df.empty:
        return set()
    dt = pd.to_datetime(df["datetime_beginning_ept"])
    counted = (pd.DataFrame({"year": dt.dt.year, "month": dt.dt.month,
                             "market": df["market"], "hour": dt})
               .groupby(["year", "month", "market"])["hour"].nunique())
    out = set()
    for (yr, mo, mkt), n_hours in counted.items():
        expected = pd.Period(f"{yr}-{mo:02d}").days_in_month * 24
        if n_hours >= HOURLY_COMPLETE_FRAC * expected:
            out.add((int(yr), int(mo), mkt))
    return out


def read_state() -> dict:
    p = paths.ZONE_PRICES_STATE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def write_state(state: dict) -> None:
    paths.ZONE_PRICES_DIR.mkdir(parents=True, exist_ok=True)
    paths.ZONE_PRICES_STATE.write_text(json.dumps(state, indent=2, default=str))


def _months_between(start: date, end: date) -> list[tuple[int, int]]:
    """Inclusive list of (year, month) from start to end month."""
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _month_bounds(year: int, month: int, today: date) -> tuple[date, date]:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    last = min(nxt - timedelta(days=1), today)
    return first, last


def days_since_update() -> float | None:
    ts = read_state().get("last_success")
    if not ts:
        return None
    try:
        last = pd.Timestamp(ts)
    except ValueError:
        return None
    now = tz.now_eastern()
    if last.tz is None:
        last = last.tz_localize(tz.EASTERN, ambiguous=True, nonexistent="shift_forward")
    return (now - last).total_seconds() / 86400.0


def store_summary() -> dict:
    info = {"exists": paths.ZONE_PRICES_PARQUET.exists(), "rows": 0,
            "start": None, "end": None, "zones": [], "markets": [],
            "days_since_update": days_since_update()}
    if not info["exists"]:
        return info
    df = load_store()
    if df.empty:
        return info
    info["rows"] = len(df)
    stamp = pd.to_datetime(dict(year=df["year"], month=df["month"], day=1))
    info["start"] = str(stamp.min().date())
    info["end"] = str(stamp.max().date())
    info["zones"] = sorted(df["zone"].unique().tolist())
    info["markets"] = sorted(df["market"].unique().tolist())

    info["hourly_exists"] = paths.ZONE_PRICES_HOURLY_PARQUET.exists()
    info["hourly_rows"] = 0
    if info["hourly_exists"]:
        try:
            hh = pd.read_parquet(paths.ZONE_PRICES_HOURLY_PARQUET,
                                 columns=["datetime_beginning_ept"])
            info["hourly_rows"] = len(hh)
            if len(hh):
                dt = pd.to_datetime(hh["datetime_beginning_ept"])
                info["hourly_start"] = str(dt.min())
                info["hourly_end"] = str(dt.max())
        except Exception:
            pass
    return info


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def update(progress_callback=None, markets: tuple[str, ...] = ("RT", "DA")) -> dict:
    """Fetch missing / trailing months of zone LMPs and merge into the store."""
    log = _make_logger(progress_callback)
    cfg = credentials.load_config()
    key = _api_key(cfg)

    existing = load_store()
    today = tz.now_eastern().date()
    start = date.fromisoformat(cfg.get("backfill_start", DEFAULT_BACKFILL_START))
    if start > today:
        start = today

    all_months = _months_between(start, today)
    # Always refresh the trailing months; only backfill earlier gaps once.
    trailing = set(all_months[-REFRESH_TRAILING_MONTHS:])
    have = set()
    if not existing.empty:
        have = set(map(tuple, existing[["year", "month"]].drop_duplicates().to_numpy().tolist()))

    have_hourly = _hourly_months()
    hourly_existing = load_hourly(market=None)

    frames = []
    hourly_frames = []
    hourly_dirty = False

    def _flush_hourly() -> None:
        """Merge whatever hourly months we have so far into the store.

        Flushed periodically rather than once at the end: the first full
        backfill is ~2M rows over 150+ requests, and a mid-run failure should
        not throw away everything already fetched.
        """
        nonlocal hourly_frames, hourly_existing, hourly_dirty
        if not hourly_frames:
            return
        hourly_existing = pd.concat([hourly_existing, *hourly_frames], ignore_index=True)
        hourly_frames = []
        save_hourly(hourly_existing)
        hourly_existing = load_hourly(market=None)
        hourly_dirty = False

    for market in markets:
        have_mkt = have if existing.empty else set(
            map(tuple, existing.loc[existing["market"] == market, ["year", "month"]]
                .drop_duplicates().to_numpy().tolist()))
        # A month is fetched if *either* store needs it. The hourly store is
        # newer than the monthly one, so on its first run it drives a full
        # backfill even though the monthly store is already complete.
        todo = [ym for ym in all_months
                if ym in trailing
                or ym not in have_mkt
                or (ym[0], ym[1], market) not in have_hourly]
        if not todo:
            log(f"[{market}] up to date.")
            continue
        n_hourly = sum(1 for ym in todo if (ym[0], ym[1], market) not in have_hourly)
        log(f"[{market}] fetching {len(todo)} month(s): "
            f"{todo[0][0]}-{todo[0][1]:02d} → {todo[-1][0]}-{todo[-1][1]:02d}"
            + (f" ({n_hourly} needed for the hourly store)" if n_hourly else ""))
        # Walk newest → oldest so that hitting PJM's archive wall lets us stop
        # (everything older is archived too) without skipping recent months.
        for i, (yr, mo) in enumerate(sorted(todo, reverse=True), start=1):
            m_start, m_end = _month_bounds(yr, mo, today)
            log(f"    {yr}-{mo:02d} ({m_start} → {m_end}) …")
            try:
                hourly = _fetch_hourly(key, m_start, m_end, market=market, log=log)
                # The inclusive day-range pulls the next month's 00:00 boundary
                # hour; keep only rows for the month we asked for.
                if not hourly.empty:
                    dt = hourly["datetime_beginning_ept"]
                    hourly = hourly[(dt.dt.year == yr) & (dt.dt.month == mo)]
                if not hourly.empty:
                    hourly_frames.append(hourly)
                    hourly_dirty = True
                monthly = _to_monthly(hourly)
                if not monthly.empty:
                    frames.append(monthly)
                    log(f"      {int(monthly['hours'].sum()):,} zone-hours → "
                        f"{len(monthly)} zone rows")
                else:
                    log("      0 rows")
            except Exception as e:  # noqa: BLE001
                log(f"      month failed: {e}")
            if hourly_dirty and i % 12 == 0:
                _flush_hourly()
                log(f"      … hourly store checkpointed ({i}/{len(todo)} months)")
            time.sleep(1.5)

    _flush_hourly()

    fetched = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    combined = (pd.concat([existing, fetched], ignore_index=True)
                if not fetched.empty else existing)
    if combined.empty:
        raise RuntimeError("No zone price data fetched and no existing store.")

    before = len(combined)
    combined = combined.drop_duplicates(subset=KEY_COLS, keep="last")
    log(f"Merged: {before:,} → {len(combined):,} rows after de-duplication.")

    save_store(combined)
    stamp = pd.to_datetime(dict(year=combined["year"], month=combined["month"], day=1))
    summary = {
        "rows": len(combined),
        "start": str(stamp.min().date()),
        "end": str(stamp.max().date()),
        "zones": sorted(combined["zone"].unique().tolist()),
        "markets": sorted(combined["market"].unique().tolist()),
        "parquet": str(paths.ZONE_PRICES_PARQUET),
    }
    if paths.ZONE_PRICES_HOURLY_PARQUET.exists():
        try:
            summary["hourly_rows"] = len(pd.read_parquet(
                paths.ZONE_PRICES_HOURLY_PARQUET, columns=["zone"]))
            summary["hourly_parquet"] = str(paths.ZONE_PRICES_HOURLY_PARQUET)
        except Exception:
            pass
    write_state({"last_success": tz.now_eastern().isoformat(), **summary})
    log(f"\nDone. {summary['rows']:,} zone-month rows saved"
        + (f", {summary['hourly_rows']:,} hourly rows." if "hourly_rows" in summary else "."))
    return summary


def load_monthly(market: str = "RT") -> pd.DataFrame:
    """Monthly zone LMPs for one market, ready to join to EIA-923.

    Returns columns: zone, year, month, avg_lmp, avg_energy, avg_congestion,
    avg_loss, hours.
    """
    df = load_store()
    if df.empty:
        return df
    if market:
        df = df[df["market"] == market]
    return df.rename(columns={
        "total_lmp": "avg_lmp", "energy": "avg_energy",
        "congestion": "avg_congestion", "loss": "avg_loss",
    }).reset_index(drop=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="PJM zone monthly LMP downloader.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("update", help="Fetch missing / trailing months and update the store.")
    sub.add_parser("status", help="Show local store summary.")
    args = parser.parse_args(argv)

    if args.cmd == "status":
        print(json.dumps(store_summary(), indent=2, default=str))
        return 0
    if args.cmd == "update":
        try:
            update()
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"Update failed: {e}")
            return 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
