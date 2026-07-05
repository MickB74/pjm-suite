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

# PJM zone pnode IDs, as published in the da_hrl_lmps / rt_hrl_lmps feed
# (type == "ZONE"). These are the load/transmission zones plants settle within;
# used to value plant generation by location in the Plant Earnings estimate.
# Pulled once from the live PJM `pnode` metadata (2026-07).
ZONE_PNODE_IDS = {
    "AECO":    51291,
    "AEP":     8445784,
    "APS":     8394954,
    "ATSI":    116013753,
    "BGE":     51292,
    "COMED":   33092371,
    "DAY":     34508503,
    "DEOK":    124076095,
    "DOM":     34964545,
    "DPL":     51293,
    "DUQ":     37737283,
    "EKPC":    970242670,
    "JCPL":    51295,
    "METED":   51296,
    "PECO":    51297,
    "PENELEC": 51300,
    "PEPCO":   51298,
    "PPL":     51299,
    "PSEG":    51301,
    "RECO":    7633629,
}

# The primary hub for this suite
PRIMARY_HUB = "DOMINION HUB"

# Approximate lat/lon for each trading hub, used for map visualizations.
# These are representative points within each hub's zone, not exact
# electrical locations (aggregate hubs don't have a single physical node).
HUB_COORDS = {
    "DOMINION HUB": (37.5407, -77.4360),     # Richmond, VA
    "AEP-DAYTON HUB": (39.7589, -84.1916),   # Dayton, OH
    "AEP GEN HUB": (39.9612, -82.9988),      # Columbus, OH
    "ATSI GEN HUB": (41.0814, -81.5190),     # Akron, OH
    "CHICAGO HUB": (41.8781, -87.6298),      # Chicago, IL
    "CHICAGO GEN HUB": (41.75, -87.85),      # Chicago (gen), offset
    "EASTERN HUB": (39.9526, -75.1652),      # Philadelphia, PA
    "WESTERN HUB": (40.4406, -79.9959),      # Pittsburgh, PA
    "N ILLINOIS HUB": (42.05, -88.20),       # Northern Illinois
    "NEW JERSEY HUB": (40.2171, -74.7429),   # Trenton, NJ
    "OHIO HUB": (40.10, -83.30),             # Central Ohio, offset from AEP GEN
    "WEST INT HUB": (40.50, -80.50),         # Western PA/OH interface
}

# LMP component columns returned by the PJM Data Miner API
LMP_COMPONENTS = ["total_lmp", "energy", "congestion", "loss"]
