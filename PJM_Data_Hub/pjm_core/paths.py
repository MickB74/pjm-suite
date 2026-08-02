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
ZONE_PRICES_DIR = DATA / "zone_prices"
SYSTEM_GEN_DIR = DATA / "system_gen"
EIA_DIR = DATA / "eia923"
EIA_RAW_DIR = EIA_DIR / "raw"
EIA860_DIR = DATA / "eia860"
EIA860_RAW_DIR = EIA860_DIR / "raw"
EIA860M_DIR = DATA / "eia860m"        # monthly generator inventory snapshots
EIA860M_STATE = EIA860M_DIR / ".last_update.json"
PRICE_FORECAST_DIR = DATA / "price_forecast"
FORECAST_BACKTEST_PARQUET = PRICE_FORECAST_DIR / "backtest_results.parquet"
ANCILLARY_DIR = DATA / "ancillary"
LOAD_DIR = DATA / "load"
CAPACITY_DIR = DATA / "capacity"
DELIVERY_DIR = DATA / "delivery_rates"
WEATHER_DIR = DATA / "weather"
QUEUE_DIR = DATA / "queue"
GAS_DIR = DATA / "gas"
CSV_EXPORTS_DIR = DATA / "csv_exports"
LOGS_DIR = ROOT / "logs"

# --- canonical file paths -----------------------------------------------------
CONFIG_PATH = ROOT / "config.json"

HUB_PRICES_PARQUET = HUB_PRICES_DIR / "pjm_hub_prices_hourly.parquet"
HUB_PRICES_CSV = HUB_PRICES_DIR / "pjm_hub_prices_hourly.csv"
HUB_PRICES_STATE = HUB_PRICES_DIR / ".last_update.json"

# Zone LMPs, aggregated to a monthly average per zone × market. Used to value
# EIA-923 monthly plant generation for the Plant Earnings estimate.
ZONE_PRICES_PARQUET = ZONE_PRICES_DIR / "pjm_zone_lmp_monthly.parquet"
ZONE_PRICES_CSV = ZONE_PRICES_DIR / "pjm_zone_lmp_monthly.csv"
ZONE_PRICES_STATE = ZONE_PRICES_DIR / ".last_update.json"

# The same zone LMPs at full hourly resolution — the monthly file above is
# derived from this. Needed wherever a zone's price at a *specific hour* matters
# (e.g. the coincident-peak view), since no hub price stands in for a zone.
# No CSV twin: this runs to millions of rows.
ZONE_PRICES_HOURLY_PARQUET = ZONE_PRICES_DIR / "pjm_zone_lmp_hourly.parquet"

# Plant → PJM zone overrides (user-maintained crosswalk, keyed by EIA plant_id).
# Refines the state-based default mapping for individual plants. Editable in-app.
PLANT_ZONE_OVERRIDES_CSV = ZONE_PRICES_DIR / "plant_zone_overrides.csv"

# Ancillary services (reserve + regulation market clearing prices)
ANCILLARY_PARQUET = ANCILLARY_DIR / "pjm_ancillary_hourly.parquet"
ANCILLARY_CSV = ANCILLARY_DIR / "pjm_ancillary_hourly.csv"
ANCILLARY_STATE = ANCILLARY_DIR / ".last_update.json"

# System load (hourly metered load by zone)
LOAD_PARQUET = LOAD_DIR / "pjm_load_hourly.parquet"
LOAD_CSV = LOAD_DIR / "pjm_load_hourly.csv"
LOAD_STATE = LOAD_DIR / ".last_update.json"

# Capacity (RPM Base Residual Auction clearing prices — user-maintained ref table)
CAPACITY_CSV = CAPACITY_DIR / "rpm_bra_clearing_prices.csv"

# Delivery / utility (EDC) C&I tariff components — user-maintained ref table,
# seeded from filed tariffs and refreshable from the NREL OpenEI URDB API.
DELIVERY_RATES_CSV = DELIVERY_DIR / "edc_ci_delivery_rates.csv"
DELIVERY_URDB_RAW_DIR = DELIVERY_DIR / "urdb_raw"

# Weather (ERA5 reanalysis via Open-Meteo, hourly, per PJM load center)
WEATHER_PARQUET = WEATHER_DIR / "pjm_weather_hourly.parquet"
WEATHER_CSV = WEATHER_DIR / "pjm_weather_hourly.csv"
WEATHER_STATE = WEATHER_DIR / ".last_update.json"

# Interconnection (New Services) queue — full snapshot of every project, pulled
# from PJM's public Planning API (no Data Miner subscription key needed).
QUEUE_PARQUET = QUEUE_DIR / "pjm_queue.parquet"
QUEUE_CSV = QUEUE_DIR / "pjm_queue.csv"
QUEUE_STATE = QUEUE_DIR / ".last_update.json"

# Henry Hub gas forward curve for the price forecast.
#   GAS_STRIP_CSV   — auto-pulled NYMEX strip, refreshed at launch (see gas_strip.py)
#   GAS_OVERRIDE_CSV — truly manual override; if present it wins over everything
GAS_STRIP_CSV = GAS_DIR / "henry_hub_nymex_strip.csv"
GAS_OVERRIDE_CSV = GAS_DIR / "gas_price_override.csv"
GAS_STRIP_STATE = GAS_DIR / ".last_update.json"
# Every strip pull is also appended here as a dated vintage (asof, month,
# gas_price) so forecasts can be re-run against the strip as of a past date.
GAS_STRIP_HISTORY_PARQUET = GAS_DIR / "henry_hub_strip_history.parquet"

# PJM Western Hub forward curve (exchange-traded power futures). No free API:
# user-maintained reference CSV seeded in futures.py, with a best-effort CME
# settlements scraper that refreshes it and falls back to the CSV on failure.
FUTURES_DIR = DATA / "futures"
FUTURES_CSV = FUTURES_DIR / "pjm_wh_forward_curve.csv"
FUTURES_STATE = FUTURES_DIR / ".last_update.json"

# Prediction log — each day's 5CP predictor output is appended here so the
# calls can be scored later against realised daily peaks. See prediction_log.py.
PREDICTIONS_DIR = DATA / "predictions"
PEAK_PREDICTIONS_PARQUET = PREDICTIONS_DIR / "peak_predictor_log.parquet"

_ALL_DIRS = [
    DATA, HUB_PRICES_DIR, ZONE_PRICES_DIR, SYSTEM_GEN_DIR, EIA_DIR, EIA_RAW_DIR,
    EIA860_DIR, EIA860_RAW_DIR,
    PRICE_FORECAST_DIR, ANCILLARY_DIR, LOAD_DIR, CAPACITY_DIR, WEATHER_DIR,
    DELIVERY_DIR, DELIVERY_URDB_RAW_DIR, QUEUE_DIR, GAS_DIR, FUTURES_DIR,
    PREDICTIONS_DIR, CSV_EXPORTS_DIR, LOGS_DIR,
]


def ensure_dirs() -> None:
    """Create the full data-lake directory tree (idempotent)."""
    for d in _ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


DATASETS = {
    "hub_prices": "PJM hub LMPs (hourly RTM)",
    "zone_prices": "PJM zone LMPs (monthly avg, for plant earnings)",
    "system_gen": "PJM system generation by fuel",
    "ancillary": "PJM ancillary services (reserve + regulation MCP)",
    "load": "PJM hourly metered load by zone",
    "eia923": "EIA-923 plant monthly generation & fuel",
    "queue": "PJM interconnection (New Services) queue",
}
