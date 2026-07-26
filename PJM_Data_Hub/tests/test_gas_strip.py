"""Gas-strip vintage history: archive, dedup, and as-of selection.

Each successful strip pull is appended to GAS_STRIP_HISTORY_PARQUET as a
dated snapshot so forecasts can be re-run "as of" a past date. These tests
pin the append/replace semantics and the strip_asof() lookup rules.
"""

from __future__ import annotations

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
