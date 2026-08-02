#!/usr/bin/env python3
"""PJM Data Hub CLI orchestrator.

Usage:
    python orchestrate.py update hub_prices                 # all hubs, RT + DA LMPs
    python orchestrate.py update hub_prices --primary-only  # DOMINION HUB only
    python orchestrate.py update zone_prices                # hourly + monthly LMP per zone
    python orchestrate.py update system_gen                 # PJM fuel mix (gridstatus)
    python orchestrate.py update load                       # hourly metered load by zone
    python orchestrate.py update ancillary                  # reserve + regulation prices
    python orchestrate.py update weather                    # ERA5 at major load centers
    python orchestrate.py update eia923                     # EIA-923 for PJM plants
    python orchestrate.py update eia860                     # EIA-860 plant capacity
    python orchestrate.py update all                        # every dataset (continue on error)
    python orchestrate.py status                            # show store state
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pjm_core import credentials, paths
from pjm_core.settlement_points import HUBS, PRIMARY_HUB


def _update_hub_prices(all_hubs: bool = True, **_) -> None:
    from datasets.hub_prices.pjm_api import update
    # gridstatus returns every hub in one call, so keep them all by default;
    # --primary-only narrows the stored set to just DOMINION HUB.
    hubs = None if all_hubs else [PRIMARY_HUB]
    print(f"Updating hub prices ({'all hubs' if all_hubs else PRIMARY_HUB}) …")
    result = update(hubs=hubs)
    print(f"Done. {result['rows']:,} rows ({result['start']} → {result['end']})")


def _update_zone_prices(**_) -> None:
    from datasets.zone_prices.pjm_zone_prices import update
    print("Updating zone prices (monthly avg LMP per zone) …")
    update()


def _update_system_gen(years=None, **_) -> None:
    from datasets.system_gen_by_fuel.pjm_gen import update
    print("Updating system generation by fuel …")
    update(years=years, log=print)


def _update_load(**_) -> None:
    from datasets.load.pjm_load import update
    print("Updating system load (hourly metered by zone) …")
    update()


def _update_ancillary(**_) -> None:
    from datasets.ancillary.pjm_as import update
    print("Updating ancillary services (reserve + regulation) …")
    update()


def _update_weather(**_) -> None:
    from datasets.weather.pjm_weather import update
    print("Updating weather (ERA5 at major load centers) …")
    update()


def _update_eia923(years=None, **_) -> None:
    from datasets.eia923.eia923 import update
    print("Updating EIA-923 (plant net generation) …")
    update(years=years, log=print)


def _update_eia860(years=None, **_) -> None:
    from datasets.eia860.eia860 import update
    print("Updating EIA-860 (plant capacity) …")
    update(years=years, log=print)


def _update_eia860m(**_) -> None:
    from datasets.eia860m.eia860m import update
    print("Updating EIA-860M (monthly generator inventory) …")
    update(log=print)


def _update_queue(**_) -> None:
    from datasets.queue.pjm_queue import update
    print("Updating interconnection queue (daily snapshot) …")
    update()


# Registry: dataset name → updater. Order defines the sequence for `update all`.
UPDATERS = {
    "hub_prices": _update_hub_prices,
    "zone_prices": _update_zone_prices,
    "system_gen": _update_system_gen,
    "load": _update_load,
    "ancillary": _update_ancillary,
    "weather": _update_weather,
    "eia923": _update_eia923,
    "eia860": _update_eia860,
    "eia860m": _update_eia860m,
    "queue": _update_queue,
}


# Datasets that keep a single parquet + .last_update.json state file.
_STATE_DATASETS = [
    ("hub_prices", paths.HUB_PRICES_STATE),
    ("zone_prices", paths.ZONE_PRICES_STATE),
    ("load", paths.LOAD_STATE),
    ("ancillary", paths.ANCILLARY_STATE),
    ("weather", paths.WEATHER_STATE),
    ("queue", paths.QUEUE_STATE),
    ("eia860m", paths.EIA860M_STATE),
]

# Year-partitioned datasets: one parquet per year, no state file.
_YEAR_DATASETS = [
    ("system_gen", paths.SYSTEM_GEN_DIR, "pjm_gen_by_fuel_*.parquet"),
    ("eia923", paths.EIA_DIR, "eia923_pjm_*.parquet"),
    ("eia860", paths.EIA860_DIR, "eia860_pjm_*.parquet"),
]


def _age(iso_ts: str | None) -> str:
    """Human 'N days ago' from an ISO timestamp (returns '' if unparseable)."""
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        return f"{days:.1f}d ago"
    except (ValueError, TypeError):
        return ""


def _status() -> dict:
    """Row counts, date range, and freshness for every dataset."""
    out = {}
    for name, state_path in _STATE_DATASETS:
        if not state_path.exists():
            out[name] = {"exists": False}
            continue
        try:
            st = json.loads(state_path.read_text())
        except (ValueError, OSError) as exc:
            out[name] = {"exists": True, "error": str(exc)}
            continue
        out[name] = {
            "exists": True,
            "rows": st.get("rows"),
            "start": st.get("start"),
            "end": st.get("end"),
            "last_success": st.get("last_success"),
            "age": _age(st.get("last_success")),
        }

    for name, ddir, pattern in _YEAR_DATASETS:
        files = sorted(ddir.glob(pattern))
        if not files:
            out[name] = {"exists": False}
            continue
        years = sorted({int(m.group()) for f in files
                        for m in [re.search(r"\d{4}", f.name)] if m})
        newest = max(files, key=lambda f: f.stat().st_mtime)
        mtime = datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc)
        out[name] = {
            "exists": True,
            "files": len(files),
            "years": f"{years[0]}–{years[-1]}" if years else None,
            "last_modified": mtime.isoformat(),
            "age": _age(mtime.isoformat()),
        }

    out["capacity"] = {"exists": paths.CAPACITY_CSV.exists()}
    return out


def main():
    parser = argparse.ArgumentParser(description="PJM Data Hub orchestrator")
    sub = parser.add_subparsers(dest="cmd")

    up = sub.add_parser("update", help="Refresh a dataset")
    up.add_argument("dataset", choices=list(UPDATERS) + ["all"])
    up.add_argument("--primary-only", action="store_true",
                    help="Store only DOMINION HUB (hub_prices; default: keep all PJM hubs)")
    up.add_argument("--years", nargs="*", type=int, default=None,
                    help="Limit to these years (system_gen, eia923, eia860)")

    stat = sub.add_parser("status", help="Show store state for every dataset")
    stat.add_argument("--json", action="store_true", help="Emit raw JSON")

    args = parser.parse_args()

    if args.cmd == "status":
        info = _status()
        if getattr(args, "json", False):
            print(json.dumps(info, indent=2, default=str))
            return 0
        for name, s in info.items():
            if not s.get("exists"):
                print(f"  {name:<12} (empty)")
            elif "error" in s:
                print(f"  {name:<12} error: {s['error']}")
            elif "rows" in s:
                rng = f"{s.get('start')} → {s.get('end')}" if s.get("start") else ""
                print(f"  {name:<12} {s['rows']:>12,} rows  {rng:<45} {s.get('age', '')}")
            elif "files" in s:
                print(f"  {name:<12} {s['files']:>2} files  years {s.get('years'):<12} "
                      f"{'':<32} {s.get('age', '')}")
            else:
                print(f"  {name:<12} present")
        return 0

    if args.cmd == "update":
        kwargs = dict(
            all_hubs=not getattr(args, "primary_only", False),
            years=args.years,
        )
        targets = list(UPDATERS) if args.dataset == "all" else [args.dataset]
        failures = []
        for name in targets:
            try:
                UPDATERS[name](**kwargs)
            except Exception as exc:
                # For `all`, keep going so one missing key / feed doesn't abort
                # the whole nightly refresh; surface the failures at the end.
                if args.dataset != "all":
                    raise
                print(f"  ✗ {name} failed: {exc}", file=sys.stderr)
                failures.append(name)
        if failures:
            print(f"\nCompleted with {len(failures)} failure(s): {', '.join(failures)}",
                  file=sys.stderr)
            return 1
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
