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

# PJM load zones (used for zone-settled contracts). Derived from the pnode map
# above rather than hand-listed: the hand-maintained version had drifted, adding
# ATC (an MISO transmission company, not a PJM zone) while missing AECO and
# ATSI, both of which the LMP feed does publish.
ZONES = sorted(ZONE_PNODE_IDS)

# The primary hub for this suite
PRIMARY_HUB = "DOMINION HUB"

# Each trading hub's "home" load zone, using the short zone codes from PJM's
# hourly metered-load feed (CE = ComEd, PE = PECO, PS = PSEG, AP = APS, ...).
# Broad aggregates (WESTERN HUB, OHIO HUB, WEST INT HUB) span many zones and
# have no single home zone, so they are absent.
HUB_LOAD_ZONE = {
    "DOMINION HUB": "DOM",
    "AEP-DAYTON HUB": "AEP",
    "AEP GEN HUB": "AEP",
    "ATSI GEN HUB": "ATSI",
    "CHICAGO HUB": "CE",
    "CHICAGO GEN HUB": "CE",
    "N ILLINOIS HUB": "CE",
    "NEW JERSEY HUB": "PS",
    "EASTERN HUB": "PE",
}

# Reverse lookup: the hub that best represents a load zone's price. Where
# several hubs share a home zone, the first (the main trading hub) wins.
#
# NOTE: this covers only the 6 load zones that have a namesake trading hub. It
# is *not* a general "price for this zone" lookup — most zones have no hub, and
# defaulting them to PRIMARY_HUB would pair (say) PEPCO load with a Virginia
# price. Use LOAD_ZONE_PRICE_ZONE below to get a zone's own LMP instead.
ZONE_HOME_HUB: dict[str, str] = {}
for _hub, _zone in HUB_LOAD_ZONE.items():
    ZONE_HOME_HUB.setdefault(_zone, _hub)

# PJM's hourly metered-load feed uses short zone codes (PEP, CE, BC …) while
# the LMP feed publishes the same zones under their long names (PEPCO, COMED,
# BGE …). This maps load-feed code → LMP-feed zone so a zone's load and its own
# price can be lined up.
#
# RTO and OVEC are deliberately absent: RTO is the system-wide load aggregate
# with no single zonal LMP, and OVEC is a generation entity that appears in the
# load feed but has no load zone price. Callers must handle a missing key
# rather than substituting an unrelated zone.
LOAD_ZONE_PRICE_ZONE = {
    "AE": "AECO",        # Atlantic City Electric
    "AEP": "AEP",
    "AP": "APS",         # Allegheny Power / Potomac Edison
    "ATSI": "ATSI",
    "BC": "BGE",         # Baltimore Gas & Electric
    "CE": "COMED",       # Commonwealth Edison
    "DAY": "DAY",
    "DEOK": "DEOK",
    "DOM": "DOM",
    "DPL": "DPL",
    "DUQ": "DUQ",
    "EKPC": "EKPC",
    "JC": "JCPL",        # Jersey Central Power & Light
    "ME": "METED",       # Metropolitan Edison
    "PE": "PECO",
    "PEP": "PEPCO",
    "PL": "PPL",
    "PN": "PENELEC",     # Pennsylvania Electric
    "PS": "PSEG",
    "RECO": "RECO",      # Rockland Electric
}

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

# Regional clusters. Several hubs are drawn from overlapping geography (a hub is
# just a basket of pnodes, and different hubs re-use the same nodes for different
# purposes), so grouping them by region makes the overlap visible on the map.
HUB_CLUSTERS = {
    "DOMINION HUB": "Southern (Dominion)",
    "EASTERN HUB": "Eastern (PECO/PSEG)",
    "NEW JERSEY HUB": "Eastern (PECO/PSEG)",
    "WESTERN HUB": "Western Hub benchmark",
    "WEST INT HUB": "Western Hub benchmark",
    "AEP-DAYTON HUB": "AEP / W. Ohio",
    "AEP GEN HUB": "AEP / W. Ohio",
    "OHIO HUB": "AEP / W. Ohio",
    "ATSI GEN HUB": "AEP / W. Ohio",
    "N ILLINOIS HUB": "ComEd / Chicago",
    "CHICAGO HUB": "ComEd / Chicago",
    "CHICAGO GEN HUB": "ComEd / Chicago",
}

# Conceptual nesting of the hubs: a broad regional aggregate contains narrower
# trading hubs, which in turn sit "above" the generation-weighted node hubs of
# the same area. This is a readability aid (broad → trading → generation), not an
# official PJM parent/child relationship. Format: hub -> (level, parent_or_None).
#   level 0 = broad regional / benchmark, 1 = trading/zone, 2 = generation node
HUB_HIERARCHY = {
    "WESTERN HUB":     (0, None),              # RTO trading benchmark
    "WEST INT HUB":    (1, "WESTERN HUB"),
    "OHIO HUB":        (0, None),              # broad Ohio aggregate
    "AEP-DAYTON HUB":  (1, "OHIO HUB"),
    "AEP GEN HUB":     (2, "AEP-DAYTON HUB"),
    "ATSI GEN HUB":    (1, "OHIO HUB"),
    "N ILLINOIS HUB":  (0, None),              # broad ComEd aggregate
    "CHICAGO HUB":     (1, "N ILLINOIS HUB"),
    "CHICAGO GEN HUB": (2, "CHICAGO HUB"),
    "EASTERN HUB":     (0, None),
    "NEW JERSEY HUB":  (0, None),
    "DOMINION HUB":    (0, None),
}

# LMP component columns returned by the PJM Data Miner API
LMP_COMPONENTS = ["total_lmp", "energy", "congestion", "loss"]
