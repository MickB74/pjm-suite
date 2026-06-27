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

P = "screens"

nav = st.navigation({
    "Start Here": [
        st.Page(f"{P}/0_API_Keys.py", title="API Keys", icon="🔑", default=True),
    ],
    "Explore": [
        st.Page(f"{P}/1_Hub_Prices.py", title="Hub Prices (LMP)", icon="💵"),
        st.Page(f"{P}/2_System_Generation.py", title="System Generation", icon="🔥"),
        st.Page(f"{P}/3_EIA_923.py", title="EIA-923 Generation", icon="📅"),
    ],
    "Analyze": [
        st.Page(f"{P}/4_Price_Forecast.py", title="Price Forecast", icon="📉"),
    ],
})

nav.run()
