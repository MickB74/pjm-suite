"""PJM weather reference points — major load centers for population-weighting.

PJM system load is driven by cooling/heating demand across its big metros. To
turn point weather into a system-level signal we sample ERA5 at each major load
center and combine with a population weight (~metro population in millions, a
proxy for that city's share of PJM cooling load).

Each point is tagged with the PJM transmission ``zone`` code exactly as it
appears in the ``hrl_load_metered`` store (COMED→CE, PECO→PE, PSEG→PS,
Pepco→PEP, BGE→BC, Dominion→DOM, …) so weather can join to zonal load.

DOM (Dominion, Virginia) is this suite's focus — Richmond, Norfolk, and the
Northern Virginia / DC load pocket carry it.
"""

from __future__ import annotations

import pandas as pd

# name, zone, latitude, longitude, pop_weight (metro millions)
_POINTS = [
    ("Chicago, IL",         "CE",   41.85, -87.65, 9.4),
    ("Washington, DC",      "PEP",  38.90, -77.04, 6.3),
    ("Philadelphia, PA",    "PE",   39.95, -75.16, 6.2),
    ("Newark, NJ",          "PS",   40.74, -74.17, 6.0),
    ("Baltimore, MD",       "BC",   39.29, -76.61, 2.8),
    ("Pittsburgh, PA",      "DUQ",  40.44, -79.99, 2.3),
    ("Cincinnati, OH",      "DEOK", 39.10, -84.51, 2.2),
    ("Columbus, OH",        "AEP",  39.96, -82.99, 2.1),
    ("Cleveland, OH",       "ATSI", 41.50, -81.69, 2.0),
    ("Norfolk, VA",         "DOM",  36.85, -76.29, 1.8),
    ("Richmond, VA",        "DOM",  37.54, -77.44, 1.3),
    ("Allentown, PA",       "PL",   40.60, -75.47, 0.9),
    ("Dayton, OH",          "DAY",  39.76, -84.19, 0.8),
    ("Trenton, NJ",         "JC",   40.22, -74.76, 0.9),
]

POINTS = pd.DataFrame(_POINTS, columns=["city", "zone", "lat", "lon", "pop_weight"])

# DOM-focus subset (Dominion zone cities)
DOM_CITIES = POINTS[POINTS["zone"] == "DOM"]["city"].tolist()


def points() -> pd.DataFrame:
    """Return the load-center table (copy)."""
    return POINTS.copy()


def weight_for(city: str) -> float:
    hit = POINTS[POINTS["city"] == city]
    return float(hit["pop_weight"].iloc[0]) if not hit.empty else 1.0
