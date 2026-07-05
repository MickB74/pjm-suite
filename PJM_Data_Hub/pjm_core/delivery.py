"""PJM utility (EDC) delivery-rate reference data — the retail "full bill" layer.

Wholesale LMP (``pjm_core.prices``) plus capacity (``pjm_core.capacity``) only
cover the **supply** side of a customer's bill. The rest — distribution,
transmission (NITS), the fixed customer charge, and per-kWh riders — is set by
each Electric Distribution Company (EDC) in its filed tariff and varies by PJM
zone and rate class. This module is that **delivery** layer, focused on the
**large commercial & industrial (C&I)** rate classes (primary / transmission
voltage, demand-metered), where the $/kW **demand charge** dominates the bill.

Two ways to populate it, mirroring ``pjm_core.capacity``:

1. **Seed table → editable CSV.** A representative row per zone is seeded below
   and mirrored to ``data/delivery_rates/edc_ci_delivery_rates.csv``. These seed
   numbers are order-of-magnitude placeholders (``verified == "no"``): replace
   them with the exact blocks from each EDC's filed tariff. Edit the CSV in place
   or via the Full Bill screen.

2. **NREL OpenEI URDB API** (``fetch_urdb``). Free structured tariffs — energy,
   demand, and fixed charges — keyed by utility. Get a free key at
   https://openei.org/services/api/ and store it via
   ``credentials.save_urdb_api_key``. URDB coverage of the largest
   transmission-voltage C&I classes is spotty, so treat it as a seed/cross-check
   for the CSV rather than the source of truth.

Bill math (per month), computed by ``estimate_delivery``:

    delivery $ = customer_charge_month
               + dist_demand_kw_month        × billing_demand_kw
               + transmission_demand_kw_month × nspl_kw            (NITS)
               + dist_energy_kwh             × kwh
               + riders_kwh                  × kwh

``billing_demand_kw`` is the metered monthly peak; ``nspl_kw`` is the customer's
Network Service Peak Load (defaults to the metered peak if not supplied).

Columns (edc_ci_delivery_rates.csv):
    zone, edc, rate_class, voltage, customer_charge_month,
    dist_demand_kw_month, transmission_demand_kw_month,
    dist_energy_kwh, riders_kwh, verified, source
"""

from __future__ import annotations

import json

import pandas as pd

from pjm_core import credentials, paths

