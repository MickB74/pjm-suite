#!/usr/bin/env python3
"""PJM system generation by fuel — ETL using gridstatus.

gridstatus.PJM().get_fuel_mix() returns hourly generation by fuel type for
the PJM footprint. Results are written as yearly parquet files to
data/system_gen/.

Data source: gridstatus wraps the EIA-930 API (hourly) plus PJM's own
real-time fuel mix endpoint. Coverage: 2019-present.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path
from datetime import date, timedelta

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pjm_core import paths, tz

# Fuel canonical names (gridstatus PJM fuel mix labels)
FUEL_MAP = {
    "Natural Gas": "gas",
    "Nuclear": "nuclear",
    "Coal": "coal",
    "Hydro": "hydro",
    "Wind": "wind",
    "Solar": "solar",
    "Oil": "oil",
    "Other Renewables": "other_renewables",
    "Other": "other",
    "Storage": "storage",
}


def _gen_parquet(year: int) -> Path:
    return paths.SYSTEM_GEN_DIR / f"pjm_gen_by_fuel_{year}.parquet"


def _fetch_year(year: int, log=print) -> pd.DataFrame:
    """Fetch one calendar year of PJM fuel mix via gridstatus."""
    try:
        from gridstatus import PJM
    except ImportError:
        raise ImportError("gridstatus not installed. Run: pip install gridstatus")

    iso = PJM()
    start = date(year, 1, 1)
    end = min(date(year, 12, 31), date.today() - timedelta(days=1))
    if start > end:
        return pd.DataFrame()

    log(f"  Fetching PJM fuel mix {start} → {end} …")
    try:
        df = iso.get_fuel_mix(
            start=pd.Timestamp(start),
            end=pd.Timestamp(end) + pd.Timedelta(days=1),
        )
    except Exception as e:
        log(f"  gridstatus error: {e}")
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    # gridstatus returns a Time column (tz-aware) and fuel columns
    time_col = next((c for c in df.columns if c.lower() in ("time", "datetime")), None)
    if time_col is None:
        log("  Could not find time column in fuel mix result.")
        return pd.DataFrame()

    df = df.rename(columns={time_col: "datetime_beginning_ept"})
    df["datetime_beginning_ept"] = tz.to_naive_eastern(
        pd.to_datetime(df["datetime_beginning_ept"])
    )

    # Rename fuel columns to canonical names
    fuel_rename = {k: v for k, v in FUEL_MAP.items() if k in df.columns}
    df = df.rename(columns=fuel_rename)

    # Keep only known fuel columns plus the timestamp
    fuel_cols = list(FUEL_MAP.values())
    keep = ["datetime_beginning_ept"] + [c for c in fuel_cols if c in df.columns]
    df = df[keep].dropna(subset=["datetime_beginning_ept"])
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
        years = list(range(2019, today.year + 1))
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
