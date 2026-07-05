#!/usr/bin/env python3
"""PJM 5CP peak-risk morning digest.

A push companion to the 5CP Peak Predictor screen: run it each summer morning
to get a short, plain-text briefing of the coming days' coincident-peak risk —
which afternoons the weather forecast says could set a 5CP, and therefore when
to curtail load to protect Peak Load Contribution.

It reuses ``pjm_core.peak`` so the numbers match the app exactly. Designed to be
piped into email/Slack or read at the terminal.

Usage:
    python scripts/peak_digest.py                 # print today's digest
    python scripts/peak_digest.py --days 10       # shorter horizon
    python scripts/peak_digest.py --refresh       # update load+weather first
    python scripts/peak_digest.py --format md     # markdown (for email/Slack)
    python scripts/peak_digest.py --min-prob 0.2  # only flag days above 20%

Exit code is 2 when at least one day is High/Elevated risk (so a scheduler can
branch on "should I alert?"), 0 otherwise, 1 on error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from pjm_core import peak, tz


def _refresh() -> None:
    """Best-effort update of the load and weather stores before predicting."""
    from datasets.load import pjm_load
    from datasets.weather import pjm_weather
    for name, mod in (("load", pjm_load), ("weather", pjm_weather)):
        try:
            mod.update()
        except Exception as e:  # noqa: BLE001 — digest still runs on stale data
            print(f"[warn] {name} refresh failed: {e}", file=sys.stderr)


def build_digest(days: int, min_prob: float, fmt: str) -> tuple[str, bool]:
    """Return (text, alert) — alert True if any day is Elevated or higher."""
    load = peak.rto_hourly_load()
    if load.empty:
        return "No RTO load data — run an update first.", False

    year = tz.now_eastern().year
    threshold = peak.current_threshold(load, year) or 0.0
    pred = peak.predict_upcoming(days=days, load=load)
    today = tz.now_eastern().date()

    md = fmt == "md"
    h = "## " if md else ""
    bullet = "- " if md else "  • "
    lines = [f"{h}PJM 5CP Peak Risk — {today:%A, %b %d, %Y}"]
    lines.append("")
    lines.append(f"Current {year} 5CP threshold: {threshold:,.0f} MW "
                 "(a day must beat this to reset capacity costs).")
    lines.append("")

    if pred.empty:
        lines.append("No summer days in the forecast horizon — the 5CP window is "
                     "Jun 1 – Sep 30. No action needed.")
        return "\n".join(lines), False

    flagged = pred[pred["prob_5cp"] >= min_prob].sort_values("day")
    alert = bool((pred["prob_5cp"] >= 0.33).any())

    if flagged.empty:
        top = pred.loc[pred["prob_5cp"].idxmax()]
        lines.append(f"🟢 No elevated 5CP risk in the next {days} days.")
        lines.append(f"Hottest day: {pd.to_datetime(top['day']):%a %b %d} — "
                     f"apparent {top['tmax_apparent_f']:.0f}°F, predicted "
                     f"{top['predicted_peak_mw']:,.0f} MW ({top['prob_5cp']*100:.0f}% chance).")
        return "\n".join(lines), alert

    lines.append(f"{h}Days to watch (≥{min_prob*100:.0f}% chance of a 5CP):")
    for _, r in flagged.iterrows():
        d = pd.to_datetime(r["day"])
        star = " ⚠ beyond training range" if r["extrapolated"] else ""
        lines.append(
            f"{bullet}{d:%a %b %d}: {peak.risk_label(r['prob_5cp'])} "
            f"({r['prob_5cp']*100:.0f}%) — apparent {r['tmax_apparent_f']:.0f}°F, "
            f"predicted {r['predicted_peak_mw']:,.0f} MW "
            f"({r['margin_mw']:+,.0f} vs threshold){star}")
    lines.append("")
    lines.append("Action: on High/Elevated days, curtail load during the late-"
                 "afternoon peak (hour ending ~17:00–18:00 EPT) to protect PLC.")
    return "\n".join(lines), alert


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PJM 5CP peak-risk morning digest.")
    ap.add_argument("--days", type=int, default=14, help="Forecast horizon (max 16).")
    ap.add_argument("--min-prob", type=float, default=0.10,
                    help="Only list days at or above this 5CP probability.")
    ap.add_argument("--format", choices=["text", "md"], default="text",
                    help="Plain text or markdown (for email/Slack).")
    ap.add_argument("--refresh", action="store_true",
                    help="Update the load and weather stores before predicting.")
    args = ap.parse_args(argv)

    try:
        if args.refresh:
            _refresh()
        text, alert = build_digest(args.days, args.min_prob, args.format)
        print(text)
        return 2 if alert else 0
    except Exception as e:  # noqa: BLE001
        print(f"Digest failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
