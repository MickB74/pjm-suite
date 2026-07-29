"""PJM capacity market (RPM) — Base Residual Auction clearing prices.

PJM has no Data Miner API for capacity clearing prices, so this screen reads a
user-maintained reference table (data/capacity/rpm_bra_clearing_prices.csv),
seeded with published BRA results. Shows the clearing-price history by LDA and
a capacity-cost calculator for a given MW obligation.
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _common  # noqa: F401

import pandas as pd
import plotly.express as px
import streamlit as st

from pjm_core import paths, capacity, prices as PX

st.title("🏛️ PJM Capacity Market (RPM)")
st.caption("Reliability Pricing Model Base Residual Auction clearing prices "
           "($/MW-day) by delivery year & Locational Deliverability Area (LDA).")

st.info(
    "PJM publishes no capacity-price API — this is a **reference table** seeded "
    "with published BRA results. Verify & extend it against PJM's posted "
    "[RPM auction results](https://www.pjm.com/markets-and-operations/rpm). "
    f"Edit `{paths.CAPACITY_CSV}` to update as new auctions clear.",
    icon="ℹ️")

with st.expander("How PJM meets its capacity obligation — the two paths"):
    st.markdown(
        "PJM runs **one** capacity construct — the **Reliability Pricing Model "
        "(RPM)** — but a load-serving entity (LSE) can meet its obligation two "
        "ways:")
    p1, p2 = st.columns(2)
    with p1:
        st.markdown(
            "#### 1 · RPM auctions — *the default*\n"
            "The competitive, price-based path most of PJM uses.\n\n"
            "- **How:** PJM buys capacity in the **Base Residual Auction (BRA)**, "
            "~3 years ahead of the Delivery Year, plus **Incremental Auctions** "
            "to true up closer to real time.\n"
            "- **Pricing:** A downward-sloping **Variable Resource Requirement "
            "(VRR)** demand curve crosses supply to set a price in **$/MW-day**, "
            "priced separately by **LDA** so constrained zones clear higher.\n"
            "- **Who sells:** Generators, demand response, energy efficiency, "
            "storage.\n"
            "- **Applies to:** Merchant generators & competitive LSEs, which pay "
            "the **Locational Reliability Charge** at their zone's price.")
    with p2:
        st.markdown(
            "#### 2 · FRR — *the opt-out*\n"
            "Fixed Resource Requirement: self-supply instead of the auction.\n\n"
            "- **How:** The entity files an **FRR Capacity Plan** committing "
            "enough resources to cover its full **UCAP** obligation plus reserve "
            "margin for its zone.\n"
            "- **Commitment:** All-or-nothing and multi-year — once elected, "
            "**all load in that zone** is served under FRR (no dipping in and "
            "out of the auction to arbitrage price).\n"
            "- **Applies to:** Vertically integrated / regulated utilities, "
            "cooperatives, and municipal utilities that own generation and "
            "prefer price certainty over auction exposure.")
    st.caption(
        "In one line — **RPM:** market-price exposure, flexibility, competition. "
        "**FRR:** self-supply, price certainty, long-term commitment.")

df = capacity.load()
if df.empty:
    st.warning("No capacity reference data.")
    st.stop()

ldas = sorted(df["lda"].unique())
years_sorted = sorted(df["delivery_year"].unique())
with st.container(border=True):
    sel_ldas = st.multiselect(
        "LDAs", ldas,
        default=[l for l in ("RTO", "DOM", "BGE", "EMAAC", "MAAC") if l in ldas] or ldas)

sel_ldas = sel_ldas or ldas

# Build a complete grid so every selected LDA has a value for every year.
# When an LDA didn't separate, its price equals the RTO price for that year.
rto = df[df["lda"] == "RTO"].set_index("delivery_year")["clearing_price_mw_day"]
rows = []
for yr in years_sorted:
    for lda_name in sel_ldas:
        hit = df[(df["delivery_year"] == yr) & (df["lda"] == lda_name)]
        price = float(hit["clearing_price_mw_day"].iloc[0]) if not hit.empty else rto.get(yr)
        if price is not None:
            rows.append({"delivery_year": yr, "lda": lda_name, "clearing_price_mw_day": price})
sub = pd.DataFrame(rows)

# Clearing-price history.
fig = px.bar(sub, x="delivery_year", y="clearing_price_mw_day", color="lda",
             barmode="group",
             category_orders={"delivery_year": years_sorted},
             labels={"delivery_year": "Delivery year", "clearing_price_mw_day": "$/MW-day", "lda": "LDA"},
             title="RPM BRA clearing price by delivery year")
fig.update_layout(height=420, margin=dict(t=40))
st.plotly_chart(fig, width="stretch")

with st.expander("Why all LDAs clear at the same price now — the FERC price cap"):
    st.markdown(
        "From **2007/2008 through 2025/2026**, constrained LDAs like BGE, DOM, and "
        "EMAAC regularly cleared **above** the RTO price — sometimes dramatically "
        "(BGE hit $466/MW-day in 2025/2026). This reflected local supply shortages "
        "behind transmission-constrained boundaries.\n\n"
        "Starting with the **2026/2027 delivery year**, FERC approved a **price cap "
        "and floor** (a \"collar\") on the BRA. The cap has flattened all regional "
        "price differences — no zone can clear above it, so constrained zones that "
        "used to separate no longer do:\n\n"
        "| Delivery Year | Price Cap | Result |\n"
        "|---|---|---|\n"
        "| 2026/2027 | $329.17/MW-day | All LDAs uniform |\n"
        "| 2027/2028 | $333.44/MW-day | All LDAs uniform |\n"
        "| 2028/2029 | $325.00/MW-day | All LDAs uniform |\n\n"
        "**What prices would have been without the cap:** PJM's own simulations show "
        "the 2027/2028 auction would have cleared at ~$542/MW-day for Dominion, and "
        "the 2028/2029 auction at ~$777/MW-day for ComEd — more than double the "
        "capped price.\n\n"
        "**Why the cap exists:** Capacity prices spiked 9x between the 2024/2025 and "
        "2025/2026 auctions (from $29 to $270/MW-day RTO-wide) due to generator "
        "retirements, rising load forecasts, and new accreditation rules. The collar "
        "protects consumers from runaway prices while still signaling scarcity.\n\n"
        "**What's next:** PJM expects to return to the normal 3-year-ahead auction "
        "schedule by the **2030/2031 delivery year** (BRA in May 2027). The cap/floor "
        "structure may evolve as FERC reviews market design.")

# Latest-year snapshot.
latest_year = df["delivery_year"].max()
latest = df[df["delivery_year"] == latest_year]
st.subheader(f"Latest delivery year: {latest_year}")
cols = st.columns(min(4, len(latest)) or 1)
for col, (_, row) in zip(cols, latest.iterrows()):
    col.metric(f"{row['lda']} $/MW-day", f"${row['clearing_price_mw_day']:,.2f}",
               help=f"≈ ${row['clearing_price_mw_day']*365:,.0f} /MW-year")

# --- Capacity-cost calculator ------------------------------------------------
st.divider()
st.subheader("Capacity cost calculator")
st.caption("Enter your obligation once, then see the annual capacity charge for "
           "that MW across every delivery year — and compare specific years "
           "head-to-head. Capacity Obligation ≈ your coincident peak × zonal "
           "scaling factors; use your actual UCAP obligation for precision.")

cc1, cc2 = st.columns([1, 1])
mw = cc1.number_input("Obligation (MW)", min_value=0.0, value=100.0, step=10.0)
lda = cc2.selectbox("LDA", ldas, index=ldas.index("RTO") if "RTO" in ldas else 0)

# Cost series for this MW & LDA across every delivery year. Where the chosen LDA
# didn't separate in a given year, price_for() falls back to the RTO price.
cost_rows = []
for yr in years_sorted:
    p = capacity.price_for(yr, lda)
    if p is not None:
        cost_rows.append({
            "delivery_year": yr,
            "clearing_price_mw_day": p,
            "daily_cost": p * mw,
            "annual_cost": p * mw * 365.0,
        })
cost_df = pd.DataFrame(cost_rows)

if cost_df.empty:
    st.warning("No clearing prices available for that LDA.")
else:
    # Headline metrics for this MW/LDA profile: latest, cheapest, priciest year.
    latest_row = cost_df.iloc[-1]
    cheap_row = cost_df.loc[cost_df["annual_cost"].idxmin()]
    peak_row = cost_df.loc[cost_df["annual_cost"].idxmax()]
    k1, k2, k3 = st.columns(3)
    k1.metric(f"Latest · {latest_row['delivery_year']}",
              f"${latest_row['annual_cost']:,.0f}/yr",
              help=f"${latest_row['clearing_price_mw_day']:,.2f}/MW-day")
    k2.metric(f"Cheapest · {cheap_row['delivery_year']}",
              f"${cheap_row['annual_cost']:,.0f}/yr",
              help=f"${cheap_row['clearing_price_mw_day']:,.2f}/MW-day")
    k3.metric(f"Most expensive · {peak_row['delivery_year']}",
              f"${peak_row['annual_cost']:,.0f}/yr",
              help=f"${peak_row['clearing_price_mw_day']:,.2f}/MW-day")

    # Annual bill over time for the chosen obligation & LDA.
    fig_cost = px.bar(
        cost_df, x="delivery_year", y="annual_cost",
        category_orders={"delivery_year": years_sorted},
        labels={"delivery_year": "Delivery year",
                "annual_cost": "Annual capacity cost ($)"},
        title=f"Annual capacity bill for {mw:,.0f} MW in {lda} — by delivery year")
    fig_cost.update_traces(hovertemplate="%{x}<br>$%{y:,.0f}/yr<extra></extra>")
    fig_cost.update_layout(height=380, margin=dict(t=40), yaxis_tickprefix="$")
    st.plotly_chart(fig_cost, width="stretch")

    # Head-to-head comparison of specific delivery years.
    st.markdown("**Compare specific delivery years**")
    cmp_years = st.multiselect(
        "Delivery years to compare", years_sorted,
        default=[years_sorted[0], years_sorted[-1]], key="cap_cmp_years")
    if cmp_years:
        # cost_df is already in chronological order; isin preserves it, so the
        # first selected row is the earliest — our comparison baseline.
        cmp_df = cost_df[cost_df["delivery_year"].isin(cmp_years)]
        base = cmp_df.iloc[0]

        # Compact metric cards. st.metric squeezes badly past ~6 columns, so we
        # wrap into rows of PER_ROW and shrink the value/label/delta fonts so
        # dollar figures stay legible even when every delivery year is selected.
        PER_ROW = 6
        st.markdown(
            """<style>
            [data-testid="stMetricValue"] { font-size: 0.95rem; }
            [data-testid="stMetricLabel"] p { font-size: 0.72rem; }
            [data-testid="stMetricDelta"] { font-size: 0.68rem; }
            [data-testid="stMetricDelta"] svg { display: none; }
            </style>""",
            unsafe_allow_html=True)
        rows_of = [cmp_df.iloc[i:i + PER_ROW] for i in range(0, len(cmp_df), PER_ROW)]
        for chunk in rows_of:
            mcols = st.columns(PER_ROW)
            for col, (_, r) in zip(mcols, chunk.iterrows()):
                is_base = r["delivery_year"] == base["delivery_year"]
                delta = r["annual_cost"] - base["annual_cost"]
                pct = (delta / base["annual_cost"] * 100.0) if base["annual_cost"] else 0.0
                col.metric(
                    r["delivery_year"], f"${r['annual_cost']:,.0f}",
                    delta=None if is_base else f"{delta:+,.0f} ({pct:+.0f}%)",
                    delta_color="inverse",  # higher cost = worse
                    help=f"${r['clearing_price_mw_day']:,.2f}/MW-day · "
                         f"${r['daily_cost']:,.0f}/day · "
                         f"${r['annual_cost']:,.0f}/yr")
        st.caption(f"Δ shown vs baseline **{base['delivery_year']}** "
                   "(earliest selected year). Annual $ obligation shown per card.")

        # Chart the selected years head-to-head, ordered chronologically.
        fig_cmp = px.bar(
            cmp_df, x="delivery_year", y="annual_cost", color="delivery_year",
            category_orders={"delivery_year": list(cmp_df["delivery_year"])},
            labels={"delivery_year": "Delivery year",
                    "annual_cost": "Annual capacity cost ($)"},
            title=f"Annual capacity bill for {mw:,.0f} MW in {lda} — "
                  "selected delivery years")
        fig_cmp.update_traces(
            hovertemplate="%{x}<br>$%{y:,.0f}/yr<extra></extra>")
        fig_cmp.update_layout(
            height=380, margin=dict(t=40), yaxis_tickprefix="$",
            showlegend=False)
        st.plotly_chart(fig_cmp, width="stretch")

        cmp_show = cmp_df.rename(columns={
            "delivery_year": "Delivery year",
            "clearing_price_mw_day": "$/MW-day",
            "daily_cost": "Daily cost",
            "annual_cost": "Annual cost"})
        st.dataframe(
            cmp_show.style.format({
                "$/MW-day": "${:,.2f}", "Daily cost": "${:,.0f}",
                "Annual cost": "${:,.0f}"}),
            width="stretch", hide_index=True)

# --- All-in cost of power by year (2020–2026) --------------------------------
# Capacity is only one slice of the bill. Stack it with the *actual* energy
# (commodity) and ancillary costs from the local PJM price stores — which cover
# calendar years 2020–2026 — to show the all-in cost of holding a MW position.
st.divider()
st.subheader("All-in cost of power by year (2020–2026)")

HOURS_PER_YEAR = 8760.0
_ALLIN_YEARS = list(range(2020, 2027))


@st.cache_data(show_spinner=False)
def _hub_options() -> list[str]:
    try:
        p = PX.load_hub_prices(market="RT")
        return sorted(p["pnode_name"].dropna().unique()) if "pnode_name" in p.columns else []
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def _annual_energy_lmp(hub: str) -> dict:
    """Calendar-year mean total LMP ($/MWh) for one hub, from the price store."""
    try:
        p = PX.load_hub_prices(market="RT")
    except Exception:
        return {}
    if p.empty or "pnode_name" not in p.columns:
        return {}
    pc = "total_lmp" if "total_lmp" in p.columns else "lmp"
    p = p[p["pnode_name"] == hub].copy()
    if p.empty:
        return {}
    dt = pd.to_datetime(p["datetime_beginning_ept"])
    return (pd.to_numeric(p[pc], errors="coerce")
            .groupby(dt.dt.year).mean().to_dict())


@st.cache_data(show_spinner=False)
def _annual_ancillary_adder() -> dict:
    """Ancillary cost as a $/MWh-of-load adder = spend ÷ system load, by year.

    Spend ≈ Σ(clearing price × cleared MW) over services & hours; load = system
    MWh. The 2022 step-up is real — PJM's Oct-2022 reserve price-formation reform.
    """
    try:
        from datasets.ancillary import pjm_as
        from datasets.load import pjm_load
        a = pjm_as.load_store()
        L = pjm_load.load_store()
    except Exception:
        return {}
    if a.empty or L.empty:
        return {}
    mw_col = "as_mw" if "as_mw" in a.columns else "total_mw"
    spend = ((pd.to_numeric(a["mcp"], errors="coerce")
              * pd.to_numeric(a[mw_col], errors="coerce"))
             .groupby(pd.to_datetime(a["datetime_beginning_ept"]).dt.year).sum())
    load_mwh = (pd.to_numeric(L["mw"], errors="coerce")
                .groupby(pd.to_datetime(L["datetime_beginning_ept"]).dt.year).sum())
    return {int(y): float(spend[y] / load_mwh[y])
            for y in spend.index if y in load_mwh.index and load_mwh[y]}


def _cap_cost_for_year(y: int, mw_: float, lda_: str = "RTO") -> float | None:
    """Annual capacity $ during calendar year y, blending the two overlapping RPM
    delivery years (Jan–May = 151 days of DY (y-1)/y; Jun–Dec = 214 of y/(y+1))."""
    p_early = capacity.price_for(f"{y-1}/{y}", lda_)
    p_late = capacity.price_for(f"{y}/{y+1}", lda_)
    if p_early is not None and p_late is not None:
        return mw_ * (151.0 * p_early + 214.0 * p_late)
    if p_late is not None:
        return mw_ * 365.0 * p_late
    if p_early is not None:
        return mw_ * 365.0 * p_early
    return None


hub_opts = _hub_options()
if not hub_opts:
    st.info("Energy/ancillary price stores are empty — run **API Keys → Update "
            "Hub Prices** (and ancillary) to populate the all-in cost view.")
else:
    st.caption("Stacks the RPM **capacity** charge with the **actual energy "
               "(commodity)** and **ancillary** costs of a MW position. Energy & "
               "ancillary come from the local PJM stores (2020–2026); earlier RPM "
               "years aren't shown — no market energy data exists for them.")
    a1, a2, a3 = st.columns(3)
    allin_mw = a1.number_input("Position (MW)", min_value=0.0, value=float(mw),
                               step=10.0, key="allin_mw")
    lf = a2.slider("Load factor", 0.10, 1.00, 0.60, 0.05, key="allin_lf",
                   help="Share of the year the MW draws energy. MWh = MW × 8,760 "
                        "× load factor. 100 MW @ 60% ≈ 525,600 MWh/yr.")
    default_hub = "WESTERN HUB" if "WESTERN HUB" in hub_opts else hub_opts[0]
    hub = a3.selectbox("Energy hub (commodity)", hub_opts,
                       index=hub_opts.index(default_hub), key="allin_hub")

    energy = _annual_energy_lmp(hub)
    anc = _annual_ancillary_adder()
    mwh = allin_mw * HOURS_PER_YEAR * lf

    allin = pd.DataFrame([{
        "Year": str(y),
        "Capacity": _cap_cost_for_year(y, allin_mw) or 0.0,
        "Commodity (energy)": (energy.get(y) or 0.0) * mwh,
        "Ancillary": (anc.get(y) or 0.0) * mwh,
    } for y in _ALLIN_YEARS])
    allin["Total"] = allin[["Capacity", "Commodity (energy)", "Ancillary"]].sum(axis=1)

    comp_order = ["Commodity (energy)", "Capacity", "Ancillary"]
    long = allin.melt(id_vars="Year", value_vars=comp_order,
                      var_name="Component", value_name="Cost")
    fig_allin = px.bar(
        long, x="Year", y="Cost", color="Component", barmode="stack",
        category_orders={"Year": [str(y) for y in _ALLIN_YEARS],
                         "Component": comp_order},
        labels={"Cost": "Annual cost ($)"},
        title=f"All-in annual cost for {allin_mw:,.0f} MW @ {lf:.0%} load factor — {hub}")
    fig_allin.update_traces(
        hovertemplate="%{x} · %{fullData.name}<br>$%{y:,.0f}<extra></extra>")
    fig_allin.update_layout(height=430, margin=dict(t=40), yaxis_tickprefix="$",
                            legend_title_text="")
    st.plotly_chart(fig_allin, width="stretch")
    st.caption(f"Energy & ancillary applied to **{mwh:,.0f} MWh/yr** "
               f"(= {allin_mw:,.0f} MW × 8,760 h × {lf:.0%}). **2026 is "
               "year-to-date** (partial). Ancillary = PJM reserve/regulation spend "
               "÷ system load; the 2022 step-up reflects PJM's reserve "
               "price-formation reform.")

    show = allin.copy()
    show["All-in $/MWh"] = (allin["Total"] / mwh) if mwh else float("nan")
    st.dataframe(
        show.style.format({
            "Capacity": "${:,.0f}", "Commodity (energy)": "${:,.0f}",
            "Ancillary": "${:,.0f}", "Total": "${:,.0f}",
            "All-in $/MWh": "${:,.2f}"}),
        width="stretch", hide_index=True)

# Reference table + download.
st.subheader("Reference table")
st.dataframe(
    df[["delivery_year", "lda", "auction", "clearing_price_mw_day", "clearing_price_mw_year"]]
    .style.format({"clearing_price_mw_day": "${:,.2f}", "clearing_price_mw_year": "${:,.0f}"}),
    width="stretch", height=380)
st.download_button(
    "⬇ Download RPM clearing prices (CSV)",
    df.to_csv(index=False).encode(),
    file_name="pjm_rpm_bra_clearing_prices.csv", mime="text/csv")
