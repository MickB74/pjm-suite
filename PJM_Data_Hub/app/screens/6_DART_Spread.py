"""Day-Ahead vs Real-Time spread ("DART") for PJM trading hubs.

The DART spread = RT LMP − DA LMP for the same delivery hour. It's the core
PJM basis-risk number: whether energy settled day-ahead or in real time was
the better deal, which hours systematically diverge, and the direct check on a
supplier's DA/RT split. Needs both markets in the local hub store (Update Hub
Prices pulls RT + DA together).
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

from pjm_core import paths, prices as PX
from pjm_core.settlement_points import HUBS, PRIMARY_HUB

st.title("⚖️ DA–RT Spread (DART)")
st.caption("Real-Time minus Day-Ahead LMP, hour by hour. Positive ⇒ real-time "
           "settled *higher* than day-ahead (DA buyers won); negative ⇒ RT was "
           "cheaper. LMP component selectable.")


@st.cache_data(show_spinner=True)
def load_both() -> pd.DataFrame:
    return PX.load_hub_prices(market=None)


df = load_both()
if df.empty or "market" not in df.columns or set(df["market"].unique()) < {"RT", "DA"}:
    _common.empty_state(
        st, "Need both RT and DA in the hub store.",
        hint="Run 'Update Hub Prices (all PJM hubs, RT + DA)' on the API Keys page.",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
hubs = sorted(df["pnode_name"].unique())
dmin, dmax = df["datetime_beginning_ept"].min().date(), df["datetime_beginning_ept"].max().date()
_common.data_status(st, path=paths.HUB_PRICES_PARQUET, rows=len(df), span=(dmin, dmax))

with st.container(border=True):
    st.header("Filters")
    sel_hubs = st.multiselect("Hubs", hubs,
                              default=[PRIMARY_HUB] if PRIMARY_HUB in hubs else hubs[:1])
    start, end = _common.period_picker(st, key="dart", min_year=dmin.year, default_mode="Month")
    component = st.selectbox("LMP component", ["total_lmp", "energy", "congestion", "loss"],
                             index=0, help=_common.LMP_COMPONENT_HELP)

if not sel_hubs:
    st.warning("Select at least one hub.")
    st.stop()

mask = (df["pnode_name"].isin(sel_hubs)
        & (df["datetime_beginning_ept"].dt.date >= start)
        & (df["datetime_beginning_ept"].dt.date <= end))
sub = df[mask]
if sub.empty:
    st.warning("No rows for that selection.")
    st.stop()

# Pivot RT and DA side by side on hub × hour, then compute the spread.
wide = (sub.pivot_table(index=["pnode_name", "datetime_beginning_ept"],
                        columns="market", values=component, aggfunc="mean")
        .reset_index())
if "RT" not in wide.columns or "DA" not in wide.columns:
    st.warning("This window is missing one of the markets.")
    st.stop()
wide = wide.dropna(subset=["RT", "DA"])
wide["spread"] = wide["RT"] - wide["DA"]

n_days = (end - start).days + 1
st.caption(f"**{start} → {end}** ({n_days} days) · {', '.join(sel_hubs)} · {component} · "
           f"{len(wide):,} matched hours")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Avg DA $/MWh", f"{wide['DA'].mean():,.2f}")
c2.metric("Avg RT $/MWh", f"{wide['RT'].mean():,.2f}")
c3.metric("Avg spread (RT−DA)", f"{wide['spread'].mean():+,.2f}")
c4.metric("Hours RT > DA", f"{(wide['spread'] > 0).mean()*100:.1f}%")
c5.metric("Spread std dev", f"{wide['spread'].std():,.2f}",
          help="Volatility of the basis — higher ⇒ more DA/RT settlement risk.")

# Daily average spread, one line per hub.
daily = (wide.assign(day=wide["datetime_beginning_ept"].dt.date)
         .groupby(["pnode_name", "day"], as_index=False)["spread"].mean())
fig = px.line(daily, x="day", y="spread", color="pnode_name",
              labels={"day": "Date", "spread": "RT − DA ($/MWh)", "pnode_name": "Hub"},
              title="Daily average DA–RT spread")
fig.add_hline(y=0, line_dash="dot", line_color="#888")
fig.update_layout(height=400, margin=dict(t=30))
st.plotly_chart(fig, use_container_width=True)

# Hour-of-day profile: which hours systematically diverge.
st.subheader("Average spread by hour of day")
wide["hour"] = wide["datetime_beginning_ept"].dt.hour
hod = wide.pivot_table(index="pnode_name", columns="hour", values="spread", aggfunc="mean")
if not hod.empty:
    vmax = float(hod.abs().to_numpy().max()) or 1.0
    fig2 = px.imshow(hod, labels={"x": "Hour (EPT)", "y": "Hub", "color": "RT−DA $/MWh"},
                     aspect="auto", color_continuous_scale="RdBu_r",
                     zmin=-vmax, zmax=vmax)
    fig2.update_xaxes(type="category")
    fig2.update_yaxes(type="category")
    fig2.update_layout(height=max(260, len(hod) * 40 + 120), margin=dict(t=20))
    st.plotly_chart(fig2, use_container_width=True)

# Biggest single-hour misses.
st.subheader("Largest spread hours")
worst = (wide.reindex(wide["spread"].abs().sort_values(ascending=False).index)
         .head(15)[["pnode_name", "datetime_beginning_ept", "DA", "RT", "spread"]]
         .rename(columns={"pnode_name": "hub", "datetime_beginning_ept": "hour (EPT)"}))
st.dataframe(
    worst.style.format({"DA": "${:,.2f}", "RT": "${:,.2f}", "spread": "${:+,.2f}"}),
    use_container_width=True, height=420)

st.download_button(
    "⬇ Download matched DA/RT hours (CSV)",
    wide[["pnode_name", "datetime_beginning_ept", "DA", "RT", "spread"]].to_csv(index=False).encode(),
    file_name=f"dart_spread_{component}_{start}_{end}.csv", mime="text/csv")
