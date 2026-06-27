#!/usr/bin/env python3
"""PJM hub LMP downloader — core engine (via gridstatus).

Pulls hourly Real-Time LMPs for the PJM trading hubs from PJM Data Miner 2
through the ``gridstatus`` library, which handles authentication, pagination,
the PJM date-range format, DST, and the canonical hub pnode names. Keeps a
local parquet store and updates it incrementally.

    data/hub_prices/pjm_hub_prices_hourly.parquet
    data/hub_prices/pjm_hub_prices_hourly.csv

Requires a free PJM Data Miner 2 subscription key (config.json: subscription_key,
or the PJM_API_KEY environment variable). Register at https://api.pjm.com/ —
non-members get free API access for internal business use; see README.

Usage:
    python pjm_api.py set-credentials
    python pjm_api.py test-auth
    python pjm_api.py update
    python pjm_api.py status
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

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pjm_core import credentials, paths, tz
from pjm_core.settlement_points import HUBS, PRIMARY_HUB

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# gridstatus market identifier for PJM real-time hourly LMPs.
RT_MARKET = "REAL_TIME_HOURLY"
DA_MARKET = "DAY_AHEAD_HOURLY"

OVERLAP_DAYS = 3        # re-fetch recent days to pick up late revisions
STALE_AFTER_DAYS = 7
DATE_CHUNK_DAYS = 30    # fetch in chunks (PJM caps a single query at 366 days)

_EASTERN = ZoneInfo("America/New_York")
DEFAULT_BACKFILL_START = date(datetime.now(tz=_EASTERN).year - 1, 1, 1).isoformat()


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
# Auth / client
# ---------------------------------------------------------------------------

class PJMAuthError(RuntimeError):
    pass


def _api_key(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else credentials.load_config()
    key = cfg.get("subscription_key") or os.environ.get("PJM_API_KEY", "")
    if not key:
        raise PJMAuthError(
            "Missing PJM API subscription key. Run 'set-credentials' or set it "
            "in config.json / the PJM_API_KEY environment variable."
        )
    return key


def _client(cfg: dict | None = None):
    """Return an authenticated gridstatus PJM client."""
    try:
        from gridstatus import PJM
    except ImportError as e:
        raise ImportError("gridstatus not installed. Run: pip install gridstatus") from e
    return PJM(api_key=_api_key(cfg))


def test_auth(cfg: dict | None = None, log=print) -> bool:
    """Verify the key works by fetching one recent hour of hub LMPs."""
    try:
        iso = _client(cfg)
        d = pd.Timestamp(date.today() - timedelta(days=2))
        df = iso.get_lmp(date=d, end=d + pd.Timedelta(hours=2),
                         market=RT_MARKET, locations="hubs")
        ok = df is not None and not df.empty
        log("Auth OK." if ok else "Auth returned no data (key may lack access).")
        return ok
    except Exception as e:
        log(f"Auth failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Fetch + normalise
# ---------------------------------------------------------------------------

# gridstatus LMP columns -> our store schema.
_GS_RENAME = {
    "Location": "pnode_name",
    "Location Type": "type",
    "LMP": "total_lmp",
    "Energy": "energy",
    "Congestion": "congestion",
    "Loss": "loss",
}


def _normalize_gs(df: pd.DataFrame) -> pd.DataFrame:
    """Map a gridstatus PJM LMP frame to the local store schema (naive Eastern)."""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns=_GS_RENAME).copy()

    # gridstatus returns tz-aware Eastern Interval Start/End; store naive Eastern.
    df["datetime_beginning_ept"] = tz.to_naive_eastern(pd.to_datetime(df["Interval Start"]))
    df["datetime_ending_ept"] = tz.to_naive_eastern(pd.to_datetime(df["Interval End"]))

    for col in ("total_lmp", "energy", "congestion", "loss"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    keep = [c for c in [
        "datetime_beginning_ept", "datetime_ending_ept",
        "pnode_name", "type",
        "total_lmp", "energy", "congestion", "loss",
    ] if c in df.columns]
    return df[keep].dropna(subset=["datetime_beginning_ept", "pnode_name"])


def fetch_hub_lmps(cfg: dict, start: date, end: date, log=print,
                   market: str = RT_MARKET) -> pd.DataFrame:
    """Fetch hourly hub LMPs for [start, end] inclusive (all PJM hubs)."""
    iso = _client(cfg)
    frames = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=DATE_CHUNK_DAYS - 1), end)
        log(f"    {chunk_start} → {chunk_end} …")
        try:
            df = iso.get_lmp(
                date=pd.Timestamp(chunk_start),
                end=pd.Timestamp(chunk_end) + pd.Timedelta(days=1),
                market=market,
                locations="hubs",
            )
            norm = _normalize_gs(df)
            if not norm.empty:
                frames.append(norm)
                log(f"      {len(norm):,} rows")
        except Exception as e:
            log(f"      chunk failed: {e}")
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(0.5)  # be polite to the API

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def load_store() -> pd.DataFrame:
    p = paths.HUB_PRICES_PARQUET
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def save_store(df: pd.DataFrame, write_csv: bool = True) -> None:
    paths.HUB_PRICES_DIR.mkdir(parents=True, exist_ok=True)
    df = df.sort_values(["datetime_beginning_ept", "pnode_name"]).reset_index(drop=True)
    df.to_parquet(paths.HUB_PRICES_PARQUET, index=False)
    if write_csv:
        df.to_csv(paths.HUB_PRICES_CSV, index=False)


def _dedup_keys(df: pd.DataFrame) -> list[str]:
    return ["pnode_name", "datetime_beginning_ept"]


def read_state() -> dict:
    p = paths.HUB_PRICES_STATE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def write_state(state: dict) -> None:
    paths.HUB_PRICES_DIR.mkdir(parents=True, exist_ok=True)
    paths.HUB_PRICES_STATE.write_text(json.dumps(state, indent=2, default=str))


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
    info = {"exists": paths.HUB_PRICES_PARQUET.exists(), "rows": 0,
            "start": None, "end": None, "hubs": [], "days_since_update": days_since_update()}
    if not info["exists"]:
        return info
    try:
        df = load_store()
        info["rows"] = len(df)
        if not df.empty:
            info["start"] = str(df["datetime_beginning_ept"].min())
            info["end"] = str(df["datetime_beginning_ept"].max())
            info["hubs"] = sorted(df["pnode_name"].unique().tolist())
    except Exception as e:
        info["error"] = str(e)
    return info


# ---------------------------------------------------------------------------
# Main update routine
# ---------------------------------------------------------------------------

def update(hubs: list[str] | None = None, progress_callback=None) -> dict:
    """Fetch new hub LMPs and merge into the local store.

    ``hubs`` filters which hub pnode names are kept (default: all PJM hubs).
    gridstatus always returns every hub in one call, so this is a post-filter.
    """
    log = _make_logger(progress_callback)
    cfg = credentials.load_config()
    _api_key(cfg)  # raises early if missing

    existing = load_store()
    start = date.fromisoformat(cfg.get("backfill_start", DEFAULT_BACKFILL_START))
    end = tz.now_eastern().date()
    if start > end:
        start = end

    # On an existing store, only refresh the recent overlap window.
    if existing.empty:
        fetch_start = start
        log(f"No local data. Backfilling hubs from {start} → {end}.")
    else:
        have_max = pd.to_datetime(existing["datetime_beginning_ept"]).max().date()
        fetch_start = max(start, have_max - timedelta(days=OVERLAP_DAYS))
        log(f"Existing data through {have_max}. Fetching {fetch_start} → {end}.")

    fetched = fetch_hub_lmps(cfg, fetch_start, end, log=log)
    if hubs and not fetched.empty:
        fetched = fetched[fetched["pnode_name"].isin(hubs)]

    combined = pd.concat([existing, fetched], ignore_index=True) if not fetched.empty else existing
    if combined.empty:
        raise RuntimeError("No data available — nothing fetched and no existing store.")

    before = len(combined)
    combined = combined.drop_duplicates(subset=_dedup_keys(combined), keep="last")
    log(f"Merged: {before:,} → {len(combined):,} rows after de-duplication.")

    save_store(combined)
    summary = {
        "rows": len(combined),
        "start": str(combined["datetime_beginning_ept"].min()),
        "end": str(combined["datetime_beginning_ept"].max()),
        "hubs": sorted(combined["pnode_name"].unique().tolist()),
        "parquet": str(paths.HUB_PRICES_PARQUET),
    }
    write_state({"last_success": tz.now_eastern().isoformat(), **summary})
    log(f"\nDone. {summary['rows']:,} rows saved.")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="PJM hub hourly LMP downloader (gridstatus).")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("set-credentials", help="Enter/save your PJM subscription key.")
    sub.add_parser("test-auth", help="Verify the key works.")
    up = sub.add_parser("update", help="Fetch latest data and update the local store.")
    up.add_argument("--auto", action="store_true",
                    help="Quiet mode for scheduled jobs; skips if data is fresh.")
    up.add_argument("--hubs", nargs="*", default=None,
                    help="Hub names to keep (default: all PJM hubs).")
    sub.add_parser("status", help="Show local store summary.")

    args = parser.parse_args(argv)

    if args.cmd == "set-credentials":
        credentials.set_credentials_interactive()
        return 0

    if args.cmd == "test-auth":
        return 0 if test_auth() else 1

    if args.cmd == "status":
        print(json.dumps(store_summary(), indent=2, default=str))
        return 0

    if args.cmd == "update":
        if getattr(args, "auto", False) and not is_stale():
            print(f"Data is fresh ({days_since_update():.1f} days old). Skipping.")
            return 0
        try:
            update(hubs=getattr(args, "hubs", None))
            return 0
        except Exception as e:
            print(f"Update failed: {e}")
            return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
