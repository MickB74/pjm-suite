# PJM Suite

A PJM-footprint equivalent of the ercot-suite, focused on **PJM DOM Hub** (Dominion Hub, Virginia). Pulls hourly Real-Time LMPs, PJM system generation by fuel, and EIA-923 plant data; runs a Monte Carlo forward price forecast; and hosts everything in a Streamlit Data Hub.

---

## Quick start

```bash
./setup.sh
```

Before the data pull works, add your PJM Data Miner 2 subscription key to `PJM_Data_Hub/config.json` (created from the example by `setup.sh`). Register free at **https://api.pjm.com/**.

Then launch the app:

```bash
./launch_portal.sh          # opens on http://localhost:8510
./launch_portal.sh 8511     # custom port
```

Or skip the data pull and just build the environment:

```bash
./setup.sh --no-data
```

---

## Projects / Modules

| Folder | What it does |
|---|---|
| `PJM_Data_Hub/` | Shared engine (`pjm_core`), data lake, Streamlit app, CLI orchestrator |
| `PJM_Data_Hub/pjm_core/` | Timezone (Eastern), paths, credentials, prices, settlement math, price forecast |
| `PJM_Data_Hub/datasets/hub_prices/` | PJM DOM Hub hourly LMP ETL (`pjm_api.py`) |
| `PJM_Data_Hub/datasets/system_gen_by_fuel/` | PJM fuel mix via gridstatus (hourly, EIA-930) |
| `PJM_Data_Hub/datasets/eia923/` | EIA Form 923 plant-level generation for PJM states |
| `PJM_Data_Hub/app/` | Streamlit Hub: Hub Prices, System Generation, EIA-923, Price Forecast |

---

## Data sources & lag

| Source | What | API | Lag |
|---|---|---|---|
| **PJM RT LMPs** | Hourly RT LMPs at DOM HUB (and other hubs) | PJM Data Miner 2 (`api.pjm.com`) | ~1 day |
| **PJM Fuel Mix** | Hourly generation by fuel (system-wide) | gridstatus → EIA-930 | ~2 days |
| **EIA Form 923** | Monthly plant net generation & fuel | EIA file download | ~6 months |

---

## Key conventions

### Timezone — Eastern Prevailing Time (EPT)

PJM settles in `US/Eastern`. Interval timestamps in the parquet lake are stored as **naive Eastern** (opens cleanly in Excel). Settlement-grade joins lift to tz-aware Eastern via `pjm_core.tz.localize_eastern()`. Spring-forward (23h) and fall-back (25h) days are handled via `ambiguous="infer"` / `nonexistent="shift_forward"`.

### Interval size — hourly

PJM LMPs are **hourly** (unlike ERCOT's 15-min SPPs). `settlement.INTERVAL_HOURS = 1.0`. Energy per interval = MW × 1.0 h.

### LMP components

Each hourly LMP has three components:
- `total_lmp` = `energy` + `congestion` + `loss`

`total_lmp` is the standard settlement price. Congestion and loss are tracked separately for basis analysis.

### Data lake layout (`PJM_Data_Hub/data/`, git-ignored)

```
data/
  hub_prices/     pjm_hub_prices_hourly.parquet, .last_update.json
  system_gen/     pjm_gen_by_fuel_<year>.parquet
  eia923/         eia923_pjm_<year>.parquet, raw/ (cached ZIPs)
  price_forecast/ pjm_forecast_DOM_HUB_<date>.parquet
```

---

## Price forecast methodology

Mirrors the ERCOT heat-rate × gas approach:

1. **Historical LMPs** from the DOM Hub parquet store.
2. **Henry Hub gas** from EIA API (`api.eia.gov`) or a mean-reversion fallback.
3. **Implied heat rate** = LMP / gas (MMBtu/MWh) — monthly distribution pooled across years.
4. **Monte Carlo** (5,000 paths, seed 42):
   - Gas: martingale lognormal, σ = 0.5·√t (annualised).
   - Heat rate: lognormal anchored on the **median** (robust to scarcity years), log-σ ≥ 0.10.
   - Price = gas × heat rate, capped at $2,000/MWh (PJM market-wide offer cap).
5. Output: monthly P10/P25/P50/P75/P90.

Gas mean-reverts to **$4.00/MMBtu** with a **24-month** e-folding time beyond the EIA strip.

---

## Settlement math (`pjm_core/settlement.py`)

| Structure | Per-interval |
|---|---|
| Merchant | `gen_MWh × market_LMP` |
| PPA | `gen_MWh × strike` |
| **CfD / VPPA** | `gen_MWh × (market_LMP − strike)` |
| Basis (optional) | `gen_MWh × (node_LMP − hub_LMP)` |

Sign: offtaker-signed (positive = offtaker receives). Default: `price_floor=0.0`, `settle_below_floor=False` (standard VPPA; sub-floor intervals excluded).

---

## CLI

```bash
cd PJM_Data_Hub
.venv/bin/python orchestrate.py update hub_prices          # DOM HUB only
.venv/bin/python orchestrate.py update hub_prices --all-hubs  # all PJM hubs
.venv/bin/python orchestrate.py update system_gen
.venv/bin/python orchestrate.py update eia923
.venv/bin/python orchestrate.py update all
.venv/bin/python orchestrate.py status
```

---

## Credentials

| Key | Used by | Where to get |
|---|---|---|
| `subscription_key` | PJM Data Miner 2 (LMPs) | see below (free for non-members) |
| `eia_api_key` | Henry Hub gas strip (price forecast) | https://www.eia.gov/opendata/ (free) |

Secrets live in `PJM_Data_Hub/config.json` (git-ignored, chmod 600). Copy from `config.example.json`.

### Getting a free PJM Data Miner 2 key as a non-member

PJM gives **non-members free API access for internal business use** (per the
official Data Miner API Guide):

1. Register a PJM Tools account at
   https://accountmanager.pjm.com/accountmanager/pages/public/new-user.jsf
   (or via https://apiportal.pjm.com/ → *Sign up*).
2. Email **accountmanager@pjm.com** with the exact statement:
   *"I confirm that the PJM Data will be used for internal business purposes only."*
   Include the username you registered and your email address.
3. Once provisioned, sign in to https://apiportal.pjm.com/ → **View Profile** →
   **Your Subscriptions** to copy your subscription key.
4. Paste it into the **API Keys** page in the app (or `config.json`).

> ⚠️ **Commercial use** — publishing the data or making derivatives of it for
> external parties — requires a PJM **Associate Membership** (minimum). Internal
> analysis (the use case for this suite) does not.

The data fetch goes through the [`gridstatus`](https://github.com/gridstatus/gridstatus)
library, which reads the key from `config.json` or the `PJM_API_KEY`
environment variable and handles auth, pagination, the PJM date-range format,
DST, and the canonical hub pnode names automatically.

---

## Relationship to ercot-suite

This repo is a standalone sibling of the ercot-suite (`../ercot-suite`). It shares the same design patterns — unified data lake, pjm_core engine, Streamlit hub, CLI orchestrator — but is independent. No shared code at import time; the architecture is parallel, not coupled.
