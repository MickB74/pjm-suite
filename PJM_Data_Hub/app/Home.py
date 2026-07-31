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

# Data refresh is manual by default — pulling everything on open blocks the first
# render and can hit PJM rate limits. We show a "Refresh PJM data" button in the
# sidebar instead. Users who want the old behavior can opt in by setting
# "auto_refresh": true in config (toggle on the API Keys page).
from pjm_core import credentials  # noqa: E402
if _common.auto_refresh_active(st) or st.session_state.get("_ar_start_requested"):
    # A refresh is already mid-flight (or the sidebar button just asked for one);
    # keep pumping the state machine so the Skip button stays live.
    _common.auto_refresh(st, force=True)
elif credentials.load_config().get("auto_refresh", False):
    _common.auto_refresh(st)
    _common.data_freshness_badge(st)
else:
    _common.refresh_prompt(st)

P = "screens"

nav = st.navigation({
    "Start Here": [
        st.Page(f"{P}/0_API_Keys.py", title="API Keys", icon="🔑", default=True),
        st.Page(f"{P}/13_Markets_Explained.py", title="Markets Explained", icon="📚"),
        st.Page(f"{P}/22_Data_Status.py", title="Data Status", icon="🩺"),
    ],
    "Explore": [
        st.Page(f"{P}/1_Hub_Prices.py", title="Hub Prices (LMP)", icon="💵"),
        st.Page(f"{P}/2_System_Generation.py", title="System Generation", icon="🔥"),
        st.Page(f"{P}/12_System_Load.py", title="System Load", icon="📈"),
        st.Page(f"{P}/9_Ancillary_Services.py", title="Ancillary Services", icon="🛡️"),
        st.Page(f"{P}/19_Interconnection_Queue.py", title="Interconnection Queue", icon="🔌"),
        st.Page(f"{P}/3_EIA_923.py", title="EIA-923 Generation", icon="📅"),
    ],
    # Weather → peaks → capacity obligation → cost: one workflow, one group.
    "Capacity & Peaks": [
        st.Page(f"{P}/14_Coincident_Peaks.py", title="5CP & Weather", icon="🌡️"),
        st.Page(f"{P}/20_Degree_Days.py", title="Degree Days (HDD/CDD)", icon="🌤️"),
        st.Page(f"{P}/10_Peak_Day_Analysis.py", title="Peak Day Analysis", icon="⛰️"),
        st.Page(f"{P}/16_Peak_Predictor.py", title="5CP Peak Predictor", icon="🔮"),
        st.Page(f"{P}/23_Prediction_Accuracy.py", title="Predictor Accuracy", icon="🎯"),
        st.Page(f"{P}/11_Capacity_RPM.py", title="Capacity (RPM)", icon="🏛️"),
        st.Page(f"{P}/21_Hub_Futures.py", title="Hub Forward Curve", icon="📈"),
        st.Page(f"{P}/17_PLC_Calculator.py", title="PLC Cost Calculator", icon="🧮"),
    ],
    "Analyze": [
        st.Page(f"{P}/4_Price_Forecast.py", title="Price Forecast", icon="📉"),
        st.Page(f"{P}/25_Forecast_Accuracy.py", title="Forecast Accuracy", icon="🎯"),
        st.Page(f"{P}/6_DART_Spread.py", title="DA–RT Spread", icon="⚖️"),
        st.Page(f"{P}/8_Hub_Basis.py", title="Hub Basis", icon="🧭"),
        st.Page(f"{P}/7_Capture_Price.py", title="Capture Price", icon="🎯"),
        st.Page(f"{P}/15_Plant_Earnings.py", title="Plant Earnings", icon="💰"),
        st.Page(f"{P}/18_Full_Bill.py", title="Full Bill Estimator", icon="🧮"),
        st.Page(f"{P}/5_Invoice_Validation.py", title="Invoice Validation", icon="🧾"),
    ],
})

nav.run()
