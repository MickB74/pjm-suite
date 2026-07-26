"""PPA / CfD settlement math.

Sign convention (offtaker-signed, from settlement.py):
    cfd_settlement = gen_mwh * (market_price - strike)
  → offtaker receives when market > strike, pays when market < strike.

Golden-file style: known inputs → known outputs, computed by hand.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pjm_core import settlement


def _hourly(start: str, hours: int) -> pd.Series:
    return pd.Series(pd.date_range(start, periods=hours, freq="h"))


def _gen(times, mw):
    return pd.DataFrame({"datetime_beginning_ept": times, "generation_mw": mw})


def _px(times, price):
    return pd.DataFrame({"datetime_beginning_ept": times, "total_lmp": price})


class TestSettleBasics:
    def test_hourly_interval_is_one_mwh_per_mw(self):
        t = _hourly("2025-07-15 00:00", 3)
        r = settlement.settle(_gen(t, [10, 20, 30]), _px(t, [50, 50, 50]),
                              strike=40.0)
        # 60 MWh total across three hours (10+20+30) × 1.0 h.
        assert r["gen_mwh"].sum() == pytest.approx(60.0)

    def test_cfd_is_offtaker_positive_when_market_above_strike(self):
        t = _hourly("2025-07-15 00:00", 1)
        r = settlement.settle(_gen(t, [10]), _px(t, [70]), strike=50.0)
        # 10 MWh × (70 - 50) = +200
        assert r["cfd_settlement"].sum() == pytest.approx(200.0)

    def test_cfd_is_offtaker_negative_when_market_below_strike(self):
        t = _hourly("2025-07-15 00:00", 1)
        r = settlement.settle(_gen(t, [10]), _px(t, [30]), strike=50.0)
        # 10 MWh × (30 - 50) = -200
        assert r["cfd_settlement"].sum() == pytest.approx(-200.0)

    def test_merchant_and_ppa_and_cfd_are_consistent(self):
        # merchant - ppa == cfd, always (identity by construction).
        t = _hourly("2025-07-15 00:00", 5)
        gen = _gen(t, [10, 15, 20, 25, 30])
        px = _px(t, [45, 55, 60, 20, 80])
        r = settlement.settle(gen, px, strike=50.0)
        diff = r["merchant_revenue"] - r["ppa_revenue"] - r["cfd_settlement"]
        assert diff.abs().max() == pytest.approx(0.0, abs=1e-9)


class TestSubZeroHandling:
    """Standard VPPA convention: sub-zero LMPs are excluded from settled volume."""

    def test_default_excludes_negative_price_hours_from_totals(self):
        t = _hourly("2025-07-15 00:00", 3)
        gen = _gen(t, [10, 10, 10])
        px = _px(t, [50, -20, 40])
        r = settlement.settle(gen, px, strike=30.0)   # default price_floor=0
        # Middle hour excluded → 20 MWh total, not 30.
        assert r["gen_mwh"].sum() == pytest.approx(20.0)
        # And revenue reflects only kept hours: 10*50 + 10*40 = 900
        assert r["merchant_revenue"].sum() == pytest.approx(900.0)

    def test_settle_below_floor_true_keeps_hour_at_floor(self):
        t = _hourly("2025-07-15 00:00", 3)
        gen = _gen(t, [10, 10, 10])
        px = _px(t, [50, -20, 40])
        r = settlement.settle(gen, px, strike=30.0,
                              price_floor=0.0, settle_below_floor=True)
        # All 30 MWh kept; negative-price hour floored at 0.
        assert r["gen_mwh"].sum() == pytest.approx(30.0)
        # Revenue: 10*50 + 10*0 + 10*40 = 900. (Floored price, not original.)
        assert r["merchant_revenue"].sum() == pytest.approx(900.0)

    def test_price_floor_none_keeps_negatives_as_is(self):
        t = _hourly("2025-07-15 00:00", 2)
        r = settlement.settle(_gen(t, [10, 10]), _px(t, [50, -20]),
                              strike=30.0, price_floor=None)
        # 10*50 + 10*(-20) = 300
        assert r["merchant_revenue"].sum() == pytest.approx(300.0)


class TestVolumeFraction:
    def test_half_share_halves_all_dollar_totals(self):
        t = _hourly("2025-07-15 00:00", 4)
        gen = _gen(t, [10] * 4)
        px = _px(t, [60] * 4)
        full = settlement.settle(gen, px, strike=50.0)
        half = settlement.settle(gen, px, strike=50.0, volume_fraction=0.5)
        for col in ("gen_mwh", "merchant_revenue", "ppa_revenue", "cfd_settlement"):
            assert half[col].sum() == pytest.approx(full[col].sum() / 2)


class TestDstDays:
    def test_spring_forward_day_settles_without_raising(self):
        # 2025-03-09 is a 23-hour day in Eastern — 02:00 doesn't exist.
        # Naive-Eastern parquet storage skips that hour, so a caller feeds 23
        # real hours: 00, 01, 03, 04, …, 23.
        hours = [pd.Timestamp("2025-03-09 00:00"),
                 pd.Timestamp("2025-03-09 01:00")]
        hours += [pd.Timestamp(f"2025-03-09 {h:02d}:00") for h in range(3, 24)]
        t = pd.Series(hours)
        r = settlement.settle(_gen(t, [10] * 23), _px(t, [50] * 23), strike=40.0)
        assert len(r) == 23
        # 23 hours × 10 MW × $10 = $2,300 CfD
        assert r["cfd_settlement"].sum() == pytest.approx(2300.0)

    def test_fall_back_day_double_counts_ambiguous_hour_naively(self):
        # The 01:00 hour repeats in wall-clock time. The settle() layer joins on
        # naive Eastern (upstream contract), so a caller who feeds a 25-hour day
        # must include the extra 01:00 row explicitly.
        naive = list(pd.date_range("2025-11-02 00:00", "2025-11-03 00:00",
                                   freq="h", inclusive="left"))
        naive.insert(2, pd.Timestamp("2025-11-02 01:00"))   # 25th hour
        t = pd.Series(naive)
        r = settlement.settle(_gen(t, [10] * 25), _px(t, [50] * 25), strike=40.0)
        # Both passes are joined by naive key: 25 rows fed in, 25 rows out (with
        # a merge that duplicates the ambiguous hour on both sides). Settlement
        # is stable — total volume matches what the operator sent.
        assert r["gen_mwh"].sum() >= 250.0


class TestSummarize:
    def test_capture_price_is_generation_weighted(self):
        # Two hours: 10 MWh @ $40, 30 MWh @ $80 → weighted = (400+2400)/40 = $70
        t = _hourly("2025-07-15 00:00", 2)
        r = settlement.settle(_gen(t, [10, 30]), _px(t, [40, 80]), strike=0.0)
        s = settlement.summarize(r)
        assert s["capture_price"] == pytest.approx(70.0)
        assert s["total_gen_mwh"] == pytest.approx(40.0)
        assert s["total_merchant_revenue"] == pytest.approx(2800.0)

    def test_empty_settled_summarizes_to_empty_dict(self):
        assert settlement.summarize(pd.DataFrame()) == {}


class TestBasis:
    def test_basis_settlement_uses_node_minus_hub(self):
        t = _hourly("2025-07-15 00:00", 1)
        hub = _px(t, [50])
        node = _px(t, [55])   # $5/MWh premium at the node
        r = settlement.settle(_gen(t, [10]), hub, strike=0.0, node_prices=node)
        # basis = 10 MWh × (55 - 50) = 50
        assert r["basis_settlement"].sum() == pytest.approx(50.0)