# ---------------------------------------------------------------------------
# Seed table — representative LARGE C&I delivery components per PJM zone.
#
# ⚠️  These are order-of-magnitude PLACEHOLDERS (verified == "no"), good enough
# to see the full-bill composition work end-to-end. Replace each row with the
# exact charges from the EDC's currently effective tariff (or a URDB pull)
# before relying on the numbers. Charges are $/month (customer), $/kW-month
# (demand), and $/kWh (energy & riders).
#
# DOM, PECO and COMED are the first zones fleshed out (per request); the rest
# are structural stubs so every major zone resolves to *something*.
# ---------------------------------------------------------------------------
_SEED = [
    # zone, edc, rate_class, voltage, cust$/mo, dist$/kW, trans(NITS)$/kW, dist$/kWh, riders$/kWh, verified, source
    ("DOM",     "Dominion Energy Virginia",        "GS-4 Large General",   "Primary",      500.0, 4.50, 3.00, 0.0020, 0.0040, "no", "Dominion VA filed tariff — VERIFY"),
    ("PECO",    "PECO Energy",                      "HT High Tension",      "Primary",      180.0, 3.50, 4.00, 0.0000, 0.0050, "no", "PECO PA Tariff Electric Pa. P.U.C. No. 6 — VERIFY"),
    ("COMED",   "Commonwealth Edison",              "Rate 6L Very Large",   "Primary",      430.0, 6.50, 3.50, 0.0000, 0.0060, "no", "ComEd Rate 6 / Ill.C.C. No. 10 — VERIFY"),
    ("BGE",     "Baltimore Gas & Electric",         "Schedule GL",          "Primary",      300.0, 4.00, 3.50, 0.0010, 0.0050, "no", "BGE MD tariff — VERIFY"),
    ("PEPCO",   "Potomac Electric Power (Pepco)",   "GT Large Demand",      "Primary",      320.0, 4.20, 3.50, 0.0010, 0.0050, "no", "Pepco DC/MD tariff — VERIFY"),
    ("PPL",     "PPL Electric Utilities",           "LP4 Large Power",      "Primary",      250.0, 3.80, 4.00, 0.0000, 0.0050, "no", "PPL PA tariff — VERIFY"),
    ("PSEG",    "Public Service Electric & Gas",    "LPL Large Power",      "Primary",      280.0, 4.00, 4.20, 0.0000, 0.0055, "no", "PSE&G NJ tariff — VERIFY"),
    ("JCPL",    "Jersey Central Power & Light",     "GT Large General",     "Primary",      260.0, 3.90, 4.20, 0.0000, 0.0055, "no", "JCP&L NJ tariff — VERIFY"),
    ("AECO",    "Atlantic City Electric",           "AGS Large",            "Primary",      260.0, 3.90, 4.20, 0.0000, 0.0055, "no", "ACE NJ tariff — VERIFY"),
    ("METED",   "Metropolitan Edison (Met-Ed)",     "GS Large Power",       "Primary",      240.0, 3.70, 4.00, 0.0000, 0.0050, "no", "Met-Ed PA tariff — VERIFY"),
    ("PENELEC", "Pennsylvania Electric (Penelec)",  "GS Large Power",       "Primary",      240.0, 3.70, 4.00, 0.0000, 0.0050, "no", "Penelec PA tariff — VERIFY"),
    ("DPL",     "Delmarva Power",                   "GSP-Secondary Large",  "Primary",      270.0, 4.00, 3.80, 0.0010, 0.0050, "no", "Delmarva DE/MD tariff — VERIFY"),
    ("APS",     "Potomac Edison (Allegheny)",       "Schedule C Large",     "Primary",      230.0, 3.60, 3.80, 0.0000, 0.0045, "no", "Potomac Edison MD/WV/VA tariff — VERIFY"),
    ("DUQ",     "Duquesne Light",                   "Rate GL Large",        "Primary",      260.0, 4.10, 4.00, 0.0000, 0.0050, "no", "Duquesne PA tariff — VERIFY"),
    ("DAY",     "AES Ohio (DP&L)",                  "Rate GS-Primary",      "Primary",      250.0, 3.80, 3.50, 0.0000, 0.0045, "no", "AES Ohio tariff — VERIFY"),
    ("AEP",     "AEP Ohio",                         "GS-4 Large Primary",   "Primary",      280.0, 4.00, 3.50, 0.0000, 0.0050, "no", "AEP Ohio tariff — VERIFY"),
    ("DEOK",    "Duke Energy Ohio",                 "Rate DP Large",        "Primary",      270.0, 3.90, 3.50, 0.0000, 0.0050, "no", "Duke Ohio tariff — VERIFY"),
    ("EKPC",    "East Kentucky Power Coop",         "Large Industrial",     "Primary",      250.0, 3.80, 3.50, 0.0000, 0.0045, "no", "EKPC / member coop tariff — VERIFY"),
]

SEED_COLUMNS = [
    "zone", "edc", "rate_class", "voltage",
    "customer_charge_month", "dist_demand_kw_month",
    "transmission_demand_kw_month", "dist_energy_kwh", "riders_kwh",
    "verified", "source",
]

_NUMERIC = [
    "customer_charge_month", "dist_demand_kw_month",
    "transmission_demand_kw_month", "dist_energy_kwh", "riders_kwh",
]


def _seed_frame() -> pd.DataFrame:
    return pd.DataFrame(_SEED, columns=SEED_COLUMNS)


def ensure_seed() -> None:
    """Write the seed CSV if the user has no delivery file yet (idempotent)."""
    paths.DELIVERY_DIR.mkdir(parents=True, exist_ok=True)
    if not paths.DELIVERY_RATES_CSV.exists():
        _seed_frame().to_csv(paths.DELIVERY_RATES_CSV, index=False)


def load() -> pd.DataFrame:
    """Load the EDC C&I delivery-rate table (seeding the CSV on first use)."""
    ensure_seed()
    try:
        df = pd.read_csv(paths.DELIVERY_RATES_CSV)
    except Exception:
        df = _seed_frame()
    for col in _NUMERIC:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("zone").reset_index(drop=True)


def rate_for(zone: str, rate_class: str | None = None) -> dict | None:
    """Return the delivery-rate row for a zone (optionally a specific rate class)."""
    df = load()
    hit = df[df["zone"].str.upper() == zone.upper()]
    if rate_class is not None and not hit.empty:
        rc = hit[hit["rate_class"].str.contains(rate_class, case=False, na=False)]
        if not rc.empty:
            hit = rc
    if hit.empty:
        return None
    return hit.iloc[0].to_dict()


