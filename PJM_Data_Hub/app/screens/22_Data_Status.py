"""Data Status — pipeline health at a glance.

Every downstream number in the app comes off the parquet lake, so a silent
pipeline failure (yesterday's update didn't run, or ran but errored) is the
worst-case failure mode: charts still render, they're just wrong. This screen
surfaces that with one coloured row per dataset — the same freshness the
sidebar badge summarises, plus row counts, span, and detected gaps.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from pjm_core import data_status, paths


st.title("Data Status")
st.caption(
    "One row per dataset in the lake. "
    "🟢 fresh · 🟡 stale (past its expected refresh cadence) · "
    "🔴 very stale · ⚪ never pulled."
)

DOT = {"green": "🟢", "yellow": "🟡", "red": "🔴", "grey": "⚪"}

statuses = data_status.all_statuses()
counts = {"green": 0, "yellow": 0, "red": 0, "grey": 0}
for s in statuses:
    counts[s.status] += 1

c1, c2, c3, c4 = st.columns(4)
c1.metric("🟢 Fresh", counts["green"])
c2.metric("🟡 Stale", counts["yellow"])
c3.metric("🔴 Very stale", counts["red"])
c4.metric("⚪ Missing", counts["grey"])

st.subheader("By dataset")
rows = []
for s in statuses:
    cadence = data_status.CADENCE_DAYS.get(s.name)
    rows.append({
        "": DOT[s.status],
        "Dataset": s.label,
        "Rows / files": (f"{s.rows:,}" if s.rows is not None
                         else f"{s.files} files" if s.files is not None else "—"),
        "Span": (f"{s.span[0]} → {s.span[1]}" if s.span
                 else s.years or "—"),
        "Last update": data_status._fmt_age(s.age_hours),
        "Expected cadence": (f"{cadence}d" if cadence else "manual"),
        "Detail": s.detail,
    })
st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

# ── Gap detection on the hourly-series datasets ──────────────────────────────
# A "gap" is a missing hour in a series that's supposed to be continuous.
# We only check the three that settle math and 5CP depend on.
st.subheader("Hourly gap detection")
st.caption(
    "Continuous-hourly datasets should have no missing hours after their start. "
    "Two per-day are expected on the fall-back DST day (the 01:00 hour repeats) "
    "and one fewer on spring-forward — those aren't gaps."
)

HOURLY = [
    ("Hub LMPs", paths.HUB_PRICES_PARQUET, "datetime_beginning_ept"),
    ("System load", paths.LOAD_PARQUET, "datetime_beginning_ept"),
    ("Ancillary", paths.ANCILLARY_PARQUET, "datetime_beginning_ept"),
    ("Weather", paths.WEATHER_PARQUET, "datetime_beginning_ept"),
]


@st.cache_data(ttl=300, show_spinner=False)
def _gap_report(path_str: str, time_col: str) -> dict:
    """Count missing hours between the min and max timestamp in a parquet.

    Cheap enough to run inline for the hourly datasets (each file is a few
    dozen MB), which is why we don't precompute this at pull time.
    """
    from pathlib import Path
    p = Path(path_str)
    if not p.exists():
        return {"exists": False}
    df = pd.read_parquet(p, columns=[time_col])
    if df.empty:
        return {"exists": True, "empty": True}
    ts = pd.to_datetime(df[time_col]).sort_values()
    lo, hi = ts.min(), ts.max()
    # Expected count over a continuous hourly range, less DST spring-forward
    # (one 24-slot day per year is really 23 hours in Eastern). We can't
    # perfectly correct for the 25-hour fall-back day without a dedup, so this
    # tolerates a small over-count and only flags real gaps.
    hours_span = int((hi - lo).total_seconds() // 3600) + 1
    have = ts.dt.floor("h").drop_duplicates().shape[0]
    missing = max(0, hours_span - have)
    return {"exists": True, "empty": False,
            "min": lo, "max": hi, "hours_span": hours_span,
            "have": have, "missing": missing}


gap_rows = []
for label, path, tcol in HOURLY:
    r = _gap_report(str(path), tcol)
    if not r.get("exists"):
        gap_rows.append({"Dataset": label, "Status": "⚪ not pulled",
                         "Missing hours": "—", "Coverage": "—"})
        continue
    if r.get("empty"):
        gap_rows.append({"Dataset": label, "Status": "⚪ empty",
                         "Missing hours": "—", "Coverage": "—"})
        continue
    missing = r["missing"]
    dot = "🟢" if missing == 0 else "🟡" if missing < 24 else "🔴"
    gap_rows.append({
        "Dataset": label,
        "Status": dot,
        "First hour": str(r["min"]),
        "Last hour": str(r["max"]),
        "Missing hours": f"{missing:,}",
        "Coverage": f"{r['have'] / r['hours_span']:.2%}" if r["hours_span"] else "—",
    })
st.dataframe(pd.DataFrame(gap_rows), hide_index=True, use_container_width=True)

with st.expander("What each colour means"):
    st.markdown(
        """
- **🟢 Fresh** — the dataset was refreshed within its expected cadence (see
  the *Expected cadence* column). Hourly PJM feeds should be under a day old;
  monthly rollups under ~30 days.
- **🟡 Stale** — 1–3 cadences behind. Nothing has crashed, but the nightly
  refresh may have been skipped. Click **Refresh PJM data** in the sidebar.
- **🔴 Very stale** — more than 3 cadences behind. The pipeline is probably
  broken (expired API key, upstream schema change, or `orchestrate.py update`
  hasn't been run). Check the sidebar refresh output or run
  `orchestrate.py update <dataset>` from a terminal.
- **⚪ Missing** — the dataset has never been pulled. First-run only, or a
  reference table (Capacity, Delivery) that wasn't seeded yet.
        """
    )
