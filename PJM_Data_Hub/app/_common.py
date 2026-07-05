"""Shared Streamlit helpers for the PJM Data Hub."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

# Shared definition of PJM's LMP components. LMP ("Locational Marginal Price")
# is the $/MWh price of energy at a specific location; it decomposes into three
# additive parts. Reused by every screen with an LMP-component selector.
LMP_COMPONENT_HELP = (
    "PJM's Locational Marginal Price (LMP) is the price of power (\\$/MWh) at a "
    "given location and hour. It's the sum of three parts:\n\n"
    "- **total_lmp** — the all-in price that actually settles "
    "(energy + congestion + loss).\n"
    "- **energy** — the system-wide marginal cost of energy; identical "
    "everywhere in PJM for that hour.\n"
    "- **congestion** — the locational adder caused by transmission "
    "constraints; the main reason two hubs differ in price (it drives *basis*). "
    "Can be negative.\n"
    "- **loss** — the cost of the electrical losses incurred delivering power "
    "to that location."
)


# Datasets refreshed automatically when the app opens. Each entry is
# (label, "module.path:function", requires_pjm_key). Updates are incremental,
# so they're fast when the store is already current.
_AUTO_REFRESH_TASKS = [
    ("Hub prices", "datasets.hub_prices.pjm_api", "update", True),
    ("Zone LMPs (monthly)", "datasets.zone_prices.pjm_zone_prices", "update", True),
    ("Ancillary services", "datasets.ancillary.pjm_as", "update", True),
    ("System load", "datasets.load.pjm_load", "update", True),
    ("Weather (ERA5)", "datasets.weather.pjm_weather", "update", False),
    ("Interconnection queue", "datasets.queue.pjm_queue", "update", False),
]


# Skip auto-refresh if the data was already refreshed within this many hours.
# Prevents a tab reload from re-pulling everything (and hitting PJM rate limits)
# while still refreshing on a genuine "open the app in the morning" basis.
_AUTO_REFRESH_MIN_INTERVAL_HOURS = 6


def _auto_refresh_marker():
    from pjm_core import paths
    return paths.DATA / ".last_auto_refresh"


def _hours_since_last_refresh() -> float | None:
    marker = _auto_refresh_marker()
    if not marker.exists():
        return None
    age_s = pd.Timestamp.now().timestamp() - marker.stat().st_mtime
    return age_s / 3600.0


def auto_refresh(st_obj, *, force: bool = False) -> None:
    """Incrementally refresh all live datasets when the app opens.

    Runs the first time a Streamlit session renders (i.e. when the app is
    opened or the browser tab is reloaded), but skips if a refresh already
    happened within the last few hours — so reloading the tab doesn't re-pull
    everything and get rate-limited by PJM. Each dataset updates independently;
    a failure in one is surfaced but never blocks the app or the others. Updates
    are incremental, so when the store is already current this is quick.
    """
    import importlib

    if not force and st_obj.session_state.get("_auto_refreshed"):
        return
    st_obj.session_state["_auto_refreshed"] = True

    if not force:
        hrs = _hours_since_last_refresh()
        if hrs is not None and hrs < _AUTO_REFRESH_MIN_INTERVAL_HOURS:
            return  # refreshed recently — nothing to do

    from pjm_core import credentials

    cfg = credentials.load_config()
    have_pjm = credentials.have_credentials(cfg)

    updated_any = False
    with st_obj.status("🔄 Refreshing PJM data…", expanded=False) as status:
        if not have_pjm:
            status.write(
                "⚠️ No PJM subscription key yet — set one on the **API Keys** page "
                "to auto-refresh prices, ancillary, and load.")
        for label, module_path, fn_name, needs_key in _AUTO_REFRESH_TASKS:
            if needs_key and not have_pjm:
                continue
            try:
                status.write(f"⏳ {label}…")
                fn = getattr(importlib.import_module(module_path), fn_name)
                result = fn()
                rows = result.get("rows") if isinstance(result, dict) else None
                status.write(f"✅ {label}" + (f" — {rows:,} rows" if rows is not None else " done"))
                updated_any = True
            except Exception as e:  # noqa: BLE001 — never let one dataset break app open
                status.write(f"❌ {label}: {e}")
        status.update(label="✅ Data up to date", state="complete")

    # Stamp the marker so a tab reload within the interval skips the refresh.
    try:
        _auto_refresh_marker().touch()
    except OSError:
        pass

    if updated_any:
        # Screens cache their parquet loads with @st.cache_data; clear so they
        # pick up the freshly written rows on this run.
        st_obj.cache_data.clear()


def rate_explainer(st_obj, *, expanded: bool = False) -> None:
    """Shared explainer: how the 5 coincident-peak hours set a customer's PLC and
    capacity charge. Rendered as a collapsible expander so it can sit at the top
    of any capacity-related screen (Peak Day Analysis, PLC Cost Calculator)."""
    with st_obj.expander(
            "💡 How these peak days set your price & how your rate is calculated",
            expanded=expanded):
        st_obj.markdown(
            r"""
