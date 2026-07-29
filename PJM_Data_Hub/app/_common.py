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
    ("Zone LMPs (hourly)", "datasets.zone_prices.pjm_zone_prices", "update", True),
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


def auto_refresh_active(st_obj) -> bool:
    """True while a refresh is mid-flight (driven across reruns). Home uses this
    to keep pumping the state machine regardless of the auto_refresh setting."""
    return bool(st_obj.session_state.get("_ar_active"))


def auto_refresh(st_obj, *, force: bool = False) -> None:
    """Incrementally refresh all live datasets when the app opens.

    Runs the first time a Streamlit session renders (i.e. when the app is
    opened or the browser tab is reloaded), but skips if a refresh already
    happened within the last few hours — so reloading the tab doesn't re-pull
    everything and get rate-limited by PJM. Each dataset updates independently;
    a failure in one is surfaced but never blocks the app or the others. Updates
    are incremental, so when the store is already current this is quick.

    Rather than blocking on a single synchronous loop, the refresh is driven
    one dataset per rerun so a **Skip** button stays live between steps — the
    user can bail out and start using the app without waiting for every pull.
    """
    ss = st_obj.session_state

    # Already refreshing? Just pump the next step.
    if ss.get("_ar_active"):
        _drive_auto_refresh(st_obj)
        return

    if not force and ss.get("_auto_refreshed"):
        return

    if not force:
        hrs = _hours_since_last_refresh()
        if hrs is not None and hrs < _AUTO_REFRESH_MIN_INTERVAL_HOURS:
            ss["_auto_refreshed"] = True
            return  # refreshed recently — nothing to do

    from pjm_core import credentials

    cfg = credentials.load_config()
    have_pjm = credentials.have_credentials(cfg)

    # Kick off a fresh run: queue up the tasks and drive the first step.
    ss.pop("_ar_start_requested", None)
    ss["_ar_active"] = True
    ss["_ar_have_pjm"] = have_pjm
    ss["_ar_queue"] = [t for t in _AUTO_REFRESH_TASKS if not (t[3] and not have_pjm)]
    ss["_ar_log"] = []
    ss["_ar_updated"] = False
    ss.pop("_ar_skip", None)
    _drive_auto_refresh(st_obj)


def _drive_auto_refresh(st_obj) -> None:
    """Process one queued dataset, then rerun so the Skip button stays live."""
    import importlib

    ss = st_obj.session_state
    status = st_obj.status("🔄 Refreshing PJM data…", expanded=True)
    with status:
        if not ss.get("_ar_have_pjm"):
            st_obj.write(
                "⚠️ No PJM subscription key yet — set one on the **API Keys** page "
                "to auto-refresh prices, ancillary, and load.")
        for line in ss.get("_ar_log", []):
            st_obj.write(line)

        # Resolve terminal states first so the Skip button never lingers on the
        # final "done" frame.
        if ss.get("_ar_skip"):
            _finish_auto_refresh(st_obj, status, skipped=True)
            return

        queue = ss.get("_ar_queue", [])
        if not queue:
            _finish_auto_refresh(st_obj, status, skipped=False)
            return

        # Still work to do — offer Skip. A click lands on the next rerun; we set
        # the flag and rerun so the pending dataset is left un-pulled.
        if st_obj.button("⏭️ Skip refresh", key="_ar_skip_btn",
                         help="Stop refreshing and use the app now. "
                              "The remaining datasets keep whatever they had."):
            ss["_ar_skip"] = True
            st_obj.rerun()

        label, module_path, fn_name, _needs_key = queue[0]
        st_obj.write(f"⏳ {label}…")
        try:
            fn = getattr(importlib.import_module(module_path), fn_name)
            result = fn()
            rows = result.get("rows") if isinstance(result, dict) else None
            ss["_ar_log"].append(
                f"✅ {label}" + (f" — {rows:,} rows" if rows is not None else " done"))
            ss["_ar_updated"] = True
        except Exception as e:  # noqa: BLE001 — never let one dataset break app open
            ss["_ar_log"].append(f"❌ {label}: {e}")
        ss["_ar_queue"] = queue[1:]

    st_obj.rerun()


def _finish_auto_refresh(st_obj, status, *, skipped: bool) -> None:
    ss = st_obj.session_state
    if skipped:
        status.update(label="⏭️ Refresh skipped — using existing data",
                      state="complete")
    else:
        # Stamp the marker so a tab reload within the interval skips the refresh.
        try:
            _auto_refresh_marker().touch()
        except OSError:
            pass
        status.update(label="✅ Data up to date", state="complete")

    updated = ss.get("_ar_updated")
    ss["_auto_refreshed"] = True
    ss["_ar_active"] = False
    for k in ("_ar_queue", "_ar_log", "_ar_updated", "_ar_have_pjm", "_ar_skip"):
        ss.pop(k, None)

    if updated:
        # Screens cache their parquet loads with @st.cache_data; clear so they
        # pick up the freshly written rows on this run.
        st_obj.cache_data.clear()


_STATUS_DOT = {"green": "🟢", "yellow": "🟡", "red": "🔴", "grey": "⚪"}


def data_freshness_badge(st_obj) -> None:
    """Sidebar badge showing per-dataset freshness at a glance.

    Reads the lake's ``.last_update.json`` markers (via ``pjm_core.data_status``)
    and shows a coloured dot per dataset. Silent failures — a pipeline that
    stopped running while charts continued to render plausible-looking numbers —
    are the highest-consequence failure mode; this makes them visible on every
    screen without needing to open a terminal.
    """
    from pjm_core import data_status

    statuses = data_status.all_statuses()
    counts = {"green": 0, "yellow": 0, "red": 0, "grey": 0}
    for s in statuses:
        counts[s.status] += 1

    worst = data_status.worst_color(counts)
    with st_obj.sidebar:
        header = f"{_STATUS_DOT[worst]} Data lake"
        stale = counts["yellow"] + counts["red"]
        subtitle = (f"{counts['green']} fresh · {stale} stale · "
                    f"{counts['grey']} missing")
        with st_obj.expander(header, expanded=False):
            st_obj.caption(subtitle)
            for s in statuses:
                st_obj.markdown(
                    f"{_STATUS_DOT[s.status]} **{s.label}** — {s.detail}"
                )
            if st_obj.button("Open Data Status page", use_container_width=True,
                             key="_ds_open_btn"):
                st.switch_page("screens/22_Data_Status.py")


def refresh_prompt(st_obj) -> None:
    """Render a sidebar refresh control instead of auto-pulling on open.

    Pulling every dataset on app open blocks the first render for a long time
    (and can hit PJM rate limits). Instead we show the data's freshness in the
    sidebar and let the user click to refresh when they actually want it. The
    pull only runs on click, so opening the app is instant.
    """
    data_freshness_badge(st_obj)
    with st_obj.sidebar:
        hrs = _hours_since_last_refresh()
        if hrs is None:
            st_obj.caption("PJM data: never refreshed this session.")
        elif hrs < 1:
            st_obj.caption(f"PJM data refreshed {int(hrs * 60)} min ago.")
        else:
            st_obj.caption(f"PJM data last refreshed {hrs:.0f} h ago.")
        if st_obj.button("🔄 Refresh PJM data", use_container_width=True,
                         help="Incrementally pull the latest hub prices, ancillary, "
                              "load, and weather. Runs only when you click."):
            # Start the skippable refresh; Home drives it (and shows Skip) in the
            # main area on the next rerun.
            st_obj.session_state["_ar_start_requested"] = True
            st_obj.rerun()


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
