"""Walk-forward forecast backtest.

The engine itself calls out to the real model (which needs fixtures) but the
summarising, grouping, and per-row bookkeeping is unit-testable with a small
handcrafted table — those are what these guard.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pjm_core import forecast_backtest as fb


def _row(asof, month, p10, p50, p90, actual, gas_from_strip=True):
    """One backtest row with all the derived columns already filled in."""
    err = p50 - actual
    return {
        "asof": pd.Timestamp(asof), "month": pd.Timestamp(month),
        "horizon": (pd.Timestamp(month).year - pd.Timestamp(asof).year) * 12
                   + (pd.Timestamp(month).month - pd.Timestamp(asof).month),
        "gas_fwd": 3.0, "hr_median": 10.0,
        "p10": p10, "p50": p50, "p90": p90, "actual": actual,
        "err": err, "ape": abs(err) / actual,
        "in_band": p10 <= actual <= p90,
        "gas_from_strip": gas_from_strip,
    }


@pytest.fixture
def bt():
    """Ten pairs across two as-ofs. Split roughly half in-band, and one is a
    big winter miss so a summary that hides the coverage tail can't slip."""
    return pd.DataFrame([
        _row("2025-08-01", "2025-09-01", 20, 30, 45, actual=28),   # in band, over
        _row("2025-08-01", "2025-10-01", 20, 30, 45, actual=50),   # over band, under
        _row("2025-08-01", "2025-11-01", 25, 35, 55, actual=40),   # in band, under
        _row("2025-08-01", "2026-01-01", 40, 60, 90, actual=120),  # winter miss
        _row("2025-08-01", "2026-02-01", 30, 45, 75, actual=40),   # in band, over
        _row("2026-01-02", "2026-02-01", 30, 45, 75, actual=87,
             gas_from_strip=False),                                # extrapolated
        _row("2026-01-02", "2026-03-01", 20, 30, 45, actual=30),   # in band
        _row("2026-01-02", "2026-04-01", 20, 28, 40, actual=35),   # in band, under
        _row("2026-01-02", "2026-05-01", 18, 25, 35, actual=25),   # in band, exact
        _row("2026-01-02", "2026-06-01", 22, 32, 48, actual=32),   # in band, exact
    ])


# ── summarise() ──────────────────────────────────────────────────────────────

def test_summarise_returns_all_metrics_and_matches_manual_calc(bt):
    got = fb.summarise(bt, real_strip_only=False)
    assert set(got) == {"bias", "mae", "mape_pct", "coverage_pct", "n"}
    assert got["n"] == 10
    assert got["bias"] == pytest.approx(bt["err"].mean())
    assert got["mae"] == pytest.approx(bt["err"].abs().mean())
    assert got["coverage_pct"] == pytest.approx(bt["in_band"].mean() * 100)


def test_real_strip_only_filters_out_extrapolated_gas(bt):
    all_rows = fb.summarise(bt, real_strip_only=False)
    real = fb.summarise(bt, real_strip_only=True)
    assert real["n"] == 9                          # one row was flagged
    assert real["n"] < all_rows["n"]
    # The extrapolated row was a $-42 miss; excluding it moves bias upward.
    assert real["bias"] > all_rows["bias"]


def test_summarise_empty_returns_zero_n_not_a_crash():
    got = fb.summarise(pd.DataFrame(columns=list(_row("2025-01-01", "2025-02-01",
                                                       1, 1, 1, 1))), )
    assert got["n"] == 0
    assert pd.isna(got["bias"])


# ── by_group() ───────────────────────────────────────────────────────────────

def test_by_group_horizon_orders_by_key_and_is_disjoint(bt):
    g = fb.by_group(bt, "horizon", real_strip_only=False)
    assert list(g.columns[0:1]) == ["horizon"]
    assert g["n"].sum() == len(bt)
    assert g["horizon"].is_monotonic_increasing


def test_by_group_season_matches_attach_season(bt):
    bts = fb.attach_season(bt)
    g = fb.by_group(bts, "season", real_strip_only=False)
    # Every row should have landed in exactly one season bucket.
    assert g["n"].sum() == len(bts)


# ── attach_season() ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("month,expected", [
    ("2026-01-01", "Q1 winter"),
    ("2026-04-01", "Q2 spring"),
    ("2026-07-01", "Q3 summer"),
    ("2026-10-01", "Q4 fall"),
    ("2026-12-01", "Q4 fall"),
])
def test_attach_season_bins_months_into_calendar_quarters(month, expected):
    df = pd.DataFrame([{"month": pd.Timestamp(month)}])
    out = fb.attach_season(df)
    assert str(out["season"].iloc[0]) == expected


# ── _monthly_asofs() ─────────────────────────────────────────────────────────

def test_monthly_asofs_takes_the_earliest_per_calendar_month():
    """Multiple vintages in a month → keep only the earliest so we don't double-
    count near-consecutive days as separate as-ofs."""
    vintages = [pd.Timestamp(d) for d in [
        "2025-07-29", "2025-07-30",           # July: keep 29
        "2025-08-01", "2025-08-15",           # August: keep 01
        "2025-09-30",                          # September: only one
    ]]
    picks = fb._monthly_asofs(vintages)
    assert [p.date().isoformat() for p in picks] == [
        "2025-07-29", "2025-08-01", "2025-09-30"]


def test_monthly_asofs_empty_history_gives_empty_list():
    assert fb._monthly_asofs([]) == []


# ── _complete_months() ───────────────────────────────────────────────────────

def _hub_parquet(tmp_path, monkeypatch, spans):
    """Write a hub-price parquet holding `spans` = {month: n_hours} and point
    paths.HUB_PRICES_PARQUET at it."""
    rows = []
    for month, n_hours in spans.items():
        start = pd.Timestamp(month)
        rows.append(pd.DataFrame({
            "datetime_beginning_ept": pd.date_range(start, periods=n_hours,
                                                    freq="h"),
            "pnode_name": fb.PRIMARY_HUB,
            "total_lmp": 30.0,
        }))
    p = tmp_path / "hub.parquet"
    pd.concat(rows, ignore_index=True).to_parquet(p, index=False)
    monkeypatch.setattr(fb.paths, "HUB_PRICES_PARQUET", p)
    return p


def test_complete_months_keeps_full_months_and_drops_the_partial_one(
        tmp_path, monkeypatch):
    # July closed at 744/744; August is only 20 days in, as it would be
    # mid-month — that is the row the backtest must not score.
    _hub_parquet(tmp_path, monkeypatch,
                 {"2026-07-01": 744, "2026-08-01": 20 * 24})
    got = fb._complete_months()
    assert pd.Timestamp("2026-07-01") in got
    assert pd.Timestamp("2026-08-01") not in got


def test_complete_months_accepts_the_dst_short_march(tmp_path, monkeypatch):
    # Spring-forward means March legitimately holds 743 hours, not 744; a
    # threshold that rejected it would silently drop a March every year.
    _hub_parquet(tmp_path, monkeypatch, {"2026-03-01": 743})
    assert pd.Timestamp("2026-03-01") in fb._complete_months()


def test_complete_months_without_a_store_is_empty_not_a_crash(
        tmp_path, monkeypatch):
    monkeypatch.setattr(fb.paths, "HUB_PRICES_PARQUET", tmp_path / "nope.parquet")
    assert fb._complete_months() == set()
