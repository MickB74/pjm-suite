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
    "Overview", "Energy (LMP)", "Trading Hubs", "Capacity (RPM)",
    "Ancillary Services", "Transmission (FTRs)", "Capacity Performance",
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
    st.subheader("PJM trading hubs")
    st.markdown(
        "A **hub** is a basket of pricing nodes (pnodes) whose LMPs are averaged "
        "into one number — a stable, liquid reference price that isn't tied to any "
        "single generator or bus. PJM publishes **12 hubs**. They split into "
        "*trading hubs* (load-weighted references used for OTC/futures trading and "
        "settlement) and *generation hubs* (generator-weighted baskets used to "
        "value output in a specific zone). See all 12 plotted on the **Hub Prices** "
        "map."
    )
    st.markdown(
        """
| Hub | Region | Type | Role |
|---|---|---|---|
| **WESTERN HUB** | Western / Allegheny | Trading | PJM's benchmark — by far the most liquid; the reference for ICE/NYMEX PJM futures & most OTC trades |
| **AEP-DAYTON HUB** | Western Ohio (AEP + DAY) | Trading | Second most-traded hub; the western-side benchmark |
| **EASTERN HUB** | Eastern (PECO/PSEG area) | Trading | Reference for the higher-priced, load-dense eastern PJM |
| **NEW JERSEY HUB** | New Jersey (PSEG/JCPL/AECO) | Trading | New Jersey load reference; typically among the priciest hubs |
| **DOMINION HUB** | Virginia / N. Carolina (DOM) | Trading | Southern PJM; the data-center load-growth region ⭐ *primary hub in this app* |
| **N ILLINOIS HUB** | Northern Illinois (ComEd) | Trading | ComEd/Chicago reference; PJM's western island, often lowest-priced |
| **CHICAGO HUB** | ComEd metro | Trading | Chicago-area load reference within the ComEd zone |
| **OHIO HUB** | Ohio | Trading | Ohio aggregate (ATSI/AEP/DAY/DEOK area) |
| **WEST INT HUB** | Western interface | Interface | Prices the western boundary/interface with neighboring areas |
| **AEP GEN HUB** | AEP zone generators | Generation | Generator-weighted basket for valuing AEP-zone output |
| **ATSI GEN HUB** | ATSI zone (N. Ohio) generators | Generation | Generator-weighted basket for the ATSI (FirstEnergy) footprint |
| **CHICAGO GEN HUB** | ComEd generators | Generation | Generator-weighted basket for ComEd-zone output |
        """
    )
    st.info(
        "**Trading vs. generation hubs:** a *trading hub* averages nodes across a "
        "region to give a location-neutral price to trade against (Western Hub is "
        "the classic example). A *generation hub* weights toward the buses where "
        "generators actually inject, so it better reflects what a plant in that "
        "zone realizes for its output.", icon="💡")

    st.markdown("#### Hub-by-hub")
    hub_writeups = [
        ("WESTERN HUB", "Western / Allegheny PJM", """
**The** PJM benchmark. Western Hub is the deepest, most liquid pricing point in
the entire Eastern Interconnection — when someone quotes "PJM power," they almost
always mean Western Hub. It's the settlement reference for exchange-traded PJM
futures (ICE, NYMEX) and the bulk of OTC forward trading. Because it aggregates a
large, well-connected western footprint, its price is stable and rarely dominated
by a single local constraint, which is exactly what makes it a good trading index.
"""),
        ("AEP-DAYTON HUB", "Western Ohio — AEP & Dayton zones", """
Usually the **second most actively traded** PJM hub, and the main *western-side*
benchmark. Covers the AEP and Dayton Power & Light zones. Traders use the
**AD Hub vs. Western Hub** spread as a core west-of-PJM basis relationship. Because
it sits in a generation-heavy, export-oriented part of the system, it often prices
at a modest discount to the eastern hubs.
"""),
        ("EASTERN HUB", "Eastern PJM (PECO / PSEG corridor)", """
The reference for the **load-dense, higher-priced eastern** side of PJM — the
Philadelphia–New Jersey corridor. Eastern Hub typically clears above Western Hub
because more load, tighter transmission, and less local generation push congestion
and losses up. Less liquid than Western Hub but a common east-side benchmark.
"""),
        ("NEW JERSEY HUB", "New Jersey — PSEG, JCPL, AECO, RECO", """
A New Jersey load-zone reference. Frequently **one of the most expensive hubs** in
PJM: New Jersey is import-dependent with binding transmission into the state, so
congestion adders show up here first during tight conditions. Useful for anyone
settling NJ retail/wholesale load.
"""),
        ("DOMINION HUB", "Virginia / North Carolina — Dominion zone", """
The southern-PJM hub covering the Dominion (DOM) zone, integrated into PJM in 2005.
This is the **fastest-changing region in PJM**: Northern Virginia's "Data Center
Alley" (Loudoun County) is driving explosive load growth, reshaping capacity and
congestion patterns. ⭐ **The primary hub this app is built around** — most default
views and the price forecast center on Dominion.
"""),
        ("N ILLINOIS HUB", "Northern Illinois — ComEd zone", """
The ComEd (Chicago / northern Illinois) reference. ComEd is PJM's **western
"island,"** connected to the rest of PJM through limited transmission and sitting
next to MISO. It often prices **below** the eastern hubs (lots of nuclear, less
congestion) and can decouple from the rest of PJM when the AEP–ComEd interface
binds. Also written "NI Hub."
"""),
        ("CHICAGO HUB", "ComEd metro (Chicago)", """
A Chicago-metro pricing reference inside the ComEd footprint. Narrower and more
load-focused than the broader N Illinois Hub; useful for pricing power specifically
in the Chicago load pocket.
"""),
        ("OHIO HUB", "Ohio (ATSI / AEP / DAY / DEOK area)", """
An Ohio aggregate reference spanning the northern-to-central Ohio zones. Sits
between the AEP-Dayton and ATSI footprints and is handy as a broad Ohio-region
index rather than a single-zone price.
"""),
        ("WEST INT HUB", "Western interface", """
The **Western Interface Hub** prices the western boundary of PJM where it meets
neighboring systems. More of an *interface/seam* reference than a load-trading hub —
useful for looking at how PJM's western edge prices relative to its interconnections.
"""),
        ("AEP GEN HUB", "AEP zone — generator-weighted", """
A **generation hub**: instead of averaging load nodes, it weights toward the buses
where AEP-zone generators inject power. That makes it a better proxy for **what a
plant in the AEP zone actually earns** for its output than a load-weighted trading
hub would be.
"""),
        ("ATSI GEN HUB", "ATSI zone (northern Ohio, FirstEnergy) — generator-weighted", """
A **generation hub** for the ATSI (American Transmission Systems Inc. / FirstEnergy)
footprint in northern Ohio. Generator-weighted, so it reflects the realized price
for generation sited in that zone.
"""),
        ("CHICAGO GEN HUB", "ComEd zone — generator-weighted", """
A **generation hub** for the ComEd zone — generator-weighted counterpart to the
load-oriented Chicago / N Illinois hubs, used to value ComEd-zone generation output.
"""),
    ]
    for name, region, body in hub_writeups:
        star = " ⭐" if name == "DOMINION HUB" else ""
        with st.expander(f"{name}{star} — {region}"):
            st.markdown(body)

    st.divider()
    st.markdown("#### Overlap between hubs")
    st.markdown(
        "The 12 hubs are **not mutually exclusive** — several are built from the "
        "same geography, so they overlap. They fall into a few regional clusters:"
    )
    st.markdown(
        """
| Cluster | Hubs that overlap here |
|---|---|
| **AEP / Western Ohio** | AEP-DAYTON HUB · AEP GEN HUB · OHIO HUB · ATSI GEN HUB |
| **ComEd / Chicago** | N ILLINOIS HUB · CHICAGO HUB · CHICAGO GEN HUB |
| **Western Hub benchmark** | WESTERN HUB · WEST INT HUB *(broad western aggregate)* |
| **Eastern** | EASTERN HUB · NEW JERSEY HUB |
| **Southern** | DOMINION HUB |
        """
    )
    st.markdown("**Why the overlap exists:**")
    st.markdown(
        """
- **A hub is a financial construct, not a territory.** Each hub is just a
  *basket of pricing nodes* that PJM averages into one reference price. Nothing
  requires the baskets to partition the map — they're allowed to re-use the same
  nodes, so they overlap freely.
- **The same region gets priced several ways for different purposes.** Over the
  AEP/Ohio footprint you'll find a *load-weighted* trading hub (AEP-DAYTON), a
  *generation-weighted* hub (AEP GEN), and a *broad regional* aggregate (OHIO) —
  same geography, three different questions ("what does load pay?", "what does a
  plant earn?", "what's the region doing?").
- **Broad trading hubs deliberately swallow narrow ones.** WESTERN HUB is kept
  intentionally wide and location-neutral so it stays liquid and hard to
  manipulate — which means it necessarily spans the same western area that
  narrower hubs (AEP-DAYTON) also sit in. Likewise CHICAGO HUB is a metro subset
  inside the wider N ILLINOIS/ComEd footprint.
- **The grid has no clean internal borders.** Pnodes are electrically
  interconnected; a "region" isn't a fenced area, so any regional reference price
  is drawn from nodes that other references also use.
- **Hubs were layered on over time.** As PJM integrated new zones (ComEd in 2004,
  Dominion in 2005) and trading needs evolved, new hubs were added *on top of*
  the existing set rather than redrawing everything — so newer and older hubs
  cover overlapping ground.

**Bottom line:** overlap is by design. Trading hubs exist to be liquid
benchmarks, generation hubs to value plant output, and regional/zonal hubs to
summarize an area — and those goals naturally point at the same nodes. On the
**Hub Prices** map, switch *Color hubs by → Region cluster* to see these groups.
        """
    )

