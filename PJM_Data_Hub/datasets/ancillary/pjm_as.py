#!/usr/bin/env python3
"""PJM ancillary services — reserve + regulation market clearing prices.

Pulls the hourly ``reserve_market_results`` feed straight from api.pjm.com (same
auth as the hub-price fetcher). This single PJM feed — the product of the Oct-2022
reserve-market reform — carries **both** the reserve products and regulation:

    service   what it is
    --------  ---------------------------------------------------------------
    REG       Regulation. mcp is the regulation market clearing price; the
              capability (reg_ccp) and performance (reg_pcp) components split
              out separately.
    SR / SYNC Synchronized reserve.
    PR        Primary reserve.
    30MIN     30-minute reserve.
    SEC       Secondary reserve.
    NSR       Non-synchronized reserve.

Locale (``locale``) is the reserve sub-zone — ``PJM_RTO`` is the system-wide
number; MAD / MAD_PJM_RTO etc. are the constrained sub-regions.

Local store (one row per locale × service × hour):

    data/ancillary/pjm_ancillary_hourly.parquet
    data/ancillary/pjm_ancillary_hourly.csv

Usage:
    python pjm_as.py update
    python pjm_as.py status
"""

from __future__ import annotations

import argparse
import json
import sys
import os
import time
from datetime import date, timedelta, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pjm_core import credentials, paths, tz

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ENDPOINT = "https://api.pjm.com/api/v1/reserve_market_results"

OVERLAP_DAYS = 3
STALE_AFTER_DAYS = 7
DATE_CHUNK_DAYS = 30
_ROW_COUNT = 50000

_EASTERN = ZoneInfo("America/New_York")
DEFAULT_BACKFILL_START = date(datetime.now(tz=_EASTERN).year - 1, 1, 1).isoformat()

# Human-friendly labels for the raw PJM ``service`` codes.
SERVICE_LABELS = {
    "REG": "Regulation",
    "SR": "Sync Reserve",
    "SYNC": "Sync Reserve",
    "PR": "Primary Reserve",
    "30MIN": "30-Min Reserve",
    "SEC": "Secondary Reserve",
    "NSR": "Non-Sync Reserve",
}

# Numeric columns we keep from the feed.
_NUM_COLS = ["mcp", "mcp_capped", "reg_ccp", "reg_pcp",
             "as_req_mw", "total_mw", "as_mw"]

KEEP_COLS = ["datetime_beginning_ept", "locale", "service"] + _NUM_COLS


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
# Auth
# ---------------------------------------------------------------------------

