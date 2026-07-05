"""Map EIA-923 plants to a PJM settlement zone.

EIA Form 923 (Page 1) reports each plant's **state** but not its PJM zone.
PJM zones are electric-distribution-company (EDC) territories that do not line
up 1:1 with states — several states hold multiple zones (e.g. PA = PPL / PECO /
PENELEC / METED / DUQ / APS). To value a plant's generation at a *locational*
LMP we need a plant → zone assignment.

Two layers, most-specific first:

  1. **Per-plant overrides** — a user-maintained CSV keyed by EIA ``plant_id``
     (``plant_zone_overrides.csv``). This is where you pin the large / important
     plants to their true zone. Editable from the Plant Earnings screen.
  2. **State default** — each state falls back to its *dominant* PJM zone
     (below). This is an approximation: a plant in a state's minority zone will
     be priced at the wrong (but same-state) zone until an override is added.

``zone_for`` returns the zone name (a key of ``settlement_points.ZONE_PNODE_IDS``)
or ``None`` when the state isn't in the PJM footprint.
"""

from __future__ import annotations

import pandas as pd

from pjm_core import paths
from pjm_core.settlement_points import ZONE_PNODE_IDS

# Dominant PJM zone per footprint state. Chosen as the zone carrying the most
# load / generation in that state. Approximate for multi-zone states — refine
# individual plants via the overrides CSV rather than editing this table.
STATE_DEFAULT_ZONE = {
    "DE": "DPL",      # Delmarva
    "DC": "PEPCO",    # Washington DC
    "IL": "COMED",    # ComEd (northern IL); southern IL is MISO, not PJM
    "IN": "AEP",      # AEP / DEOK straddle; AEP dominant
    "KY": "EKPC",     # East Kentucky Power Coop (+ DEOK, LGE/KU outside PJM)
    "MD": "BGE",      # BGE (+ PEPCO, DPL, APS shares)
    "MI": "AEP",      # small PJM slice (Indiana Michigan Power)
    "NJ": "PSEG",     # PSEG (+ JCPL, AECO, RECO)
    "NC": "DOM",      # Dominion NC (PJM part; much of NC is outside PJM)
    "OH": "AEP",      # AEP (+ DAY, DEOK, ATSI, DUQ)
    "PA": "PPL",      # PPL (+ PECO, PENELEC, METED, DUQ, APS)
    "TN": "EKPC",     # tiny PJM footprint
    "VA": "DOM",      # Dominion
    "WV": "APS",      # Allegheny Power / Potomac Edison (+ AEP, DOM shares)
}


def load_overrides() -> dict[str, str]:
    """Read the per-plant zone override table as ``{plant_id: zone}``.

    ``plant_id`` keys are normalised to strings so they match the EIA-923 store
    (which carries ``plant_id`` as a string). Unknown / blank rows are skipped.
    """
    p = paths.PLANT_ZONE_OVERRIDES_CSV
    if not p.exists():
        return {}
    try:
        df = pd.read_csv(p, dtype=str)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for _, row in df.iterrows():
        pid = str(row.get("plant_id", "")).strip()
        zone = str(row.get("zone", "")).strip().upper()
        if pid and zone in ZONE_PNODE_IDS:
            out[pid] = zone
    return out


def save_overrides(mapping: dict[str, str]) -> None:
    """Persist a ``{plant_id: zone}`` override table to CSV."""
    paths.ZONE_PRICES_DIR.mkdir(parents=True, exist_ok=True)
    rows = [{"plant_id": str(pid), "zone": zone}
            for pid, zone in sorted(mapping.items())]
    pd.DataFrame(rows, columns=["plant_id", "zone"]).to_csv(
        paths.PLANT_ZONE_OVERRIDES_CSV, index=False)


def zone_for(plant_id, state, overrides: dict[str, str] | None = None) -> str | None:
    """Zone for one plant: override first, else the state default."""
    overrides = load_overrides() if overrides is None else overrides
    pid = str(plant_id).strip()
    if pid in overrides:
        return overrides[pid]
    st = str(state).strip().upper()
    return STATE_DEFAULT_ZONE.get(st)


def assign_zones(df: pd.DataFrame) -> pd.DataFrame:
    """Add a ``zone`` column to an EIA-923 frame (plant_id + state).

    ``source`` marks how each row was mapped: ``override`` (pinned by plant_id)
    or ``state_default`` (state's dominant zone). Rows outside the PJM footprint
    get ``zone = NaN`` / ``zone_source = "unmapped"``.
    """
    overrides = load_overrides()
    out = df.copy()
    pid = out["plant_id"].astype(str).str.strip()
    st = out["state"].astype(str).str.strip().str.upper()

    override_zone = pid.map(overrides)
    default_zone = st.map(STATE_DEFAULT_ZONE)
    out["zone"] = override_zone.fillna(default_zone)
    out["zone_source"] = (
        override_zone.notna().map({True: "override", False: None})
        .fillna(default_zone.notna().map({True: "state_default", False: "unmapped"}))
    )
    return out
