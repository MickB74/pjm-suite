"""PJM PLC & Capacity-Cost Calculator.

The 5CP & Weather screen shows the *system's* five coincident peaks. This screen
turns them into *your* dollars: enter your metered load (MW) during those five
hours and it computes your **Peak Load Contribution (PLC)** — the average of your
load across the 5CP — then multiplies by the RPM Base Residual Auction clearing
price to estimate your annual capacity charge.

PLC drives roughly a quarter of a commercial load's all-in power cost, and it is
set entirely by five hours. Shaving load during those hours (see the Peak
Predictor) is the single highest-leverage cost lever a load has.

Simplified model: PLC = mean(load at the 5 CP hours). Real PJM settlement layers
on a Forecast Pool Requirement / loss-and-reserve scaling factor (~1.05–1.1);
enter it below if you want the grossed-up obligation.
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _common  # noqa: F401

import pandas as pd
import streamlit as st

from pjm_core import peak, capacity

st.title("🧮 PLC & Capacity-Cost Calculator")
st.caption("Enter your load during the five coincident peaks → your Peak Load "
           "Contribution → your annual PJM capacity charge at the RPM clearing "
           "price.")

_common.rate_explainer(st)


@st.cache_data(show_spinner=True)
def _load():
    return peak.rto_hourly_load()


load = _load()
if load.empty:
    _common.empty_state(
        st, "No RTO load data yet — needed to show the 5CP hours.",
        hint="Run 'Update System Load' on the API Keys page.",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

# --- Pick the summer whose 5CP to bill against ------------------------------
summers = sorted({d.year for d in load["datetime_beginning_ept"].dt.date
                  if peak.SUMMER_START <= (d.month, d.day) <= peak.SUMMER_END},
                 reverse=True)
summers = [y for y in summers if not peak.daily_peaks(load, y).empty]
if not summers:
    st.warning("No summer (Jun–Sep) load data in the store yet.")
    st.stop()

c1, c2 = st.columns([1, 2])
year = c1.selectbox("Summer (defines the 5CP hours)", summers, index=0)
cp = peak.five_cp(load, year)
partial = load["datetime_beginning_ept"].max().date() < \
    pd.Timestamp(year, peak.SUMMER_END[0], peak.SUMMER_END[1]).date()
if partial:
    c2.info(f"⚠ {year} summer is still in progress — these 5CP hours are "
            "provisional and may shift as hotter days settle.")

# --- Enter your load at each CP hour ----------------------------------------
st.subheader("Your metered load at each coincident peak")
st.info(
    "**What to do:** in the table below, type your facility's metered load "
    "(MW) into the **rightmost \"Your load (MW)\" column** — one value for each "
    "of the five peak hours. That's the *only* column you edit; Rank, 5CP hour "
    "and RTO peak are PJM's system numbers and are read-only.\n\n"
    "Fill **all five rows** for a settlement-grade PLC. Leave a row blank only "
    "if you don't know that hour's load — it's dropped from the average. Your "
    "PLC and estimated capacity cost appear below once at least one row is "
    "filled.")

editor_rows = []
for _, r in cp.iterrows():
    editor_rows.append({
        "Rank": int(r["rank"]),
        "5CP hour (EPT)": pd.to_datetime(r["peak_hour"]).strftime("%a %b %d, %Y %H:00"),
        "RTO peak (MW)": float(r["peak_mw"]),
        "👉 Your load (MW)": None,
    })
edf = pd.DataFrame(editor_rows)
edited = st.data_editor(
    edf, hide_index=True, width="stretch",
    disabled=["Rank", "5CP hour (EPT)", "RTO peak (MW)"],
    column_config={
        "RTO peak (MW)": st.column_config.NumberColumn(format="%.0f"),
        "👉 Your load (MW)": st.column_config.NumberColumn(
            help="ENTER HERE — your facility's metered MW during this hour.",
            min_value=0.0, format="%.3f"),
    },
    key="cp_editor")

st.caption("These five hours are simply the summer's five highest RTO load "
           "hours — any day of the week. They almost always land on hot weekday "
           "afternoons (weekend/holiday load runs lower), but a very hot weekend "
           "can appear.")

your_vals = pd.to_numeric(edited["👉 Your load (MW)"], errors="coerce").dropna()

# --- Settlement + capacity-price inputs -------------------------------------
st.subheader("Capacity price & scaling")
p1, p2, p3 = st.columns(3)
cap = capacity.load()
delivery_years = sorted(cap["delivery_year"].unique(), reverse=True)
ldas = sorted(cap["lda"].unique())
dy = p1.selectbox("Delivery year", delivery_years,
                  index=0 if delivery_years else None,
                  help="Capacity is billed per Delivery Year (Jun 1 – May 31).")
lda = p2.selectbox("LDA (zone)", ldas, index=ldas.index("RTO") if "RTO" in ldas else 0,
                   help="Locational Deliverability Area — use your zone if it "
                        "separated from the RTO price (e.g. DOM).")
fpr = p3.number_input("FPR / scaling factor", min_value=1.0, max_value=1.30,
                      value=1.0, step=0.01,
                      help="Forecast Pool Requirement gross-up (loss + reserve "
                           "margin). PJM's is ~1.05–1.10; 1.0 = raw 5CP average.")

price = capacity.price_for(dy, lda)

# --- Results ----------------------------------------------------------------
st.divider()
if your_vals.empty:
    st.info("Enter your load for at least one coincident-peak hour above to see "
            "your PLC and capacity cost.")
    st.stop()

plc_raw = float(your_vals.mean())
plc = plc_raw * fpr
n_used = len(your_vals)

m = st.columns(3)
m[0].metric("Your PLC", f"{plc:,.3f} MW",
            help=f"Mean of your load across {n_used} of 5 coincident peaks"
                 + (f", grossed up ×{fpr:.2f}." if fpr != 1.0 else "."))
share = plc_raw / cp["peak_mw"].mean() if cp["peak_mw"].mean() else float("nan")
m[1].metric("Share of RTO 5CP", f"{share*100:.4f}%",
            help="Your PLC as a fraction of the average RTO coincident peak.")
if price is not None:
    annual = price * plc * 365.0
    m[2].metric("Est. annual capacity cost", f"${annual:,.0f}",
                help=f"PLC × {price:,.2f} $/MW-day × 365 days.")

if price is None:
    st.warning(f"No RPM clearing price on file for {dy} / {lda}. Add it on the "
               "**Capacity (RPM)** screen to compute the cost.")
    st.stop()

if n_used < peak.N_CP:
    st.caption(f"⚠ Only {n_used} of 5 CP hours entered — PLC is the average of "
               "what you provided. Fill all five for a settlement-grade figure.")

# Cost breakdown
st.subheader("Cost breakdown")
monthly = annual / 12.0
daily = price * plc
bd = pd.DataFrame({
    "Item": ["RPM clearing price", "Your PLC (billed MW)", "Daily capacity charge",
             "Monthly (avg)", "Annual capacity charge"],
    "Value": [f"{price:,.2f} $/MW-day", f"{plc:,.3f} MW", f"${daily:,.0f}/day",
              f"${monthly:,.0f}/mo", f"${annual:,.0f}/yr"],
})
st.dataframe(bd, hide_index=True, width="stretch")

# --- What curtailment is worth ----------------------------------------------
st.subheader("What shaving load at the peaks is worth")
st.caption("If you could cut your load by this much at *each* of the five "
           "coincident peaks, here's the capacity cost you'd avoid next year.")
shave = st.slider("Load reduction per CP hour (MW)", 0.0,
                  float(max(plc_raw, 1.0)), min(1.0, float(plc_raw)), step=0.1)
saved = price * (shave * fpr) * 365.0
st.metric("Annual capacity savings", f"${saved:,.0f}",
          help="Reducing your average 5CP load by this much lowers your PLC and "
               "therefore your capacity bill for the delivery year.")

st.download_button(
    "⬇ Download PLC worksheet (CSV)",
    edited.to_csv(index=False).encode(),
    file_name=f"pjm_plc_{year}.csv", mime="text/csv")

st.caption("PLC method: average of your metered load across PJM's five RTO "
           "coincident peaks for the summer, optionally scaled by the Forecast "
           "Pool Requirement. Capacity cost = PLC × RPM clearing price × 365. "
           "Verify the clearing price and FPR against your PJM invoice / LSE.")
