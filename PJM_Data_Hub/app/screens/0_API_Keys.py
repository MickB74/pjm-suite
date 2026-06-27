"""API Keys & Control Tower."""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _common  # noqa: F401

import streamlit as st
from pjm_core import credentials, paths
from datasets.hub_prices import pjm_api

st.title("🔑 API Keys & Control Tower")

cfg = credentials.load_config()

with st.expander("PJM Data Miner 2", expanded=not credentials.have_credentials(cfg)):
    st.markdown(
        "Register at [api.pjm.com](https://api.pjm.com/) to get a free subscription key. "
        "Grants access to hourly LMPs, load, and generation data."
    )
    key_in = st.text_input("Subscription key", value=cfg.get("subscription_key", ""), type="password")
    start_in = st.text_input("Backfill start date", value=cfg.get("backfill_start", "2020-01-01"),
                              help="Earliest date to pull (YYYY-MM-DD). Farther back = more data, slower first run.")
    if st.button("Save PJM credentials"):
        cfg.update({"subscription_key": key_in.strip(), "backfill_start": start_in.strip()})
        credentials.save_config(cfg)
        st.success("Saved.")

with st.expander("EIA API Key (optional — for price forecast gas strip)"):
    st.markdown("Get a free key at [eia.gov/opendata](https://www.eia.gov/opendata/register.php).")
    eia_in = st.text_input("EIA API key", value=credentials.get_eia_api_key(), type="password")
    if st.button("Save EIA key"):
        credentials.save_eia_api_key(eia_in.strip())
        st.success("Saved.")

st.divider()
st.subheader("Data Updates")

summary = pjm_api.store_summary()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Hub price rows", f"{summary.get('rows', 0):,}")
c2.metric("Start", summary.get("start", "—"))
c3.metric("End", summary.get("end", "—"))
days_stale = summary.get("days_since_update")
c4.metric("Days since update", f"{days_stale:.1f}" if days_stale is not None else "never")

log_area = st.empty()
log_lines: list[str] = []

if st.button("Update Hub Prices (all PJM hubs)", type="primary",
             disabled=not credentials.have_credentials(cfg)):
    def _log(msg):
        log_lines.append(msg)
        log_area.text("\n".join(log_lines[-40:]))
    with st.spinner("Fetching from PJM …"):
        try:
            result = pjm_api.update(progress_callback=_log)
            st.success(f"Done — {result['rows']:,} rows.")
        except Exception as e:
            st.error(f"Update failed: {e}")