def _api_key(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else credentials.load_config()
    key = cfg.get("subscription_key") or os.environ.get("PJM_API_KEY", "")
    if not key:
        raise RuntimeError(
            "Missing PJM API subscription key. Set it on the API Keys page "
            "or in config.json / the PJM_API_KEY environment variable.")
    return key


# ---------------------------------------------------------------------------
# Fetch + normalise
# ---------------------------------------------------------------------------

def _fetch_window(api_key: str, start: date, end: date, log=print) -> pd.DataFrame:
    """Fetch all reserve_market_results rows for [start, end] inclusive."""
    # End the range at the same day 23:59 (captures that day's last hour)
    # rather than spilling to the next day 00:00. Spilling across a boundary in
    # PJM's data store — a calendar-year edge or the archived↔current split —
    # makes the archive reject the whole request with a 400.
    dt_range = (
        f"{start.strftime('%m/%d/%Y')} 00:00"
        f"to{end.strftime('%m/%d/%Y')} 23:59"
    )
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    rows: list[dict] = []
    start_row = 1
    while True:
        params = {
            "datetime_beginning_ept": dt_range,
            "startRow": start_row,
            "rowCount": _ROW_COUNT,
        }
        for attempt in range(5):
            resp = requests.get(_ENDPOINT, params=params, headers=headers, timeout=60)
            if resp.status_code == 429:
                wait = 10.0 * (2 ** attempt)
                log(f"      rate-limited, waiting {wait:.0f}s …")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        else:
            log("      rate-limited after 5 retries, skipping page")
            break
        data = resp.json()
        items = data if isinstance(data, list) else data.get("items", [])
        rows.extend(items)
        if len(items) < _ROW_COUNT:
            break
        start_row += _ROW_COUNT
        time.sleep(1.0)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["datetime_beginning_ept"] = tz.to_naive_eastern(
        pd.to_datetime(df["datetime_beginning_ept"], errors="coerce"))
    for col in _NUM_COLS:
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    keep = [c for c in KEEP_COLS if c in df.columns]
    return df[keep].dropna(subset=["datetime_beginning_ept", "service"])


def _fetch_window_bisect(key: str, start: date, end: date, log=print) -> pd.DataFrame:
    """Fetch [start, end], bisecting the range on a 400 error.

    PJM returns 400 for a range that crosses a boundary in its data store — the
    calendar-year edge, or the archived↔current split (a moving ~2-year-old
    date). Splitting and retrying isolates the offending boundary to at most a
    single day, so everything on either side is still captured.
    """
    try:
        return _fetch_window(key, start, end, log=log)
    except Exception as e:
        if start >= end:
            log(f"      day {start} failed, skipping: {e}")
            return pd.DataFrame()
        mid = start + (end - start) // 2
        log(f"      400 on {start}→{end}; splitting at {mid}")
        left = _fetch_window_bisect(key, start, mid, log=log)
        time.sleep(1.0)
        right = _fetch_window_bisect(key, mid + timedelta(days=1), end, log=log)
        parts = [f for f in (left, right) if not f.empty]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def fetch(cfg: dict, start: date, end: date, log=print) -> pd.DataFrame:
    """Fetch ancillary market results for [start, end] inclusive, in date chunks."""
    key = _api_key(cfg)
    frames = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=DATE_CHUNK_DAYS - 1), end)
        # PJM's archived data store rejects a range that crosses a calendar-year
        # boundary (400), so never let a chunk span Dec 31 → Jan 1. The
        # archived↔current boundary is handled by bisect-on-failure below.
        year_end = date(chunk_start.year, 12, 31)
        if chunk_end > year_end:
            chunk_end = year_end
        log(f"    {chunk_start} → {chunk_end} …")
        try:
            df = _fetch_window_bisect(key, chunk_start, chunk_end, log=log)
            if not df.empty:
                frames.append(df)
                log(f"      {len(df):,} rows")
            else:
                log("      0 rows")
        except Exception as e:
            log(f"      chunk failed: {e}")
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(2.0)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def load_store() -> pd.DataFrame:
    p = paths.ANCILLARY_PARQUET
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def save_store(df: pd.DataFrame, write_csv: bool = True) -> None:
    paths.ANCILLARY_DIR.mkdir(parents=True, exist_ok=True)
    df = df.sort_values(["datetime_beginning_ept", "locale", "service"]).reset_index(drop=True)
    df.to_parquet(paths.ANCILLARY_PARQUET, index=False)
    if write_csv:
        df.to_csv(paths.ANCILLARY_CSV, index=False)


def _dedup_keys() -> list[str]:
    return ["datetime_beginning_ept", "locale", "service"]


def read_state() -> dict:
    p = paths.ANCILLARY_STATE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def write_state(state: dict) -> None:
    paths.ANCILLARY_DIR.mkdir(parents=True, exist_ok=True)
    paths.ANCILLARY_STATE.write_text(json.dumps(state, indent=2, default=str))


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


def is_stale() -> bool:
    d = days_since_update()
    return d is None or d >= STALE_AFTER_DAYS


