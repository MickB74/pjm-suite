"""Peak Day Analysis: the price shown must belong to the selected zone.

Regression guard. The screen used to resolve a zone's price with
``ZONE_HOME_HUB.get(zone, PRIMARY_HUB)``, which only maps 6 of PJM's 22 load
zones — so picking PEPCO (or any of the other 15) silently rendered a DOMINION
HUB price next to PEPCO load and labelled the pair coincident. On PEP's 2026
peak hours that hub price was off by -36% to +90%.

These drive the real screen through Streamlit's AppTest harness, so they cover
the wiring end to end rather than a reimplementation of it. They need the local
data lake and are skipped where it is absent (e.g. CI).
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from pjm_core import paths

SCREEN = Path(__file__).resolve().parents[1] / "app" / "screens" / "10_Peak_Day_Analysis.py"

pytest.importorskip("streamlit.testing.v1")


def _have_data() -> bool:
    return (paths.LOAD_PARQUET.exists()
            and paths.ZONE_PRICES_HOURLY_PARQUET.exists()
            and paths.HUB_PRICES_PARQUET.exists())


pytestmark = pytest.mark.skipif(
    not _have_data(), reason="needs the local data lake (load + zone/hub prices)")


def _cards(at) -> dict[str, str]:
    """{label: value} from the screen's hand-rolled KPI card HTML."""
    out = {}
    for block in (m.value for m in at.markdown if "kpi-card" in m.value):
        for lbl, val in re.findall(
                r'kpi-label">(.*?)</div><div class="kpi-value">(.*?)</div>', block):
            out[lbl] = val
    return out


def _tips(at) -> list[str]:
    out = []
    for block in (m.value for m in at.markdown if "kpi-card" in m.value):
        out += re.findall(r'kpi-tip">(.*?)</div>', block)
    return out


def _run(zone: str):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(SCREEN), default_timeout=240)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    sb = [s for s in at.selectbox if s.label == "Load zone"][0]
    if zone not in sb.options:
        pytest.skip(f"zone {zone} absent from the local load store")
    sb.set_value(zone).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_zone_with_its_own_lmp_shows_that_zone_not_a_hub():
    """PEP must price off PEPCO. Before the fix this card read 'Dominion'."""
    at = _run("PEP")
    cards = _cards(at)
    assert "PEPCO RT LMP" in cards, cards
    assert not any("Dominion" in k for k in cards), cards
    assert "PEP zone's own zonal LMP" in _tips(at)


def test_zone_price_matches_the_hourly_store_at_the_peak_hour():
    """The rendered number must be the zone's actual LMP at that hour, not a
    nearby hour or a daily average."""
    from datasets.zone_prices import pjm_zone_prices as Z
    at = _run("PEP")
    shown = float(_cards(at)["PEPCO RT LMP"].lstrip("$").replace(",", ""))

    # Recover the peak hour the screen chose from its own subheader.
    head = next(h.value for h in at.subheader if "Coincident peak" in h.value)
    stamp = re.search(r"(\d{4}-\d{2}-\d{2}) (\d{2}):00", head)
    peak_hour = pd.Timestamp(f"{stamp.group(1)} {stamp.group(2)}:00")

    zp = Z.load_hourly(market="RT", zones=["PEPCO"])
    expected = zp.loc[zp["datetime_beginning_ept"] == peak_hour, "total_lmp"]
    assert len(expected) == 1
    assert shown == pytest.approx(float(expected.iloc[0]), abs=0.01)


def test_rto_has_no_zonal_lmp_and_says_so():
    """RTO is the system aggregate — no zonal price exists. Falling back to a
    hub is fine; presenting it as RTO's own price is not."""
    at = _run("RTO")
    cards = _cards(at)
    assert any("RT LMP" in k for k in cards), cards
    assert not any(k.startswith("RTO RT LMP") for k in cards), cards
    assert any("Reference hub" in t and "RTO" in t for t in _tips(at)), _tips(at)


def test_zone_load_and_price_cards_describe_the_same_zone():
    """The two cards sit side by side and read as a coincident pair, so they
    must not come from different places on the system."""
    at = _run("BC")            # Baltimore Gas & Electric — no namesake hub
    cards = _cards(at)
    assert "BC load" in cards, cards
    assert "BGE RT LMP" in cards, cards
