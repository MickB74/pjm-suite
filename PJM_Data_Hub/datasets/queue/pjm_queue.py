#!/usr/bin/env python3
"""PJM interconnection (New Services) queue — full generation queue snapshot.

Unlike the other datasets, this is NOT a Data Miner 2 time-series. PJM publishes
the full interconnection queue as an Excel export from its Planning API:

    https://services.pjm.com/PJMPlanningApi/api/Queue/ExportToXls

That endpoint uses a fixed public ``api-subscription-key`` (the same one the
pjm.com public queue page uses), so this dataset needs **no** PJM Data Miner
subscription key. Each pull is a complete snapshot of every project ever
submitted (~9k+ rows) — active, in-service, and withdrawn.

PJM only serves the *current* state (there's no historical queue archive), so to
track how the queue changes over time we ARCHIVE each pull: ``update()`` stamps
every snapshot with ``snapshot_date`` and APPENDS it to the store (re-running on
the same day replaces that day's rows). The store therefore holds one full copy
of the queue per day it was refreshed, and history accrues going forward. Use
``latest()`` for the current view and ``snapshot_dates()`` / ``compare()`` for
the change-over-time analytics.

One row per project × snapshot:

    project_id     PJM queue ID (e.g. "AG1-045").
    name / commercial_name
    state / county
    status         raw PJM status (Active, In Service, Withdrawn, …).
    bucket         normalized lifecycle: Active / Operational / Withdrawn.
    transmission_owner
    fuel           primary fuel/technology (Solar, Gas, Wind, Storage, …).
    mw_energy / mw_capacity / mw_in_service   the three MW ratings PJM reports.
    mw             headline sizing MW = first of in_service / capacity / energy.
    submitted_date / projected_in_service_date / actual_in_service_date /
    withdrawal_date

Local store (one archived snapshot per refresh day; appended, deduped on
``(snapshot_date, project_id)``):

    data/queue/pjm_queue.parquet
    data/queue/pjm_queue.csv

Usage:
    python pjm_queue.py update
    python pjm_queue.py status
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import requests
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pjm_core import paths, tz

# PJM Planning API — full queue Excel export. The subscription key is the fixed
# public key served by pjm.com's own interconnection-queue page (not a personal
# Data Miner 2 key), so no user credential is required.
_ENDPOINT = "https://services.pjm.com/PJMPlanningApi/api/Queue/ExportToXls"
_PUBLIC_KEY = "E29477D0-70E0-4825-89B0-43F460BF9AB4"
_HEADERS = {
    "api-subscription-key": _PUBLIC_KEY,
    "Host": "services.pjm.com",
    "Origin": "https://www.pjm.com",
    "Referer": "https://www.pjm.com/",
}

STALE_AFTER_DAYS = 7

# Raw PJM column -> our snake_case name. Columns not listed are still kept
# (with their original names) so nothing is lost.
_RENAME = {
    "Project ID": "project_id",
    "Name": "name",
    "Commercial Name": "commercial_name",
    "State": "state",
    "County": "county",
    "Status": "status",
    "Transmission Owner": "transmission_owner",
    "Fuel": "fuel",
    "Project Type": "project_type",
    "MW Energy": "mw_energy",
    "MW Capacity": "mw_capacity",
    "MW In Service": "mw_in_service",
    "MFO": "mfo",
    "Submitted Date": "submitted_date",
    "Projected In Service Date": "projected_in_service_date",
    "Actual In Service Date": "actual_in_service_date",
    "Withdrawal Date": "withdrawal_date",
    "Withdrawn Remarks": "withdrawal_comment",
}

_DATE_COLS = [
    "submitted_date", "projected_in_service_date",
    "actual_in_service_date", "withdrawal_date",
]
_NUM_COLS = ["mw_energy", "mw_capacity", "mw_in_service", "mfo"]

# Lifecycle buckets. Anything operational vs dead vs still-in-the-queue.
_OPERATIONAL = {"In Service", "Partially in Service - Under Construction"}
_WITHDRAWN = {"Withdrawn", "Retracted", "Deactivated"}


def _make_logger(callback=None):
    def log(msg: str):
        line = str(msg)
        print(line, flush=True)
        if callback is not None:
            try:
                callback(line)
            except Exception:
                pass
    return log


def _bucket(status: str) -> str:
    if status in _OPERATIONAL:
        return "Operational"
    if status in _WITHDRAWN:
        return "Withdrawn"
    return "Active"


def fetch(log=print) -> pd.DataFrame:
    """Download the full PJM queue Excel export and normalize it."""
    log("  Requesting full queue export from PJM Planning API …")
    resp = requests.post(_ENDPOINT, headers=_HEADERS, timeout=180)
    resp.raise_for_status()
    df = pd.read_excel(io.BytesIO(resp.content))
    log(f"  Received {len(df):,} projects, {len(df.columns)} columns.")

    df = df.rename(columns=_RENAME)

    for c in _NUM_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in _DATE_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")

    # Headline sizing MW: prefer the in-service rating, then capacity, then energy.
    def _pick_mw(row):
        for c in ("mw_in_service", "mw_capacity", "mw_energy"):
            v = row.get(c)
            if pd.notna(v) and v > 0:
                return v
        return pd.NA
    df["mw"] = df.apply(_pick_mw, axis=1)

    if "status" in df.columns:
        df["bucket"] = df["status"].fillna("").map(_bucket)
    else:
        df["bucket"] = "Active"

    df["snapshot_date"] = tz.now_eastern().date().isoformat()
    return df


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def load_store() -> pd.DataFrame:
    p = paths.QUEUE_PARQUET
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


def save_store(df: pd.DataFrame, write_csv: bool = True) -> None:
    paths.QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(paths.QUEUE_PARQUET, index=False)
    if write_csv:
        df.to_csv(paths.QUEUE_CSV, index=False)


def read_state() -> dict:
    p = paths.QUEUE_STATE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def write_state(state: dict) -> None:
    paths.QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    paths.QUEUE_STATE.write_text(json.dumps(state, indent=2, default=str))


def days_since_update() -> float | None:
    ts = read_state().get("last_success")
    if not ts:
        return None
    try:
        last = pd.Timestamp(ts)
    except ValueError:
        return None
    now = tz.now_eastern()
    if last.tz is None:
        last = last.tz_localize(tz.EASTERN, ambiguous=True, nonexistent="shift_forward")
    return (now - last).total_seconds() / 86400.0


def is_stale() -> bool:
    d = days_since_update()
    return d is None or d >= STALE_AFTER_DAYS


def store_summary() -> dict:
    info = {"exists": paths.QUEUE_PARQUET.exists(), "rows": 0,
            "active": 0, "active_mw": 0.0, "snapshots": 0,
            "snapshot": None, "days_since_update": days_since_update()}
    if not info["exists"]:
        return info
    try:
        df = load_store()
        info["rows"] = len(df)
        if not df.empty:
            snaps = snapshot_dates(df)
            info["snapshots"] = len(snaps)
            info["snapshot"] = snaps[-1] if snaps else None
            cur = latest(df)
            act = cur[cur["bucket"] == "Active"]
            info["active"] = len(act)
            info["active_mw"] = float(act["mw"].sum(skipna=True))
    except Exception as e:
        info["error"] = str(e)
    return info


def update(progress_callback=None) -> dict:
    """Fetch the full PJM queue and archive it as a dated snapshot.

    Appends today's snapshot to the store, replacing any prior pull from the
    same day. Prior days are retained so the queue's evolution is preserved.
    """
    log = _make_logger(progress_callback)
    log("Fetching PJM interconnection queue …")
    df = fetch(log=log)
    if df.empty:
        raise RuntimeError("PJM returned an empty queue export.")

    today = df["snapshot_date"].iloc[0]
    existing = load_store()
    if not existing.empty:
        # Drop any earlier pull from today, then append the fresh one.
        existing = existing[existing.get("snapshot_date") != today]
        combined = pd.concat([existing, df], ignore_index=True)
        combined = combined.drop_duplicates(
            subset=["snapshot_date", "project_id"], keep="last")
    else:
        combined = df

    save_store(combined)

    act = df[df["bucket"] == "Active"]
    snaps = snapshot_dates(combined)
    summary = {
        "rows": len(combined),
        "snapshots": len(snaps),
        "active": len(act),
        "active_mw": float(act["mw"].sum(skipna=True)),
        "snapshot": today,
        "parquet": str(paths.QUEUE_PARQUET),
    }
    write_state({"last_success": tz.now_eastern().isoformat(), **summary})
    log(f"\nDone. Snapshot {today}: {len(df):,} projects "
        f"({summary['active']:,} active, {summary['active_mw']:,.0f} MW). "
        f"Store now holds {len(snaps)} snapshot(s), {len(combined):,} rows.")
    return summary


# ---------------------------------------------------------------------------
# Access layer
# ---------------------------------------------------------------------------

def snapshot_dates(df: pd.DataFrame | None = None) -> list[str]:
    """Sorted list of snapshot dates held in the store (ascending)."""
    df = load_store() if df is None else df
    if df.empty or "snapshot_date" not in df.columns:
        return []
    return sorted(df["snapshot_date"].dropna().unique().tolist())


def latest(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Rows from the most recent snapshot only (the current queue state)."""
    df = load_store() if df is None else df
    if df.empty or "snapshot_date" not in df.columns:
        return df
    snaps = snapshot_dates(df)
    if not snaps:
        return df
    return df[df["snapshot_date"] == snaps[-1]].copy()


