"""Canonical timezone handling for the PJM Data Hub.

PJM settles in **Eastern Prevailing Time** (``US/Eastern``): hourly LMPs in
both Day-Ahead and Real-Time markets. Two days a year break naive wall-clock
arithmetic:

  * spring forward — 02:00–03:00 never happens (23-hour day).
  * fall back      — 01:00–02:00 happens twice (25-hour day). The two passes
                     share the same naive wall-clock label, so groupby / join /
                     dedupe on a naive timestamp silently collapses or
                     double-counts those two hours.

**Storage convention.** Interval columns in the parquet lake are stored *naive
Eastern* on purpose (they open cleanly in Excel). Settlement-grade joins must
lift to tz-aware Eastern first using :func:`localize_eastern`.
"""

from __future__ import annotations

import pandas as pd

EASTERN = "US/Eastern"


def now_eastern() -> pd.Timestamp:
    """Current instant as a tz-aware Eastern timestamp ("today in PJM")."""
    return pd.Timestamp.now(tz=EASTERN)


def now_utc() -> pd.Timestamp:
    """Current instant as a tz-aware UTC timestamp (for ``fetched_at``)."""
    return pd.Timestamp.now(tz="UTC")


def _is_datetimelike(obj) -> bool:
    return isinstance(obj, (pd.Series, pd.DatetimeIndex, pd.Index))


def localize_eastern(s, *, flags=None):
    """Lift *naive Eastern* timestamps to tz-aware Eastern.

    Handles DST: ``ambiguous="infer"`` resolves the duplicated fall-back hour
    from sort order (first pass = EDT, second = EST) and
    ``nonexistent="shift_forward"`` nudges any value that lands in the
    spring-forward gap.

    ``flags`` — an optional bool Series where ``True`` marks the *second*,
    standard-time pass of the fall-back hour. Rarely needed for PJM since PJM
    timestamps come in UTC and are converted, but kept for parity with the
    ERCOT version.
    """
    if not _is_datetimelike(s):
        s = pd.to_datetime(s)
    is_index = isinstance(s, (pd.DatetimeIndex, pd.Index))
    ser = pd.Series(pd.to_datetime(s)) if is_index else pd.to_datetime(s)

    if getattr(ser.dt, "tz", None) is not None:
        out = ser.dt.tz_convert(EASTERN)
    else:
        out = _localize_naive(ser, flags)
    return pd.DatetimeIndex(out) if is_index else out


def _localize_naive(ser: pd.Series, flags) -> pd.Series:
    if flags is not None:
        f = flags if isinstance(flags, pd.Series) else pd.Series(list(flags))
        repeated = f.astype(bool).reset_index(drop=True)
        ambiguous = (~repeated).to_numpy()
        return ser.dt.tz_localize(EASTERN, ambiguous=ambiguous,
                                  nonexistent="shift_forward")
    try:
        return ser.dt.tz_localize(EASTERN, ambiguous="infer",
                                  nonexistent="shift_forward")
    except pd.errors.OutOfBoundsDatetime:
        raise
    except Exception:
        return ser.dt.tz_localize(EASTERN, ambiguous=True,
                                  nonexistent="shift_forward")


def to_naive_eastern(s):
    """Convert any timestamps to Eastern wall-clock, then drop the tz.

    Accepts tz-aware (any zone) or already-naive input; result is naive
    Eastern, matching the parquet storage convention.
    """
    is_index = isinstance(s, (pd.DatetimeIndex, pd.Index))
    ser = pd.Series(pd.to_datetime(s)) if is_index else pd.to_datetime(s)
    if getattr(ser.dt, "tz", None) is not None:
        ser = ser.dt.tz_convert(EASTERN).dt.tz_localize(None)
    return pd.DatetimeIndex(ser) if is_index else ser


def to_utc(s, *, flags=None):
    """Convert naive-Eastern interval timestamps to tz-aware UTC (DST-correct)."""
    aware = localize_eastern(s, flags=flags)
    if isinstance(aware, pd.DatetimeIndex):
        return aware.tz_convert("UTC")
    return aware.dt.tz_convert("UTC")


def assert_naive_eastern(df: pd.DataFrame, *cols: str) -> None:
    """Dev guard: raise if a column meant to be naive Eastern is tz-aware."""
    for col in cols:
        if col not in df.columns:
            continue
        tz = getattr(df[col].dt, "tz", None)
        if tz is not None:
            raise AssertionError(
                f"column {col!r} should be naive Eastern but is tz-aware ({tz})"
            )
