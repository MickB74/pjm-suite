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
                     #   futures (ICE fwd curve), forecast, plant earnings,
                     #   timezone, paths, credentials
  datasets/          # ETL modules: hub_prices, zone_prices, system_gen_by_fuel,
                     #   load, ancillary, weather, eia923, eia860
  app/Home.py        # Streamlit entry point; defines the sidebar nav (st.navigation)
  app/_common.py     # Shared helpers (auto-refresh, LMP component definitions)
  app/screens/       # Numbered screen modules (0–21)
  scripts/           # peak_digest.py (morning 5CP briefing)
  orchestrate.py     # CLI: update <dataset> | status
  config.json        # Secrets (git-ignored); copy from config.example.json
  data/              # Parquet lake (git-ignored)
```

## Dev environment

- **Python 3.12+**, venv at `PJM_Data_Hub/.venv`
- Key deps: `pandas`, `pyarrow`, `streamlit`, `plotly`, `gridstatus`, `numpy`, `requests`, `yfinance`, `openpyxl`, `curl_cffi` (browser-TLS client for the ICE forward-curve pull)
- Install: `cd PJM_Data_Hub && .venv/bin/pip install -r requirements.txt`
- Tests: `cd PJM_Data_Hub && .venv/bin/python -m pytest tests/ -q` (pure-unit, no network or data lake required — the forecast tests stub the EIA/parquet inputs)

## Conventions

- **Timezone**: All timestamps are naive Eastern Prevailing Time (US/Eastern). Use `pjm_core.tz.localize_eastern()` when tz-aware math is needed.
- **Interval size**: Hourly (unlike ERCOT's 15-min). `settlement.INTERVAL_HOURS = 1.0`.
- **LMP components**: `total_lmp = energy + congestion + loss`. Always keep all three.
- **Credentials**: `PJM_Data_Hub/config.json` (git-ignored, chmod 600). Keys: `subscription_key` (PJM Data Miner 2), `eia_api_key` (EIA gas strip). Access via `pjm_core.credentials`.
- **Paths**: All data paths go through `pjm_core.paths` — never hardcode `data/` paths.
- **Screens**: Numbered `NN_Name.py` in `app/screens/`. Sidebar order and grouping are **not** the filename prefix — they're defined explicitly by the `st.navigation({...})` dict in `app/Home.py`, keyed by section ("Start Here", "Explore", "Capacity & Peaks", "Analyze"). Each page's URL slug also comes from its `st.Page(..., title=...)` there.
- **Data lake**: Parquet files under `PJM_Data_Hub/data/` with `.last_update.json` freshness markers.
- **Gas strip vintages**: every `gas_strip.update()` pull is also archived to `data/gas/henry_hub_strip_history.parquet` (one snapshot per day, keyed by `asof`). `gas_strip.strip_asof(date)` returns the strip as it stood on a date; `price_forecast.run(asof=d, gas_asof=d)` re-runs the forecast from that vintage (Price Forecast screen → "Gas strip vintage" picker). A missing vintage raises — never silently substitute today's strip in a backtest.
- **Gas anchor**: the forecast anchors on `gas_strip.strip_median()` — the per-contract median of the last 5 vintages, not a single day's settle — which rejects bad ticks from the unofficial Yahoo feed. Keep the window short: a forward is near-martingale, so averaging over weeks lags real moves rather than smoothing noise. `anchor_vintages=1` gives the raw settle.
- **Gas volatility**: seasonal in the *delivery* month (a January contract carries ~2x a July one) and mean-reverting in horizon (`_gas_terminal_sigma`, OU with κ = 0.29/yr), not a flat σ·√t. Seasonality scales only the final `DELIVERY_WINDOW_YEARS` of variance — multiplying the whole accumulated path puts σ ≈ 1.2 on a far-out January, which implies a P50 at half the forward. Constants are fitted to EIA Henry Hub spot; `gas_strip.forward_vol()` supersedes them per-contract once ~30 vintages exist.
- **Gas → power pass-through**: modelled as a structural elasticity (`GAS_PASS_THROUGH_BETA = 0.90`, measured on DOM Hub), *not* a correlation between the gas and heat-rate shocks — a fixed correlation lets the implied β drift with the ratio of the two σ's. A plain product of independent lognormals implies β = 1.0 and overstates the band.
- **Cross-month correlation**: gas is drawn as one OU-correlated path across the horizon, so `run()` also returns `strip_p10/p50/p90` — the distribution of the *horizon-average* price. Use those for annual/PPA-level numbers; don't derive them from the monthly bands. Averaging the monthly P10/P90s assumes lockstep months and runs too wide; independent monthly draws diversify the regime risk away and run far too narrow (on the current 18-month strip: 50 / 38 / 15 $/MWh of spread respectively).
- **Heat rate**: recency-weighted per calendar month (3-year half-life, `HR_RECENCY_HALFLIFE_YEARS`). The DOM Hub fleet has shifted enough (coal retirement, solar, data-centre load) that pooling 2020 with 2025 anchors the P50 too low.
- **Primary hub**: `DOMINION HUB` — defined in `pjm_core.settlement_points.PRIMARY_HUB`.
- **Zone names differ by feed**: the metered-load feed uses short codes (`PEP`, `CE`, `BC`, `PL`…), the LMP feed uses long names (`PEPCO`, `COMED`, `BGE`, `PPL`…). Cross them with `settlement_points.LOAD_ZONE_PRICE_ZONE` (20 of 22 load zones). `RTO` and `OVEC` are deliberately absent — no zonal LMP exists for either.
- **Never default a zone to a hub price**: only 6 of 22 load zones have a namesake trading hub, so `ZONE_HOME_HUB.get(zone, PRIMARY_HUB)` silently paired e.g. PEPCO load with a Virginia price. For a zone's price at a specific hour use `pjm_zone_prices.load_hourly(market="RT", zones=[...])`; fall back to a hub only where no zonal LMP exists, and label it as a reference.
- **Zone prices are two stores**: `pjm_zone_lmp_hourly.parquet` (hour × zone × market) and the `pjm_zone_lmp_monthly.parquet` average derived from it. `update()` re-fetches a month when *either* store lacks it; hourly completeness is judged on distinct-hour count (`HOURLY_COMPLETE_FRAC`), not row presence, so an interrupted backfill resumes instead of being marked done.
- **Settlement math**: Offtaker-signed (positive = offtaker receives). See `pjm_core/settlement.py`.

## Running the Streamlit app for development

```bash
cd PJM_Data_Hub
.venv/bin/streamlit run app/Home.py --server.port 8510
```

There is a `.claude/launch.json` in `PJM_Data_Hub/` for the Browser preview pane.

## Common tasks

- **Add a new screen**: Create `app/screens/NN_Name.py` (import shared helpers from `app._common`), then register it in the `st.navigation({...})` dict in `app/Home.py` under the right section — a screen file that isn't listed there won't appear.
- **External market data with no free API**: some sources are Akamai-gated (CME, ICE product pages). The pattern is a user-maintainable CSV seed plus a best-effort scraper that never raises and falls back to the CSV — see `pjm_core/futures.py` (ICE free ~15-min-delayed forward curve via `curl_cffi`) and `pjm_core/capacity.py` (RPM reference table).
- **Add a new dataset**: Create a module under `datasets/`, add its update function to `orchestrate.py`, register data paths in `pjm_core/paths.py`.
- **Modify engine logic**: Edit the relevant module in `pjm_core/`. Multiple screens may depend on it — check callers.
