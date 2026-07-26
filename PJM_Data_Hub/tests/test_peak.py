"""5CP eligibility & threshold — the rules that drive the capacity charge.

Only PJM-observed holiday and 5CP-window pieces are pinned here. The
load-vs-temperature model needs a real weather+load history; that's a job for
an integration test, not the unit suite.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from pjm_core import peak


class TestHolidays:
    def test_july_4_on_a_weekday_is_the_holiday(self):
        # 2025-07-04 is a Friday → observed on the 4th.
        assert peak.holiday_name(date(2025, 7, 4)) == "Independence Day"
        assert not peak.is_eligible_5cp(date(2025, 7, 4))

    def test_july_4_saturday_shifts_to_friday_july_3(self):
        # 2026-07-04 is a Saturday → PJM observes Friday, Jul 3.
        assert peak.holiday_name(date(2026, 7, 3)) == "Independence Day"
        assert not peak.is_eligible_5cp(date(2026, 7, 3))
        # And the Saturday itself is a weekend, so also ineligible for a different reason.
        assert not peak.is_eligible_5cp(date(2026, 7, 4))

    def test_july_4_sunday_shifts_to_monday_july_5(self):
        # 2027-07-04 is a Sunday → PJM observes Monday, Jul 5.
        assert peak.holiday_name(date(2027, 7, 5)) == "Independence Day"
        assert not peak.is_eligible_5cp(date(2027, 7, 5))

    def test_labor_day_is_first_monday_of_september(self):
        # 2025: first Monday of Sept = Sept 1
        assert peak.holiday_name(date(2025, 9, 1)) == "Labor Day"
        # 2026: first Monday of Sept = Sept 7
        assert peak.holiday_name(date(2026, 9, 7)) == "Labor Day"


class TestEligibility:
    def test_weekend_is_never_eligible(self):
        # 2025-07-05 is a Saturday.
        assert not peak.is_eligible_5cp(date(2025, 7, 5))
        # And a Sunday.
        assert not peak.is_eligible_5cp(date(2025, 7, 6))

    def test_ordinary_summer_weekday_is_eligible(self):
        # 2025-07-15 Tuesday, no holiday.
        assert peak.is_eligible_5cp(date(2025, 7, 15))


def _load(hours):
    """Build a minimal RTO-hourly-load frame for daily_peaks/current_threshold."""
    return pd.DataFrame({
        "datetime_beginning_ept": [h for h, _ in hours],
        "mw": [mw for _, mw in hours],
    })


class TestDailyPeaks:
    def test_picks_max_load_hour_per_day(self):
        hours = [
            (pd.Timestamp("2025-07-15 08:00"), 100000),
            (pd.Timestamp("2025-07-15 17:00"), 150000),  # peak of the day
            (pd.Timestamp("2025-07-15 22:00"), 120000),
            (pd.Timestamp("2025-07-16 09:00"), 110000),
            (pd.Timestamp("2025-07-16 16:00"), 140000),  # peak
        ]
        peaks = peak.daily_peaks(_load(hours), 2025)
        assert set(peaks["day"]) == {date(2025, 7, 15), date(2025, 7, 16)}
        d15 = peaks.set_index("day").loc[date(2025, 7, 15)]
        assert d15["peak_mw"] == 150000
        assert d15["peak_hour"] == pd.Timestamp("2025-07-15 17:00")

    def test_summer_window_excludes_may_and_october(self):
        # A May day above every summer day must still not appear in peaks.
        hours = [
            (pd.Timestamp("2025-05-30 17:00"), 999999),   # outside window
            (pd.Timestamp("2025-07-15 17:00"), 100000),   # inside
            (pd.Timestamp("2025-10-15 17:00"), 999999),   # outside
        ]
        peaks = peak.daily_peaks(_load(hours), 2025)
        assert list(peaks["day"]) == [date(2025, 7, 15)]


class TestCurrentThreshold:
    def test_fewer_than_five_eligible_days_returns_zero(self):
        # Two eligible summer weekday peaks → threshold is 0 (any new day makes it).
        hours = [
            (pd.Timestamp("2025-07-15 17:00"), 140000),   # Tue
            (pd.Timestamp("2025-07-16 17:00"), 145000),   # Wed
        ]
        assert peak.current_threshold(_load(hours), 2025) == 0.0

    def test_returns_fifth_highest_when_five_or_more_eligible_days(self):
        # Five weekdays with distinct peaks. Threshold = 5th highest.
        hours = [
            (pd.Timestamp("2025-07-14 17:00"), 100000),   # Mon
            (pd.Timestamp("2025-07-15 17:00"), 110000),   # Tue
            (pd.Timestamp("2025-07-16 17:00"), 120000),   # Wed
            (pd.Timestamp("2025-07-17 17:00"), 130000),   # Thu
            (pd.Timestamp("2025-07-18 17:00"), 140000),   # Fri
            (pd.Timestamp("2025-07-21 17:00"),  90000),   # Mon — new 5th
        ]
        # Top 5 = [140,130,120,110,100]. 5th = 100k.
        assert peak.current_threshold(_load(hours), 2025) == pytest.approx(100000)

    def test_weekend_peaks_are_excluded_from_threshold(self):
        hours = [
            (pd.Timestamp("2025-07-14 17:00"), 100000),   # Mon
            (pd.Timestamp("2025-07-15 17:00"), 110000),   # Tue
            (pd.Timestamp("2025-07-19 17:00"), 999999),   # Sat — huge but ineligible
            (pd.Timestamp("2025-07-20 17:00"), 999999),   # Sun — huge but ineligible
        ]
        # Only 2 eligible peaks → threshold is 0, not 999999.
        assert peak.current_threshold(_load(hours), 2025) == 0.0


class TestFiveCp:
    def test_returns_ranked_top_five_eligible(self):
        hours = [
            (pd.Timestamp("2025-06-30 17:00"), 100000),
            (pd.Timestamp("2025-07-01 17:00"), 110000),
            (pd.Timestamp("2025-07-02 17:00"), 120000),
            (pd.Timestamp("2025-07-03 17:00"), 130000),
            # Jul 4 is a Friday holiday in 2025 → should be dropped even if hot.
            (pd.Timestamp("2025-07-04 17:00"), 200000),
            (pd.Timestamp("2025-07-07 17:00"),  95000),
        ]
        top = peak.five_cp(_load(hours), 2025)
        assert list(top["rank"]) == [1, 2, 3, 4, 5]
        # Jul 4 (200k) must be excluded — it was a holiday.
        assert date(2025, 7, 4) not in set(top["day"])
        # Top peak is Jul 3 at 130k.
        assert top.iloc[0]["peak_mw"] == 130000
