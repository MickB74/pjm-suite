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
from datasets.zone_prices import pjm_zone_prices
from datasets.ancillary import pjm_as
from datasets.load import pjm_load
from datasets.weather import pjm_weather
from datasets.queue import pjm_queue

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

auto_on = st.toggle(
    "🔄 Auto-refresh all data when the app opens",
    value=cfg.get("auto_refresh", False),
    help="Off by default: opening the app is instant, and you refresh on demand "
         "with the 🔄 Refresh PJM data button in the sidebar. Turn this on to pull "
         "the latest hub prices, ancillary, load, and weather automatically each "
         "time you open the app (this can make the first render slow).")
if auto_on != cfg.get("auto_refresh", False):
    cfg["auto_refresh"] = auto_on
    credentials.save_config(cfg)

summary = pjm_api.store_summary()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Hub price rows", f"{summary.get('rows', 0):,}")
c2.metric("Start", summary.get("start", "—"))
c3.metric("End", summary.get("end", "—"))
days_stale = summary.get("days_since_update")
c4.metric("Days since update", f"{days_stale:.1f}" if days_stale is not None else "never")

mkts = summary.get("markets", {})
if mkts:
    st.caption("  ·  ".join(
        f"**{m}**: {v['rows']:,} rows through {str(v['end'])[:10]}"
        for m, v in sorted(mkts.items())))

log_area = st.empty()
log_lines: list[str] = []

def _run_update(fn, label):
    def _log(msg):
        log_lines.append(msg)
        log_area.text("\n".join(log_lines[-40:]))
    with st.spinner(f"Fetching {label} from PJM …"):
        try:
            result = fn(progress_callback=_log)
            st.success(f"{label}: done — {result['rows']:,} rows.")
        except Exception as e:
            st.error(f"{label} update failed: {e}")


if st.button("Update Hub Prices (all PJM hubs, RT + DA)", type="primary",
             disabled=not credentials.have_credentials(cfg)):
    _run_update(pjm_api.update, "Hub prices")

# --- Zone LMPs (monthly avg, powers Plant Earnings) --------------------------
zp_sum = pjm_zone_prices.store_summary()
st.caption(f"💰 **Zone LMPs (monthly avg)** — {zp_sum.get('rows', 0):,} zone-month rows · "
           f"{zp_sum.get('start','—')} → {zp_sum.get('end','—')} · "
           f"{len(zp_sum.get('zones', []))} zones · powers the **Plant Earnings** estimate.")
if st.button("Update Zone LMPs (RT + DA)", disabled=not credentials.have_credentials(cfg)):
    _run_update(pjm_zone_prices.update, "Zone LMPs")

# --- Ancillary services + load stores ---------------------------------------
as_sum = pjm_as.store_summary()
ld_sum = pjm_load.store_summary()
a1, a2 = st.columns(2)
a1.metric("Ancillary rows", f"{as_sum.get('rows', 0):,}",
          help=f"{as_sum.get('start','—')} → {as_sum.get('end','—')}")
a2.metric("Load rows", f"{ld_sum.get('rows', 0):,}",
          help=f"{ld_sum.get('start','—')} → {ld_sum.get('end','—')}")

b1, b2 = st.columns(2)
if b1.button("Update Ancillary Services", disabled=not credentials.have_credentials(cfg)):
    _run_update(pjm_as.update, "Ancillary services")
if b2.button("Update System Load", disabled=not credentials.have_credentials(cfg)):
    _run_update(pjm_load.update, "System load")

# --- Weather (ERA5 via Open-Meteo — no API key needed) ----------------------
wx_sum = pjm_weather.store_summary()
st.caption(f"🌡️ **Weather (ERA5)** — {wx_sum.get('rows', 0):,} rows · "
           f"{str(wx_sum.get('start','—'))[:10]} → {str(wx_sum.get('end','—'))[:10]} · "
           "free, no key (Open-Meteo). Auto-aligns to the load store window; "
           "used by 5CP & Peak Day Analysis.")
if st.button("Update Weather (ERA5)"):
    _run_update(pjm_weather.update, "Weather (ERA5)")

# --- Interconnection queue (PJM public Planning API — no key needed) ---------
q_sum = pjm_queue.store_summary()
st.caption(f"🔌 **Interconnection Queue** — {q_sum.get('active', 0):,} active "
           f"({q_sum.get('active_mw', 0):,.0f} MW) as of {q_sum.get('snapshot', '—')} · "
           f"{q_sum.get('snapshots', 0)} archived snapshot(s) · free, no key (PJM "
           "Planning API). Each refresh is archived to track queue changes over time.")
if st.button("Update Interconnection Queue"):
    _run_update(pjm_queue.update, "Interconnection queue")
