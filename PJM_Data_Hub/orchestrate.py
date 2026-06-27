#!/usr/bin/env python3
"""PJM Data Hub CLI orchestrator.

Usage:
    python orchestrate.py update hub_prices           # update DOM Hub LMPs
    python orchestrate.py update hub_prices --all-hubs # update all PJM hubs
    python orchestrate.py update system_gen           # PJM fuel mix (gridstatus)
    python orchestrate.py update eia923               # EIA-923 for PJM plants
    python orchestrate.py update all                  # all datasets
    python orchestrate.py status                      # show store state
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pjm_core import credentials, paths
from pjm_core.settlement_points import HUBS, PRIMARY_HUB


def _update_hub_prices(all_hubs: bool = True) -> None:
    from datasets.hub_prices.pjm_api import update
    # gridstatus returns every hub in one call, so keep them all by default;
    # --primary-only narrows the stored set to just DOMINION HUB.
    hubs = None if all_hubs else [PRIMARY_HUB]
    print(f"Updating hub prices ({'all hubs' if all_hubs else PRIMARY_HUB}) …")
    result = update(hubs=hubs)
    print(f"Done. {result['rows']:,} rows ({result['start']} → {result['end']})")


def _update_system_gen(years=None) -> None:
    from datasets.system_gen_by_fuel.pjm_gen import update
    update(years=years, log=print)


def _update_eia923(years=None) -> None:
    from datasets.eia923.eia923 import update
    update(years=years, log=print)


def main():
    parser = argparse.ArgumentParser(description="PJM Data Hub orchestrator")
    sub = parser.add_subparsers(dest="cmd")

    up = sub.add_parser("update", help="Refresh a dataset")
    up.add_argument("dataset", choices=["hub_prices", "system_gen", "eia923", "all"])
    up.add_argument("--primary-only", action="store_true",
                    help="Store only DOMINION HUB (default: keep all PJM hubs)")
    up.add_argument("--years", nargs="*", type=int, default=None)

    sub.add_parser("status", help="Show store state")

    args = parser.parse_args()

    if args.cmd == "status":
        from datasets.hub_prices.pjm_api import store_summary
        import json
        print(json.dumps(store_summary(), indent=2, default=str))
        return 0

    if args.cmd == "update":
        ds = args.dataset
        if ds in ("hub_prices", "all"):
            _update_hub_prices(all_hubs=not getattr(args, "primary_only", False))
        if ds in ("system_gen", "all"):
            _update_system_gen(years=args.years)
        if ds in ("eia923", "all"):
            _update_eia923(years=args.years)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
