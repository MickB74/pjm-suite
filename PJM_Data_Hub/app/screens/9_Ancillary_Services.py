"""PJM ancillary services — reserve & regulation market clearing prices.

The Oct-2022 reserve-market reform put every reserve product and regulation on
one hourly clearing engine. This screen reads the local ancillary store and
shows: MCP time series by product, the regulation capability/performance split,
and a $/MWh cost view alongside energy so you can see when reserves and
regulation actually move the total cost of power.
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

from pjm_core import paths
from datasets.ancillary import pjm_as

st.title("🛡️ PJM Ancillary Services")
st.caption("Reserve & regulation market clearing prices (MCP, $/MWh) from PJM's "
           "unified reserve market. REG = regulation; SR/PR/30MIN/SEC/NSR are "
           "reserve products.")


@st.cache_data(show_spinner=True)
def load_all() -> pd.DataFrame:
    return pjm_as.load_store()


df = load_all()
if df.empty:
    _common.empty_state(
        st, "No ancillary-services data yet.",
        hint="Run 'Update Ancillary Services' on the API Keys page.",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
dmin, dmax = df["datetime_beginning_ept"].min().date(), df["datetime_beginning_ept"].max().date()
_common.data_status(st, path=paths.ANCILLARY_PARQUET, rows=len(df), span=(dmin, dmax))

locales = sorted(df["locale"].dropna().unique())
services = sorted(df["service"].dropna().unique())

with st.container(border=True):
    st.header("Filters")
    c1, c2 = st.columns(2)
    locale = c1.selectbox(
        "Reserve zone (locale)", locales,
        index=locales.index("PJM_RTO") if "PJM_RTO" in locales else 0,
        help="PJM_RTO is the system-wide market; others are constrained sub-zones.")
    sel_services = c2.multiselect(
        "Products", services,
        default=services,
        format_func=lambda s: f"{s} — {pjm_as.SERVICE_LABELS.get(s, s)}")
    start, end = _common.period_picker(st, key="as", min_year=dmin.year, default_mode="Month")

if not sel_services:
    st.warning("Select at least one product.")
    st.stop()

mask = ((df["locale"] == locale)
        & df["service"].isin(sel_services)
        & (df["datetime_beginning_ept"].dt.date >= start)
        & (df["datetime_beginning_ept"].dt.date <= end))
sub = df[mask].copy()
if sub.empty:
    st.warning("No rows for that selection.")
    st.stop()

sub["product"] = sub["service"].map(lambda s: f"{s} — {pjm_as.SERVICE_LABELS.get(s, s)}")
n_days = (end - start).days + 1
st.caption(f"**{start} → {end}** ({n_days} days) · {locale} · {len(sub):,} rows")

# --- KPI row: average MCP per product ---------------------------------------
avg = (sub.groupby("service")["mcp"].mean().reindex(sel_services).dropna())
if not avg.empty:
    cols = st.columns(len(avg))
    for col, (svc, val) in zip(cols, avg.items()):
        col.metric(f"{svc} avg MCP", f"${val:,.2f}",
                   help=pjm_as.SERVICE_LABELS.get(svc, svc) + " · $/MWh")
    _zero_products = [s for s in avg.index if avg[s] < 0.01]
    if _zero_products:
        _zero_list = ", ".join(_zero_products)
        st.caption(
            f"**{_zero_list}** averages ≈ $0 because these reserves rarely bind — "
            "the supply curve clears at zero in normal conditions. They only price "
            "during scarcity events (e.g. Winter Storm Elliott, summer peaks), when "
            "they can spike to the $850/MWh offer cap.")

# --- Daily average MCP by product -------------------------------------------
daily = (sub.assign(day=sub["datetime_beginning_ept"].dt.date)
         .groupby(["product", "day"], as_index=False)["mcp"].mean())
fig = px.line(daily, x="day", y="mcp", color="product",
              labels={"day": "Date", "mcp": "MCP ($/MWh)", "product": "Product"},
              title="Daily average market clearing price")
fig.update_layout(height=420, margin=dict(t=30))
st.plotly_chart(fig, use_container_width=True)

# --- Regulation capability vs performance split -----------------------------
reg = sub[sub["service"] == "REG"]
if not reg.empty and reg[["reg_ccp", "reg_pcp"]].notna().any().any():
    st.subheader("Regulation: capability vs performance")
    st.caption("PJM regulation MCP splits into a capability component (RegCCP) "
               "and a performance component (RegPCP).")
    regd = (reg.assign(day=reg["datetime_beginning_ept"].dt.date)
            .groupby("day", as_index=False)[["reg_ccp", "reg_pcp", "mcp"]].mean()
            .melt(id_vars="day", var_name="component", value_name="price"))
    label = {"reg_ccp": "Capability (RegCCP)", "reg_pcp": "Performance (RegPCP)",
             "mcp": "Total REG MCP"}
    regd["component"] = regd["component"].map(label)
    fig2 = px.area(regd[regd["component"] != "Total REG MCP"],
                   x="day", y="price", color="component",
                   labels={"day": "Date", "price": "$/MWh", "component": ""},
                   title="Regulation price components (daily avg)")
    fig2.update_layout(height=340, margin=dict(t=30))
    st.plotly_chart(fig2, use_container_width=True)

# --- Hour-of-day profile ----------------------------------------------------
st.subheader("Average MCP by hour of day")
sub["hour"] = sub["datetime_beginning_ept"].dt.hour
hod = sub.pivot_table(index="service", columns="hour", values="mcp", aggfunc="mean")
if not hod.empty:
    fig3 = px.imshow(hod, labels={"x": "Hour (EPT)", "y": "Product", "color": "MCP $/MWh"},
                     aspect="auto", color_continuous_scale="Viridis")
    fig3.update_xaxes(type="category")
    fig3.update_yaxes(type="category")
    fig3.update_layout(height=max(240, len(hod) * 42 + 120), margin=dict(t=20))
    st.plotly_chart(fig3, use_container_width=True)

# --- Summary table + download -----------------------------------------------
st.subheader("Product summary")
summ = (sub.groupby("service")
        .agg(avg_mcp=("mcp", "mean"), max_mcp=("mcp", "max"),
             p95_mcp=("mcp", lambda s: s.quantile(0.95)), hours=("mcp", "size"))
        .reindex(sel_services).dropna(how="all").reset_index())
summ["product"] = summ["service"].map(lambda s: pjm_as.SERVICE_LABELS.get(s, s))
summ = summ[["service", "product", "avg_mcp", "p95_mcp", "max_mcp", "hours"]].rename(
    columns={"service": "Product", "product": "Description", "avg_mcp": "Avg MCP",
             "p95_mcp": "P95 MCP", "max_mcp": "Max MCP", "hours": "Hours"})
st.dataframe(
    summ.style.format({"Avg MCP": "${:,.2f}", "P95 MCP": "${:,.2f}",
                       "Max MCP": "${:,.2f}", "Hours": "{:,.0f}"}),
    use_container_width=True, hide_index=True)

st.download_button(
    "⬇ Download filtered ancillary rows (CSV)",
    sub[[c for c in pjm_as.KEEP_COLS if c in sub.columns]].to_csv(index=False).encode(),
    file_name=f"pjm_ancillary_{locale}_{start}_{end}.csv", mime="text/csv")