These peak days aren't just trivia — they set your **capacity charge**, which is
roughly **a quarter of a commercial load's all-in power cost**. Here's the chain
from a peak hour to a dollar figure on your bill.

**1 · The 5 peak days set the measuring stick (5CP).**
Each summer PJM records the **5 highest RTO load hours of the year** (each on a
separate day). Those five hours are the **only** hours all year that determine
your capacity charge; the other ~8,755 hours don't count.

**2 · Your load during those 5 hours = your Peak Load Contribution (PLC).**
PJM averages *your* metered demand across those five coincident-peak hours, then
grosses it up for losses and reserve margin:
"""
        )
        st_obj.latex(r"PLC_{billed} = \left(\frac{1}{5}\sum_{i=1}^{5} MW_i\right)\times FPR")
        st_obj.markdown(
            r"""
**FPR** = Forecast Pool Requirement / loss-&-reserve scaling (~1.05–1.10). This
single PLC number is your **capacity obligation**, locked in for the whole
**delivery year** (June 1 → May 31).

**3 · Your rate = PLC × the capacity clearing price × 365.**
"""
        )
        st_obj.latex(r"Annual\ capacity\ \$ = PLC \times RPM\ price\ \left(\tfrac{\$}{MW\text{-}day}\right) \times 365")
        st_obj.markdown(
            """
- The **RPM clearing price** is set in PJM's capacity auction ~3 years ahead
  (see the **Capacity (RPM)** screen) and is **zone/LDA-specific** — e.g. DOM
  can price apart from the rest of the RTO.
- Because the charge is fixed by just 5 hours, two customers with identical total
  kWh can pay very different capacity bills.

**The lever — peak shaving.** Cutting load during the *predicted* CP hours lowers
next year's PLC and therefore your bill:
"""
        )
        st_obj.latex(r"Savings = RPM\ price \times (MW_{shaved}\times FPR) \times 365")
        st_obj.markdown(
            """
| Step | What sets it | Where in this app |
|---|---|---|
| Which 5 hours count | PJM's 5 highest RTO load hours (5CP) | **Peak Day Analysis** |
| Your PLC (billed MW) | your load in those 5 hrs × FPR | **PLC Cost Calculator** |
| The \\$/MW-day price | RPM capacity auction (LDA-specific) | **Capacity (RPM)** |
| Your annual charge | PLC × price × 365 | **PLC Cost Calculator** |
| Reducing it | shave load on predicted CP days | **5CP Peak Predictor** |

*Simplified model — real PJM settlement layers on additional scaling; the PLC
Cost Calculator lets you enter your FPR and metered load to get your own number.*
            """
        )


def data_status(st_obj, *, path: Path, rows: int, span: tuple) -> None:
    dmin, dmax = span
    st_obj.caption(
        f"**{rows:,} rows** · {dmin} → {dmax} · "
        f"last modified {pd.Timestamp(path.stat().st_mtime, unit='s').strftime('%Y-%m-%d %H:%M') if path.exists() else 'never'}"
    )


def empty_state(st_obj, title: str, hint: str = "", page: str = "", page_label: str = "") -> None:
    st_obj.warning(f"**{title}** {hint}")
    if page and page_label:
        if st_obj.button(page_label):
            st.switch_page(page)
    st_obj.stop()


def period_picker(
    st_obj,
    *,
    key: str,
    min_year: int = 2019,
    default_mode: str = "Month",
) -> tuple[date, date]:
    today = date.today()
    mode = st_obj.radio("Period", ["Month", "Year", "Custom"], index=["Month", "Year", "Custom"].index(default_mode),
                        horizontal=True, key=f"{key}_mode")
    if mode == "Month":
        col1, col2 = st_obj.columns(2)
        year = col1.selectbox("Year", range(today.year, min_year - 1, -1), key=f"{key}_yr")
        month = col2.selectbox("Month", range(1, 13), index=today.month - 2 if today.month > 1 else 0,
                               key=f"{key}_mo")
        start = date(year, month, 1)
        end = (pd.Timestamp(start) + pd.offsets.MonthEnd(0)).date()
        return start, end
    if mode == "Year":
        year = st_obj.selectbox("Year", range(today.year, min_year - 1, -1), key=f"{key}_yr_y")
        return date(year, 1, 1), date(year, 12, 31)
    # Custom
    col1, col2 = st_obj.columns(2)
    start = col1.date_input("Start", date(today.year, 1, 1), key=f"{key}_s")
    end = col2.date_input("End", today, key=f"{key}_e")
    return start, end