def store_summary() -> dict:
    info = {"exists": paths.ANCILLARY_PARQUET.exists(), "rows": 0,
            "start": None, "end": None, "services": [], "locales": [],
            "days_since_update": days_since_update()}
    if not info["exists"]:
        return info
    try:
        df = load_store()
        info["rows"] = len(df)
        if not df.empty:
            info["start"] = str(df["datetime_beginning_ept"].min())
            info["end"] = str(df["datetime_beginning_ept"].max())
            info["services"] = sorted(df["service"].dropna().unique().tolist())
            info["locales"] = sorted(df["locale"].dropna().unique().tolist())
    except Exception as e:
        info["error"] = str(e)
    return info


# ---------------------------------------------------------------------------
# Main update routine
# ---------------------------------------------------------------------------

def update(progress_callback=None) -> dict:
    """Fetch new ancillary market results and merge into the local store."""
    log = _make_logger(progress_callback)
    cfg = credentials.load_config()
    _api_key(cfg)  # raises early if missing

    existing = load_store()
    start = date.fromisoformat(cfg.get("backfill_start", DEFAULT_BACKFILL_START))
    end = tz.now_eastern().date()
    if start > end:
        start = end

    if existing.empty:
        fetch_start = start
        log(f"No local data. Backfilling ancillary services from {start} → {end}.")
    else:
        have_max = pd.to_datetime(existing["datetime_beginning_ept"]).max().date()
        fetch_start = max(start, have_max - timedelta(days=OVERLAP_DAYS))
        log(f"Existing data through {have_max}. Fetching {fetch_start} → {end}.")

    fetched = fetch(cfg, fetch_start, end, log=log)

    combined = pd.concat([existing, fetched], ignore_index=True) if not fetched.empty else existing
    if combined.empty:
        raise RuntimeError("No data available — nothing fetched and no existing store.")

    before = len(combined)
    combined = combined.drop_duplicates(subset=_dedup_keys(), keep="last")
    log(f"Merged: {before:,} → {len(combined):,} rows after de-duplication.")

    save_store(combined)
    summary = {
        "rows": len(combined),
        "start": str(combined["datetime_beginning_ept"].min()),
        "end": str(combined["datetime_beginning_ept"].max()),
        "services": sorted(combined["service"].dropna().unique().tolist()),
        "locales": sorted(combined["locale"].dropna().unique().tolist()),
        "parquet": str(paths.ANCILLARY_PARQUET),
    }
    write_state({"last_success": tz.now_eastern().isoformat(), **summary})
    log(f"\nDone. {summary['rows']:,} rows saved.")
    return summary


# ---------------------------------------------------------------------------
# Access layer (used by screens)
# ---------------------------------------------------------------------------

def load(start=None, end_excl=None, services=None, locale=None) -> pd.DataFrame:
    """Load ancillary results from the local store with optional filters."""
    df = load_store()
    if df.empty:
        return df
    df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
    if services:
        df = df[df["service"].isin(services)]
    if locale:
        df = df[df["locale"] == locale]
    if start is not None:
        df = df[df["datetime_beginning_ept"] >= pd.Timestamp(start)]
    if end_excl is not None:
        df = df[df["datetime_beginning_ept"] < pd.Timestamp(end_excl)]
    return df.sort_values(["datetime_beginning_ept", "locale", "service"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="PJM ancillary services downloader.")
    sub = parser.add_subparsers(dest="cmd")
    up = sub.add_parser("update", help="Fetch latest data and update the local store.")
    up.add_argument("--auto", action="store_true",
                    help="Quiet mode for scheduled jobs; skips if data is fresh.")
    sub.add_parser("status", help="Show local store summary.")

    args = parser.parse_args(argv)

    if args.cmd == "status":
        print(json.dumps(store_summary(), indent=2, default=str))
        return 0

    if args.cmd == "update":
        if getattr(args, "auto", False) and not is_stale():
            print(f"Data is fresh ({days_since_update():.1f} days old). Skipping.")
            return 0
        try:
            update()
            return 0
        except Exception as e:
            print(f"Update failed: {e}")
            return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
