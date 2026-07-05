"""Estimate PJM plant *energy* revenue: EIA-923 MWh × zonal LMP.

There is **no public record of what a PJM plant actually earns** — PJM does not
disclose per-unit dispatch or settlement. This module reconstructs a defensible
*estimate* from two public sources:

  * **Generation (MWh)** — EIA Form 923, monthly net generation per plant × fuel
    (``datasets/eia923``).
  * **Price ($/MWh)** — the monthly-average LMP of the plant's PJM **zone**
    (``datasets/zone_prices``), with the plant → zone mapping from
    ``pjm_core.plant_zones``.

    estimated energy revenue = net_generation_mwh × zone monthly avg LMP

Caveats baked into the naming (``est_`` prefix everywhere):
  * **Energy only.** Excludes capacity (RPM), ancillary, REC/PTC, and any
    PPA / hedge — a merchant unit's real revenue can differ materially.
  * **Zone, not node.** Uses the zone LMP, not the plant's own generator-node
    LMP (PJM publishes GEN-node LMPs but there is no clean EIA-plant → node
    crosswalk). Congestion at the specific node is not captured.
  * **Monthly average price × monthly MWh** — no intra-month hourly capture
    weighting (EIA generation is monthly, so a finer weighting isn't possible
    from this source). This tends to *overstate* solar/wind (which produce in
    lower-priced hours) and is roughly neutral for baseload.
  * **Market choice.** Defaults to the Day-Ahead LMP (where most generation
    self-schedules); Real-Time is available for comparison.
"""

from __future__ import annotations

import pandas as pd

from datasets.eia923.eia923 import load as load_eia, fuel_group
from datasets.zone_prices.pjm_zone_prices import load_monthly as load_zone_lmp
from pjm_core import plant_zones, capacity

# Rough class-average capacity credit (UCAP / ELCC) by fuel — the share of a
# unit's summer MW that counts toward a PJM capacity obligation and is paid the
# RPM clearing price. Approximations for an *estimate*; thermal ~0.85–0.95,
# variable resources far lower (PJM ELCC ratings). Tune as needed.
CAPACITY_CREDIT = {
    "Nuclear": 0.95, "Gas": 0.90, "Coal": 0.88, "Oil": 0.90,
    "Hydro": 0.40, "Biomass": 0.85, "Geothermal": 0.90,
    "Wind": 0.15, "Solar": 0.09, "Storage": 0.55, "Other": 0.50,
}

# PJM capacity clears by Locational Deliverability Area (LDA). Our RPM table
# (pjm_core.capacity) carries the RTO-wide price plus the DOM LDA when it
# separated; map each zone to its LDA (fallback RTO handled by price_for).
ZONE_TO_LDA = {"DOM": "DOM"}


def _delivery_year(cal_year: int) -> str:
    """PJM delivery year (Jun 1–May 31) that begins in `cal_year`."""
    return f"{cal_year}/{cal_year + 1}"


def estimate(years: list[int] | None = None, market: str = "DA",
             pjm_only: bool = True) -> pd.DataFrame:
    """Return a tidy plant × fuel × month table with estimated energy revenue.

    Columns: year, month, plant_id, plant_name, state, fuel_type, source,
    zone, zone_source, net_generation_mwh, avg_lmp (+ components), market,
    est_revenue, est_rev_per_mwh.

    ``pjm_only`` (default True) keeps only plants whose EIA balancing-authority
    code is "PJM" — excluding same-state non-PJM units (Duke Carolinas, MISO,
    TVA, …). Falls back to the state footprint if the store predates ba_code.

    Rows with no monthly generation, no zone mapping, or no zone price for that
    month drop out of the revenue (``est_revenue`` is NaN) but are retained so
    the screen can report coverage.
    """
    gen = load_eia(years=years)
    if gen.empty:
        return gen

    gen = gen.dropna(subset=["month"]).copy()
    # Drop EIA's "State-Fuel Level Increment" adjustment rows (plant_id 99999) —
    # a per-state reconciliation figure, not a real plant.
    gen = gen[gen["plant_id"].astype(str).str.strip() != "99999"]
    # Restrict to true PJM units by balancing-authority code when available.
    if pjm_only and "ba_code" in gen.columns:
        gen = gen[gen["ba_code"].astype(str).str.strip().str.upper() == "PJM"]
    gen["month"] = gen["month"].astype(int)
    gen["year"] = gen["year"].astype(int)
    gen["source"] = gen["fuel_type"].map(fuel_group)
    gen = plant_zones.assign_zones(gen)

    lmp = load_zone_lmp(market=market)
    if lmp.empty:
        gen["market"] = market
        for c in ["avg_lmp", "avg_energy", "avg_congestion", "avg_loss",
                  "est_revenue", "est_rev_per_mwh"]:
            gen[c] = pd.NA
        return gen

    lmp = lmp[["zone", "year", "month", "avg_lmp",
               "avg_energy", "avg_congestion", "avg_loss"]].copy()
    merged = gen.merge(lmp, on=["zone", "year", "month"], how="left")
    merged["market"] = market
    merged["est_revenue"] = merged["net_generation_mwh"] * merged["avg_lmp"]
    merged["est_rev_per_mwh"] = merged["avg_lmp"]
    return merged.reset_index(drop=True)


