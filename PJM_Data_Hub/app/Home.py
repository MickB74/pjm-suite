"""PJM Data Hub — router / entry point.

Run:  .venv/bin/streamlit run app/Home.py
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))       # app/
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # PJM_Data_Hub/

import _common  # noqa: F401,E402

import streamlit as st
from pjm_core import paths

st.set_page_config(page_title="PJM Data Hub", page_icon="⚡", layout="wide",
                   initial_sidebar_state="expanded")
paths.ensure_dirs()

# Refresh all datasets once per session when the app is opened. Controlled by
# the "auto_refresh" flag in config (default on); toggle it on the API Keys page.
from pjm_core import credentials  # noqa: E402
if credentials.load_config().get("auto_refresh", True):
    _common.auto_refresh(st)

P = "screens"

nav = st.navigation({
    "Start Here": [
        st.Page(f"{P}/0_API_Keys.py", title="API Keys", icon="🔑", default=True),
        st.Page(f"{P}/13_Markets_Explained.py", title="Markets Explained", icon="📚"),
    ],
    "Explore": [
        st.Page(f"{P}/1_Hub_Prices.py", title="Hub Prices (LMP)", icon="💵"),
        st.Page(f"{P}/2_System_Generation.py", title="System Generation", icon="🔥"),
        st.Page(f"{P}/12_System_Load.py", title="System Load", icon="📈"),
        st.Page(f"{P}/9_Ancillary_Services.py", title="Ancillary Services", icon="🛡️"),
        st.Page(f"{P}/11_Capacity_RPM.py", title="Capacity (RPM)", icon="🏛️"),
        st.Page(f"{P}/19_Interconnection_Queue.py", title="Interconnection Queue", icon="🔌"),
        st.Page(f"{P}/3_EIA_923.py", title="EIA-923 Generation", icon="📅"),
    ],
    "Analyze": [
        st.Page(f"{P}/4_Price_Forecast.py", title="Price Forecast", icon="📉"),
        st.Page(f"{P}/10_Peak_Day_Analysis.py", title="Peak Day Analysis", icon="⛰️"),
        st.Page(f"{P}/14_Coincident_Peaks.py", title="5CP & Weather", icon="🌡️"),
        st.Page(f"{P}/16_Peak_Predictor.py", title="5CP Peak Predictor", icon="🔮"),
        st.Page(f"{P}/17_PLC_Calculator.py", title="PLC Cost Calculator", icon="🧮"),
        st.Page(f"{P}/6_DART_Spread.py", title="DA–RT Spread", icon="⚖️"),
        st.Page(f"{P}/8_Hub_Basis.py", title="Hub Basis", icon="🧭"),
        st.Page(f"{P}/7_Capture_Price.py", title="Capture Price", icon="🎯"),
        st.Page(f"{P}/15_Plant_Earnings.py", title="Plant Earnings", icon="💰"),
        st.Page(f"{P}/18_Full_Bill.py", title="Full Bill Estimator", icon="🧮"),
        st.Page(f"{P}/5_Invoice_Validation.py", title="Invoice Validation", icon="🧾"),
    ],
})

nav.run()