def estimate_delivery(
    zone: str,
    kwh: float,
    billing_demand_kw: float,
    nspl_kw: float | None = None,
    rate_class: str | None = None,
) -> dict | None:
    """Itemize the monthly delivery charge for a C&I customer in a PJM zone.

    Args:
        zone: PJM load zone (e.g. "DOM", "PECO", "COMED").
        kwh: monthly energy consumption.
        billing_demand_kw: metered monthly peak demand (kW) — bills the
            distribution demand charge.
        nspl_kw: Network Service Peak Load (kW) for the transmission/NITS charge.
            Defaults to ``billing_demand_kw`` when not supplied.
        rate_class: optional substring to pick a specific rate class.

    Returns an itemized dict of $ components plus the total, or None if the
    zone is unknown. All figures are one month.
    """
    r = rate_for(zone, rate_class)
    if r is None:
        return None
    nspl = billing_demand_kw if nspl_kw is None else nspl_kw

    customer = float(r["customer_charge_month"])
    dist_demand = float(r["dist_demand_kw_month"]) * billing_demand_kw
    trans_demand = float(r["transmission_demand_kw_month"]) * nspl
    dist_energy = float(r["dist_energy_kwh"]) * kwh
    riders = float(r["riders_kwh"]) * kwh
    total = customer + dist_demand + trans_demand + dist_energy + riders

    return {
        "zone": zone,
        "edc": r.get("edc"),
        "rate_class": r.get("rate_class"),
        "verified": r.get("verified"),
        "customer_charge": round(customer, 2),
        "distribution_demand": round(dist_demand, 2),
        "transmission_demand_nits": round(trans_demand, 2),
        "distribution_energy": round(dist_energy, 2),
        "riders": round(riders, 2),
        "total_delivery": round(total, 2),
        "delivery_per_kwh": round(total / kwh, 5) if kwh else None,
    }


# ---------------------------------------------------------------------------
# NREL OpenEI Utility Rate Database (URDB) — optional structured tariff pull.
# Docs: https://openei.org/services/doc/rest/util_rates/
# ---------------------------------------------------------------------------
URDB_ENDPOINT = "https://api.openei.org/utility_rates"


def fetch_urdb(
    utility: str,
    api_key: str | None = None,
    sector: str = "Commercial",
    limit: int = 25,
    cache: bool = True,
) -> list[dict]:
    """Fetch tariffs for a utility from the NREL OpenEI URDB and normalize them.

    Args:
        utility: utility name as URDB indexes it (e.g. "Commonwealth Edison Co").
        api_key: OpenEI key; falls back to the stored/env URDB key.
        sector: "Commercial", "Industrial", or "Residential".
        limit: max tariffs to return.
        cache: write the raw JSON to data/delivery_rates/urdb_raw/ for inspection.

    Returns a list of normalized dicts: name, sector, fixed_charge_month,
    max_demand_charge_kw, max_energy_charge_kwh, and a "raw" handle. Network
    access required — raises on request failure.
    """
    import urllib.parse
    import urllib.request

    key = (api_key or credentials.get_urdb_api_key()).strip()
    if not key:
        raise RuntimeError(
            "No URDB API key. Get a free key at https://openei.org/services/api/ "
            "and store it with credentials.save_urdb_api_key().")

    params = {
        "version": "latest",
        "format": "json",
        "api_key": key,
        "ratesforutility": utility,
        "sector": sector,
        "detail": "full",
        "limit": limit,
        "approved": "true",
    }
    url = f"{URDB_ENDPOINT}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    items = payload.get("items", [])
    if cache and items:
        paths.DELIVERY_URDB_RAW_DIR.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in utility)[:60]
        (paths.DELIVERY_URDB_RAW_DIR / f"{safe}_{sector}.json").write_text(
            json.dumps(payload, indent=2))

    return [_normalize_urdb(it) for it in items]


def _flatten_rate_structure(structure) -> list[float]:
    """Pull all 'rate' values out of a URDB {energy,demand}ratestructure."""
    rates: list[float] = []
    for period in structure or []:
        for tier in period or []:
            val = tier.get("rate") if isinstance(tier, dict) else None
            if isinstance(val, (int, float)):
                rates.append(float(val))
    return rates


