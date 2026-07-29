"""Hourly zone-LMP store and the load-zone → price-zone crosswalk.

The hourly store exists because most PJM load zones have no namesake trading
hub — pairing such a zone's load with a hub price (as the peak-day view once
did via a PRIMARY_HUB default) silently reports an unrelated price as
coincident. These pin the crosswalk and the store's completeness bookkeeping.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pjm_core import paths
from pjm_core.settlement_points import (
    HUB_LOAD_ZONE, LOAD_ZONE_PRICE_ZONE, ZONE_HOME_HUB, ZONES)
from datasets.zone_prices import pjm_zone_prices as Z


# ── Crosswalk ────────────────────────────────────────────────────────────────

def test_every_mapped_target_is_a_real_price_zone():
    """The load feed's short codes must resolve to zones the LMP feed publishes."""
    assert set(LOAD_ZONE_PRICE_ZONE.values()) <= set(ZONES)


def test_crosswalk_is_injective():
    """Two load zones mapping to one price zone would double-count a price."""
    vals = list(LOAD_ZONE_PRICE_ZONE.values())
    assert len(vals) == len(set(vals))


def test_aggregate_and_non_load_zones_are_absent():
    """RTO is the system aggregate and OVEC is a generation entity; neither has
    a zonal LMP. They must be missing so callers fall back deliberately."""
    assert "RTO" not in LOAD_ZONE_PRICE_ZONE
    assert "OVEC" not in LOAD_ZONE_PRICE_ZONE


def test_crosswalk_covers_every_zone_that_has_a_home_hub():
    """Any zone good enough for the hub map must also resolve to its own price,
    otherwise the better data source is unavailable exactly where it exists."""
    for zone in ZONE_HOME_HUB:
        assert zone in LOAD_ZONE_PRICE_ZONE, zone


def test_hub_home_zones_use_the_same_short_codes():
    assert set(HUB_LOAD_ZONE.values()) <= set(LOAD_ZONE_PRICE_ZONE)


# ── Hourly store ─────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_hourly(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ZONE_PRICES_HOURLY_PARQUET",
                        tmp_path / "hourly.parquet")
    monkeypatch.setattr(paths, "ZONE_PRICES_DIR", tmp_path)
    return tmp_path


def _hours(year: int, month: int, zones=("PEPCO", "DOM"), market="RT",
           days: int | None = None, lmp: float = 30.0) -> pd.DataFrame:
    n_days = days or pd.Period(f"{year}-{month:02d}").days_in_month
    idx = pd.date_range(f"{year}-{month:02d}-01", periods=n_days * 24, freq="h")
    return pd.DataFrame([
        {"datetime_beginning_ept": t, "zone": z, "market": market,
         "total_lmp": lmp, "energy": lmp, "congestion": 0.0, "loss": 0.0}
        for t in idx for z in zones
    ])


def test_full_month_counts_as_complete(tmp_hourly):
    Z.save_hourly(_hours(2026, 3))
    assert Z._hourly_months() == {(2026, 3, "RT")}


def test_partial_month_is_not_complete(tmp_hourly):
    """An interrupted backfill must not mark the month done and never retry."""
    Z.save_hourly(_hours(2026, 3, days=5))
    assert Z._hourly_months() == set()


def test_cutoff_month_missing_one_day_still_counts(tmp_hourly):
    """The archived↔live cutoff month legitimately loses a single day."""
    Z.save_hourly(_hours(2026, 3, days=30))      # March has 31
    assert Z._hourly_months() == {(2026, 3, "RT")}


def test_markets_are_tracked_separately(tmp_hourly):
    Z.save_hourly(pd.concat([_hours(2026, 3, market="RT"),
                             _hours(2026, 3, market="DA", days=4)]))
    assert Z._hourly_months() == {(2026, 3, "RT")}


def test_hourly_months_empty_without_a_store(tmp_hourly):
    assert Z._hourly_months() == set()


def test_save_hourly_dedups_on_reload(tmp_hourly):
    """Re-fetching a month must replace its rows, not stack duplicates."""
    first = _hours(2026, 3, days=2, lmp=10.0)
    second = _hours(2026, 3, days=2, lmp=99.0)
    Z.save_hourly(pd.concat([first, second], ignore_index=True))
    got = Z.load_hourly(market="RT")
    assert len(got) == len(first)
    assert got["total_lmp"].eq(99.0).all()       # last write wins


def test_load_hourly_filters_by_zone_and_market(tmp_hourly):
    Z.save_hourly(pd.concat([
        _hours(2026, 3, days=2, market="RT", lmp=10.0),
        _hours(2026, 3, days=2, market="DA", lmp=20.0),
    ], ignore_index=True))
    pep = Z.load_hourly(market="RT", zones=["PEPCO"])
    assert set(pep["zone"]) == {"PEPCO"}
    assert set(pep["market"]) == {"RT"}
    assert pep["total_lmp"].eq(10.0).all()
    assert len(Z.load_hourly(market=None)) == 2 * len(pep) * 2


def test_load_hourly_returns_empty_without_a_store(tmp_hourly):
    out = Z.load_hourly(market="RT", zones=["PEPCO"])
    assert out.empty


def test_load_hourly_parses_timestamps(tmp_hourly):
    Z.save_hourly(_hours(2026, 3, days=1))
    out = Z.load_hourly(market="RT")
    assert pd.api.types.is_datetime64_any_dtype(out["datetime_beginning_ept"])


def test_monthly_mean_matches_the_hourly_rows(tmp_hourly):
    """The monthly store is a derived view; it must agree with the hourly one."""
    hourly = _hours(2026, 3, days=3, zones=("PEPCO",), lmp=42.0)
    monthly = Z._to_monthly(hourly)
    assert len(monthly) == 1
    assert monthly["total_lmp"].iloc[0] == pytest.approx(42.0)
    assert monthly["hours"].iloc[0] == 3 * 24
