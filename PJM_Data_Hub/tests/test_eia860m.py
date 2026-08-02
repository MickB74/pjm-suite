"""EIA-860M monthly generator inventory ETL — store lookup + parser wiring.

The download itself hits eia.gov and would be flaky in CI; these unit-test the
in-memory parser (via a tiny synthetic xlsx built with pandas) and the
store-lookup helpers that decide which snapshot the screen uses. That's the
part that used to leave the EIA-923 screen showing "as of 2024" for a 2026
query — get that wrong and the fix is silently useless.
"""

from __future__ import annotations

import io
import pandas as pd
import pytest

from datasets.eia860m import eia860m
from pjm_core import paths


# ── Parser ──────────────────────────────────────────────────────────────────

def _make_synthetic_860m_xlsx(rows: list[dict]) -> bytes:
    """Build a minimum-viable 860M workbook: two title rows, then headers,
    then data. Mirrors the layout EIA actually publishes (header row idx 2)."""
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        # Two junk rows so header lands on row idx 2 as in the real files.
        pd.DataFrame([["Title"], ["Notes"]]).to_excel(
            xw, sheet_name="Operating", header=False, index=False, startrow=0)
        df.to_excel(xw, sheet_name="Operating", index=False, startrow=2)
    return buf.getvalue()


BASE_COLS = {
    "Plant ID": 100, "Plant Name": "Test Plant", "Plant State": "PA",
    "Nameplate Capacity (MW)": 500.0, "Net Summer Capacity (MW)": 480.0,
    "Net Winter Capacity (MW)": 490.0, "Energy Source Code": "NG",
}


def test_parse_extracts_plant_fuel_rows_and_reference_stamp():
    xlsx = _make_synthetic_860m_xlsx([BASE_COLS])
    df = eia860m._parse(xlsx, 2026, 4)
    assert len(df) == 1
    assert set(df.columns) >= {"plant_id", "plant_name", "state", "energy_source",
                                "nameplate_mw", "summer_mw", "winter_mw",
                                "fuel_group", "reference_year", "reference_month"}
    assert df.iloc[0]["nameplate_mw"] == 500.0
    assert df.iloc[0]["reference_year"] == 2026
    assert df.iloc[0]["reference_month"] == 4
    assert df.iloc[0]["fuel_group"] == "Gas"


def test_parse_filters_to_pjm_footprint_states():
    """A plant in California (outside PJM) must be dropped, or the KPI would
    include capacity that has nothing to do with PJM."""
    xlsx = _make_synthetic_860m_xlsx([
        {**BASE_COLS, "Plant ID": 1, "Plant State": "PA"},
        {**BASE_COLS, "Plant ID": 2, "Plant State": "CA"},
        {**BASE_COLS, "Plant ID": 3, "Plant State": "NY"},
    ])
    df = eia860m._parse(xlsx, 2026, 4)
    assert set(df["state"]) == {"PA"}


def test_parse_sums_multiple_generators_at_a_plant_per_fuel():
    """A plant with three NG generators listed separately should collapse to
    one row whose MW is the sum. n_generators tracks the source count."""
    xlsx = _make_synthetic_860m_xlsx([
        {**BASE_COLS, "Plant ID": 100, "Nameplate Capacity (MW)": 200.0},
        {**BASE_COLS, "Plant ID": 100, "Nameplate Capacity (MW)": 200.0},
        {**BASE_COLS, "Plant ID": 100, "Nameplate Capacity (MW)": 100.0},
    ])
    df = eia860m._parse(xlsx, 2026, 4)
    assert len(df) == 1
    assert df.iloc[0]["nameplate_mw"] == 500.0
    assert df.iloc[0]["n_generators"] == 3


def test_parse_keeps_multi_fuel_plants_split_by_fuel():
    """Dual-fuel plants must remain two rows so a per-fuel MW join to 923
    stays accurate (not collapsed into a plant-total)."""
    xlsx = _make_synthetic_860m_xlsx([
        {**BASE_COLS, "Plant ID": 100, "Energy Source Code": "NG",
         "Nameplate Capacity (MW)": 300.0},
        {**BASE_COLS, "Plant ID": 100, "Energy Source Code": "BIT",
         "Nameplate Capacity (MW)": 400.0},
    ])
    df = eia860m._parse(xlsx, 2026, 4)
    assert set(df["energy_source"]) == {"NG", "BIT"}
    assert df["nameplate_mw"].sum() == 700.0


def test_parse_drops_rows_with_missing_key_fields():
    xlsx = _make_synthetic_860m_xlsx([
        {**BASE_COLS, "Plant ID": None},
        {**BASE_COLS, "Nameplate Capacity (MW)": None},
        {**BASE_COLS, "Energy Source Code": None},
        BASE_COLS,
    ])
    df = eia860m._parse(xlsx, 2026, 4)
    assert len(df) == 1                     # only the fully-populated row


def test_parse_returns_empty_when_schema_drifts():
    """If EIA renames a required column, degrade cleanly rather than crash."""
    bad = _make_synthetic_860m_xlsx([{"X": 1}])
    assert eia860m._parse(bad, 2026, 4).empty


# ── Store lookup ────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "EIA860M_DIR", tmp_path)
    monkeypatch.setattr(paths, "EIA860M_STATE", tmp_path / ".state.json")
    return tmp_path


