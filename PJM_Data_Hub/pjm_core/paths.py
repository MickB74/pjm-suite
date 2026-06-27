"""Unified data-lake layout for the PJM Data Hub.

Every dataset writes under one ``data/`` root. Layout:

    data/
      hub_prices/    pjm_hub_prices_hourly.parquet   (full LMP history)
                     pjm_hub_prices_hourly.csv
                     .last_update.json
      system_gen/    pjm_gen_by_fuel_<year>.parquet
      eia923/        eia923_<region>_<year>.parquet
                     raw/  (cached zips)
      price_forecast/ pjm_price_forecast_<asof>.parquet
      csv_exports/   on-demand slices
    config.json      PJM API credential store (git-ignored)
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DATA = Path(os.environ.get("PJM_HUB_DATA", ROOT / "data"))

# --- per-dataset directories --------------------------------------------------
HUB_PRICES_DIR = DATA / "hub_prices"
SYSTEM_GEN_DIR = DATA / "system_gen"
EIA_DIR = DATA / "eia923"
EIA_RAW_DIR = EIA_DIR / "raw"
PRICE_FORECAST_DIR = DATA / "price_forecast"
CSV_EXPORTS_DIR = DATA / "csv_exports"
LOGS_DIR = ROOT / "logs"

# --- canonical file paths -----------------------------------------------------
CONFIG_PATH = ROOT / "config.json"

HUB_PRICES_PARQUET = HUB_PRICES_DIR / "pjm_hub_prices_hourly.parquet"
HUB_PRICES_CSV = HUB_PRICES_DIR / "pjm_hub_prices_hourly.csv"
HUB_PRICES_STATE = HUB_PRICES_DIR / ".last_update.json"

_ALL_DIRS = [
    DATA, HUB_PRICES_DIR, SYSTEM_GEN_DIR, EIA_DIR, EIA_RAW_DIR,
    PRICE_FORECAST_DIR, CSV_EXPORTS_DIR, LOGS_DIR,
]


def ensure_dirs() -> None:
    """Create the full data-lake directory tree (idempotent)."""
    for d in _ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


DATASETS = {
    "hub_prices": "PJM hub LMPs (hourly RTM)",
    "system_gen": "PJM system generation by fuel",
    "eia923": "EIA-923 plant monthly generation & fuel",
}
