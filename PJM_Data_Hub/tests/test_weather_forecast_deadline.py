"""Wall-clock cap on Open-Meteo forecast fetches.

The 5CP peak predictor renders live weather every time it opens. Without a
deadline, worst-case Open-Meteo rate-limiting (5 retries × 14 cities with
exponential backoff) can hang the page for ~72 minutes. `forecast_weighted`
now caps at 30 seconds by default; these pin the deadline plumbing without
touching the network.
"""

from __future__ import annotations

import time
from datetime import date
from unittest import mock

import pandas as pd
import pytest

from datasets.weather import pjm_weather


class _StubResponse:
    def __init__(self, status: int, hourly: dict | None = None):
        self.status_code = status
        self._payload = {"hourly": hourly or {}}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 429:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── _fetch_city deadline ────────────────────────────────────────────────────

def test_fetch_city_stops_retrying_once_past_deadline(monkeypatch):
    calls = {"get": 0, "sleep": 0.0}

    def fake_get(*a, **kw):
        calls["get"] += 1
        return _StubResponse(429)

    def fake_sleep(s):
        calls["sleep"] += s

    monkeypatch.setattr(pjm_weather.requests, "get", fake_get)
    monkeypatch.setattr(pjm_weather.time, "sleep", fake_sleep)

    # Deadline already in the past → bail before the first request.
    df = pjm_weather._fetch_city("X", "X", 0, 0, date.today(), date.today(),
                                 log=lambda *_: None,
                                 max_retries=5,
                                 deadline=time.monotonic() - 1)
    assert df.empty
    assert calls["get"] == 0
    assert calls["sleep"] == 0


def test_fetch_city_shortens_retry_wait_to_the_deadline(monkeypatch):
    """A 10 s backoff must be truncated when the deadline is 2 s away."""
    calls = []
    monkeypatch.setattr(pjm_weather.requests, "get",
                        lambda *a, **kw: _StubResponse(429))
    monkeypatch.setattr(pjm_weather.time, "sleep", lambda s: calls.append(s))

    deadline = time.monotonic() + 2.0
    df = pjm_weather._fetch_city("X", "X", 0, 0, date.today(), date.today(),
                                 log=lambda *_: None, max_retries=5,
                                 deadline=deadline)
    assert df.empty
    # The first backoff would be 10 s; truncated to at most ~2 s.
    assert calls, "expected at least one truncated sleep"
    assert max(calls) < 3.0


def test_fetch_city_respects_max_retries_when_no_deadline(monkeypatch):
    calls = {"get": 0}

    def fake_get(*a, **kw):
        calls["get"] += 1
        return _StubResponse(429)

    monkeypatch.setattr(pjm_weather.requests, "get", fake_get)
    monkeypatch.setattr(pjm_weather.time, "sleep", lambda s: None)

    df = pjm_weather._fetch_city("X", "X", 0, 0, date.today(), date.today(),
                                 log=lambda *_: None, max_retries=1)
    assert df.empty
    assert calls["get"] == 1                     # one attempt, no retries


# ── fetch() deadline across cities ──────────────────────────────────────────

def test_fetch_stops_iterating_cities_after_deadline(monkeypatch):
    """Partial results are better than none — return what completed and stop.

    Uses a synthetic monotonic clock so the test is deterministic and doesn't
    slow the suite with real sleeps.
    """
    seen_cities = []
    clock = [0.0]

    def fake_monotonic():
        clock[0] += 0.03    # every check advances 30 ms of "wall time"
        return clock[0]

    def fake_fetch_city(city, *a, **kw):
        seen_cities.append(city)
        clock[0] += 0.05    # each city consumes 50 ms of "wall time"
        return pd.DataFrame([{"datetime_beginning_ept": pd.Timestamp("2026-01-01"),
                              "city": city, "zone": "Z", "temp_f": 60.0,
                              "apparent_f": 60.0, "rh_pct": 50.0}])

    monkeypatch.setattr(pjm_weather.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(pjm_weather.time, "sleep", lambda s: None)
    monkeypatch.setattr(pjm_weather, "_fetch_city", fake_fetch_city)

    got = pjm_weather.fetch(date(2026, 1, 1), date(2026, 1, 1),
                            log=lambda *_: None, source="recent",
                            timeout_seconds=0.20)
    n_pts = len(pjm_weather.weather_points.points())
    # First check consumes budget → deadline is 0.20; each iteration burns
    # ~0.08 s of "clock", so we expect a few cities, definitely not all 14.
    assert 1 <= len(seen_cities) < n_pts
    assert not got.empty
    assert set(got["city"]) == set(seen_cities)


def test_fetch_without_timeout_visits_every_city(monkeypatch):
    seen_cities = []

    def fake_fetch_city(city, *a, **kw):
        seen_cities.append(city)
        return pd.DataFrame()

    monkeypatch.setattr(pjm_weather, "_fetch_city", fake_fetch_city)
    monkeypatch.setattr(pjm_weather.time, "sleep", lambda s: None)

    pjm_weather.fetch(date(2026, 1, 1), date(2026, 1, 1),
                      log=lambda *_: None, source="recent")
    assert len(seen_cities) == len(pjm_weather.weather_points.points())


# ── forecast_weighted end-to-end plumbing ───────────────────────────────────

def test_forecast_weighted_passes_short_deadline_and_low_retries(monkeypatch):
    """The interactive path must not fall through to the batch defaults."""
    captured = {}

    def fake_fetch(start, end, log=print, source="era5",
                   max_retries=5, timeout_seconds=None):
        captured["max_retries"] = max_retries
        captured["timeout_seconds"] = timeout_seconds
        captured["source"] = source
        return pd.DataFrame()

    monkeypatch.setattr(pjm_weather, "fetch", fake_fetch)
    pjm_weather.forecast_weighted(days=7)
    assert captured["source"] == "recent"
    assert captured["max_retries"] == 1
    assert captured["timeout_seconds"] == pjm_weather.FORECAST_WALL_CLOCK_SECONDS
    assert captured["timeout_seconds"] <= 30
