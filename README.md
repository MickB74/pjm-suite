# PJM Suite

A PJM-footprint equivalent of the ercot-suite, focused on the **Dominion Hub** (Virginia / North Carolina) but covering all 12 PJM trading hubs. It pulls hourly Real-Time **and Day-Ahead** LMPs, PJM system generation by fuel, system load by zone, ancillary-services clearing prices, weather at the major load centers, and plant-level data from EIA Forms 860/923. On top of that lake it runs a Monte Carlo forward price forecast, a 5CP peak predictor and PLC calculator, plant-earnings and capacity-market analytics, invoice validation, and DART/basis/capture-price tools — all hosted in a Streamlit Data Hub with 18 screens.

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

## Layout

| Folder | What it does |
|---|---|
| `PJM_Data_Hub/` | Shared engine (`pjm_core`), data lake, Streamlit app, CLI orchestrator |
| `PJM_Data_Hub/pjm_core/` | Engine: timezone, paths, credentials, prices, settlement, invoice, price forecast, peak/5CP, capacity (RPM), plant earnings, plant→zone mapping, hub & weather reference points |
| `PJM_Data_Hub/datasets/hub_prices/` | Hourly RT + DA LMP ETL for all 12 PJM hubs (`pjm_api.py`, via gridstatus) |
| `PJM_Data_Hub/datasets/zone_prices/` | Monthly average zone LMPs (for valuing monthly plant generation) |
| `PJM_Data_Hub/datasets/system_gen_by_fuel/` | PJM fuel mix via gridstatus (hourly, EIA-930) |
| `PJM_Data_Hub/datasets/load/` | Hourly metered system load by zone (`hrl_load_metered`, direct PJM API) |
| `PJM_Data_Hub/datasets/ancillary/` | Reserve + regulation market clearing prices (`reserve_market_results`) |
| `PJM_Data_Hub/datasets/weather/` | Hourly ERA5 weather at major load centers (Open-Meteo) |
| `PJM_Data_Hub/datasets/eia923/` | EIA Form 923 plant-level monthly net generation for PJM states |
| `PJM_Data_Hub/datasets/eia860/` | EIA Form 860 plant nameplate / summer capacity (MW) |
| `PJM_Data_Hub/app/` | Streamlit Hub (`Home.py` + `screens/`) — see below |
| `PJM_Data_Hub/scripts/` | `peak_digest.py` — morning 5CP peak-risk briefing |

### App screens (`app/screens/`)

API Keys & Control Tower · Hub Prices · System Generation · EIA-923 · Price Forecast · Invoice Validation · DA–RT Spread (DART) · Capture Price · Hub Basis · Ancillary Services · Peak Day Analysis · Capacity Market (RPM) · System Load · Markets Explained · 5 Coincident Peaks (5CP) & Weather · Plant Earnings · 5CP Peak Predictor · PLC & Capacity-Cost Calculator

---

## Data sources & lag

| Source | What | API | Lag |
|---|---|---|---|
| **PJM RT LMPs** | Hourly RT LMPs at all 12 trading hubs | PJM Data Miner 2 (via gridstatus) | ~1 day |
| **PJM DA LMPs** | Hourly Day-Ahead LMPs at all 12 trading hubs | PJM Data Miner 2 (via gridstatus) | same day |
| **PJM Zone LMPs** | Monthly average LMP per zone × market | PJM Data Miner 2 (via gridstatus) | ~1 day |
| **PJM System Load** | Hourly metered load by zone | PJM Data Miner 2 (`api.pjm.com`) | ~1 day |
| **PJM Ancillary** | Hourly reserve + regulation clearing prices | PJM Data Miner 2 (`api.pjm.com`) | ~1 day |
| **PJM Fuel Mix** | Hourly generation by fuel (system-wide) | gridstatus → EIA-930 | ~2 days |
| **Weather** | Hourly ERA5 at major load centers | Open-Meteo (no key) | ~5 days |
| **EIA Form 923** | Monthly plant net generation & fuel | EIA file download | ~6 months |
| **EIA Form 860** | Plant nameplate / summer capacity (MW) | EIA file download | annual |

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
                  (one row per hub × hour × market; market ∈ {RT, DA})
  zone_prices/    monthly average LMP per zone × market
  system_gen/     pjm_gen_by_fuel_<year>.parquet
  load/           hourly metered load by zone
  ancillary/      hourly reserve + regulation clearing prices
  weather/        hourly ERA5 at major load centers
  eia923/         eia923_pjm_<year>.parquet, raw/ (cached ZIPs)
  eia860/         plant nameplate / summer capacity (MW)
  capacity/       RPM auction clearing prices by LDA
  price_forecast/ pjm_forecast_<hub>_<date>.parquet
  csv_exports/    Excel-friendly CSV mirrors
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

