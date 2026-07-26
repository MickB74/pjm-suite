"""RPM (capacity) reference-table math.

capacity.py loads a user-editable CSV of BRA clearing prices ($/MW-day) with
an LDA → RTO fallback. These tests pin the small helpers, using a temp CSV so
they don't depend on whatever the local seed happens to be today.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pjm_core import capacity, paths


@pytest.fixture
def seeded_capacity_csv(tmp_path, monkeypatch):
    """Redirect capacity.load() at a tiny CSV under a temp directory."""
    cdir = tmp_path / "capacity"
    cdir.mkdir()
    csv = cdir / "rpm.csv"
    pd.DataFrame(
        [
            {"delivery_year": "2024/2025", "lda": "RTO", "clearing_price_mw_day": 28.92, "auction": "BRA"},
            {"delivery_year": "2024/2025", "lda": "BGE", "clearing_price_mw_day": 73.00, "auction": "BRA"},
            {"delivery_year": "2025/2026", "lda": "RTO", "clearing_price_mw_day": 269.92, "auction": "BRA"},
            {"delivery_year": "2025/2026", "lda": "DOM", "clearing_price_mw_day": 444.26, "auction": "BRA"},
        ]
    ).to_csv(csv, index=False)
    monkeypatch.setattr(paths, "CAPACITY_DIR", cdir)
    monkeypatch.setattr(paths, "CAPACITY_CSV", csv)
    return csv


class TestPriceFor:
    def test_returns_lda_price_when_lda_row_exists(self, seeded_capacity_csv):
        assert capacity.price_for("2025/2026", "DOM") == pytest.approx(444.26)

    def test_falls_back_to_rto_when_lda_missing(self, seeded_capacity_csv):
        # No PEPCO row for 2025/2026 in the seeded table → falls back to RTO.
        assert capacity.price_for("2025/2026", "PEPCO") == pytest.approx(269.92)

    def test_returns_none_when_delivery_year_missing(self, seeded_capacity_csv):
        assert capacity.price_for("1999/2000", "RTO") is None

    def test_default_lda_is_rto(self, seeded_capacity_csv):
        assert capacity.price_for("2024/2025") == pytest.approx(28.92)


class TestAnnualCost:
    def test_annual_cost_is_price_times_mw_times_365(self, seeded_capacity_csv):
        # 100 MW × $269.92/MW-day × 365 days = $9,852,080
        got = capacity.annual_capacity_cost(100.0, "2025/2026", "RTO")
        assert got == pytest.approx(269.92 * 100.0 * 365.0)

    def test_dom_zone_is_far_pricier_than_rto_in_25_26(self, seeded_capacity_csv):
        rto = capacity.annual_capacity_cost(100.0, "2025/2026", "RTO")
        dom = capacity.annual_capacity_cost(100.0, "2025/2026", "DOM")
        # DOM cleared above RTO — a real feature the LDA fallback must not mask.
        assert dom > rto

    def test_missing_year_returns_none(self, seeded_capacity_csv):
        assert capacity.annual_capacity_cost(100.0, "1999/2000", "RTO") is None


class TestLoad:
    def test_derived_annual_column_is_price_times_365(self, seeded_capacity_csv):
        df = capacity.load()
        rto2526 = df[(df.delivery_year == "2025/2026") & (df.lda == "RTO")].iloc[0]
        assert rto2526["clearing_price_mw_year"] == pytest.approx(269.92 * 365.0)

    def test_seed_written_when_csv_missing(self, tmp_path, monkeypatch):
        cdir = tmp_path / "capacity_empty"
        csv = cdir / "rpm.csv"
        monkeypatch.setattr(paths, "CAPACITY_DIR", cdir)
        monkeypatch.setattr(paths, "CAPACITY_CSV", csv)
        assert not csv.exists()
        df = capacity.load()
        assert csv.exists()               # seeded
        assert not df.empty               # and readable
        assert "clearing_price_mw_day" in df.columns
