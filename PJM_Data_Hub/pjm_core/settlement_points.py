"""PJM trading hub reference list.

PJM publishes LMPs for aggregated pricing nodes (APNodes), zones, and
load-aggregation points (LAPs). The primary trading hubs used for financial
settlement and price benchmarking are listed here.

PJM LMP = Energy Component + Congestion Component + Loss Component.
"""

from __future__ import annotations

# PJM Aggregate Hub pricing nodes. These pnode_name values are used in the
# Data Miner 2 API (rt_hrl_lmps / da_hrl_lmps).
HUBS = [
    "DOM HUB",          # Dominion Hub (Virginia / North Carolina) — primary focus
    "AEP-DAYTON HUB",   # AEP Ohio / Dayton Power & Light zone
    "COMED HUB",        # ComEd (Northern Illinois)
    "NI HUB",           # Northern Illinois (closely tracks COMED HUB)
    "EASTERN HUB",      # Eastern aggregate
    "WESTERN HUB",      # Western aggregate
    "AECO HUB",         # Atlantic City Electric zone
]

# PJM load zones (used for zone-settled contracts)
ZONES = [
    "AEP", "APS", "ATC", "BGE", "COMED", "DAY", "DEOK", "DOM",
    "DPL", "DUQ", "EKPC", "JCPL", "METED", "PECO", "PENELEC",
    "PEPCO", "PPL", "PSEG", "RECO",
]

# The primary hub for this suite
PRIMARY_HUB = "DOM HUB"

# LMP component columns returned by the PJM Data Miner API
LMP_COMPONENTS = ["total_lmp", "energy", "congestion", "loss"]
