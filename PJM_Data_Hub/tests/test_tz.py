"""Timezone layer — the settlement-grade guarantees.

PJM stores naive Eastern in parquet. The two days that break naive arithmetic
are DST transitions: spring-forward (23-hour day, one wall-clock hour never
happens) and fall-back (25-hour day, one wall-clock hour repeats). Everything
downstream that joins on time depends on this layer getting them right.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pjm_core import tz


class TestLocalizeEastern:
    def test_plain_summer_hour_gets_edt_offset(self):
        s = pd.Series([pd.Timestamp("2025-07-15 14:00")])
        out = tz.localize_eastern(s)
        assert str(out.dt.tz) == "US/Eastern"
        # July → EDT = UTC-4
        assert out.iloc[0].utcoffset() == pd.Timedelta(hours=-4)

    def test_winter_hour_gets_est_offset(self):
        s = pd.Series([pd.Timestamp("2025-01-15 14:00")])
        out = tz.localize_eastern(s)
        assert out.iloc[0].utcoffset() == pd.Timedelta(hours=-5)

    def test_spring_forward_nonexistent_hour_is_shifted(self):
        # 2025-03-09 02:30 does not exist in Eastern. Should shift forward, not raise.
        s = pd.Series([pd.Timestamp("2025-03-09 02:30")])
        out = tz.localize_eastern(s)
        # Shifts to 03:00 EDT (UTC-4).
        assert out.iloc[0].hour == 3
        assert out.iloc[0].utcoffset() == pd.Timedelta(hours=-4)

    def test_fall_back_ambiguous_hour_is_disambiguated_by_order(self):
        # 2025-11-02 01:00 happens twice. Sorted input → first pass EDT, second EST.
        s = pd.Series([
            pd.Timestamp("2025-11-02 00:00"),
            pd.Timestamp("2025-11-02 01:00"),   # first pass, EDT (UTC-4)
            pd.Timestamp("2025-11-02 01:00"),   # second pass, EST (UTC-5)
            pd.Timestamp("2025-11-02 02:00"),
        ])
        out = tz.localize_eastern(s)
        assert out.iloc[1].utcoffset() == pd.Timedelta(hours=-4)
        assert out.iloc[2].utcoffset() == pd.Timedelta(hours=-5)
        # And they resolve to distinct absolute instants.
        assert out.iloc[1] != out.iloc[2]

    def test_fall_back_flags_override_infer(self):
        # Explicit flags: True marks the second pass. Must be consulted over sort.
        idx = pd.Series([
            pd.Timestamp("2025-11-02 01:00"),
            pd.Timestamp("2025-11-02 01:00"),
        ])
        # Pretend the invoice put the standard-time pass first.
        flags = pd.Series([True, False])
        out = tz.localize_eastern(idx, flags=flags)
        assert out.iloc[0].utcoffset() == pd.Timedelta(hours=-5)  # EST
        assert out.iloc[1].utcoffset() == pd.Timedelta(hours=-4)  # EDT

    def test_already_aware_input_is_converted_not_localized(self):
        s = pd.Series([pd.Timestamp("2025-07-15 18:00", tz="UTC")])
        out = tz.localize_eastern(s)
        # 18Z in July → 14:00 EDT
        assert out.iloc[0].hour == 14
        assert out.iloc[0].utcoffset() == pd.Timedelta(hours=-4)


class TestRoundTrip:
    def test_naive_to_aware_to_naive_is_identity_for_unambiguous_hours(self):
        s = pd.Series(pd.date_range("2025-07-01", periods=24, freq="h"))
        aware = tz.localize_eastern(s)
        naive = tz.to_naive_eastern(aware)
        pd.testing.assert_series_equal(
            naive.reset_index(drop=True), s.reset_index(drop=True),
            check_names=False,
        )

    def test_to_utc_produces_correct_utc(self):
        s = pd.Series([pd.Timestamp("2025-07-15 14:00")])   # 14 EDT = 18 UTC
        out = tz.to_utc(s)
        assert out.iloc[0] == pd.Timestamp("2025-07-15 18:00", tz="UTC")


class TestDaylengths:
    def test_spring_forward_day_has_23_hours(self):
        s = pd.Series(pd.date_range("2025-03-09 00:00", "2025-03-10 00:00",
                                    freq="h", inclusive="left"))
        # Naive range has 24 slots; the 02:00 hour doesn't exist in Eastern.
        # After localize, that hour shifts to 03:00 → we get a duplicated 03:00.
        # What matters for downstream code: no raise, unique absolute instants.
        aware = tz.localize_eastern(s)
        assert aware.dt.tz_convert("UTC").nunique() == 23

    def test_fall_back_day_has_25_hours(self):
        # Build the 25-hour fall-back day by unioning both passes explicitly.
        naive = list(pd.date_range("2025-11-02 00:00", "2025-11-03 00:00",
                                   freq="h", inclusive="left"))
        # Insert the extra 01:00 pass.
        naive.insert(2, pd.Timestamp("2025-11-02 01:00"))
        aware = tz.localize_eastern(pd.Series(naive))
        assert aware.dt.tz_convert("UTC").nunique() == 25


class TestAssertNaiveEastern:
    def test_flags_tz_aware_column(self):
        df = pd.DataFrame({"t": pd.Series(pd.date_range("2025-07-01", periods=3, freq="h", tz="UTC"))})
        with pytest.raises(AssertionError):
            tz.assert_naive_eastern(df, "t")

    def test_passes_naive_column(self):
        df = pd.DataFrame({"t": pd.date_range("2025-07-01", periods=3, freq="h")})
        tz.assert_naive_eastern(df, "t")   # no raise
