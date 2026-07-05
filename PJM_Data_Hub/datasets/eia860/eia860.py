#!/usr/bin/env python3
"""EIA Form 860 ETL — plant nameplate / summer capacity for PJM-footprint plants.

EIA-923 (generation) tells us how many MWh a plant produced; it does NOT carry
the plant's **capacity** (MW). Capacity is what the PJM RPM/capacity market pays
for, so to estimate a plant's capacity revenue we need its MW rating — that
lives in EIA Form 860.

This pulls the annual EIA-860 ZIP, reads the "3_1_Generator" workbook (one row
per generator), sums generator capacities up to plant × energy-source, filters
to PJM-footprint states, and caches one parquet per year:

    data/eia860/eia860_pjm_<year>.parquet

Columns: year, plant_id, plant_name, state, energy_source (EIA code),
         technology, nameplate_mw, summer_mw, winter_mw, n_generators.

Data source: https://www.eia.gov/electricity/data/eia860/
Lag: ~10 months (prior-year final released ~September). Only *operating* units
(Status == "OP") are kept.
"""

from __future__ import annotations

import io
import sys
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pjm_core import paths
from datasets.eia923.eia923 import PJM_STATES, fuel_group  # reuse footprint + fuel map

EIA860_BASE = "https://www.eia.gov/electricity/data/eia860/archive/xls"
EIA860_CURRENT_BASE = "https://www.eia.gov/electricity/data/eia860/xls"
HTTP_TIMEOUT = 120

# Column name candidates (EIA-860 headers vary slightly by year).
_PLANT_COLS = ["Plant Code", "PLANT_CODE", "Plant Id"]
_PLANT_NAME_COLS = ["Plant Name", "PLANT_NAME"]
_STATE_COLS = ["State", "STATE", "Plant State"]
_NAMEPLATE_COLS = ["Nameplate Capacity (MW)", "Nameplate Capacity (MWs)", "NAMEPLATE"]
_SUMMER_COLS = ["Summer Capacity (MW)", "Summer Capacity (MWs)"]
_WINTER_COLS = ["Winter Capacity (MW)", "Winter Capacity (MWs)"]
_ENERGY_SRC_COLS = ["Energy Source 1", "Energy Source Code", "EnergySource1"]
_TECH_COLS = ["Technology", "Prime Mover"]
_STATUS_COLS = ["Status", "STATUS"]


def _candidate_urls(year: int) -> list[str]:
    return [f"{EIA860_BASE}/eia860{year}.zip",
            f"{EIA860_CURRENT_BASE}/eia860{year}.zip"]


def _download_zip(year: int, log=print) -> bytes | None:
    cache = paths.EIA860_RAW_DIR / f"eia860_{year}.zip"
    if cache.exists() and year < date.today().year:
        log(f"  Using cached {cache.name}")
        return cache.read_bytes()
    for url in _candidate_urls(year):
        log(f"  Downloading {url} …")
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            if r.content[:2] != b"PK":
                log(f"    {url.rsplit('/', 2)[-2]} path returned non-ZIP, skipping.")
                continue
            paths.EIA860_RAW_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(r.content)
            return r.content
        except Exception as e:  # noqa: BLE001
            log(f"    {url.rsplit('/', 2)[-2]} path failed: {e}")
    return None


def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False)
        .replace({".": None, "": None, "nan": None, " ": None}),
        errors="coerce")


