#!/usr/bin/env python3
"""Streamlit GUI for the PJM hub price downloader."""

from __future__ import annotations

import sys
import os
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import streamlit as st
from pjm_core import credentials
from datasets.hub_prices import pjm_api
from pjm_core.settlement_points import HUBS, PRIMARY_HUB

st.set_page_config(page_title="PJM Hub Price Updater", page_icon="⚡", layout="wide")
st.title("⚡ PJM Hub Price Updater")
st.caption("Fetches hourly Real-Time LMPs from PJM Data Miner 2 (api.pjm.com) and keeps a local parquet store.")

cfg = credentials.load_config()
have_creds = credentials.have_credentials(cfg)

with st.expander("Credentials", expanded=not have_creds):
    key_in = st.text_input("PJM subscription key", value=cfg.get("subscription_key", ""),
                           type="password")
    if st.button("Save credentials"):
        cfg["subscription_key"] = key_in.strip()
        credentials.save_config(cfg)
        st.success("Saved.")
        st.rerun()

if not have_creds:
    st.warning("Set your PJM subscription key above before updating.")
    st.stop()

summary = pjm_api.store_summary()
c1, c2, c3 = st.columns(3)
c1.metric("Rows in store", f"{summary.get('rows', 0):,}")
c2.metric("Start", summary.get("start", "—"))
c3.metric("End", summary.get("end", "—"))

hubs_in = st.multiselect("Hubs to fetch", HUBS, default=[PRIMARY_HUB])

log_area = st.empty()
log_lines: list[str] = []

if st.button("Update now", type="primary"):
    with st.spinner("Fetching from PJM …"):
        def on_log(msg):
            log_lines.append(msg)
            log_area.text("\n".join(log_lines[-40:]))
        try:
            result = pjm_api.update(hubs=hubs_in or [PRIMARY_HUB], progress_callback=on_log)
            st.success(f"Done — {result['rows']:,} rows ({result['start']} → {result['end']})")
        except Exception as e:
            st.error(f"Update failed: {e}")
