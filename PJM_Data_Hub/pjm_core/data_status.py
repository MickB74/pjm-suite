"""Uniform data-lake freshness view for every dataset.

The store markers (``.last_update.json`` per single-file dataset, file mtimes
for the year-partitioned ones) already exist — they were only surfaced through
``orchestrate.py status`` in a terminal. This module reads the same markers and
returns them in a shape both the CLI and the app can render.

Each dataset has an expected refresh cadence (hub prices land daily, EIA-923 is
yearly, etc.). The status colour is a simple age-vs-cadence check:

    green   fresh — within one cadence
    yellow  stale — 1–3 cadences behind (worth a nudge)
    red     very stale — 3+ cadences behind (probably a broken pipeline)
    grey    the dataset has never been pulled

Stale-but-plausible data is the silent-failure mode we care most about, so this
runs entirely off the state file and never touches the network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from pjm_core import paths


# Cadence in days — how often each dataset is expected to be refreshed. Used
# only to colour freshness; the actual pull cadence is up to orchestrate/cron.
CADENCE_DAYS = {
    "hub_prices": 1,
    "zone_prices": 30,      # monthly rollup
    "load": 1,
    "ancillary": 1,
    "weather": 1,           # ERA5 reanalysis lags ~5 days; ok if a bit behind
    "queue": 30,            # PJM refreshes queue reports ~monthly
    "system_gen": 365,      # year-partitioned; new file per year
    "eia923": 365,          # ditto
    "eia860": 365,          # ditto
    "capacity": None,       # user-maintained; freshness is not time-based
    "futures": 1,           # scraped intraday
    "gas": 1,               # gas strip refreshes at launch
    "delivery": None,       # user-maintained tariff table
}


# Per-dataset human labels for the UI.
LABELS = {
    "hub_prices": "Hub LMPs (hourly)",
    "zone_prices": "Zone LMPs (monthly avg)",
    "load": "System load (hourly)",
    "ancillary": "Ancillary services",
    "weather": "Weather (ERA5)",
    "queue": "Interconnection queue",
    "system_gen": "System generation by fuel",
    "eia923": "EIA-923 (plant generation)",
    "eia860": "EIA-860 (plant capacity)",
    "capacity": "RPM capacity clearing prices",
    "futures": "Hub forward curve",
    "gas": "Henry Hub gas strip",
    "delivery": "EDC delivery rates",
}


# Datasets with a single parquet + .last_update.json state file.
_STATE_DATASETS = [
    ("hub_prices", paths.HUB_PRICES_STATE),
    ("zone_prices", paths.ZONE_PRICES_STATE),
    ("load", paths.LOAD_STATE),
    ("ancillary", paths.ANCILLARY_STATE),
    ("weather", paths.WEATHER_STATE),
    ("queue", paths.QUEUE_STATE),
    ("gas", paths.GAS_STRIP_STATE),
    ("futures", paths.FUTURES_STATE),
]


# Year-partitioned datasets: one parquet per year, no state file.
_YEAR_DATASETS = [
    ("system_gen", paths.SYSTEM_GEN_DIR, "pjm_gen_by_fuel_*.parquet"),
    ("eia923", paths.EIA_DIR, "eia923_pjm_*.parquet"),
    ("eia860", paths.EIA860_DIR, "eia860_pjm_*.parquet"),
]


# User-maintained reference tables — freshness is not meaningful, only presence.
_REFERENCE_TABLES = [
    ("capacity", paths.CAPACITY_CSV),
    ("delivery", paths.DELIVERY_RATES_CSV),
]


@dataclass
class DatasetStatus:
    name: str
    label: str
    exists: bool
    status: str                 # "green" | "yellow" | "red" | "grey"
    detail: str                 # one-line human summary
    age_hours: float | None     # None when the dataset has never been pulled
    rows: int | None = None
    span: tuple | None = None   # (start, end) as strings; only for state datasets
    files: int | None = None    # only for year-partitioned datasets
    years: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _classify(age_hours: float | None, cadence_days: int | None) -> str:
    if age_hours is None:
        return "grey"
    if cadence_days is None:
        return "green"          # reference tables — presence is all we check
    cadence_hours = cadence_days * 24
    if age_hours <= cadence_hours:
        return "green"
    if age_hours <= 3 * cadence_hours:
        return "yellow"
    return "red"


def _age_hours(ts_iso: str | None) -> float | None:
    if not ts_iso:
        return None
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "never"
    if hours < 1:
        return f"{int(hours * 60)} min ago"
    if hours < 48:
        return f"{hours:.1f} h ago"
    return f"{hours / 24:.1f} d ago"


def _state_dataset(name: str, state_path: Path) -> DatasetStatus:
    label = LABELS.get(name, name)
    cadence = CADENCE_DAYS.get(name)
    if not state_path.exists():
        return DatasetStatus(name=name, label=label, exists=False,
                             status="grey", detail="never pulled", age_hours=None)
    try:
        st = json.loads(state_path.read_text())
    except (ValueError, OSError) as exc:
        return DatasetStatus(name=name, label=label, exists=True,
                             status="red", detail=f"state file unreadable: {exc}",
                             age_hours=None, error=str(exc))

    age = _age_hours(st.get("last_success"))
    if age is None:
        # Markers written by the gas-strip and forward-curve pulls don't
        # record last_success (only a date-only "asof") — the marker file's
        # own write time is the pull time, so use that.
        mtime = datetime.fromtimestamp(state_path.stat().st_mtime, timezone.utc)
        age = _age_hours(mtime.isoformat())
    color = _classify(age, cadence)
    if st.get("start"):
        span = (st.get("start"), st.get("end"))
    elif st.get("first_month"):
        span = (st.get("first_month"), st.get("last_month"))
    else:
        span = None
    rows = st.get("rows")
    detail_bits = []
    if rows is not None:
        detail_bits.append(f"{rows:,} rows")
    elif st.get("months") is not None:
        detail_bits.append(f"{st['months']} contract months")
    if span:
        detail_bits.append(f"{span[0]} → {span[1]}")
    detail_bits.append(_fmt_age(age))
    return DatasetStatus(name=name, label=label, exists=True, status=color,
                         detail=" · ".join(detail_bits), age_hours=age,
                         rows=rows, span=span)


def _year_dataset(name: str, ddir: Path, pattern: str) -> DatasetStatus:
    label = LABELS.get(name, name)
    cadence = CADENCE_DAYS.get(name)
    files = sorted(ddir.glob(pattern))
    if not files:
        return DatasetStatus(name=name, label=label, exists=False,
                             status="grey", detail="never pulled", age_hours=None)
    years = sorted({int(m.group()) for f in files
                    for m in [re.search(r"\d{4}", f.name)] if m})
    newest = max(files, key=lambda f: f.stat().st_mtime)
    mtime = datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc)
    age = _age_hours(mtime.isoformat())
    color = _classify(age, cadence)
    span = f"{years[0]}–{years[-1]}" if years else None
    detail = f"{len(files)} files · years {span} · {_fmt_age(age)}"
    return DatasetStatus(name=name, label=label, exists=True, status=color,
                         detail=detail, age_hours=age,
                         files=len(files), years=span)


def _reference_table(name: str, csv_path: Path) -> DatasetStatus:
    label = LABELS.get(name, name)
    if not csv_path.exists():
        return DatasetStatus(name=name, label=label, exists=False,
                             status="grey", detail="not seeded", age_hours=None)
    mtime = datetime.fromtimestamp(csv_path.stat().st_mtime, timezone.utc)
    age = _age_hours(mtime.isoformat())
    return DatasetStatus(name=name, label=label, exists=True, status="green",
                         detail=f"user-maintained · edited {_fmt_age(age)}",
                         age_hours=age)


def all_statuses() -> list[DatasetStatus]:
    """Every dataset's current freshness, in a stable display order."""
    out: list[DatasetStatus] = []
    for name, path in _STATE_DATASETS:
        out.append(_state_dataset(name, path))
    for name, ddir, pat in _YEAR_DATASETS:
        out.append(_year_dataset(name, ddir, pat))
    for name, path in _REFERENCE_TABLES:
        out.append(_reference_table(name, path))
    return out


def summary_counts() -> dict:
    """{'green': n, 'yellow': n, 'red': n, 'grey': n} for the whole lake."""
    counts = {"green": 0, "yellow": 0, "red": 0, "grey": 0}
    for s in all_statuses():
        counts[s.status] += 1
    return counts


def worst_color(counts: dict | None = None) -> str:
    """Overall lake colour: red > yellow > grey > green."""
    c = counts or summary_counts()
    if c.get("red"):
        return "red"
    if c.get("yellow"):
        return "yellow"
    if c.get("grey"):
        return "grey"
    return "green"
