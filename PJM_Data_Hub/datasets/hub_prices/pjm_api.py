#!/usr/bin/env python3
"""PJM DOM Hub LMP downloader — core engine.

Pulls hourly Real-Time LMPs for PJM trading hubs from the PJM Data Miner 2
API (api.pjm.com, endpoint ``rt_hrl_lmps``). Keeps a local parquet store and
updates it incrementally.

    data/hub_prices/pjm_hub_prices_hourly.parquet
    data/hub_prices/pjm_hub_prices_hourly.csv

Usage:
    python pjm_api.py set-credentials
    python pjm_api.py test-auth
    python pjm_api.py update
    python pjm_api.py status

API registration: https://api.pjm.com/
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
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pjm_core import credentials, paths, tz
from pjm_core.settlement_points import HUBS, PRIMARY_HUB

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE = "https://api.pjm.com/api/v1"
RT_LMP_ENDPOINT = f"{API_BASE}/rt_hrl_lmps"
DA_LMP_ENDPOINT = f"{API_BASE}/da_hrl_lmps"

# PJM's free Data Miner API is paginated at max 50,000 rows per request.
PAGE_SIZE = 50_000
HTTP_TIMEOUT = 60
MAX_RETRIES = 4
MIN_REQUEST_INTERVAL = 1.0   # seconds between requests
MAX_RATE_LIMIT_WAITS = 10
OVERLAP_DAYS = 3
STALE_AFTER_DAYS = 7
DATE_CHUNK_DAYS = 30

# Default backfill start (beginning of last year) — can be overridden in config.
_EASTERN = ZoneInfo("America/New_York")

DEFAULT_BACKFILL_START = date(
    datetime.now(tz=_EASTERN).year - 1, 1, 1
).isoformat()

_last_request_time = 0.0


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
# Authentication (PJM only needs a subscription key in a header)
# ---------------------------------------------------------------------------

class PJMAuthError(RuntimeError):
    pass


def _headers(cfg: dict) -> dict:
    key = cfg.get("subscription_key", "")
    if not key:
        raise PJMAuthError(
            "Missing PJM API subscription key. Run 'set-credentials' or set it in config.json."
        )
    return {"Ocp-Apim-Subscription-Key": key}


def test_auth(cfg: dict, log=print) -> bool:
    """Verify the subscription key works by fetching one row."""
    try:
        yesterday = (date.today() - timedelta(days=2)).isoformat()
        params = {
            "datetime_beginning_ept": f"{yesterday} 00:00",
            "pnode_name": PRIMARY_HUB,
            "rowCount": 1,
            "startRow": 1,
        }
        r = requests.get(RT_LMP_ENDPOINT, headers=_headers(cfg), params=params,
                         timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        log("Auth OK.")
        return True
    except Exception as e:
        log(f"Auth failed: {e}")
        return False


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _throttle():
    global _last_request_time
    wait = MIN_REQUEST_INTERVAL - (time.time() - _last_request_time)
    if wait > 0:
        time.sleep(wait)
    _last_request_time = time.time()


def _do_request(url: str, headers: dict, params: dict, log=print) -> dict:
    """GET with retry on 429 / 5xx. Returns parsed JSON."""
    rate_waits = 0
    attempt = 0
    while True:
        _throttle()
        try:
            r = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            attempt += 1
            if attempt > MAX_RETRIES:
                raise RuntimeError(f"PJM API request failed after {MAX_RETRIES} tries: {e}")
            time.sleep(2 * attempt)
            continue

        if r.status_code == 200:
            return r.json()

        if r.status_code == 429:
            rate_waits += 1
            if rate_waits > MAX_RATE_LIMIT_WAITS:
                raise RuntimeError("Still rate-limited after many waits.")
            wait_secs = float(r.headers.get("Retry-After", 30)) + 1.0
            log(f"    rate-limited; waiting {wait_secs:.0f}s …")
            time.sleep(wait_secs)
            continue

        if r.status_code in (500, 502, 503, 504):
            attempt += 1
            if attempt > MAX_RETRIES:
                raise RuntimeError(f"PJM API HTTP {r.status_code}: {r.text[:200]}")
            time.sleep(3 * attempt)
            continue

        raise RuntimeError(f"PJM API HTTP {r.status_code}: {r.text[:200]}")


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def _fetch_lmps_page(
    headers: dict,
    hub: str,
    start_dt: datetime,
    end_dt: datetime,
    start_row: int,
    log=print,
    endpoint: str = RT_LMP_ENDPOINT,
) -> dict:
    params = {
        "datetime_beginning_ept": start_dt.strftime("%Y-%m-%d %H:%M"),
        "datetime_ending_ept": end_dt.strftime("%Y-%m-%d %H:%M"),
        "pnode_name": hub,
        "rowCount": PAGE_SIZE,
        "startRow": start_row,
        "fields": "datetime_beginning_ept,datetime_ending_ept,pnode_id,pnode_name,voltage,equipment,type,system_energy_price_rt,total_lmp_rt,congestion_price_rt,marginal_loss_price_rt",
    }
    return _do_request(endpoint, headers, params, log=log)


def fetch_hub_lmps(
    cfg: dict,
    hub: str,
    start: date,
    end: date,
    log=print,
    endpoint: str = RT_LMP_ENDPOINT,
) -> pd.DataFrame:
    """Fetch all hourly RT LMP rows for one hub over [start, end] inclusive."""
    hdrs = _headers(cfg)
    frames = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=DATE_CHUNK_DAYS - 1), end)
        s_dt = datetime(chunk_start.year, chunk_start.month, chunk_start.day, 0, 0)
        # end: first hour of the day AFTER chunk_end (exclusive)
        e_dt = datetime(chunk_end.year, chunk_end.month, chunk_end.day, 0, 0) + timedelta(days=1)

        start_row = 1
        while True:
            payload = _fetch_lmps_page(hdrs, hub, s_dt, e_dt, start_row, log=log, endpoint=endpoint)
            items = payload.get("items", [])
            total = payload.get("totalRows", 0)
            if items:
                frames.append(pd.DataFrame(items))
            fetched_so_far = start_row - 1 + len(items)
            if fetched_so_far >= total or not items:
                break
            start_row += PAGE_SIZE

        chunk_start = chunk_end + timedelta(days=1)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_RENAME_RT = {
    "datetime_beginning_ept": "datetime_beginning_ept",
    "datetime_ending_ept": "datetime_ending_ept",
    "pnode_id": "pnode_id",
    "pnode_name": "pnode_name",
    "voltage": "voltage",
    "equipment": "equipment",
    "type": "type",
    "system_energy_price_rt": "energy",
    "total_lmp_rt": "total_lmp",
    "congestion_price_rt": "congestion",
    "marginal_loss_price_rt": "loss",
}


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.rename(columns=_RENAME_RT).copy()

    for col in ("total_lmp", "energy", "congestion", "loss"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"], errors="coerce")
    df["datetime_ending_ept"] = pd.to_datetime(df.get("datetime_ending_ept", pd.NaT), errors="coerce")

    # Store as naive Eastern wall-clock (lake convention — matches ERCOT approach)
    df["datetime_beginning_ept"] = tz.to_naive_eastern(df["datetime_beginning_ept"])
    df["datetime_ending_ept"] = tz.to_naive_eastern(df["datetime_ending_ept"])

    keep = [c for c in [
        "datetime_beginning_ept", "datetime_ending_ept",
        "pnode_name", "pnode_id", "type", "voltage", "equipment",
        "total_lmp", "energy", "congestion", "loss",
    ] if c in df.columns]

    df = df[keep].dropna(subset=["datetime_beginning_ept", "pnode_name"])
    return df


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
    state = read_state()
    ts = state.get("last_success")
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
    """Fetch new data and merge into the local store."""
    log = _make_logger(progress_callback)
    cfg = credentials.load_config()
    if not credentials.have_credentials(cfg):
        raise PJMAuthError("Missing PJM subscription key. Run 'set-credentials'.")

    hubs = hubs or [PRIMARY_HUB]  # default to DOM HUB only
    existing = load_store()

    cfg_start = cfg.get("backfill_start", DEFAULT_BACKFILL_START)
    start = date.fromisoformat(cfg_start)
    end = tz.now_eastern().date()
    if start > end:
        start = end

    if existing.empty:
        log(f"No local data. Backfilling from {start} for: {', '.join(hubs)}")
    else:
        have_min = existing["datetime_beginning_ept"].min()
        have_max = existing["datetime_beginning_ept"].max()
        log(f"Existing data {have_min} → {have_max}. Updating through {end}.")

    # Overlap window to pick up late revisions
    fetch_start = max(start, (end - timedelta(days=OVERLAP_DAYS + 1)))
    if existing.empty:
        fetch_start = start

    combined = existing
    for i, hub in enumerate(hubs, 1):
        log(f"[{i}/{len(hubs)}] {hub} ({fetch_start} → {end}) …")
        raw = fetch_hub_lmps(cfg, hub, fetch_start, end, log=log)
        norm = normalize(raw)
        log(f"    got {len(norm):,} rows.")
        if not norm.empty:
            combined = pd.concat([combined, norm], ignore_index=True)

    if combined.empty:
        raise RuntimeError("No data available.")

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
    parser = argparse.ArgumentParser(description="PJM hub hourly LMP downloader.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("set-credentials", help="Enter/save your PJM subscription key.")
    sub.add_parser("test-auth", help="Verify the key works.")
    up = sub.add_parser("update", help="Fetch latest data and update the local store.")
    up.add_argument("--auto", action="store_true",
                    help="Quiet mode for scheduled jobs; skips if data is fresh.")
    up.add_argument("--hubs", nargs="*", default=None,
                    help="Hub names to fetch (default: DOM HUB).")
    sub.add_parser("status", help="Show local store summary.")

    args = parser.parse_args(argv)

    if args.cmd == "set-credentials":
        credentials.set_credentials_interactive()
        return 0

    if args.cmd == "test-auth":
        cfg = credentials.load_config()
        return 0 if test_auth(cfg) else 1

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
