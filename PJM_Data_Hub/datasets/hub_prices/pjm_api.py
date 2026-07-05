#!/usr/bin/env python3
"""PJM hub LMP downloader — core engine (via gridstatus).

Pulls hourly Real-Time AND Day-Ahead LMPs for the PJM trading hubs from PJM
Data Miner 2 through the ``gridstatus`` library, which handles authentication,
pagination, the PJM date-range format, DST, and the canonical hub pnode names.
Keeps a local parquet store (one row per hub × hour × market, ``market`` in
{"RT", "DA"}) and updates it incrementally.

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

import requests
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from pjm_core import credentials, paths, tz
from pjm_core.settlement_points import HUBS, PRIMARY_HUB

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RT_MARKET = "REAL_TIME_HOURLY"
DA_MARKET = "DAY_AHEAD_HOURLY"
MARKETS = {"RT": RT_MARKET, "DA": DA_MARKET}

OVERLAP_DAYS = 3
STALE_AFTER_DAYS = 7
DATE_CHUNK_DAYS = 30

_EASTERN = ZoneInfo("America/New_York")
DEFAULT_BACKFILL_START = date(datetime.now(tz=_EASTERN).year - 1, 1, 1).isoformat()

# PJM DataMiner 2 direct API endpoints (bypass gridstatus to avoid pnode lookups)
_PJM_API_BASE = "https://api.pjm.com/api/v1"
_RT_ENDPOINT = f"{_PJM_API_BASE}/rt_hrl_lmps"
_DA_ENDPOINT = f"{_PJM_API_BASE}/da_hrl_lmps"

# Hub pnode IDs — stable PJM node IDs for the 12 trading hubs.
# Pulled from gridstatus pnode lookup; avoids per-chunk pnode API calls.
_HUB_PNODE_IDS = {
    "DOMINION HUB":    51217,
    "AEP-DAYTON HUB":  116013751,
    "AEP GEN HUB":     35010337,
    "ATSI GEN HUB":    34497151,
    "CHICAGO HUB":     34497127,
    "CHICAGO GEN HUB": 34497125,
    "EASTERN HUB":     33092315,
    "WESTERN HUB":     33092313,
    "N ILLINOIS HUB":  33092311,
    "NEW JERSEY HUB":  4669664,
    "OHIO HUB":        51288,
    "WEST INT HUB":    51287,
}
_PNODE_ID_STR = ";".join(str(v) for v in _HUB_PNODE_IDS.values())
_ID_TO_HUB = {v: k for k, v in _HUB_PNODE_IDS.items()}


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



def _fetch_direct(api_key: str, start: date, end: date,
                  market: str = "RT", log=print) -> pd.DataFrame:
    """Query PJM API directly for hub LMPs — no gridstatus, no pnode lookup."""
    endpoint = _RT_ENDPOINT if market == "RT" else _DA_ENDPOINT
    lmp_col = "total_lmp_rt" if market == "RT" else "total_lmp_da"
    energy_col = "system_energy_price_rt" if market == "RT" else "system_energy_price_da"
    cong_col = "congestion_price_rt" if market == "RT" else "congestion_price_da"
    loss_col = "marginal_loss_price_rt" if market == "RT" else "marginal_loss_price_da"

    dt_range = (
        f"{start.strftime('%m/%d/%Y')} 00:00"
        f"to"
        f"{(end + timedelta(days=1)).strftime('%m/%d/%Y')} 00:00"
    )
    params = {
        "pnode_id": _PNODE_ID_STR,
        "row_is_current": "TRUE",
        "datetime_beginning_ept": dt_range,
    }
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    all_rows = []
    row_count = 50000
    start_row = 1
    while True:
        p = {**params, "startRow": start_row, "rowCount": row_count}
        for attempt in range(5):
            resp = requests.get(endpoint, params=p, headers=headers, timeout=60)
            if resp.status_code == 429:
                wait = 10.0 * (2 ** attempt)
                log(f"      rate-limited, waiting {wait:.0f}s …")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        else:
            log(f"      rate-limited after 5 retries, skipping page")
            break
        data = resp.json()
        items = data if isinstance(data, list) else data.get("items", [])
        all_rows.extend(items)
        if len(items) < row_count:
            break
        start_row += row_count
        time.sleep(1.0)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df["pnode_id"] = pd.to_numeric(df.get("pnode_id", pd.Series(dtype=int)), errors="coerce")
    df["pnode_name"] = df["pnode_id"].map(_ID_TO_HUB).fillna(df.get("pnode_name", ""))
    df["market"] = market
    df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"], errors="coerce")
    df["datetime_ending_ept"] = pd.to_datetime(df.get("datetime_ending_ept"), errors="coerce")
    df = df.rename(columns={
        lmp_col: "total_lmp",
        energy_col: "energy",
        cong_col: "congestion",
        loss_col: "loss",
        "type": "type",
    })
    for col in ("total_lmp", "energy", "congestion", "loss"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    keep = [c for c in [
        "datetime_beginning_ept", "datetime_ending_ept",
        "pnode_name", "type", "market",
        "total_lmp", "energy", "congestion", "loss",
    ] if c in df.columns]
    return df[keep].dropna(subset=["datetime_beginning_ept", "pnode_name"])


def test_auth(cfg: dict | None = None, log=print) -> bool:
    """Verify the key works by fetching one recent hour of hub LMPs."""
    try:
        key = _api_key(cfg)
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=1)
        df = _fetch_direct(key, start, end, market="RT", log=log)
        ok = df is not None and not df.empty
        log("Auth OK." if ok else "Auth returned no data (key may lack access).")
        return ok
    except Exception as e:
        log(f"Auth failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Fetch + normalise
# ---------------------------------------------------------------------------

def fetch_hub_lmps(cfg: dict, start: date, end: date, log=print,
                   market: str = "RT") -> pd.DataFrame:
    """Fetch hourly hub LMPs for [start, end] inclusive (all PJM hubs).

    Queries PJM DataMiner 2 directly (no gridstatus pnode lookup per chunk).
    ``market`` is "RT" or "DA".
    """
    key = _api_key(cfg)
    frames = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=DATE_CHUNK_DAYS - 1), end)
        log(f"    {chunk_start} → {chunk_end} …")
        try:
            df = _fetch_direct(key, chunk_start, chunk_end, market=market, log=log)
            if not df.empty:
                frames.append(df)
                log(f"      {len(df):,} rows")
            else:
                log(f"      0 rows")
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
    p = paths.HUB_PRICES_PARQUET
    df = pd.read_parquet(p) if p.exists() else pd.DataFrame()
    # Migration: stores written before DA support are all real-time rows.
    if not df.empty and "market" not in df.columns:
        df["market"] = "RT"
    return df


def save_store(df: pd.DataFrame, write_csv: bool = True) -> None:
    paths.HUB_PRICES_DIR.mkdir(parents=True, exist_ok=True)
    df = df.sort_values(["market", "datetime_beginning_ept", "pnode_name"]).reset_index(drop=True)
    df.to_parquet(paths.HUB_PRICES_PARQUET, index=False)
    if write_csv:
        df.to_csv(paths.HUB_PRICES_CSV, index=False)


def _dedup_keys(df: pd.DataFrame) -> list[str]:
    return ["pnode_name", "market", "datetime_beginning_ept"]


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
            "start": None, "end": None, "hubs": [], "markets": {},
            "days_since_update": days_since_update()}
    if not info["exists"]:
        return info
    try:
        df = load_store()
        info["rows"] = len(df)
        if not df.empty:
            info["start"] = str(df["datetime_beginning_ept"].min())
            info["end"] = str(df["datetime_beginning_ept"].max())
            info["hubs"] = sorted(df["pnode_name"].unique().tolist())
            for mkt, grp in df.groupby("market"):
                info["markets"][mkt] = {
                    "rows": len(grp),
                    "start": str(grp["datetime_beginning_ept"].min()),
                    "end": str(grp["datetime_beginning_ept"].max()),
                }
    except Exception as e:
        info["error"] = str(e)
    return info


# ---------------------------------------------------------------------------
# Main update routine
# ---------------------------------------------------------------------------

def update(hubs: list[str] | None = None, progress_callback=None,
           markets: tuple[str, ...] = ("RT", "DA")) -> dict:
    """Fetch new hub LMPs (RT and DA) and merge into the local store.

    ``hubs`` filters which hub pnode names are kept (default: all PJM hubs).
    gridstatus always returns every hub in one call, so this is a post-filter.
    ``markets`` selects which markets to refresh (default: both).
    """
    log = _make_logger(progress_callback)
    cfg = credentials.load_config()
    _api_key(cfg)  # raises early if missing

    existing = load_store()
    start = date.fromisoformat(cfg.get("backfill_start", DEFAULT_BACKFILL_START))
    end = tz.now_eastern().date()
    if start > end:
        start = end

    frames = []
    for market in markets:
        have = existing[existing["market"] == market] if not existing.empty else existing
        # On an existing store, only refresh the recent overlap window.
        if have.empty:
            fetch_start = start
            log(f"[{market}] No local data. Backfilling hubs from {start} → {end}.")
        else:
            have_max = pd.to_datetime(have["datetime_beginning_ept"]).max().date()
            fetch_start = max(start, have_max - timedelta(days=OVERLAP_DAYS))
            log(f"[{market}] Existing data through {have_max}. "
                f"Fetching {fetch_start} → {end}.")
        fetched = fetch_hub_lmps(cfg, fetch_start, end, log=log, market=market)
        if not fetched.empty:
            frames.append(fetched)

    fetched = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
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
        "markets": sorted(combined["market"].unique().tolist()),
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
    up.add_argument("--markets", nargs="*", default=["RT", "DA"],
                    choices=["RT", "DA"],
                    help="Markets to refresh (default: both RT and DA).")
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
            update(hubs=getattr(args, "hubs", None),
                   markets=tuple(getattr(args, "markets", ["RT", "DA"])))
            return 0
        except Exception as e:
            print(f"Update failed: {e}")
            return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
