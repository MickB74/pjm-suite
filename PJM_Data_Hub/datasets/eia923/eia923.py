#!/usr/bin/env python3
"""EIA Form 923 ETL for PJM-footprint plants.

Downloads annual ZIP files from EIA, extracts plant-level monthly net
generation (MWh) and fuel consumption (MMBtu), and writes yearly parquets
to data/eia923/.

EIA-923 covers all US utility-scale plants. We filter to PJM states:
  DE, IL, IN, KY, MD, MI, NJ, NC, OH, PA, TN, VA, WV, DC.

Data source: https://www.eia.gov/electricity/data/eia923/
Lag: ~6 months (prior-year final released ~October).
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from datetime import date

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pjm_core import paths

# PJM footprint states
PJM_STATES = {
    "DE", "IL", "IN", "KY", "MD", "MI", "NJ", "NC", "OH", "PA", "TN", "VA", "WV", "DC",
}

EIA923_BASE = "https://www.eia.gov/electricity/data/eia923/archive/xls"
HTTP_TIMEOUT = 120

# EIA-923 column mappings (vary slightly by year)
_NETGEN_COLS = [
    "Net Generation\n(Megawatthours)",
    "Net Generation (Megawatthours)",
    "NETGEN",
]
_STATE_COLS = ["Plant State", "STATE"]
_PLANT_COLS = ["Plant Id", "Plant ID", "PLANT_ID"]
_PLANT_NAME_COLS = ["Plant Name", "PLANT_NAME"]
_FUEL_COLS = ["Reported\nFuel Type Code", "Reported Fuel Type Code", "FUEL_TYPE_CODE"]
_MONTH_COLS = [f"Netgen_{m}" for m in range(1, 13)]  # wide format alternative


def _eia923_url(year: int) -> str:
    return f"{EIA923_BASE}/f923_{year}.zip"


def _download_zip(year: int, log=print) -> bytes | None:
    cache = paths.EIA_RAW_DIR / f"eia923_{year}.zip"
    if cache.exists():
        log(f"  Using cached {cache.name}")
        return cache.read_bytes()
    url = _eia923_url(year)
    log(f"  Downloading {url} …")
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        paths.EIA_RAW_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(r.content)
        return r.content
    except Exception as e:
        log(f"  Download failed: {e}")
        return None


def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _parse_zip(content: bytes, year: int, log=print) -> pd.DataFrame:
    """Extract plant monthly generation from an EIA-923 ZIP."""
    frames = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        xlsx_files = [n for n in zf.namelist() if n.lower().endswith((".xlsx", ".xls"))]
        for name in xlsx_files:
            if "page_1" not in name.lower() and "generation" not in name.lower():
                # EIA-923 has multiple sheets; Page 1 has generation & fuel
                # Try all xlsx files and filter by column presence
                pass
            try:
                data = zf.read(name)
                # Try header rows 0, 4, 5 (EIA format varies by year)
                for hdr in (4, 5, 0):
                    try:
                        df = pd.read_excel(io.BytesIO(data), sheet_name=0, header=hdr)
                        df.columns = [str(c).strip() for c in df.columns]
                        state_col = _find_col(df, _STATE_COLS)
                        if state_col:
                            frames.append((df, state_col, name))
                            break
                    except Exception:
                        continue
            except Exception as e:
                log(f"    Could not parse {name}: {e}")

    if not frames:
        log("  No parseable generation sheet found in ZIP.")
        return pd.DataFrame()

    # Use the first sheet with a state column
    df, state_col, fname = frames[0]
    log(f"  Parsing {fname} (header auto-detected)")

    plant_col = _find_col(df, _PLANT_COLS)
    name_col = _find_col(df, _PLANT_NAME_COLS)
    fuel_col = _find_col(df, _FUEL_COLS)
    netgen_col = _find_col(df, _NETGEN_COLS)

    # Filter to PJM states
    df[state_col] = df[state_col].astype(str).str.strip().str.upper()
    df = df[df[state_col].isin(PJM_STATES)].copy()
    if df.empty:
        return pd.DataFrame()

    if netgen_col:
        # Annual total column — melt to monthly is not available in this format
        out = pd.DataFrame()
        out["plant_id"] = df[plant_col].astype(str) if plant_col else ""
        out["plant_name"] = df[name_col].astype(str) if name_col else ""
        out["state"] = df[state_col]
        out["fuel_type"] = df[fuel_col].astype(str).str.strip() if fuel_col else ""
        out["year"] = year
        out["net_generation_mwh"] = pd.to_numeric(df[netgen_col], errors="coerce")
        return out.dropna(subset=["net_generation_mwh"])

    return pd.DataFrame()


def fetch_year(year: int, log=print) -> pd.DataFrame:
    content = _download_zip(year, log=log)
    if not content:
        return pd.DataFrame()
    return _parse_zip(content, year, log=log)


def update(years: list[int] | None = None, log=print) -> None:
    paths.EIA_DIR.mkdir(parents=True, exist_ok=True)
    paths.EIA_RAW_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    if years is None:
        # EIA-923 lags ~6 months; include last 2 full years
        years = [today.year - 2, today.year - 1]

    for year in years:
        log(f"EIA-923 year {year}:")
        df = fetch_year(year, log=log)
        if df.empty:
            log(f"  No data for {year}.")
            continue
        p = paths.EIA_DIR / f"eia923_pjm_{year}.parquet"
        df.to_parquet(p, index=False)
        log(f"  Saved {len(df):,} rows → {p.name}")


def load(years: list[int] | None = None) -> pd.DataFrame:
    today = date.today()
    if years is None:
        years = list(range(2015, today.year))
    frames = [pd.read_parquet(p) for year in years
              if (p := paths.EIA_DIR / f"eia923_pjm_{year}.parquet").exists()]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


if __name__ == "__main__":
    update(log=print)
