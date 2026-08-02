#!/usr/bin/env python3
"""EIA Form 860M ETL — monthly generator inventory snapshots for PJM plants.

Annual EIA-860 lags by ~10 months (2025's file lands mid-2026, 2026's mid-2027).
Between annual releases the "capacity as of {year}" question falls back to the
last available annual vintage, which stops reflecting real additions and
retirements. EIA-860M is the early-release monthly form that closes that gap:
it publishes ~1–2 months after each reference month with the *current*
operating fleet (plus planned adds and retirements).

One parquet per monthly snapshot:

    data/eia860m/eia860m_pjm_YYYY_MM.parquet

Same schema as `datasets.eia860.eia860`'s output — plant_id, plant_name, state,
energy_source, nameplate_mw, summer_mw, winter_mw, n_generators, fuel_group,
plus `reference_year` / `reference_month` (which snapshot this row belongs to).
Callers can join to 923 the same way they join annual 860.

Data source: https://www.eia.gov/electricity/data/eia860m/
Retention: keeps the most recent few snapshots — old ones would just be
historical noise for our use case (capacity for a specific point in time).
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from curl_cffi import requests as cf

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pjm_core import paths
from datasets.eia923.eia923 import PJM_STATES, fuel_group


_BASE = "https://www.eia.gov/electricity/data/eia860m/xls"
_ARCHIVE_BASE = "https://www.eia.gov/electricity/data/eia860m/archive/xls"
_MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december"]

_HTTP_TIMEOUT = 90
_HDRS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 "
                  "Safari/605.1.15",
    "Referer": "https://www.eia.gov/electricity/data/eia860m/",
}


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------

def _candidate_urls(year: int, month: int) -> list[str]:
    """Latest snapshot lives under /xls/; anything older is moved to /archive/xls/."""
    name = f"{_MONTH_NAMES[month - 1]}_generator{year}.xlsx"
    return [f"{_BASE}/{name}", f"{_ARCHIVE_BASE}/{name}"]


def _download(year: int, month: int, log=print) -> bytes | None:
    """Try the current path first, then the archive. Never raises."""
    for url in _candidate_urls(year, month):
        try:
            log(f"  fetching {url} …")
            r = cf.get(url, headers=_HDRS, timeout=_HTTP_TIMEOUT,
                       impersonate="safari17_0")
            if r.status_code == 200 and r.content[:2] == b"PK":
                return r.content
            log(f"    HTTP {r.status_code} ({len(r.content)}b)")
        except Exception as e:  # noqa: BLE001
            log(f"    {type(e).__name__}: {e}")
        time.sleep(1.5)
    return None


def _parse(content: bytes, year: int, month: int, log=print) -> pd.DataFrame:
    """Read the Operating sheet, filter to PJM states, aggregate to plant×fuel."""
    xl = pd.ExcelFile(io.BytesIO(content))
    if "Operating" not in xl.sheet_names:
        log(f"    Operating sheet missing (found {xl.sheet_names})")
        return pd.DataFrame()
    # The first two rows are title/notes; column names on row index 2.
    df = pd.read_excel(xl, sheet_name="Operating", header=2)

    # Column names are stable in 860M (post-2015 vintages) but keep a small
    # remap in case EIA renames "Plant ID" → "Plant Code" again.
    remap = {
        "Plant ID": "plant_id", "Plant Code": "plant_id",
        "Plant Name": "plant_name",
        "Plant State": "state", "State": "state",
        "Nameplate Capacity (MW)": "nameplate_mw",
        "Net Summer Capacity (MW)": "summer_mw",
        "Net Winter Capacity (MW)": "winter_mw",
        "Energy Source Code": "energy_source",
    }
    df = df.rename(columns={k: v for k, v in remap.items() if k in df.columns})
    need = {"plant_id", "state", "nameplate_mw", "energy_source"}
    missing = need - set(df.columns)
    if missing:
        log(f"    schema drift — missing {missing}")
        return pd.DataFrame()

    # Coerce numerics; EIA sometimes uses "." for missing.
    for c in ("plant_id", "nameplate_mw", "summer_mw", "winter_mw"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["plant_id", "state", "energy_source", "nameplate_mw"])
    df = df[df["state"].isin(PJM_STATES)].copy()
    if df.empty:
        log("    no PJM-footprint rows")
        return df

    df["plant_id"] = df["plant_id"].astype(int)
    df["fuel_group"] = df["energy_source"].map(fuel_group)
    grouped = (df.groupby(["plant_id", "plant_name", "state", "energy_source",
                            "fuel_group"], as_index=False, dropna=False)
               .agg(nameplate_mw=("nameplate_mw", "sum"),
                    summer_mw=("summer_mw", "sum"),
                    winter_mw=("winter_mw", "sum"),
                    n_generators=("plant_id", "size")))
    grouped["reference_year"] = year
    grouped["reference_month"] = month
    return grouped


def fetch_month(year: int, month: int, log=print) -> pd.DataFrame:
    """Download and parse one 860M snapshot. Returns empty on any failure."""
    content = _download(year, month, log=log)
    if content is None:
        log(f"  {_MONTH_NAMES[month - 1]}-{year}: not available")
        return pd.DataFrame()
    return _parse(content, year, month, log=log)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _snap_path(year: int, month: int) -> Path:
    return paths.EIA860M_DIR / f"eia860m_pjm_{year}_{month:02d}.parquet"


def available() -> list[tuple[int, int]]:
    """(year, month) tuples we already have on disk, oldest first."""
    if not paths.EIA860M_DIR.exists():
        return []
    out = []
    for p in paths.EIA860M_DIR.glob("eia860m_pjm_*.parquet"):
        try:
            _, ym = p.stem.rsplit("pjm_", 1)
            y, m = ym.split("_")
            out.append((int(y), int(m)))
        except Exception:
            continue
    return sorted(out)


def latest() -> tuple[int, int] | None:
    got = available()
    return got[-1] if got else None


def load(year: int | None = None, month: int | None = None) -> pd.DataFrame:
    """One snapshot as a DataFrame. Without args, returns the latest.

    Passing ``year`` alone returns the newest snapshot within that year (so
    year=2025 selects Dec-2025 if we have it, else the latest 2025 month we
    do). Missing snapshot → empty DataFrame; callers should fall back to the
    annual 860 store.
    """
    if year is None:
        got = latest()
        if got is None:
            return pd.DataFrame()
        year, month = got
    elif month is None:
        yrs = [(y, m) for y, m in available() if y == year]
        if not yrs:
            return pd.DataFrame()
        month = max(m for _, m in yrs)
    p = _snap_path(year, month)
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def latest_before(year: int, month: int = 12) -> tuple[int, int] | None:
    """Newest snapshot on or before (year, month), or None."""
    key = (year, month)
    eligible = [ym for ym in available() if ym <= key]
    return max(eligible) if eligible else None


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def _months_back(n: int) -> list[tuple[int, int]]:
    """Last n (year, month) tuples, most recent first — 860M lags reality by
    ~1-2 months so today's month is usually not yet published."""
    today = date.today()
    # Start from last month; skip it if the file isn't out yet — _download will
    # simply return None and we move on.
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
        out.append((y, m))
    return out


