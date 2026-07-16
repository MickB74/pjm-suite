"""PJM capacity market (RPM) reference data.

PJM procures capacity through the Reliability Pricing Model (RPM) Base Residual
Auction (BRA), held ~3 years ahead of each Delivery Year (June 1 – May 31).
Clearing prices are set in **$/MW-day** and vary by Locational Deliverability
Area (LDA): the RTO-wide price plus constrained sub-zones (e.g. DOM, EMAAC,
BGE, PEPCO) that can separate to a higher local price.

**There is no PJM Data Miner API for RPM clearing prices** — PJM posts them only
in the periodic "BRA Planning Parameters / Results" reports. So this is a
*user-maintained reference table* seeded below and mirrored to a CSV you can
edit as new auctions clear:

    data/capacity/rpm_bra_clearing_prices.csv

Source of truth — verify/update against PJM's published results:
  https://www.pjm.com/markets-and-operations/rpm

Columns: delivery_year (e.g. "2025/2026"), lda ("RTO", "DOM", …),
         clearing_price_mw_day ($/MW-day), auction (e.g. "BRA").
"""

from __future__ import annotations

import pandas as pd

from pjm_core import paths

# ---------------------------------------------------------------------------
# Seed table — RPM Base Residual Auction Resource Clearing Prices ($/MW-day).
# RTO row for every delivery year; additional LDA rows only where that LDA
# separated (cleared above the RTO price). When all LDAs clear at the price
# cap, only the RTO row is needed. VERIFY against PJM's posted BRA results;
# edit the CSV (data/capacity/rpm_bra_clearing_prices.csv) to correct/extend.
# ---------------------------------------------------------------------------
_SEED = [
    # delivery_year, lda, clearing_price_mw_day, auction
    # Source: PJM BRA reports (https://www.pjm.com/markets-and-operations/rpm)
    # Only separated (constrained) LDAs get their own row; unseparated LDAs
    # inherit the RTO price via price_for() fallback.
    ("2007/2008", "RTO",  40.80, "BRA"),
    ("2008/2009", "RTO", 111.92, "BRA"),
    ("2009/2010", "RTO", 102.04, "BRA"),
    ("2010/2011", "RTO", 174.29, "BRA"),
    ("2011/2012", "RTO", 110.00, "BRA"),
    ("2012/2013", "RTO",  16.46, "BRA"),
    ("2013/2014", "RTO",  27.73, "BRA"),
    ("2014/2015", "RTO", 125.99, "BRA"),
    ("2015/2016", "RTO", 136.00, "BRA"),
    ("2016/2017", "RTO",  59.37, "BRA"),
    ("2017/2018", "RTO", 120.00, "BRA"),
    ("2018/2019", "RTO", 164.77, "BRA"),
    ("2019/2020", "RTO", 100.00, "BRA"),
    ("2020/2021", "RTO",  76.53, "BRA"),
    ("2021/2022", "RTO", 140.00, "BRA"),
    ("2022/2023", "RTO",   50.00, "BRA"),
    ("2022/2023", "EMAAC", 95.79, "BRA"),
    ("2022/2023", "MAAC",  97.86, "BRA"),
    ("2022/2023", "BGE",  126.50, "BRA"),
    ("2023/2024", "RTO",   34.13, "BRA"),
    ("2023/2024", "MAAC",  49.49, "BRA"),
    ("2023/2024", "BGE",   69.95, "BRA"),
    ("2024/2025", "RTO",   28.92, "BRA"),
    ("2024/2025", "EMAAC", 54.95, "BRA"),
    ("2024/2025", "MAAC",  49.49, "BRA"),
    ("2024/2025", "SWMAAC",49.49, "BRA"),
    ("2024/2025", "BGE",   73.00, "BRA"),
    ("2025/2026", "RTO",  269.92, "BRA"),
    ("2025/2026", "DOM",  444.26, "BRA"),
    ("2025/2026", "BGE",  466.35, "BRA"),
    ("2026/2027", "RTO",  329.17, "BRA"),   # price cap; all LDAs uniform
    ("2027/2028", "RTO",  333.44, "BRA"),   # price cap; all LDAs uniform
    ("2028/2029", "RTO",  325.00, "BRA"),   # price cap; all LDAs uniform
]

SEED_COLUMNS = ["delivery_year", "lda", "clearing_price_mw_day", "auction"]


def _seed_frame() -> pd.DataFrame:
    return pd.DataFrame(_SEED, columns=SEED_COLUMNS)


def ensure_seed() -> None:
    """Write the seed CSV if the user has no capacity file yet (idempotent)."""
    paths.CAPACITY_DIR.mkdir(parents=True, exist_ok=True)
    if not paths.CAPACITY_CSV.exists():
        _seed_frame().to_csv(paths.CAPACITY_CSV, index=False)


def load() -> pd.DataFrame:
    """Load the RPM clearing-price table (seeding the CSV on first use)."""
    ensure_seed()
    try:
        df = pd.read_csv(paths.CAPACITY_CSV)
    except Exception:
        df = _seed_frame()
    df["clearing_price_mw_day"] = pd.to_numeric(
        df["clearing_price_mw_day"], errors="coerce")
    # Derived monthly & annual $/MW figures for quick cost math.
    df["clearing_price_mw_year"] = df["clearing_price_mw_day"] * 365.0
    return df.sort_values(["lda", "delivery_year"]).reset_index(drop=True)


def price_for(delivery_year: str, lda: str = "RTO") -> float | None:
    """Clearing price ($/MW-day) for a delivery year & LDA (falls back to RTO)."""
    df = load()
    hit = df[(df["delivery_year"] == delivery_year) & (df["lda"] == lda)]
    if hit.empty and lda != "RTO":
        hit = df[(df["delivery_year"] == delivery_year) & (df["lda"] == "RTO")]
    if hit.empty:
        return None
    return float(hit["clearing_price_mw_day"].iloc[0])


def annual_capacity_cost(mw: float, delivery_year: str, lda: str = "RTO") -> float | None:
    """Annual capacity charge ($) for `mw` of obligation in a delivery year."""
    p = price_for(delivery_year, lda)
    return None if p is None else p * mw * 365.0
