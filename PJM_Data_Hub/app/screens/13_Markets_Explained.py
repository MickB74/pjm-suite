"""PJM markets explainer — energy, capacity, ancillary services & FTRs.

Plain-language reference tying together the concepts used across the other
screens (LMP, RPM capacity, reserve/regulation, congestion/basis) so someone
new to PJM has one page to read before digging into the data views.
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _common  # noqa: F401

import streamlit as st

st.title("📚 PJM Markets Explained")
st.caption("A plain-language map of the markets PJM runs, how they relate, "
           "and where to find each one in this app.")

st.markdown(
    "PJM runs several distinct but related markets. Generators and load get "
    "paid (or pay) through each of these separately — together they make up "
    "the total cost of electricity in PJM."
)

tabs = st.tabs([
    "Overview", "Energy (LMP)", "Capacity (RPM)", "Ancillary Services",
    "Transmission (FTRs)", "Capacity Performance",
])

# ---------------------------------------------------------------------------
with tabs[0]:
    st.subheader("The four markets, in one table")
    st.markdown(
        """
| Market | Pays for | Priced in | Cleared | See in this app |
|---|---|---|---|---|
| **Energy** | MWh actually delivered | $/MWh (LMP) | Day-ahead & real-time, every 5 min–hour | Hub Prices, DA–RT Spread, Hub Basis |
| **Capacity (RPM)** | Being *available* 1–3 years out | $/MW-day | Base Residual Auction, ~3 years ahead | Capacity (RPM) |
| **Ancillary Services** | Reserves & regulation for reliability | $/MWh (MCP) | Real-time, every 5 min–hour | Ancillary Services |
| **Transmission (FTRs)** | Hedging congestion cost | $/MWh (auction-cleared) | Annual/monthly auctions | Hub Basis (congestion component) |
        """
    )
    st.info(
        "Rule of thumb: **capacity** pays generators to *exist and be ready*; "
        "**energy** pays them to *actually run*; **ancillary services** pay them "
        "to hold something in reserve in case something breaks; **FTRs** let "
        "market participants hedge the locational price differences (congestion) "
        "that show up in energy prices.",
        icon="💡")

# ---------------------------------------------------------------------------
with tabs[1]:
    st.subheader("Energy market — Locational Marginal Price (LMP)")
    st.markdown(_common.LMP_COMPONENT_HELP)
    st.markdown(
        """
**Two settlement passes:**
- **Day-ahead (DA)** — a financially binding auction cleared the day before,
  based on offers/bids and forecast conditions.
- **Real-time (RT)** — the actual dispatch price, settled every 5 minutes off
  live system conditions.

The difference between the two (**DART spread**) is a common hedging/trading
signal — see the *DA–RT Spread* screen.

**Congestion** is the part of LMP that varies by location; when a transmission
line is constrained, downstream nodes need more expensive generation, driving
their LMP above the system energy price. The gap between any two locations'
LMP is called **basis** — see the *Hub Basis* screen.
        """
    )

# ---------------------------------------------------------------------------
with tabs[2]:
    st.subheader("Capacity market — Reliability Pricing Model (RPM)")
    st.markdown(
        """
PJM must ensure enough generation (or demand response / imports) will be
available roughly **3 years in the future** to meet forecast peak demand plus
a reserve margin. Rather than mandate this, PJM runs the **Base Residual
Auction (BRA)**: generators bid in the capacity they can commit, and a single
clearing price is set per **Locational Deliverability Area (LDA)** — a
sub-region where transmission constraints can isolate local capacity needs
from the rest of the RTO.

- **Cleared in $/MW-day** — a generator that clears 100 MW at \\$100/MW-day
  earns \\$10,000/day (~\\$3.65M/year) just for being available, regardless of
  whether it ever runs.
- **LDA-specific pricing** — an import-constrained zone (e.g. a load pocket)
  can clear at a large premium over the system-wide RTO price.
