"""PPA settlement math: hourly generation × LMP vs a contract price.

PJM settles in hourly intervals (unlike ERCOT's 15-min). Each interval is
1.0 h of real elapsed time. Spring-forward days have 23 intervals; fall-back
days have 25 intervals.

Structures reported side by side:
  Merchant   Σ gen_MWh × price
  PPA        Σ gen_MWh × strike
  CfD/swap   Σ gen_MWh × (price − strike)   offtaker-signed

Plus volume, generation-weighted capture price, and optional basis settlement
when both nodal and hub prices are supplied.
"""

from __future__ import annotations

import pandas as pd

from pjm_core import tz

INTERVAL_HOURS = 1.0  # PJM settles hourly


def _aware(df: pd.DataFrame, col: str = "datetime_beginning_ept") -> pd.Series:
    """Lift naive Eastern interval timestamps to tz-aware Eastern."""
    return tz.localize_eastern(df[col])


def settle(
    generation: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    strike: float,
    gen_col: str = "generation_mw",
    gen_time_col: str = "datetime_beginning_ept",
    price_col: str = "total_lmp",
    price_time_col: str = "datetime_beginning_ept",
    price_floor: float | None = 0.0,
    settle_below_floor: bool = False,
    volume_fraction: float = 1.0,
    node_prices: pd.DataFrame | None = None,
    hub_price_col: str = "total_lmp",
) -> pd.DataFrame:
    """Compute per-interval PPA settlement.

    Args:
        generation: DataFrame with gen_time_col (naive Eastern) and gen_col (MW).
        prices: DataFrame with price_time_col (naive Eastern) and price_col ($/MWh).
        strike: contract strike price ($/MWh).
        price_floor: if set, intervals below this price are handled per
            settle_below_floor. Default 0.0 (standard VPPA: exclude sub-zero).
        settle_below_floor: if False (default), exclude sub-floor intervals from
            totals. If True, floor the price but keep the interval.
        volume_fraction: fraction of generation volume the contract covers
            (e.g. 0.5 for a 50% share contract). Default 1.0.
        node_prices: optional nodal prices for basis tracking (same schema as prices).
        hub_price_col: column in prices for hub LMP when basis is tracked.

    Returns DataFrame with columns:
        datetime_beginning_ept, gen_mw, gen_mwh, market_price, settled_price,
        merchant_revenue, ppa_revenue, cfd_settlement, basis_settlement (when node prices given).
    """
    gen = generation.copy()
    px = prices.copy()

    gen["_t"] = _aware(gen, gen_time_col)
    px["_t"] = _aware(px, price_time_col)

    merged = gen.merge(
        px[["_t", price_col]].rename(columns={price_col: "market_price"}),
        on="_t", how="inner",
    )

    merged["gen_mw"] = pd.to_numeric(merged[gen_col], errors="coerce").fillna(0.0)
    merged["gen_mwh"] = merged["gen_mw"] * INTERVAL_HOURS * volume_fraction

    if price_floor is not None:
        below_floor = merged["market_price"] < price_floor
        if settle_below_floor:
            merged["settled_price"] = merged["market_price"].clip(lower=price_floor)
        else:
            merged["settled_price"] = merged["market_price"]
            merged.loc[below_floor, "gen_mwh"] = 0.0  # exclude from totals
    else:
        merged["settled_price"] = merged["market_price"]

    merged["merchant_revenue"] = merged["gen_mwh"] * merged["settled_price"]
    merged["ppa_revenue"] = merged["gen_mwh"] * strike
    merged["cfd_settlement"] = merged["gen_mwh"] * (merged["settled_price"] - strike)

    if node_prices is not None:
        np2 = node_prices.copy()
        np2["_t"] = _aware(np2, price_time_col)
        merged = merged.merge(
            np2[["_t", hub_price_col]].rename(columns={hub_price_col: "node_price"}),
            on="_t", how="left",
        )
        merged["basis_settlement"] = merged["gen_mwh"] * (
            merged["node_price"].fillna(merged["settled_price"]) - merged["settled_price"]
        )

    merged["datetime_beginning_ept"] = merged[gen_time_col]
    keep = ["datetime_beginning_ept", "gen_mw", "gen_mwh",
            "market_price", "settled_price",
            "merchant_revenue", "ppa_revenue", "cfd_settlement"]
    if "basis_settlement" in merged.columns:
        keep.append("basis_settlement")

    return merged[keep].sort_values("datetime_beginning_ept").reset_index(drop=True)


def summarize(settled: pd.DataFrame) -> dict:
    """Aggregate interval-level settlement to monthly and total summaries."""
    if settled.empty:
        return {}
    s = settled.copy()
    s["month"] = pd.to_datetime(s["datetime_beginning_ept"]).dt.to_period("M")

    monthly = s.groupby("month").agg(
        gen_mwh=("gen_mwh", "sum"),
        merchant_revenue=("merchant_revenue", "sum"),
        ppa_revenue=("ppa_revenue", "sum"),
        cfd_settlement=("cfd_settlement", "sum"),
    )
    total_mwh = s["gen_mwh"].sum()
    total_merchant = s["merchant_revenue"].sum()
    capture_price = total_merchant / total_mwh if total_mwh else float("nan")

    return {
        "monthly": monthly,
        "total_gen_mwh": total_mwh,
        "total_merchant_revenue": total_merchant,
        "total_ppa_revenue": s["ppa_revenue"].sum(),
        "total_cfd_settlement": s["cfd_settlement"].sum(),
        "capture_price": capture_price,
        "intervals": len(s),
    }