def _normalize_urdb(item: dict) -> dict:
    """Reduce a raw URDB tariff to headline C&I bill drivers."""
    energy = _flatten_rate_structure(item.get("energyratestructure"))
    demand = _flatten_rate_structure(item.get("demandratestructure"))
    flat_demand = _flatten_rate_structure(item.get("flatdemandstructure"))
    all_demand = demand + flat_demand
    return {
        "name": item.get("name"),
        "utility": item.get("utility"),
        "sector": item.get("sector"),
        "fixed_charge_month": item.get("fixedchargefirstmeter"),
        "fixed_charge_units": item.get("fixedchargeunits"),
        "max_demand_charge_kw": max(all_demand) if all_demand else None,
        "max_energy_charge_kwh": max(energy) if energy else None,
        "min_energy_charge_kwh": min(energy) if energy else None,
        "start_date": item.get("startdate"),
        "uri": item.get("uri"),
    }


# ---------------------------------------------------------------------------
# Zone → URDB utility + large-C&I class hints, for refresh_from_urdb().
#
# `utility` must match how URDB indexes the EDC (ratesforutility). `hints` are
# class-name keywords ranked best-first — the selector prefers a demand-metered
# tariff whose name contains an earlier hint (large / primary / high-tension
# service), so we grab the correct large-C&I tier rather than a small-GS or
# residential-adjacent class.
# ---------------------------------------------------------------------------
URDB_ZONE_MAP = {
    "DOM":     {"utility": "Virginia Electric & Power Co",  "hints": ["gs-4", "large general", "primary", "large"]},
    "PECO":    {"utility": "PECO Energy Co",                "hints": ["gs-ht", "high tension", "gs-pd", "primary", "large"]},
    "COMED":   {"utility": "Commonwealth Edison Co",        "hints": ["very large", "large load", "6l", "rate 6", "medium load"]},
    "BGE":     {"utility": "Baltimore Gas & Electric Co",   "hints": ["schedule gl", "large", "primary", "general large"]},
    "PEPCO":   {"utility": "Potomac Electric Power Co",     "hints": ["gt", "large demand", "large", "primary"]},
    "PPL":     {"utility": "PPL Electric Utilities Corp",   "hints": ["lp4", "large power", "large", "primary"]},
    "PSEG":    {"utility": "Public Service Elec & Gas Co (New Jersey)", "hints": ["lpl", "large power", "large", "primary"]},
    "JCPL":    {"utility": "Jersey Central Power & Lt Co (New Jersey)", "hints": ["gt", "large", "general large", "primary"]},
    "AECO":    {"utility": "Atlantic City Electric Co",     "hints": ["ags", "large", "primary"]},
    "METED":   {"utility": "Metropolitan Edison Co (Pennsylvania)", "hints": ["large power", "gs large", "large", "primary"]},
    "PENELEC": {"utility": "Pennsylvania Electric Co (Pennsylvania)", "hints": ["large power", "gs large", "large", "primary"]},
    "DPL":     {"utility": "Delmarva Power",               "hints": ["gsp", "large", "primary"]},
    "APS":     {"utility": "The Potomac Edison Co (Maryland)", "hints": ["schedule c", "large", "primary"]},
    "DUQ":     {"utility": "Duquesne Light Co",             "hints": ["gl", "large", "primary"]},
    "DAY":     {"utility": "Dayton Power & Light Co",       "hints": ["primary", "large", "gs-primary"]},
    "AEP":     {"utility": "Ohio Power Co",                 "hints": ["gs-4", "large", "primary"]},
    "DEOK":    {"utility": "Duke Energy Ohio Inc",          "hints": ["dp", "large", "primary"]},
    "EKPC":    {"utility": "East Kentucky Power Coop, Inc", "hints": ["large", "industrial", "primary"]},
}


def _score_class(name: str, hints: list[str]) -> int:
    """Rank a tariff name against ordered hint keywords (earlier hint = better)."""
    lname = (name or "").lower()
    for i, h in enumerate(hints):
        if h in lname:
            return len(hints) - i  # earliest hint scores highest
    return 0


# Keywords that signal a delivery-only (unbundled) tariff vs a bundled one that
# folds in generation supply. We add LMP energy separately in the Full Bill
# screen, so a delivery-only class is required — a bundled one double-counts
# energy. A high per-kWh energy charge (> this) is the tell-tale of bundling.
_DELIVERY_WORDS = ("delivery", "unbundled", "distribution")
_BUNDLED_WORDS = ("bundled",)
_BUNDLED_ENERGY_KWH = 0.02  # delivery adders sit well below this; supply sits above


