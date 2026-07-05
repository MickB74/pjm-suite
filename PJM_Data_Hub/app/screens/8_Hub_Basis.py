"""Hub basis matrix — each PJM hub priced against a reference hub.

Basis = hub LMP − reference-hub LMP for the same hour. In PJM this is driven
almost entirely by the congestion (and loss) components, so it's the number
that decides where a CfD/basis-swap should settle and how much locational risk
a hub carries vs DOM (or whatever reference you pick).
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

st.title("🧭 Hub Basis")
st.caption("Each hub's LMP minus a reference hub, hour by hour. Positive ⇒ that "
           "hub prices *above* the reference (congestion into it). Basis is the "
           "locational spread that drives CfD/basis-swap settlement.")


@st.cache_data(show_spinner=True)
def load_market(market: str) -> pd.DataFrame:
    df = PX.load_hub_prices(market=market)
    if not df.empty:
        df["datetime_beginning_ept"] = pd.to_datetime(df["datetime_beginning_ept"])
    return df


with st.container(border=True):
    st.header("Filters")
    market = st.radio("Market", ["RT", "DA"], horizontal=True)
    df = load_market(market)
    if df.empty:
        _common.empty_state(
            st, f"No {market} hub prices yet.",
            hint="Run 'Update Hub Prices' on the API Keys page.",
            page="screens/0_API_Keys.py", page_label="Go to API Keys")
    hubs = sorted(df["pnode_name"].unique())
    dmin, dmax = df["datetime_beginning_ept"].min().date(), df["datetime_beginning_ept"].max().date()
    ref = st.selectbox("Reference hub", hubs,
                       index=hubs.index(PRIMARY_HUB) if PRIMARY_HUB in hubs else 0)
    start, end = _common.period_picker(st, key="basis", min_year=dmin.year, default_mode="Month")
    component = st.selectbox("LMP component", ["total_lmp", "congestion", "loss", "energy"],
                             index=0,
                             help="total_lmp is the settled basis; congestion isolates "
                                  "the locational driver.\n\n" + _common.LMP_COMPONENT_HELP)

_common.data_status(st, path=paths.HUB_PRICES_PARQUET, rows=len(df), span=(dmin, dmax))

mask = (df["datetime_beginning_ept"].dt.date >= start) & (df["datetime_beginning_ept"].dt.date <= end)
sub = df[mask]
if sub.empty:
    st.warning("No rows for that window.")
    st.stop()

wide = (sub.pivot_table(index="datetime_beginning_ept", columns="pnode_name",
                        values=component, aggfunc="mean"))
if ref not in wide.columns:
    st.warning("Reference hub has no data in this window.")
    st.stop()

basis = wide.sub(wide[ref], axis=0).drop(columns=[ref]).dropna(how="all")
n_days = (end - start).days + 1
st.caption(f"**{start} → {end}** ({n_days} days) · {market} · {component} · "
           f"basis vs **{ref}** · {len(basis):,} hours")

# Average basis by hub (bar).
avg = basis.mean().sort_values().reset_index()
avg.columns = ["hub", "avg_basis"]
fig = px.bar(avg, x="avg_basis", y="hub", orientation="h",
             color="avg_basis", color_continuous_scale="RdBu_r",
             color_continuous_midpoint=0,
             labels={"avg_basis": f"Avg basis vs {ref} ($/MWh)", "hub": ""},
             title=f"Average {component} basis vs {ref} ({market})")
fig.add_vline(x=0, line_dash="dot", line_color="#888")
fig.update_layout(height=max(300, 34 * len(avg)), margin=dict(t=40),
                  coloraxis_showscale=False)
st.plotly_chart(fig, use_container_width=True)

# Monthly basis heatmap (hub × month).
st.subheader("Monthly average basis")
bl = basis.reset_index().melt(id_vars="datetime_beginning_ept",
                              var_name="hub", value_name="basis")
bl["month"] = bl["datetime_beginning_ept"].dt.to_period("M").astype(str)
pivot = bl.pivot_table(index="hub", columns="month", values="basis", aggfunc="mean")
if not pivot.empty:
    vmax = float(pivot.abs().to_numpy().max()) or 1.0
    fig2 = px.imshow(pivot, labels={"x": "Month", "y": "Hub", "color": f"$/MWh vs {ref}"},
                     aspect="auto", color_continuous_scale="RdBu_r", zmin=-vmax, zmax=vmax)
    fig2.update_xaxes(type="category")
    fig2.update_yaxes(type="category")
    fig2.update_layout(height=max(300, len(pivot) * 34 + 120), margin=dict(t=20))
    st.plotly_chart(fig2, use_container_width=True)

# Summary table: mean / std / min / max basis per hub.
stats = pd.DataFrame({
    "avg_basis": basis.mean(),
    "std_basis": basis.std(),
    "min_basis": basis.min(),
    "max_basis": basis.max(),
}).sort_values("avg_basis", ascending=False)
st.dataframe(
    stats.style.format("${:,.2f}"),
    use_container_width=True)

st.download_button(
    "⬇ Download hourly basis (CSV)",
    basis.reset_index().to_csv(index=False).encode(),
    file_name=f"hub_basis_vs_{ref.replace(' ', '_')}_{component}_{market}.csv",
    mime="text/csv")
