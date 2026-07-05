#!/usr/bin/env python3
"""PJM system generation by fuel — ETL via the PJM Data Miner 2 API directly.

Pulls the hourly ``gen_by_fuel`` feed straight from api.pjm.com (same auth as
the hub-price fetcher) rather than through gridstatus, because gridstatus's PJM
fuel-mix wrapper drops the **Gas** category — which is ~40% of PJM output. The
direct feed returns one row per hour × fuel; we pivot it to a wide frame (one
column per canonical fuel) and write yearly parquet files to data/system_gen/.

Feed: https://api.pjm.com/api/v1/gen_by_fuel  (hourly, ~2016-present)
Fuel types PJM publishes: Coal, Gas, Hydro, Multiple Fuels, Nuclear, Oil,
Other Renewables, Solar, Storage, Wind.
"""

from __future__ import annotations

import sys
import os
import time
from pathlib import Path
from datetime import date, timedelta

import requests
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pjm_core import paths, tz, credentials

# PJM published fuel_type label -> canonical column name.
FUEL_MAP = {
    "Gas": "gas",
    "Nuclear": "nuclear",
    "Coal": "coal",
    "Hydro": "hydro",
    "Wind": "wind",
    "Solar": "solar",
    "Oil": "oil",
    "Other Renewables": "other_renewables",
    "Multiple Fuels": "other",
    "Storage": "storage",
}
# Canonical column order for the wide store.
FUEL_COLS = ["gas", "nuclear", "coal", "hydro", "wind", "solar", "oil",
             "other_renewables", "other", "storage"]

_GEN_ENDPOINT = "https://api.pjm.com/api/v1/gen_by_fuel"
_DATE_CHUNK_DAYS = 45   # keep each API window well under the row cap
_ROW_COUNT = 50000      # PJM page size


def _gen_parquet(year: int) -> Path:
    return paths.SYSTEM_GEN_DIR / f"pjm_gen_by_fuel_{year}.parquet"


def _api_key() -> str:
    cfg = credentials.load_config()
    key = cfg.get("subscription_key") or os.environ.get("PJM_API_KEY", "")
    if not key:
        raise RuntimeError(
            "Missing PJM API subscription key. Set it on the API Keys page "
            "or in config.json / the PJM_API_KEY environment variable.")
    return key


def _fetch_window(api_key: str, start: date, end: date, log=print) -> list[dict]:
    """Fetch all gen_by_fuel rows for [start, end] inclusive (paginated)."""
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
            resp = requests.get(_GEN_ENDPOINT, params=params, headers=headers, timeout=60)
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
    return rows


def _to_wide(rows: list[dict]) -> pd.DataFrame:
    """Pivot long gen_by_fuel rows to one column per canonical fuel."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["datetime_beginning_ept"] = tz.to_naive_eastern(
        pd.to_datetime(df["datetime_beginning_ept"], errors="coerce"))
    df["fuel"] = df["fuel_type"].map(FUEL_MAP)
    df = df.dropna(subset=["datetime_beginning_ept", "fuel"])
    df["mw"] = pd.to_numeric(df["mw"], errors="coerce")
    # One row per hour × fuel; use mean so the midnight hour shared by two
    # adjacent date chunks collapses to a single (correct) value, not double.
    wide = (df.pivot_table(index="datetime_beginning_ept", columns="fuel",
                           values="mw", aggfunc="mean")
            .reset_index())
    wide.columns.name = None
    for c in FUEL_COLS:
        if c not in wide.columns:
            wide[c] = pd.NA
    keep = ["datetime_beginning_ept"] + FUEL_COLS
    return wide[keep].sort_values("datetime_beginning_ept").reset_index(drop=True)


def _fetch_year(year: int, log=print) -> pd.DataFrame:
    """Fetch one calendar year of PJM fuel mix, in date chunks."""
    api_key = _api_key()
    start = date(year, 1, 1)
    end = min(date(year, 12, 31), date.today() - timedelta(days=1))
    if start > end:
        return pd.DataFrame()

    all_rows: list[dict] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=_DATE_CHUNK_DAYS - 1), end)
        log(f"  {chunk_start} → {chunk_end} …")
        try:
            rows = _fetch_window(api_key, chunk_start, chunk_end, log=log)
            all_rows.extend(rows)
            log(f"    {len(rows):,} rows")
        except Exception as e:
            log(f"    chunk failed: {e}")
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(0.5)

    df = _to_wide(all_rows)
    if not df.empty:
        df["year"] = year
    return df


def update(years: list[int] | None = None, log=print) -> None:
    """Refresh PJM fuel mix data for the given years (default: current and last)."""
    paths.SYSTEM_GEN_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    if years is None:
        years = sorted({today.year, today.year - 1})

    for year in years:
        log(f"Year {year}:")
        df = _fetch_year(year, log=log)
        if df.empty:
            log(f"  No data for {year}.")
            continue
        p = _gen_parquet(year)
        df.to_parquet(p, index=False)
        log(f"  Saved {len(df):,} rows → {p.name}")


def load(years: list[int] | None = None) -> pd.DataFrame:
    """Load PJM fuel mix from the local parquet lake."""
    today = date.today()
    if years is None:
        years = list(range(2016, today.year + 1))
    frames = []
    for year in years:
        p = _gen_parquet(year)
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
    return df.sort_values("datetime_beginning_ept").reset_index(drop=True)


if __name__ == "__main__":
    update(log=print)
