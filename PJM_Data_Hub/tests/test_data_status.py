"""Freshness classification in data_status.py.

The gas-strip and forward-curve pulls write state markers without a
last_success timestamp (only a date-only "asof"). These regression tests pin
the fallback that keeps such datasets from showing as "never pulled" when
their marker file exists — the marker's own mtime is the pull time.
"""

from __future__ import annotations

import json

from pjm_core.data_status import _state_dataset


def _write(tmp_path, payload):
    p = tmp_path / ".last_update.json"
    p.write_text(json.dumps(payload))
    return p


def test_missing_marker_is_never_pulled(tmp_path):
    s = _state_dataset("gas", tmp_path / ".last_update.json")
    assert s.status == "grey"
    assert s.detail == "never pulled"
    assert s.age_hours is None


def test_standard_marker_uses_last_success(tmp_path):
    p = _write(tmp_path, {
        "last_success": "2020-01-01T00:00:00+00:00",
        "rows": 100, "start": "2020-01-01", "end": "2020-06-01",
    })
    s = _state_dataset("hub_prices", p)
    assert s.status == "red"           # years old vs 1-day cadence
    assert s.age_hours > 24 * 365
    assert "100 rows" in s.detail


def test_gas_strip_marker_falls_back_to_mtime(tmp_path):
    # Real schema written by gas_strip.update() before last_success existed.
    p = _write(tmp_path, {
        "asof": "2026-07-26", "months": 23,
        "first_month": "2026-08", "last_month": "2028-06",
    })
    s = _state_dataset("gas", p)
    assert s.status == "green"         # file just written → fresh
    assert s.age_hours is not None and s.age_hours < 1
    assert "23 contract months" in s.detail
    assert s.span == ("2026-08", "2028-06")
    assert "never" not in s.detail


def test_futures_marker_falls_back_to_mtime(tmp_path):
    # Real schema written by the ICE forward-curve pull.
    p = _write(tmp_path, {
        "asof": "2026-07-25", "trade_date": "2026-07-24", "rows": 14,
    })
    s = _state_dataset("futures", p)
    assert s.status == "green"
    assert s.age_hours is not None and s.age_hours < 1
    assert "14 rows" in s.detail
    assert "never" not in s.detail