# ---------------------------------------------------------------------------
with tabs[3]:
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

**Two ways to meet the obligation** — RPM is one *construct*, but a
load-serving entity (LSE) satisfies it via one of two paths:
        """
    )
    fr1, fr2 = st.columns(2)
    with fr1:
        st.markdown(
            "##### 1 · RPM auctions — *the default*\n"
            "The competitive, price-based path most of PJM uses.\n\n"
            "- Buy capacity in the **Base Residual Auction (BRA)** ~3 years "
            "ahead, plus **Incremental Auctions** to true up.\n"
            "- A **Variable Resource Requirement (VRR)** demand curve sets the "
            "**$/MW-day** price, per **LDA**.\n"
            "- **Applies to:** merchant generators & competitive LSEs, which pay "
            "the **Locational Reliability Charge** at their zone's price.")
    with fr2:
        st.markdown(
            "##### 2 · FRR — *the opt-out*\n"
            "Fixed Resource Requirement: self-supply instead of the auction.\n\n"
            "- File an **FRR Capacity Plan** covering the full **UCAP** "
            "obligation plus reserve margin for the zone.\n"
            "- All-or-nothing & multi-year — once elected, **all load in that "
            "zone** is served under FRR.\n"
            "- **Applies to:** vertically integrated / regulated utilities, "
            "cooperatives, and municipal utilities that own generation and want "
            "price certainty.")
    st.info(
        "In one line — **RPM:** market-price exposure, flexibility, "
        "competition. **FRR:** self-supply, price certainty, long-term "
        "commitment. See the *Capacity (RPM)* screen for cleared prices.",
        icon="💡")

# ---------------------------------------------------------------------------
with tabs[4]:
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
with tabs[5]:
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
with tabs[6]:
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