def snapshot(date_str: str, df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Rows from one specific snapshot date."""
    df = load_store() if df is None else df
    if df.empty:
        return df
    return df[df["snapshot_date"] == date_str].copy()


def trend(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per-snapshot totals for the change-over-time view.

    One row per snapshot_date with active project count + active MW, plus
    operational and withdrawn cumulative counts.
    """
    df = load_store() if df is None else df
    if df.empty or "snapshot_date" not in df.columns:
        return pd.DataFrame(columns=["snapshot_date", "active", "active_mw",
                                     "operational", "withdrawn"])
    rows = []
    for d, g in df.groupby("snapshot_date"):
        act = g[g["bucket"] == "Active"]
        rows.append({
            "snapshot_date": d,
            "active": len(act),
            "active_mw": float(act["mw"].sum(skipna=True)),
            "operational": int((g["bucket"] == "Operational").sum()),
            "withdrawn": int((g["bucket"] == "Withdrawn").sum()),
        })
    return pd.DataFrame(rows).sort_values("snapshot_date").reset_index(drop=True)


def compare(date_a: str, date_b: str, df: pd.DataFrame | None = None) -> dict:
    """Diff two snapshots (a = earlier, b = later).

    Returns dicts of DataFrames: projects that newly appeared, went operational,
    were withdrawn, or changed status between the two snapshots.
    """
    df = load_store() if df is None else df
    a = snapshot(date_a, df).set_index("project_id")
    b = snapshot(date_b, df).set_index("project_id")

    added_ids = b.index.difference(a.index)
    gone_ids = a.index.difference(b.index)
    common = a.index.intersection(b.index)

    changed = []
    newly_operational = []
    newly_withdrawn = []
    for pid in common:
        sa, sb = a.loc[pid, "status"], b.loc[pid, "status"]
        if sa != sb:
            row = b.loc[pid, ["name", "state", "fuel", "mw", "status"]].to_dict()
            row["project_id"] = pid
            row["prev_status"] = sa
            changed.append(row)
            if b.loc[pid, "bucket"] == "Operational" and a.loc[pid, "bucket"] != "Operational":
                newly_operational.append(row)
            if b.loc[pid, "bucket"] == "Withdrawn" and a.loc[pid, "bucket"] != "Withdrawn":
                newly_withdrawn.append(row)

    def _frame(rows):
        return pd.DataFrame(rows) if rows else pd.DataFrame()

    return {
        "added": b.loc[added_ids].reset_index()[
            [c for c in ["project_id", "name", "state", "fuel", "mw", "status"]
             if c in b.reset_index().columns]],
        "removed": a.loc[gone_ids].reset_index()[
            [c for c in ["project_id", "name", "state", "fuel", "mw", "status"]
             if c in a.reset_index().columns]],
        "changed": _frame(changed),
        "newly_operational": _frame(newly_operational),
        "newly_withdrawn": _frame(newly_withdrawn),
    }


def active(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Active projects in the current (latest) snapshot."""
    cur = latest(df)
    if cur.empty:
        return cur
    return cur[cur["bucket"] == "Active"].copy()


def fuels() -> list[str]:
    df = load_store()
    if df.empty or "fuel" not in df.columns:
        return []
    return sorted(df["fuel"].dropna().unique().tolist())


def states() -> list[str]:
    df = load_store()
    if df.empty or "state" not in df.columns:
        return []
    return sorted(df["state"].dropna().unique().tolist())


def main(argv=None):
    parser = argparse.ArgumentParser(description="PJM interconnection queue downloader.")
    sub = parser.add_subparsers(dest="cmd")
    up = sub.add_parser("update", help="Fetch the full queue and replace the store.")
    up.add_argument("--auto", action="store_true",
                    help="Quiet mode for scheduled jobs; skips if data is fresh.")
    sub.add_parser("status", help="Show local store summary.")

    args = parser.parse_args(argv)

    if args.cmd == "status":
        print(json.dumps(store_summary(), indent=2, default=str))
        return 0

    if args.cmd == "update":
        if getattr(args, "auto", False) and not is_stale():
            print(f"Data is fresh ({days_since_update():.1f} days old). Skipping.")
            return 0
        try:
            update()
            return 0
        except Exception as e:
            print(f"Update failed: {e}")
            return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