def update(months_back: int = 4, log=print) -> dict:
    """Pull the last `months_back` monthly snapshots, keep whatever comes back.

    Idempotent: an existing snapshot is skipped unless it's the newest month
    (which we always refresh in case EIA revised it). Retention is not enforced
    here — old snapshots are useful for tracking additions over time.
    """
    paths.EIA860M_DIR.mkdir(parents=True, exist_ok=True)
    want = _months_back(months_back)
    log(f"EIA-860M: refreshing {len(want)} months back to "
        f"{_MONTH_NAMES[want[-1][1] - 1]}-{want[-1][0]}")
    got: list[tuple[int, int]] = []
    for i, (y, m) in enumerate(want):
        path = _snap_path(y, m)
        # Skip existing files EXCEPT the newest month (which EIA may revise).
        if path.exists() and i > 0:
            log(f"  {_MONTH_NAMES[m - 1]}-{y}: cached")
            got.append((y, m))
            continue
        df = fetch_month(y, m, log=log)
        if df.empty:
            continue
        df.to_parquet(path, index=False)
        got.append((y, m))
        log(f"  {_MONTH_NAMES[m - 1]}-{y}: {len(df)} plant×fuel rows "
            f"({df['nameplate_mw'].sum():,.0f} MW)")

    if not got:
        raise RuntimeError("EIA-860M: no snapshots fetched.")
    _latest = max(got)   # NOT got[-1] — got is in fetch order (newest-first)
    summary = {
        "snapshots": [f"{y}-{m:02d}" for y, m in sorted(got)],
        "latest": f"{_latest[0]}-{_latest[1]:02d}",
        "count": len(got),
    }
    state = {"last_success": pd.Timestamp.now(tz="UTC").isoformat(), **summary}
    paths.EIA860M_STATE.write_text(json.dumps(state, indent=2))
    log(f"Done. {len(got)} snapshots on disk, latest {summary['latest']}.")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def days_since_update() -> float | None:
    if not paths.EIA860M_STATE.exists():
        return None
    try:
        ts = json.loads(paths.EIA860M_STATE.read_text()).get("last_success")
        return (pd.Timestamp.now(tz="UTC") - pd.Timestamp(ts)).total_seconds() / 86400.0
    except Exception:
        return None


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["update", "status"])
    p.add_argument("--months-back", type=int, default=4)
    args = p.parse_args()
    if args.cmd == "update":
        update(months_back=args.months_back)
    else:
        got = available()
        print(f"snapshots on disk: {len(got)}")
        for y, m in got:
            print(f"  {y}-{m:02d}")
