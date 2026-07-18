# CLAUDE.md

## What this is

PJM energy-market analytics suite: a parquet data lake, a `pjm_core` engine library, a CLI orchestrator, and a Streamlit app with 20+ screens. Covers all 12 PJM trading hubs (primary: Dominion Hub). Sibling of `../ercot-suite` — same design, no shared code.

## Quick reference

```bash
# Setup
./setup.sh                # venv + config scaffold + initial data pull
./setup.sh --no-data      # venv only

# Run the app
./launch_portal.sh        # http://localhost:8510
./launch_portal.sh 8511   # custom port

# Data updates (run from PJM_Data_Hub/)
.venv/bin/python orchestrate.py update all
.venv/bin/python orchestrate.py status
```

## Project layout

```
PJM_Data_Hub/
  pjm_core/          # Engine: prices, settlement, invoice, peak/5CP, capacity,
                     #   forecast, plant earnings, timezone, paths, credentials
  datasets/          # ETL modules: hub_prices, zone_prices, system_gen_by_fuel,
                     #   load, ancillary, weather, eia923, eia860
  app/Home.py        # Streamlit entry point
  app/_common.py     # Shared helpers (auto-refresh, LMP component definitions)
  app/screens/       # Numbered screen modules (0–20)
  scripts/           # peak_digest.py (morning 5CP briefing)
  orchestrate.py     # CLI: update <dataset> | status
  config.json        # Secrets (git-ignored); copy from config.example.json
  data/              # Parquet lake (git-ignored)
```

## Dev environment

- **Python 3.12+**, venv at `PJM_Data_Hub/.venv`
- Key deps: `pandas`, `pyarrow`, `streamlit`, `plotly`, `gridstatus`, `numpy`, `requests`, `yfinance`, `openpyxl`
- Install: `cd PJM_Data_Hub && .venv/bin/pip install -r requirements.txt`
- No test suite currently

## Conventions

- **Timezone**: All timestamps are naive Eastern Prevailing Time (US/Eastern). Use `pjm_core.tz.localize_eastern()` when tz-aware math is needed.
- **Interval size**: Hourly (unlike ERCOT's 15-min). `settlement.INTERVAL_HOURS = 1.0`.
- **LMP components**: `total_lmp = energy + congestion + loss`. Always keep all three.
- **Credentials**: `PJM_Data_Hub/config.json` (git-ignored, chmod 600). Keys: `subscription_key` (PJM Data Miner 2), `eia_api_key` (EIA gas strip). Access via `pjm_core.credentials`.
- **Paths**: All data paths go through `pjm_core.paths` — never hardcode `data/` paths.
- **Screens**: Numbered `NN_Name.py` in `app/screens/`. Streamlit sorts by filename prefix.
- **Data lake**: Parquet files under `PJM_Data_Hub/data/` with `.last_update.json` freshness markers.
- **Primary hub**: `DOMINION HUB` — defined in `pjm_core.settlement_points.PRIMARY_HUB`.
- **Settlement math**: Offtaker-signed (positive = offtaker receives). See `pjm_core/settlement.py`.

## Running the Streamlit app for development

```bash
cd PJM_Data_Hub
.venv/bin/streamlit run app/Home.py --server.port 8510
```

There is a `.claude/launch.json` in `PJM_Data_Hub/` for the Browser preview pane.

## Common tasks

- **Add a new screen**: Create `app/screens/NN_Name.py`. Import shared helpers from `app._common`. The number prefix controls sidebar order.
- **Add a new dataset**: Create a module under `datasets/`, add its update function to `orchestrate.py`, register data paths in `pjm_core/paths.py`.
- **Modify engine logic**: Edit the relevant module in `pjm_core/`. Multiple screens may depend on it — check callers.