def _parse_zip(content: bytes, year: int, log=print) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        members = [n for n in zf.namelist()
                   if n.lower().endswith((".xlsx", ".xls"))
                   and "3_1_generator" in n.lower().replace(" ", "_")]
        if not members:  # fall back: any workbook mentioning "generator"
            members = [n for n in zf.namelist()
                       if n.lower().endswith((".xlsx", ".xls")) and "generator" in n.lower()]
        if not members:
            log("  No generator workbook found in EIA-860 ZIP.")
            return pd.DataFrame()
        data = zf.read(members[0])
        log(f"  Parsing {members[0]}")

    # EIA-860 sheets have a 1-row title before the header; find the header row
    # whose cells include "Plant Code".
    raw = pd.read_excel(io.BytesIO(data), sheet_name="Operable"
                        if "Operable" in pd.ExcelFile(io.BytesIO(data)).sheet_names else 0,
                        header=None)
    first_hits = raw.apply(lambda r: r.astype(str).str.strip().isin(_PLANT_COLS).any(), axis=1)
    hdr = first_hits.index[first_hits][0] if first_hits.any() else 1
    df = pd.read_excel(io.BytesIO(data),
                       sheet_name="Operable"
                       if "Operable" in pd.ExcelFile(io.BytesIO(data)).sheet_names else 0,
                       header=hdr)
    df.columns = [str(c).strip() for c in df.columns]

    plant_col = _find_col(df, _PLANT_COLS)
    state_col = _find_col(df, _STATE_COLS)
    if not plant_col or not state_col:
        log("  Missing Plant Code / State columns.")
        return pd.DataFrame()

    df[state_col] = df[state_col].astype(str).str.strip().str.upper()
    df = df[df[state_col].isin(PJM_STATES)].copy()
    status_col = _find_col(df, _STATUS_COLS)
    if status_col:
        df = df[df[status_col].astype(str).str.strip().str.upper() == "OP"]
    if df.empty:
        return pd.DataFrame()

    name_col = _find_col(df, _PLANT_NAME_COLS)
    np_col = _find_col(df, _NAMEPLATE_COLS)
    su_col = _find_col(df, _SUMMER_COLS)
    wi_col = _find_col(df, _WINTER_COLS)
    es_col = _find_col(df, _ENERGY_SRC_COLS)
    tech_col = _find_col(df, _TECH_COLS)

    gen = pd.DataFrame({
        "plant_id": df[plant_col].astype(str).str.strip(),
        "plant_name": df[name_col].astype(str).str.strip() if name_col else "",
        "state": df[state_col],
        "energy_source": (df[es_col].astype(str).str.strip().str.upper() if es_col else ""),
        "technology": df[tech_col].astype(str).str.strip() if tech_col else "",
        "nameplate_mw": _num(df[np_col]) if np_col else pd.NA,
        "summer_mw": _num(df[su_col]) if su_col else pd.NA,
        "winter_mw": _num(df[wi_col]) if wi_col else pd.NA,
    })
    gen["year"] = year
    # Normalise plant_id to a bare integer string ("1001", not "1001.0") so it
    # matches EIA-923's plant_id for downstream joins.
    pid_num = pd.to_numeric(gen["plant_id"], errors="coerce")
    gen = gen[pid_num.notna()].copy()
    gen["plant_id"] = pid_num[pid_num.notna()].astype("int64").astype(str)

    # Aggregate generators → plant × energy-source (fuel), so capacity lines up
    # with the EIA-923 plant × fuel generation grain.
    agg = (gen.groupby(["year", "plant_id", "plant_name", "state", "energy_source"],
                       as_index=False)
           .agg(nameplate_mw=("nameplate_mw", "sum"),
                summer_mw=("summer_mw", "sum"),
                winter_mw=("winter_mw", "sum"),
                n_generators=("nameplate_mw", "size")))
    agg["fuel_group"] = agg["energy_source"].map(fuel_group)
    return agg.reset_index(drop=True)


def fetch_year(year: int, log=print) -> pd.DataFrame:
    content = _download_zip(year, log=log)
    if not content:
        return pd.DataFrame()
    return _parse_zip(content, year, log=log)


def update(years: list[int] | None = None, log=print) -> dict:
    paths.EIA860_DIR.mkdir(parents=True, exist_ok=True)
    paths.EIA860_RAW_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    if years is None:
        # EIA-860 final lags ~10 months; try the last 3 vintages.
        years = [today.year - 3, today.year - 2, today.year - 1]
    saved = 0
    for year in years:
        log(f"EIA-860 year {year}:")
        df = fetch_year(year, log=log)
        if df.empty:
            log(f"  No data for {year}.")
            continue
        p = paths.EIA860_DIR / f"eia860_pjm_{year}.parquet"
        df.to_parquet(p, index=False)
        saved += len(df)
        log(f"  Saved {len(df):,} plant-fuel rows → {p.name}")
    return {"rows": saved}


def available_years() -> list[int]:
    if not paths.EIA860_DIR.exists():
        return []
    out = []
    for p in paths.EIA860_DIR.glob("eia860_pjm_*.parquet"):
        try:
            out.append(int(p.stem.rsplit("_", 1)[-1]))
        except ValueError:
            continue
    return sorted(out)


def load(year: int | None = None) -> pd.DataFrame:
    """Load one year's capacity table (latest available if year is None)."""
    yrs = available_years()
    if not yrs:
        return pd.DataFrame()
    y = year if (year in yrs) else yrs[-1]
    p = paths.EIA860_DIR / f"eia860_pjm_{y}.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


if __name__ == "__main__":
    print(update(log=print))
