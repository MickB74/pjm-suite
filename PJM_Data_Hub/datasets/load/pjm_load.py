#!/usr/bin/env python3
"""PJM system load — hourly metered load by zone, direct from the PJM API.

Pulls the ``hrl_load_metered`` feed from api.pjm.com (same auth as the hub-price
fetcher). One row per hour × zone × load_area:

    zone       PJM transmission zone (AEP, COMED, DOM, PSEG, …) plus the
               system-wide ``RTO`` aggregate.
    load_area  metered sub-area within the zone.
    mw         metered load, MW.
    is_verified whether PJM has finalised the meter data.

Local store (one row per zone × load_area × hour):

    data/load/pjm_load_hourly.parquet
    data/load/pjm_load_hourly.csv

Peak-day analysis keys off the ``RTO`` zone (true system peak) and the ``DOM``
zone (Dominion, this suite's focus).

Usage:
    python pjm_load.py update
    python pjm_load.py status
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

_ENDPOINT = "https://api.pjm.com/api/v1/hrl_load_metered"

OVERLAP_DAYS = 3
STALE_AFTER_DAYS = 7
DATE_CHUNK_DAYS = 30
_ROW_COUNT = 50000

_EASTERN = ZoneInfo("America/New_York")
DEFAULT_BACKFILL_START = date(datetime.now(tz=_EASTERN).year - 1, 1, 1).isoformat()

KEEP_COLS = ["datetime_beginning_ept", "zone", "load_area", "mw", "is_verified"]


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


def _api_key(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else credentials.load_config()
    key = cfg.get("subscription_key") or os.environ.get("PJM_API_KEY", "")
    if not key:
        raise RuntimeError(
            "Missing PJM API subscription key. Set it on the API Keys page "
            "or in config.json / the PJM_API_KEY environment variable.")
    return key


def _fetch_window(api_key: str, start: date, end: date, log=print) -> pd.DataFrame:
    dt_range = (
        f"{start.strftime('%m/%d/%Y')} 00:00"
        f"to"
        f"{(end + timedelta(days=1)).strftime('%m/%d/%Y')} 00:00"
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
    df["mw"] = pd.to_numeric(df.get("mw"), errors="coerce")
    for c in ("zone", "load_area"):
        if c not in df.columns:
            df[c] = pd.NA
    keep = [c for c in KEEP_COLS if c in df.columns]
    return df[keep].dropna(subset=["datetime_beginning_ept", "zone"])


def fetch(cfg: dict, start: date, end: date, log=print) -> pd.DataFrame:
    key = _api_key(cfg)
    frames = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=DATE_CHUNK_DAYS - 1), end)
        log(f"    {chunk_start} → {chunk_end} …")
        try:
            df = _fetch_window(key, chunk_start, chunk_end, log=log)
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
    p = paths.LOAD_PARQUET
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def save_store(df: pd.DataFrame, write_csv: bool = True) -> None:
    paths.LOAD_DIR.mkdir(parents=True, exist_ok=True)
    df = df.sort_values(["datetime_beginning_ept", "zone", "load_area"]).reset_index(drop=True)
    df.to_parquet(paths.LOAD_PARQUET, index=False)
    if write_csv:
        df.to_csv(paths.LOAD_CSV, index=False)


def _dedup_keys() -> list[str]:
    return ["datetime_beginning_ept", "zone", "load_area"]


def read_state() -> dict:
    p = paths.LOAD_STATE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def write_state(state: dict) -> None:
    paths.LOAD_DIR.mkdir(parents=True, exist_ok=True)
    paths.LOAD_STATE.write_text(json.dumps(state, indent=2, default=str))


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
    info = {"exists": paths.LOAD_PARQUET.exists(), "rows": 0,
            "start": None, "end": None, "zones": [],
            "days_since_update": days_since_update()}
    if not info["exists"]:
        return info
    try:
        df = load_store()
        info["rows"] = len(df)
        if not df.empty:
            info["start"] = str(df["datetime_beginning_ept"].min())
            info["end"] = str(df["datetime_beginning_ept"].max())
            info["zones"] = sorted(df["zone"].dropna().unique().tolist())
    except Exception as e:
        info["error"] = str(e)
    return info


def update(progress_callback=None) -> dict:
    """Fetch new metered load and merge into the local store."""
    log = _make_logger(progress_callback)
    cfg = credentials.load_config()
    _api_key(cfg)

    existing = load_store()
    start = date.fromisoformat(cfg.get("backfill_start", DEFAULT_BACKFILL_START))
    end = tz.now_eastern().date()
    if start > end:
        start = end

    if existing.empty:
        fetch_start = start
        log(f"No local data. Backfilling metered load from {start} → {end}.")
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
        "zones": sorted(combined["zone"].dropna().unique().tolist()),
        "parquet": str(paths.LOAD_PARQUET),
    }
    write_state({"last_success": tz.now_eastern().isoformat(), **summary})
    log(f"\nDone. {summary['rows']:,} rows saved.")
    return summary


# ---------------------------------------------------------------------------
# Access layer
# ---------------------------------------------------------------------------

def load_zone(zone: str = "RTO", start=None, end_excl=None) -> pd.DataFrame:
    """Hourly load for one zone, aggregated across its load_areas.

    Returns columns [datetime_beginning_ept, mw]. ``zone='RTO'`` is the
    system-wide total.
    """
    df = load_store()
    if df.empty:
        return pd.DataFrame(columns=["datetime_beginning_ept", "mw"])
    df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
    df = df[df["zone"] == zone]
    if start is not None:
        df = df[df["datetime_beginning_ept"] >= pd.Timestamp(start)]
    if end_excl is not None:
        df = df[df["datetime_beginning_ept"] < pd.Timestamp(end_excl)]
    if df.empty:
        return pd.DataFrame(columns=["datetime_beginning_ept", "mw"])
    out = (df.groupby("datetime_beginning_ept", as_index=False)["mw"].sum()
           .sort_values("datetime_beginning_ept").reset_index(drop=True))
    return out


def zones() -> list[str]:
    df = load_store()
    if df.empty:
        return []
    return sorted(df["zone"].dropna().unique().tolist())


def main(argv=None):
    parser = argparse.ArgumentParser(description="PJM hourly metered load downloader.")
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
