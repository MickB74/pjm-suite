"""PJM trading hub reference list.

PJM publishes LMPs for aggregated pricing nodes (APNodes), zones, and
load-aggregation points (LAPs). The primary trading hubs used for financial
settlement and price benchmarking are listed here.

PJM LMP = Energy Component + Congestion Component + Loss Component.
"""

from __future__ import annotations

# PJM aggregate hub pnode_name values, exactly as PJM publishes them in the
# rt_hrl_lmps feed (and as gridstatus returns them in the "Location" column).
# These are the 12 PJM trading hubs. DOMINION HUB is our primary focus.
HUBS = [
    "DOMINION HUB",      # Dominion zone (Virginia / North Carolina) — primary focus
    "AEP-DAYTON HUB",    # AEP / Dayton Power & Light
    "AEP GEN HUB",       # AEP generation hub
    "ATSI GEN HUB",      # ATSI generation hub
    "CHICAGO HUB",       # ComEd / Chicago
    "CHICAGO GEN HUB",   # Chicago generation hub
    "EASTERN HUB",       # Eastern aggregate
    "WESTERN HUB",       # Western aggregate
    "N ILLINOIS HUB",    # Northern Illinois
    "NEW JERSEY HUB",    # New Jersey aggregate
    "OHIO HUB",          # Ohio aggregate
    "WEST INT HUB",      # Western interface hub
]

# PJM load zones (used for zone-settled contracts)
ZONES = [
    "AEP", "APS", "ATC", "BGE", "COMED", "DAY", "DEOK", "DOM",
    "DPL", "DUQ", "EKPC", "JCPL", "METED", "PECO", "PENELEC",
    "PEPCO", "PPL", "PSEG", "RECO",
]

# The primary hub for this suite
PRIMARY_HUB = "DOMINION HUB"

# LMP component columns returned by the PJM Data Miner API
LMP_COMPONENTS = ["total_lmp", "energy", "congestion", "loss"]