def _delivery_score(r: dict) -> int:
    """+ if the tariff looks delivery-only, − if it looks bundled."""
    lname = (r.get("name") or "").lower()
    energy = r.get("min_energy_charge_kwh")
    score = 0
    if any(w in lname for w in _DELIVERY_WORDS):
        score += 2
    if any(w in lname for w in _BUNDLED_WORDS):
        score -= 2
    if isinstance(energy, (int, float)) and energy > _BUNDLED_ENERGY_KWH:
        score -= 1  # priced-in generation → probably bundled
    return score


def _pick_large_ci(rows: list[dict], hints: list[str]) -> dict | None:
    """Choose the best large-C&I DELIVERY tariff: demand-metered, unbundled, on-hint."""
    demand_metered = [r for r in rows
                      if isinstance(r.get("max_demand_charge_kw"), (int, float))
                      and r["max_demand_charge_kw"] > 0]
    if not demand_metered:
        return None
    # Prefer delivery-only (avoid double-counting energy), then the right large-C&I
    # tier (hint match), then the big monthly customer charge of primary service.
    return sorted(
        demand_metered,
        key=lambda r: (_delivery_score(r),
                       _score_class(r["name"], hints),
                       r.get("fixed_charge_month") or 0),
        reverse=True,
    )[0]


def refresh_from_urdb(
    zones: list[str] | None = None,
    api_key: str | None = None,
) -> pd.DataFrame:
    """Overwrite delivery-table rows with real URDB tariffs for each zone.

    For every mapped zone, pulls the EDC's Commercial + Industrial tariffs,
    picks the large-C&I demand-metered class (via URDB_ZONE_MAP hints), and
    writes its fixed / demand / energy charges into
    ``edc_ci_delivery_rates.csv`` with ``verified == "urdb"``. The manual
    ``transmission_demand_kw_month`` (NITS) and ``riders_kwh`` columns are left
    untouched — URDB doesn't break those out.

    Returns a report DataFrame (zone, edc, chosen class, values, status).
    URDB is distribution-only, so these become the *delivery* backbone; NITS
    stays a manual estimate.
    """
    df = load()
    targets = zones or list(URDB_ZONE_MAP.keys())
    report = []

    for zone in targets:
        spec = URDB_ZONE_MAP.get(zone)
        if not spec:
            report.append({"zone": zone, "status": "no URDB mapping"})
            continue
        rows: list[dict] = []
        for sector in ("Commercial", "Industrial"):
            try:
                rows += fetch_urdb(spec["utility"], api_key=api_key,
                                   sector=sector, limit=25)
            except Exception as e:  # network / name miss — record, keep seed
                report.append({"zone": zone, "utility": spec["utility"],
                               "status": f"{sector} error: {e}"})
        chosen = _pick_large_ci(rows, spec["hints"]) if rows else None
        if chosen is None:
            report.append({"zone": zone, "utility": spec["utility"],
                           "status": "no demand-metered class found"})
            continue

        mask = df["zone"] == zone
        fixed = float(chosen.get("fixed_charge_month") or 0)
        demand = float(chosen.get("max_demand_charge_kw") or 0)
        energy = chosen.get("min_energy_charge_kwh")
        energy = float(energy) if isinstance(energy, (int, float)) else 0.0
        # A high per-kWh energy charge means URDB only had a BUNDLED class for
        # this utility (generation baked in). Flag it so the Full Bill screen
        # doesn't add LMP energy on top and double-count.
        bundled = energy > _BUNDLED_ENERGY_KWH
        df.loc[mask, "edc"] = spec["utility"]
        df.loc[mask, "rate_class"] = chosen.get("name")
        df.loc[mask, "customer_charge_month"] = round(fixed, 2)
        df.loc[mask, "dist_demand_kw_month"] = round(demand, 4)
        df.loc[mask, "dist_energy_kwh"] = round(energy, 5)
        df.loc[mask, "verified"] = "urdb-bundled" if bundled else "urdb"
        df.loc[mask, "source"] = f"URDB: {chosen.get('name')} ({chosen.get('uri','')})"
        report.append({
            "zone": zone, "edc": spec["utility"], "class": chosen.get("name"),
            "fixed_$mo": round(fixed, 2), "demand_$kw": round(demand, 4),
            "energy_$kwh": round(energy, 5), "status": "updated",
        })

    df.to_csv(paths.DELIVERY_RATES_CSV, index=False)
    return pd.DataFrame(report)
