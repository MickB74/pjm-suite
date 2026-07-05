#!/usr/bin/env python3
"""PJM weather — hourly ERA5 reanalysis at major load centers (via Open-Meteo).

Weather is the exogenous driver of PJM peak load: the system's five coincident
peaks (5CP) that set capacity obligations land on the hottest, most humid summer
afternoons. This dataset samples ERA5 at each PJM metro load center and stores
hourly temperature, humidity, dewpoint and apparent temperature (heat-index
proxy), so peak days can be explained and 5CP days shown with their weather.

Source: **Open-Meteo Historical Archive API** (archive-api.open-meteo.com) —
ERA5 / ERA5-Land reanalysis, free, no API key. ERA5 lags real time by ~5 days.
The fetcher is source-pluggable (``source="era5"`` today); the official
Copernicus CDS API could be added as an alternate source later.

Local store (one row per city × hour, naive Eastern):

    data/weather/pjm_weather_hourly.parquet
    data/weather/pjm_weather_hourly.csv

Usage:
    python pjm_weather.py update
    python pjm_weather.py status
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pjm_core import paths, tz, weather_points

_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_HOURLY_VARS = "temperature_2m,relative_humidity_2m,dew_point_2m,apparent_temperature"
_ERA5_LAG_DAYS = 5          # ERA5 archive trails real time
OVERLAP_DAYS = 5
STALE_AFTER_DAYS = 7

_COL_MAP = {
    "temperature_2m": "temp_f",
    "relative_humidity_2m": "rh_pct",
    "dew_point_2m": "dewpoint_f",
    "apparent_temperature": "apparent_f",
}
VALUE_COLS = list(_COL_MAP.values())
KEEP_COLS = ["datetime_beginning_ept", "city", "zone"] + VALUE_COLS


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
# Fetch (Open-Meteo ERA5)
# ---------------------------------------------------------------------------

def _fetch_city(city: str, zone: str, lat: float, lon: float,
                start: date, end: date, log=print, url: str = _ARCHIVE_URL) -> pd.DataFrame:
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": _HOURLY_VARS,
        "temperature_unit": "fahrenheit",
        "timezone": "America/New_York",
    }
    for attempt in range(5):
        resp = requests.get(url, params=params, timeout=90)
        if resp.status_code == 429:
            wait = 10.0 * (2 ** attempt)
            log(f"      rate-limited, waiting {wait:.0f}s …")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        break
    else:
        log("      rate-limited after 5 retries, skipping city")
        return pd.DataFrame()

    hourly = resp.json().get("hourly", {})
    if not hourly or not hourly.get("time"):
        return pd.DataFrame()
    df = pd.DataFrame(hourly).rename(columns=_COL_MAP)
    # Open-Meteo returns naive local (Eastern) ISO strings — matches store convention.
    df["datetime_beginning_ept"] = pd.to_datetime(df["time"], errors="coerce")
    df["city"] = city
    df["zone"] = zone
    for c in VALUE_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    keep = [c for c in KEEP_COLS if c in df.columns]
    return df[keep].dropna(subset=["datetime_beginning_ept"])


def fetch(start: date, end: date, log=print, source: str = "era5") -> pd.DataFrame:
    """Fetch hourly weather for all load centers over [start, end] inclusive.

    ``source="era5"`` uses the ERA5 reanalysis archive (authoritative, ~5-day
    lag). ``source="recent"`` uses the near-real-time forecast endpoint, which
    reports the last several days of hourly actuals — used to fill the ERA5 lag
    gap so the newest peaks show weather before the reanalysis settles.
    """
    if source == "era5":
        url = _ARCHIVE_URL
    elif source == "recent":
        url = _FORECAST_URL
    else:
        raise ValueError(f"Unknown weather source {source!r} "
                         "(only 'era5' and 'recent' supported).")
    pts = weather_points.points()
    frames = []
    for _, row in pts.iterrows():
        log(f"    {row['city']} ({row['zone']}) {start} → {end} …")
        try:
            df = _fetch_city(row["city"], row["zone"], row["lat"], row["lon"],
                             start, end, log=log, url=url)
            if not df.empty:
                frames.append(df)
                log(f"      {len(df):,} rows")
            else:
                log("      0 rows")
        except Exception as e:
            log(f"      failed: {e}")
        time.sleep(1.0)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def load_store() -> pd.DataFrame:
    p = paths.WEATHER_PARQUET
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def save_store(df: pd.DataFrame, write_csv: bool = True) -> None:
    paths.WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    df = df.sort_values(["datetime_beginning_ept", "city"]).reset_index(drop=True)
    df.to_parquet(paths.WEATHER_PARQUET, index=False)
    if write_csv:
        df.to_csv(paths.WEATHER_CSV, index=False)


def _dedup_keys() -> list[str]:
    return ["datetime_beginning_ept", "city"]


def read_state() -> dict:
    p = paths.WEATHER_STATE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def write_state(state: dict) -> None:
    paths.WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    paths.WEATHER_STATE.write_text(json.dumps(state, indent=2, default=str))


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


def _load_store_min() -> date | None:
    """Earliest date in the load store, so weather can auto-align to load coverage."""
    try:
        p = paths.LOAD_PARQUET
        if not p.exists():
            return None
        d = pd.read_parquet(p, columns=["datetime_beginning_ept"])["datetime_beginning_ept"]
        return pd.to_datetime(d).min().date()
    except Exception:
        return None


def _default_start() -> date:
    """Align weather backfill to the load store (or 2 years back as a fallback)."""
    lo = _load_store_min()
    if lo is not None:
        return lo
    return date(tz.now_eastern().year - 2, 1, 1)


def store_summary() -> dict:
    info = {"exists": paths.WEATHER_PARQUET.exists(), "rows": 0,
            "start": None, "end": None, "cities": [],
            "days_since_update": days_since_update()}
    if not info["exists"]:
        return info
    try:
        df = load_store()
        info["rows"] = len(df)
        if not df.empty:
            info["start"] = str(df["datetime_beginning_ept"].min())
            info["end"] = str(df["datetime_beginning_ept"].max())
            info["cities"] = sorted(df["city"].dropna().unique().tolist())
    except Exception as e:
        info["error"] = str(e)
    return info


def update(progress_callback=None, start: date | None = None) -> dict:
    """Fetch new weather and merge into the local store (incremental)."""
    log = _make_logger(progress_callback)
    existing = load_store()
    end = tz.now_eastern().date() - timedelta(days=_ERA5_LAG_DAYS)

    if start is None:
        start = _default_start()
    if existing.empty:
        fetch_start = start
        log(f"No local data. Backfilling weather from {start} → {end}.")
    else:
        have_max = pd.to_datetime(existing["datetime_beginning_ept"]).max().date()
        # Refresh from the later of (backfill start) and the recent overlap window.
        fetch_start = max(start, have_max - timedelta(days=OVERLAP_DAYS))
        log(f"Existing data through {have_max}. Fetching {fetch_start} → {end}.")

    if fetch_start > end:
        log("Nothing new to fetch.")
        fetched = pd.DataFrame()
    else:
        fetched = fetch(fetch_start, end, log=log)

    # Fill the ERA5 lag gap (end → yesterday) with near-real-time forecast data
    # so the newest peak days show weather. True ERA5 overwrites it on overlap.
    recent_start = end + timedelta(days=1)
    recent_end = tz.now_eastern().date() - timedelta(days=1)
    if recent_end >= recent_start:
        log(f"Filling ERA5 lag gap {recent_start} → {recent_end} "
            "from near-real-time forecast API.")
        recent = fetch(recent_start, recent_end, log=log, source="recent")
    else:
        recent = pd.DataFrame()

    # Order matters for the keep="last" de-dup below: the authoritative ERA5
    # rows (`fetched`) come after the provisional `recent` fill, so any hour
    # present in both keeps the ERA5 value.
    frames = [df for df in (recent, existing, fetched) if not df.empty]
    combined = pd.concat(frames, ignore_index=True) if frames else existing
    if combined.empty:
        raise RuntimeError("No weather data available — nothing fetched and no existing store.")

    before = len(combined)
    combined = combined.drop_duplicates(subset=_dedup_keys(), keep="last")
    log(f"Merged: {before:,} → {len(combined):,} rows after de-duplication.")

    save_store(combined)
    summary = {
        "rows": len(combined),
        "start": str(combined["datetime_beginning_ept"].min()),
        "end": str(combined["datetime_beginning_ept"].max()),
        "cities": sorted(combined["city"].dropna().unique().tolist()),
        "parquet": str(paths.WEATHER_PARQUET),
    }
    write_state({"last_success": tz.now_eastern().isoformat(), **summary})
    log(f"\nDone. {summary['rows']:,} rows saved.")
    return summary


# ---------------------------------------------------------------------------
# Access layer
# ---------------------------------------------------------------------------

def load(start=None, end_excl=None, cities=None) -> pd.DataFrame:
    df = load_store()
    if df.empty:
        return df
    df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
    if cities:
        df = df[df["city"].isin(cities)]
    if start is not None:
        df = df[df["datetime_beginning_ept"] >= pd.Timestamp(start)]
    if end_excl is not None:
        df = df[df["datetime_beginning_ept"] < pd.Timestamp(end_excl)]
    return df.sort_values(["datetime_beginning_ept", "city"]).reset_index(drop=True)


def _pop_weighted(df: pd.DataFrame, zone: str | None = None) -> pd.DataFrame:
    """Collapse a per-city hourly frame to one pop-weighted row per hour.

    Shared by :func:`weighted_temp` (stored ERA5) and :func:`forecast_weighted`
    (upcoming forecast). ``zone`` restricts to a single PJM zone's cities.
    """
    if df.empty:
        return pd.DataFrame(columns=["datetime_beginning_ept"] + VALUE_COLS)
    pts = weather_points.points()[["city", "pop_weight"]]
    df = df.merge(pts, on="city", how="left")
    df["pop_weight"] = df["pop_weight"].fillna(1.0)
    if zone:
        df = df[df["zone"] == zone]
        if df.empty:
            return pd.DataFrame(columns=["datetime_beginning_ept"] + VALUE_COLS)

    # Population-weighted average per hour, ignoring NaN readings per column
    # (vectorised — a city with a missing value drops out of that hour's weight).
    g = df["datetime_beginning_ept"]
    res = pd.DataFrame({"datetime_beginning_ept": sorted(df["datetime_beginning_ept"].unique())})
    res = res.set_index("datetime_beginning_ept")
    for c in VALUE_COLS:
        w = df["pop_weight"].where(df[c].notna(), 0.0)
        num = (df[c].fillna(0.0) * w).groupby(g).sum()
        den = w.groupby(g).sum().replace(0.0, float("nan"))
        res[c] = num / den
    return res.reset_index().sort_values("datetime_beginning_ept").reset_index(drop=True)


def weighted_temp(start=None, end_excl=None, zone: str | None = None) -> pd.DataFrame:
    """Population-weighted PJM (or single-zone) hourly temperature & apparent temp.

    Returns columns [datetime_beginning_ept, temp_f, apparent_f, rh_pct]. When
    ``zone`` is given, only that zone's cities are averaged (unweighted if a
    single city), otherwise all load centers weighted by metro population.
    """
    return _pop_weighted(load(start=start, end_excl=end_excl), zone=zone)


def forecast_weighted(days: int = 16, zone: str | None = None) -> pd.DataFrame:
    """Population-weighted **forecast** temperature for the next ``days`` days.

    Pulls the Open-Meteo forecast endpoint (near-real-time model, up to +16
    days) for every load center and collapses to one pop-weighted row per hour,
    matching :func:`weighted_temp`'s columns. This is live forecast data — it is
    *not* written to the ERA5 store; the 5CP peak predictor consumes it in
    memory. Returns empty on any fetch failure so callers can degrade cleanly.
    """
    from pjm_core import tz
    days = max(1, min(int(days), 16))     # Open-Meteo forecast horizon caps at 16
    today = tz.now_eastern().date()
    try:
        df = fetch(today, today + timedelta(days=days - 1),
                   log=lambda *_: None, source="recent")
    except Exception:
        return pd.DataFrame(columns=["datetime_beginning_ept"] + VALUE_COLS)
    return _pop_weighted(df, zone=zone)


def main(argv=None):
    parser = argparse.ArgumentParser(description="PJM ERA5 weather downloader (Open-Meteo).")
    sub = parser.add_subparsers(dest="cmd")
    up = sub.add_parser("update", help="Fetch latest data and update the local store.")
    up.add_argument("--auto", action="store_true",
                    help="Quiet mode for scheduled jobs; skips if data is fresh.")
    up.add_argument("--start", default=None, help="Backfill start date (YYYY-MM-DD).")
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
            start = date.fromisoformat(args.start) if getattr(args, "start", None) else None
            update(start=start)
            return 0
        except Exception as e:
            print(f"Update failed: {e}")
            return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
