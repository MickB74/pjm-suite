#!/usr/bin/env python3
"""EIA Form 923 ETL for PJM-footprint plants.

Downloads annual ZIP files from EIA, extracts plant-level MONTHLY net
generation (MWh) — one row per plant × fuel × month — and writes yearly
parquets to data/eia923/. Older file formats without monthly columns fall
back to annual rows (month = <NA>).

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

# Finalized years live under archive/; the in-progress current year is only at
# the non-archive path (a rolling year-to-date file EIA refreshes monthly).
EIA923_BASE = "https://www.eia.gov/electricity/data/eia923/archive/xls"
EIA923_CURRENT_BASE = "https://www.eia.gov/electricity/data/eia923/xls"
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
# Balancing-authority code — the authoritative grid-operator tag. PJM plants
# carry "PJM"; lets us tell true PJM units from same-state non-PJM ones (Duke
# Carolinas, MISO, TVA, …).
_BA_COLS = ["Balancing\nAuthority Code", "Balancing Authority Code", "BA_CODE"]

# Monthly net-generation headers: "Netgen\nJanuary" (2011+) or "NETGEN_JAN" (older).
_MONTH_NAMES = ["january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december"]

# EIA-923 reported fuel type codes -> readable source groups.
FUEL_GROUPS = {
    "Gas":        {"NG", "OG", "PG", "BFG", "SGP"},
    "Coal":       {"BIT", "SUB", "LIG", "RC", "WC", "SGC", "SC", "ANT"},
    "Nuclear":    {"NUC"},
    "Oil":        {"DFO", "RFO", "KER", "JF", "PC", "WO"},
    "Wind":       {"WND"},
    "Solar":      {"SUN"},
    "Hydro":      {"WAT"},
    "Storage":    {"MWH"},
    "Biomass":    {"WDS", "WDL", "BLQ", "AB", "MSW", "MSB", "MSN", "LFG",
                   "OBG", "OBS", "OBL", "TDF", "SLW"},
    "Geothermal": {"GEO"},
}
_CODE_TO_GROUP = {code: g for g, codes in FUEL_GROUPS.items() for code in codes}


def fuel_group(code) -> str:
    """Readable source group ('Gas', 'Nuclear', …) for an EIA fuel code."""
    return _CODE_TO_GROUP.get(str(code).strip().upper(), "Other")


def _candidate_urls(year: int) -> list[str]:
    """URLs to try for a year's ZIP: archive (final) then current (in-progress)."""
    return [f"{EIA923_BASE}/f923_{year}.zip",
            f"{EIA923_CURRENT_BASE}/f923_{year}.zip"]


def _download_zip(year: int, log=print) -> bytes | None:
    cache = paths.EIA_RAW_DIR / f"eia923_{year}.zip"
    # A cached copy of the still-updating current year goes stale each month, so
    # only trust the cache for finalized (past) years.
    if cache.exists() and year < date.today().year:
        log(f"  Using cached {cache.name}")
        return cache.read_bytes()
    for url in _candidate_urls(year):
        log(f"  Downloading {url} …")
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            # The archive path serves a 200 HTML "not found" page for years that
            # aren't finalized yet, so confirm real ZIP bytes before accepting.
            if r.content[:2] != b"PK":
                log(f"    {url.rsplit('/', 2)[-2]} path returned non-ZIP content, skipping.")
                continue
            paths.EIA_RAW_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(r.content)
            return r.content
        except Exception as e:
            log(f"    {url.rsplit('/', 2)[-2]} path failed: {e}")
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
    ba_col = _find_col(df, _BA_COLS)

    # Filter to PJM states
    df[state_col] = df[state_col].astype(str).str.strip().str.upper()
    df = df[df[state_col].isin(PJM_STATES)].copy()
    if df.empty:
        return pd.DataFrame()

    base = pd.DataFrame({
        "plant_id": df[plant_col].astype(str) if plant_col else "",
        "plant_name": df[name_col].astype(str) if name_col else "",
        "state": df[state_col],
        "ba_code": (df[ba_col].astype(str).str.strip().str.upper() if ba_col else ""),
        "fuel_type": df[fuel_col].astype(str).str.strip() if fuel_col else "",
    })
    base["year"] = year

    # Prefer the monthly Netgen columns; melt to one row per plant × fuel × month.
    norm = {c: str(c).strip().lower().replace("\n", " ") for c in df.columns}
    month_cols = {}
    for c, n in norm.items():
        for m, mon in enumerate(_MONTH_NAMES, start=1):
            if n == f"netgen {mon}" or n == f"netgen_{mon[:3]}":
                month_cols[m] = c
    if month_cols:
        monthly = []
        for m, c in sorted(month_cols.items()):
            fm = base.copy()
            fm["month"] = m
            fm["net_generation_mwh"] = pd.to_numeric(df[c], errors="coerce")
            monthly.append(fm)
        out = pd.concat(monthly, ignore_index=True).dropna(subset=["net_generation_mwh"])
        out["month"] = out["month"].astype("Int64")
        return out.reset_index(drop=True)

    if netgen_col:
        # Annual total only — older formats without monthly columns.
        out = base.copy()
        out["month"] = pd.array([pd.NA] * len(out), dtype="Int64")
        out["net_generation_mwh"] = pd.to_numeric(df[netgen_col], errors="coerce")
        return out.dropna(subset=["net_generation_mwh"]).reset_index(drop=True)

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
        # Two finalized years plus the in-progress current year (rolling
        # monthly file, ~2-month lag).
        years = [today.year - 2, today.year - 1, today.year]

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
        years = list(range(2015, today.year + 1))
    frames = [pd.read_parquet(p) for year in years
              if (p := paths.EIA_DIR / f"eia923_pjm_{year}.parquet").exists()]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


if __name__ == "__main__":
    update(log=print)