- **PJM publishes no live API for this** — the *Capacity (RPM)* screen reads a
  maintained reference table of published BRA results; update it as new
  auctions clear.
- Load-serving entities pay capacity charges based on their share of peak
  demand (their "capacity obligation"); this shows up on retail/wholesale
  invoices as a separate line from energy.
        """
    )

# ---------------------------------------------------------------------------
with tabs[3]:
    st.subheader("Ancillary services — reserves & regulation")
    st.markdown(
        """
Ancillary services keep the grid stable on a minute-to-minute basis — energy
prices alone don't guarantee enough spare capacity exists to respond if a
generator trips or load spikes. Since PJM's **October 2022 reserve-market
reform**, every reserve product and regulation clears on one unified hourly
engine, priced as a **Market Clearing Price (MCP, $/MWh)**.

**Products you'll see in the Ancillary Services screen:**
- **REG (Regulation)** — very fast (seconds-scale) automatic response to
  small, continuous imbalances; split into a *capability* and *performance*
  component (performance rewards how accurately a resource tracks the signal).
- **SR (Synchronized Reserve)** — online generation that can ramp within 10
  minutes.
- **PR (Primary Reserve)** — combines synchronized reserve with fast-responding
  non-synchronized resources.
- **30MIN (30-Minute Reserve)** — can start and reach full output within 30
  minutes; a cheaper, slower backstop.
- **NSR (Non-Synchronized Reserve)** — offline units that can start and
  synchronize within 10 minutes.
- **SEC (Secondary Reserve)** — additional reserve tier introduced in the 2022
  reform, sitting alongside synchronized/primary reserve.

**PJM_RTO** is the system-wide clearing price; other locales are constrained
sub-zones that can clear at a premium during local reserve shortages, similar
to how LDAs work for capacity.

Ancillary costs are usually a small fraction of total power cost compared to
energy, but they spike sharply during tight system conditions — worth watching
alongside energy price in the same screen.
        """
    )

# ---------------------------------------------------------------------------
with tabs[4]:
    st.subheader("Transmission — congestion & Financial Transmission Rights")
    st.markdown(
        """
Congestion (part of LMP, see the Energy tab) means the price to deliver power
differs by location. **Financial Transmission Rights (FTRs)** let market
participants hedge this: an FTR from point A to point B pays the holder the
difference in day-ahead congestion between A and B, regardless of physical
flow.

- Cleared in **PJM's annual and monthly FTR auctions**.
- A generator or load-serving entity exposed to basis risk (e.g. because it's
  physically located far from the hub it trades against) can buy FTRs to
  offset that risk.
- This app doesn't model FTR auction clearing directly, but the **Hub Basis**
  screen shows the realized congestion/basis that FTRs are designed to hedge —
  useful context for whether a given path's basis has been stable or volatile.
        """
    )

# ---------------------------------------------------------------------------
with tabs[5]:
    st.subheader("Capacity Performance (CP) — the accountability layer")
    st.markdown(
        """
Since the 2015/16 delivery year, PJM's capacity product is **Capacity
Performance**, not the older "base capacity" product. The key difference:

- CP resources face much larger **non-performance penalties** if they fail to
  respond during a declared emergency, and correspondingly larger
  **performance bonuses** if they over-perform.
- This was a direct response to generator failures during extreme cold events
  (e.g. the 2014 Polar Vortex), pushing PJM to price *reliability* into the
  capacity product itself, not just availability.
- Practically: the clearing price you see in the Capacity (RPM) screen already
  reflects CP risk — generators bid in the penalty exposure they're accepting.
        """
    )

st.divider()
st.caption(
    "This page is a conceptual reference, not official PJM documentation. "
    "For authoritative detail see PJM's "
    "[Markets & Operations](https://www.pjm.com/markets-and-operations) pages "
    "and the [Learning Center](https://learn.pjm.com/)."
)
