"""Peak-predictor accuracy tracking.

Tests use a temp parquet path (monkeypatched) plus a fake load frame with
known daily peaks — so scoring math (MAPE, hit rate, recall) is verifiable
against numbers computed by hand, without needing real load/weather history.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from pjm_core import paths, peak, prediction_log


@pytest.fixture
def temp_log(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "PREDICTIONS_DIR", tmp_path)
    monkeypatch.setattr(paths, "PEAK_PREDICTIONS_PARQUET",
                        tmp_path / "peak_predictor_log.parquet")
    return tmp_path


def _pred_row(target_day, predicted_mw, prob, threshold=140000, eligible=True):
    return {
        "day": target_day,
        "predicted_peak_mw": float(predicted_mw),
        "prob_5cp": float(prob),
        "threshold_mw": float(threshold),
        "eligible": eligible,
        "tmax_apparent_f": 95.0,
        "extrapolated": False,
    }


class TestLogForecast:
    def test_first_call_writes_all_rows(self, temp_log):
        pred = pd.DataFrame([
            _pred_row(date(2025, 7, 15), 145000, 0.7),
            _pred_row(date(2025, 7, 16), 130000, 0.2),
        ])
        r = prediction_log.log_forecast(pred, as_of=date(2025, 7, 14))
        assert r["appended"] == 2
        assert paths.PEAK_PREDICTIONS_PARQUET.exists()

    def test_second_call_same_asof_is_idempotent(self, temp_log):
        pred = pd.DataFrame([_pred_row(date(2025, 7, 15), 145000, 0.7)])
        prediction_log.log_forecast(pred, as_of=date(2025, 7, 14))
        r = prediction_log.log_forecast(pred, as_of=date(2025, 7, 14))
        assert r["appended"] == 0

    def test_different_asof_appends(self, temp_log):
        pred = pd.DataFrame([_pred_row(date(2025, 7, 15), 145000, 0.7)])
        prediction_log.log_forecast(pred, as_of=date(2025, 7, 13))
        prediction_log.log_forecast(pred, as_of=date(2025, 7, 14))
        stored = pd.read_parquet(paths.PEAK_PREDICTIONS_PARQUET)
        assert len(stored) == 2

    def test_empty_pred_is_noop(self, temp_log):
        r = prediction_log.log_forecast(pd.DataFrame())
        assert r["appended"] == 0
        assert not paths.PEAK_PREDICTIONS_PARQUET.exists()


def _load_with_daily_peaks(day_peaks):
    """Build a minimal RTO load frame that has each day's peak at 17:00."""
    return pd.DataFrame({
        "datetime_beginning_ept": [
            pd.Timestamp(d).replace(hour=17) for d, _ in day_peaks
        ],
        "mw": [mw for _, mw in day_peaks],
    })


class TestScoredHistory:
    def test_only_past_target_days_are_scored(self, temp_log):
        today = pd.Timestamp.today().date()
        past = today - timedelta(days=5)
        future = today + timedelta(days=5)
        pred = pd.DataFrame([
            _pred_row(past, 145000, 0.7),
            _pred_row(future, 130000, 0.2),
        ])
        prediction_log.log_forecast(pred, as_of=today - timedelta(days=6))
        # If our fake load has the past day, only the past row scores.
        load = _load_with_daily_peaks([(past, 140000)])
        scored = prediction_log.scored_history(load=load)
        assert list(scored["target_day"]) == [past]

    def test_error_and_pct_error_are_correct(self, temp_log):
        # Real world days that fall inside the summer window so peak.daily_peaks
        # actually picks them up. Predicted 150000, actual 140000 → error +10000
        # and abs_pct_error = 10000/140000 * 100 ≈ 7.14%.
        target = date(2024, 7, 15)   # summer weekday, definitely past
        pred = pd.DataFrame([_pred_row(target, 150000, 0.8)])
        prediction_log.log_forecast(pred, as_of=date(2024, 7, 10))
        load = _load_with_daily_peaks([(target, 140000)])
        scored = prediction_log.scored_history(load=load)
        assert len(scored) == 1
        assert scored.loc[0, "error_mw"] == pytest.approx(10000.0)
        assert scored.loc[0, "abs_pct_error"] == pytest.approx(10000 / 140000 * 100)
        # 5 lead days
        assert scored.loc[0, "lead_days"] == 5


class TestScorecard:
    def test_perfect_calls_score_zero_mape_and_zero_brier(self, temp_log):
        # Two summer weekdays; predictions match reality exactly. Both are
        # top-two of the summer we synthesize, so was_5cp is True and prob
        # was 1.0 → Brier = 0.
        d1, d2 = date(2024, 7, 15), date(2024, 7, 16)
        pred = pd.DataFrame([
            _pred_row(d1, 150000, 1.0),
            _pred_row(d2, 140000, 1.0),
        ])
        prediction_log.log_forecast(pred, as_of=date(2024, 7, 10))
        load = _load_with_daily_peaks([(d1, 150000), (d2, 140000)])
        scored = prediction_log.scored_history(load=load)
        card = prediction_log.scorecard(scored)
        assert card["n"] == 2
        assert card["mape"] == pytest.approx(0.0)
        assert card["brier"] == pytest.approx(0.0)
        # High-confidence calls (≥66%) are 2/2 hits.
        assert card["high_hit_rate"] == pytest.approx(1.0)

    def test_high_confidence_miss_shows_up_as_false_alarm(self, temp_log):
        # Predict 200k for a mid-week summer day; actual = 90k. Model called
        # 90% CP-probability but the day nowhere near cracks the top 5.
        d = date(2024, 7, 15)
        pred = pd.DataFrame([_pred_row(d, 200000, 0.90)])
        prediction_log.log_forecast(pred, as_of=date(2024, 7, 10))
        # Populate the "summer" with six much bigger *eligible* days so d is
        # nowhere near the top-5 threshold. Six, not five, because one of the
        # first five (Jul 4) is Independence Day and therefore ineligible —
        # without the sixth big weekday, Jul 15 would sneak into the top 5.
        big_days = []
        i = 0
        while len(big_days) < 6:
            cand = date(2024, 7, 1) + timedelta(days=i)
            if peak.is_eligible_5cp(cand):
                big_days.append((cand, 300000))
            i += 1
        load = _load_with_daily_peaks(big_days + [(d, 90000)])
        scored = prediction_log.scored_history(load=load)
        card = prediction_log.scorecard(scored)
        assert card["high_calls"] == 1
        assert card["high_hit_rate"] == pytest.approx(0.0)
        assert card["high_false_alarm"] == pytest.approx(1.0)

    def test_empty_history_returns_only_n(self, temp_log):
        card = prediction_log.scorecard(pd.DataFrame())
        assert card == {"n": 0}