def capacity_by_plant(cal_year: int, eia860_year: int | None = None) -> pd.DataFrame:
    """Estimated *annual* capacity revenue per plant for a calendar year.

        est_capacity_rev = summer_mw × capacity_credit(fuel)
                           × RPM clearing $/MW-day (plant's LDA) × 365

    Uses EIA-860 summer capacity (latest vintage ≤ eia860_year) and the RPM/BRA
    clearing price for the delivery year beginning that June. This is a full
    delivery-year annual figure — it is NOT prorated to partial generation
    years. Returns one row per plant.
    """
    from datasets.eia860.eia860 import load as load_860

    cap = load_860(year=eia860_year)
    if cap.empty:
        return pd.DataFrame()

    cap = plant_zones.assign_zones(cap)
    cap["credit"] = cap["fuel_group"].map(CAPACITY_CREDIT).fillna(0.5)
    cap["ucap_mw"] = cap["summer_mw"].fillna(0) * cap["credit"]

    dy = _delivery_year(cal_year)
    lda = cap["zone"].map(ZONE_TO_LDA).fillna("RTO")
    price = lda.map(lambda l: capacity.price_for(dy, l))
    cap["rpm_price_mw_day"] = pd.to_numeric(price, errors="coerce")
    cap["est_capacity_rev"] = cap["ucap_mw"] * cap["rpm_price_mw_day"] * 365.0

    grp = (cap.groupby(["plant_id", "plant_name", "state", "zone"], as_index=False)
           .agg(summer_mw=("summer_mw", "sum"),
                ucap_mw=("ucap_mw", "sum"),
                est_capacity_rev=("est_capacity_rev", "sum")))
    grp["delivery_year"] = dy
    return grp


def by_plant(est: pd.DataFrame, cal_year: int | None = None,
             with_capacity: bool = False) -> pd.DataFrame:
    """Roll a tidy estimate up to one row per plant (summed over fuel × month)."""
    if est.empty:
        return est
    grp = (est.groupby(["plant_id", "plant_name", "state", "zone", "zone_source"],
                       as_index=False)
           .agg(net_generation_mwh=("net_generation_mwh", "sum"),
                est_revenue=("est_revenue", "sum")))
    grp["est_rev_per_mwh"] = grp["est_revenue"] / grp["net_generation_mwh"]

    if with_capacity:
        yr = cal_year if cal_year is not None else int(est["year"].max())
        cap = capacity_by_plant(yr)
        if not cap.empty:
            # Prorate the full-delivery-year capacity revenue to the same window
            # the energy figure covers, so energy + capacity are comparable. A
            # partial year (e.g. Jan–Apr = 4 of 12 months) scales capacity ×4/12.
            n_months = int(est.loc[est["year"] == yr, "month"].nunique()) or 12
            frac = n_months / 12.0
            cap = cap.copy()
            cap["est_capacity_rev"] = cap["est_capacity_rev"] * frac
            grp = grp.merge(cap[["plant_id", "summer_mw", "ucap_mw",
                                 "est_capacity_rev", "delivery_year"]],
                            on="plant_id", how="left")
            grp["est_total_rev"] = (grp["est_revenue"].fillna(0)
                                    + grp["est_capacity_rev"].fillna(0))
            grp.attrs["capacity_months"] = n_months
        else:
            grp["est_capacity_rev"] = pd.NA
            grp["est_total_rev"] = grp["est_revenue"]
        return grp.sort_values("est_total_rev", ascending=False).reset_index(drop=True)

    return grp.sort_values("est_revenue", ascending=False).reset_index(drop=True)


def coverage(est: pd.DataFrame) -> dict:
    """Share of generation that got a price (i.e. mapped zone with LMP data)."""
    if est.empty:
        return {"mwh_total": 0.0, "mwh_priced": 0.0, "priced_share": 0.0,
                "unmapped_mwh": 0.0}
    total = est["net_generation_mwh"].sum()
    priced = est.loc[est["est_revenue"].notna(), "net_generation_mwh"].sum()
    unmapped = est.loc[est["zone"].isna(), "net_generation_mwh"].sum()
    return {
        "mwh_total": float(total),
        "mwh_priced": float(priced),
        "priced_share": float(priced / total) if total else 0.0,
        "unmapped_mwh": float(unmapped),
    }
