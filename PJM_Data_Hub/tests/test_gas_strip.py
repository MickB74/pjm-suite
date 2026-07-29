"""Gas-strip vintage history: archive, dedup, and as-of selection.

Each successful strip pull is appended to GAS_STRIP_HISTORY_PARQUET as a
dated snapshot so forecasts can be re-run "as of" a past date. These tests
pin the append/replace semantics and the strip_asof() lookup rules.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pjm_core import gas_strip, paths


def _strip(base_price: float) -> pd.DataFrame:
    months = pd.date_range("2026-08-01", periods=4, freq="MS")
    return pd.DataFrame({"month": months,
                         "gas_price": [base_price + i * 0.1 for i in range(4)]})


@pytest.fixture
def tmp_history(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "GAS_STRIP_HISTORY_PARQUET",
                        tmp_path / "hist.parquet")
    monkeypatch.setattr(paths, "GAS_DIR", tmp_path)
    return tmp_path


def test_append_builds_vintages(tmp_history):
    gas_strip._append_history(_strip(3.0), pd.Timestamp("2026-07-20"))
    gas_strip._append_history(_strip(3.5), pd.Timestamp("2026-07-25"))
    assert [v.date().isoformat() for v in gas_strip.vintages()] == \
        ["2026-07-20", "2026-07-25"]


def test_same_day_repull_replaces(tmp_history):
    gas_strip._append_history(_strip(3.0), pd.Timestamp("2026-07-20"))
    gas_strip._append_history(_strip(9.9), pd.Timestamp("2026-07-20"))
    hist = gas_strip.history()
    assert len(hist) == 4                       # one snapshot, not two
    assert hist["gas_price"].iloc[0] == pytest.approx(9.9)


def test_strip_asof_picks_latest_on_or_before(tmp_history):
    gas_strip._append_history(_strip(3.0), pd.Timestamp("2026-07-20"))
    gas_strip._append_history(_strip(3.5), pd.Timestamp("2026-07-25"))

    snap, vintage = gas_strip.strip_asof("2026-07-23")   # between the two
    assert vintage == pd.Timestamp("2026-07-20")
    assert snap["gas_price"].iloc[0] == pytest.approx(3.0)

    snap, vintage = gas_strip.strip_asof("2026-07-25")   # exact hit
    assert vintage == pd.Timestamp("2026-07-25")
    assert snap["gas_price"].iloc[0] == pytest.approx(3.5)


def test_strip_asof_none_before_first_vintage(tmp_history):
    gas_strip._append_history(_strip(3.0), pd.Timestamp("2026-07-20"))
    assert gas_strip.strip_asof("2026-07-19") is None


def test_strip_asof_none_when_no_history(tmp_history):
    assert gas_strip.strip_asof("2026-07-26") is None
    assert gas_strip.vintages() == []


# ── Median anchor ────────────────────────────────────────────────────────────
# The anchor is a per-contract median over a short window of vintages: enough
# to reject a bad tick from the unofficial Yahoo feed, short enough not to lag
# a real move.

def test_strip_median_rejects_an_outlier_vintage(tmp_history):
    for day, price in [(20, 3.0), (21, 3.1), (22, 9.9), (23, 3.05), (24, 3.02)]:
        gas_strip._append_history(_strip(price), pd.Timestamp(f"2026-07-{day}"))
    med, newest, n_used = gas_strip.strip_median(n=5)
    assert n_used == 5
    assert newest == pd.Timestamp("2026-07-24")
    assert med["gas_price"].iloc[0] == pytest.approx(3.05)   # not 9.9, not the mean


def test_strip_median_uses_only_the_last_n_vintages(tmp_history):
    for day, price in [(20, 1.0), (21, 2.0), (22, 8.0), (23, 9.0)]:
        gas_strip._append_history(_strip(price), pd.Timestamp(f"2026-07-{day}"))
    med, _, n_used = gas_strip.strip_median(n=2)
    assert n_used == 2
    assert med["gas_price"].iloc[0] == pytest.approx(8.5)    # median of 8 and 9


def test_strip_median_ignores_stale_vintages(tmp_history):
    """A gap in the daily pull must not blend a months-old curve into today's."""
    gas_strip._append_history(_strip(1.0), pd.Timestamp("2026-01-10"))
    gas_strip._append_history(_strip(5.0), pd.Timestamp("2026-07-27"))
    gas_strip._append_history(_strip(5.2), pd.Timestamp("2026-07-28"))
    med, _, n_used = gas_strip.strip_median(n=5, max_age_days=14)
    assert n_used == 2
    assert med["gas_price"].iloc[0] == pytest.approx(5.1)


def test_strip_median_respects_asof(tmp_history):
    gas_strip._append_history(_strip(3.0), pd.Timestamp("2026-07-20"))
    gas_strip._append_history(_strip(3.4), pd.Timestamp("2026-07-21"))
    gas_strip._append_history(_strip(9.0), pd.Timestamp("2026-07-28"))
    med, newest, n_used = gas_strip.strip_median("2026-07-22", n=5)
    assert newest == pd.Timestamp("2026-07-21")
    assert n_used == 2
    assert med["gas_price"].iloc[0] == pytest.approx(3.2)   # 2026-07-28 excluded


def test_strip_median_degrades_to_single_vintage(tmp_history):
    gas_strip._append_history(_strip(3.0), pd.Timestamp("2026-07-20"))
    med, newest, n_used = gas_strip.strip_median(n=5)
    assert n_used == 1
    assert med["gas_price"].iloc[0] == pytest.approx(3.0)


def test_strip_median_none_without_history(tmp_history):
    assert gas_strip.strip_median() is None


# ── Forward volatility from the archive ──────────────────────────────────────

def _fill_history(n_days: int, seed: int = 0, skip: set[int] | None = None):
    """n_days of vintages with a random-walk forward price per contract month."""
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2026-01-01")
    level = np.array([3.0, 3.1, 3.2, 3.3])
    for i in range(n_days):
        level = level * np.exp(rng.normal(0, 0.02, 4))
        if skip and i in skip:
            continue
        months = pd.date_range("2026-08-01", periods=4, freq="MS")
        gas_strip._append_history(
            pd.DataFrame({"month": months, "gas_price": level}),
            start + pd.Timedelta(float(i), unit="D"))


def test_forward_vol_none_until_enough_vintages(tmp_history):
    _fill_history(10)
    assert gas_strip.forward_vol() is None


def test_forward_vol_recovers_the_input_volatility(tmp_history):
    _fill_history(200, seed=3)
    vol = gas_strip.forward_vol()
    assert vol is not None and len(vol) == 4
    # 2% daily → ~0.02·√252 ≈ 0.32 annualised.
    for v in vol.values():
        assert 0.22 < v < 0.44


def test_forward_vol_normalises_gaps_in_the_archive(tmp_history):
    """A missed pull spans two days of moves; left unscaled it would read as a
    volatility spike rather than a hole in the data."""
    _fill_history(200, seed=3, skip={40, 41, 90, 91, 140, 141})
    vol = gas_strip.forward_vol()
    assert vol is not None
    for v in vol.values():
        assert 0.22 < v < 0.44


def test_forward_vol_respects_asof(tmp_history):
    _fill_history(200, seed=3)
    assert gas_strip.forward_vol("2026-01-15") is None      # too few by then
    assert gas_strip.forward_vol() is not None