## Peak / 5CP & capacity analytics

PJM sets each load's **Peak Load Contribution (PLC)** from its demand during the
**five highest RTO peak-load hours of the summer** — the "5 Coincident Peaks"
(5CP). Those hours land on the hottest, most humid summer afternoons, so peak
risk is largely a weather signal.

- `pjm_core/peak.py` — shared 5CP analytics used by the **Peak Day Analysis**,
  **5CP & Weather**, and **5CP Peak Predictor** screens and by `peak_digest.py`,
  so the app, the predictor, and the morning briefing all agree.
- `pjm_core/weather_points.py` — major load centers with population weights to
  turn point ERA5 weather into a system-level peak-load proxy.
- `pjm_core/capacity.py` — RPM Base Residual Auction clearing prices ($/MW-day)
  by Locational Deliverability Area (RTO + constrained sub-zones like DOM,
  EMAAC), powering the **Capacity Market (RPM)** and **PLC Calculator** screens.

## Plant earnings

There is no public record of what a PJM plant actually earns. `pjm_core/plant_earnings.py`
reconstructs a defensible **estimate** of energy revenue as EIA-923 monthly MWh
× the zonal LMP for that month, mapping each plant to its PJM zone via
`pjm_core/plant_zones.py` (EIA reports state, not zone) and pulling capacity (MW)
from EIA-860 for capacity-revenue estimates.

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

## Invoice validation (`pjm_core/invoice.py`)

The **Invoice Validation** screen reconciles an uploaded invoice / settlement
statement (any CSV/Excel with an interval timestamp plus some of {price $/MWh,
volume MWh, amount $}) against the cached RT or DA LMP at any of the 12 PJM
hubs, interval by interval:

- Column roles are auto-guessed and user-correctable; "Hour Ending" labels and
  sub-hourly (5/15/30-min) rows are handled — sub-hourly rows match the LMP for
  the hour that contains them.
- Both sides are lifted to tz-aware Eastern before joining, so the November
  fall-back hour reconciles on the absolute instant, not the repeated label.
- Per-interval statuses: `match`, `price_mismatch`, `amount_mismatch`,
  `missing_in_invoice`, `extra_in_invoice`; summary shows the signed $ variance
  (positive = overbilled) and the worst offenders. Results download as CSV.

---

## CLI

The orchestrator drives every dataset:

```bash
cd PJM_Data_Hub
.venv/bin/python orchestrate.py update hub_prices                 # all hubs, RT + DA
.venv/bin/python orchestrate.py update hub_prices --primary-only  # DOMINION HUB only
.venv/bin/python orchestrate.py update zone_prices                # monthly avg LMP per zone
.venv/bin/python orchestrate.py update system_gen
.venv/bin/python orchestrate.py update load                       # hourly metered load by zone
.venv/bin/python orchestrate.py update ancillary                  # reserve + regulation prices
.venv/bin/python orchestrate.py update weather                    # ERA5 at load centers
.venv/bin/python orchestrate.py update eia923
.venv/bin/python orchestrate.py update eia860
.venv/bin/python orchestrate.py update all                        # every dataset (continues on error)
.venv/bin/python orchestrate.py status                            # rows, date range & freshness, all datasets
.venv/bin/python orchestrate.py status --json                     # same, machine-readable

# --years limits the annual datasets (system_gen, eia923, eia860)
.venv/bin/python orchestrate.py update eia923 --years 2024 2025
```

Each dataset also has its own module CLI for finer control (e.g. one market only):

```bash
.venv/bin/python datasets/hub_prices/pjm_api.py update --markets RT   # RT only
```

Morning 5CP peak-risk briefing (pipe into email/Slack):

```bash
.venv/bin/python scripts/peak_digest.py            # today's digest
.venv/bin/python scripts/peak_digest.py --days 10  # shorter horizon
.venv/bin/python scripts/peak_digest.py --refresh  # update load + weather first
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