def _seed(store_dir, year, month, mw=1000.0):
    df = pd.DataFrame([{
        "plant_id": 100, "plant_name": "P", "state": "PA",
        "energy_source": "NG", "fuel_group": "Gas",
        "nameplate_mw": mw, "summer_mw": mw * 0.95, "winter_mw": mw * 0.98,
        "n_generators": 1, "reference_year": year, "reference_month": month,
    }])
    df.to_parquet(store_dir / f"eia860m_pjm_{year}_{month:02d}.parquet",
                  index=False)


def test_available_returns_sorted_year_month_pairs(tmp_store):
    _seed(tmp_store, 2026, 3); _seed(tmp_store, 2025, 12); _seed(tmp_store, 2026, 1)
    got = eia860m.available()
    assert got == [(2025, 12), (2026, 1), (2026, 3)]


def test_available_empty_without_a_store(tmp_store):
    assert eia860m.available() == []


def test_latest_before_selects_the_closest_prior_snapshot(tmp_store):
    _seed(tmp_store, 2024, 8); _seed(tmp_store, 2025, 6); _seed(tmp_store, 2026, 4)
    # This is the case the screen actually hits: "what's the most recent
    # snapshot for year_sel = 2025?" Should get Dec-2025 if we have it, or
    # whatever the newest 2025 snapshot is otherwise.
    assert eia860m.latest_before(2025) == (2025, 6)
    assert eia860m.latest_before(2026) == (2026, 4)
    assert eia860m.latest_before(2024) == (2024, 8)


def test_latest_before_returns_none_when_all_snapshots_are_newer(tmp_store):
    """Screen falls back to annual 860 when we have no 860M for that vintage."""
    _seed(tmp_store, 2026, 4)
    assert eia860m.latest_before(2023) is None


def test_load_without_args_returns_the_latest_snapshot(tmp_store):
    _seed(tmp_store, 2025, 6, mw=100.0)
    _seed(tmp_store, 2026, 4, mw=999.0)
    df = eia860m.load()
    assert len(df) == 1
    assert df["nameplate_mw"].iloc[0] == 999.0


def test_load_by_year_alone_takes_the_latest_month_in_that_year(tmp_store):
    _seed(tmp_store, 2025, 3, mw=100.0)
    _seed(tmp_store, 2025, 11, mw=999.0)
    df = eia860m.load(2025)
    assert df["nameplate_mw"].iloc[0] == 999.0


def test_load_missing_snapshot_returns_empty(tmp_store):
    assert eia860m.load(2020, 1).empty


# ── Weekly self-throttle ─────────────────────────────────────────────────────

def test_update_skips_when_last_success_within_a_week(tmp_store, monkeypatch):
    """`update()` is on the auto-refresh queue that fires on every app open.
    EIA publishes each 860M snapshot once, so hitting them more than weekly is
    pure waste."""
    import json
    # Simulate a run 2 days ago.
    (tmp_store / ".state.json").write_text(json.dumps({
        "last_success": (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)).isoformat(),
        "latest": "2026-06", "count": 1, "snapshots": ["2026-06"],
    }))
    calls = []
    monkeypatch.setattr(eia860m, "fetch_month",
                        lambda *a, **kw: calls.append(a) or pd.DataFrame())
    result = eia860m.update(log=lambda *_: None)
    assert result.get("skipped") is True
    assert calls == [], "update() should not have fetched anything on cooldown"


def test_update_runs_after_the_cooldown_expires(tmp_store, monkeypatch):
    import json
    (tmp_store / ".state.json").write_text(json.dumps({
        "last_success": (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=10)).isoformat(),
        "latest": "2026-06", "count": 1, "snapshots": ["2026-06"],
    }))
    calls = []
    monkeypatch.setattr(eia860m, "fetch_month",
                        lambda y, m, **kw: (calls.append((y, m)) or
                                            _synthetic_df(y, m)))
    result = eia860m.update(months_back=2, log=lambda *_: None)
    assert result.get("skipped") is not True
    assert calls, "update() should have fetched at least one month past cooldown"


def test_update_force_bypasses_the_cooldown(tmp_store, monkeypatch):
    import json
    (tmp_store / ".state.json").write_text(json.dumps({
        "last_success": pd.Timestamp.now(tz="UTC").isoformat(),
        "latest": "2026-06", "count": 1, "snapshots": ["2026-06"],
    }))
    calls = []
    monkeypatch.setattr(eia860m, "fetch_month",
                        lambda y, m, **kw: (calls.append((y, m)) or
                                            _synthetic_df(y, m)))
    eia860m.update(months_back=2, force=True, log=lambda *_: None)
    assert calls, "force=True should bypass the cooldown"


def _synthetic_df(year, month):
    return pd.DataFrame([{
        "plant_id": 100, "plant_name": "P", "state": "PA",
        "energy_source": "NG", "fuel_group": "Gas",
        "nameplate_mw": 500.0, "summer_mw": 480.0, "winter_mw": 490.0,
        "n_generators": 1, "reference_year": year, "reference_month": month,
    }])


# ── Month enumeration ───────────────────────────────────────────────────────

def test_months_back_produces_the_expected_count():
    got = eia860m._months_back(6)
    assert len(got) == 6
    # Sequence should be strictly decreasing (most recent first) with no gaps.
    for a, b in zip(got, got[1:]):
        ay, am = a; by, bm = b
        assert (ay, am) > (by, bm)
        assert (ay * 12 + am) - (by * 12 + bm) == 1
