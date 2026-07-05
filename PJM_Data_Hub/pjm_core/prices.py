"""PJM hub LMP access layer.

Reads the local hub_prices parquet store (populated by datasets/hub_prices/pjm_api.py)
and exposes it in a tidy schema usable by the settlement and price-forecast modules.

PJM LMPs are hourly (unlike ERCOT's 15-min SPPs). Each row covers one delivery
hour identified by datetime_beginning_ept / datetime_ending_ept.
"""

from __future__ import annotations

import pandas as pd

from pjm_core import paths

PRICE_COLUMNS = [
    "datetime_beginning_ept", "datetime_ending_ept",
    "pnode_name", "type", "market",
    "total_lmp", "energy", "congestion", "loss",
    "source",
]


def load_hub_prices(
    hubs: list[str] | None = None,
    start=None,
    end_excl=None,
    market: str | None = "RT",
) -> pd.DataFrame:
    """Load PJM hub LMPs from the local parquet store.

    Args:
        hubs: filter to these pnode_name values; None = all hubs.
        start: inclusive lower bound on datetime_beginning_ept.
        end_excl: exclusive upper bound on datetime_beginning_ept.
        market: "RT" or "DA" (None = both markets).

    Returns a DataFrame with columns matching PRICE_COLUMNS (source added).
    """
    if not paths.HUB_PRICES_PARQUET.exists():
        return pd.DataFrame(columns=PRICE_COLUMNS)

    df = pd.read_parquet(paths.HUB_PRICES_PARQUET)
    if df.empty:
        return pd.DataFrame(columns=PRICE_COLUMNS)

    df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
    df["datetime_ending_ept"] = pd.to_datetime(df["datetime_ending_ept"])

    # Stores written before DA support have no market column: all rows are RT.
    if "market" not in df.columns:
        df = df.copy()
        df["market"] = "RT"
    if market:
        df = df[df["market"] == market]

    if hubs:
        df = df[df["pnode_name"].isin(hubs)]
    if start is not None:
        df = df[df["datetime_beginning_ept"] >= pd.Timestamp(start)]
    if end_excl is not None:
        df = df[df["datetime_beginning_ept"] < pd.Timestamp(end_excl)]

    if "source" not in df.columns:
        df = df.copy()
        df["source"] = "pjm_hub_store"

    cols = [c for c in PRICE_COLUMNS if c in df.columns]
    return df[cols].sort_values(["pnode_name", "datetime_beginning_ept"]).reset_index(drop=True)


def hub_store_coverage() -> tuple | None:
    """(min_datetime_beginning_ept, max_datetime_beginning_ept) or None."""
    if not paths.HUB_PRICES_PARQUET.exists():
        return None
    df = pd.read_parquet(paths.HUB_PRICES_PARQUET, columns=["datetime_beginning_ept"])
    if df.empty:
        return None
    ts = pd.to_datetime(df["datetime_beginning_ept"])
    return (ts.min(), ts.max())
