"""Validate a PJM invoice / settlement statement against cached hub LMPs.

Upload any invoice that has an interval timestamp plus some of {price $/MWh,
volume MWh, amount $}. The page maps its columns, lifts both the invoice and
our cached LMPs to tz-aware Eastern (DST-correct — the November fall-back hour
is matched on the absolute instant, not the repeated wall-clock label), and
shows a per-interval reconciliation with the dollar variance. See
pjm_core.invoice.
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # PJM_Data_Hub/
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # app/ (for _common)
import _common  # noqa: E402

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from pjm_core import invoice as INV  # noqa: E402
from pjm_core import prices as PX  # noqa: E402
from pjm_core import paths, tz  # noqa: E402
from pjm_core.settlement_points import HUBS, PRIMARY_HUB  # noqa: E402

_STATUS_COLOR = {
    "match": "#1b5e20",
    "price_mismatch": "#b71c1c",
    "amount_mismatch": "#b71c1c",
    "missing_in_invoice": "#4527a0",
    "extra_in_invoice": "#37474f",
}
_NONE = "— none —"

st.title("🧾 Invoice Validation")
st.caption("Reconcile an uploaded PJM invoice / settlement statement against "
           "cached hub LMPs (RT or DA), interval by interval, in Eastern "
           "Prevailing Time with DST handled.")

up = st.file_uploader("Invoice file (CSV or Excel)", type=["csv", "xlsx", "xls"])
if up is None:
    _common.empty_state(
        st, "Upload an invoice to begin.",
        hint="Any CSV/Excel with an interval timestamp column and at least one "
             "of price (\\$/MWh), volume (MWh), or amount (\\$). PJM settlement "
             "extracts and supplier bills work directly.")


@st.cache_data(show_spinner=False)
def _read(name: str, data: bytes) -> pd.DataFrame:
    import io
    if name.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(data))
    return pd.read_csv(io.BytesIO(data))


raw = _read(up.name, up.getvalue())
st.write(f"**{len(raw):,}** rows · **{len(raw.columns)}** columns")
st.dataframe(raw.head(8), width="stretch")

guess = INV.suggest_mapping(raw.columns)
cols = list(raw.columns)


def _sel(label, role, *, required=False, help=None):
    options = ([] if required else [_NONE]) + cols
    default = guess.get(role)
    idx = options.index(default) if default in options else 0
    val = st.selectbox(label, options, index=idx, key=f"map_{role}", help=help)
    return None if val == _NONE else val


with st.container(border=True):
    st.header("1 · Map columns")
    time_col = _sel("Interval timestamp", "time_col", required=True)
    time_basis = st.radio("Timestamp marks the interval's…", ["ending", "beginning"],
                          index=0 if guess["time_basis"] == "ending" else 1,
                          horizontal=True,
                          help="PJM 'Hour Ending' labels are interval-ENDING; "
                               "Data Miner datetime_beginning_ept is beginning.")
    interval = st.radio("Interval length", ["hour", "30min", "15min", "5min"],
                        index=0, horizontal=True,
                        help="PJM settles hourly; sub-hourly rows are matched "
                             "to the LMP for the hour that contains them.")
    dst_flag_col = _sel("DST / repeated-hour flag (optional)", "dst_flag_col",
                        help="Exactly disambiguates the November fall-back hour. "
                             "Without it, the duplicated hour is inferred.")
    price_col = _sel("Price $/MWh", "price_col")
    volume_col = _sel("Volume", "volume_col")
    volume_unit = st.radio("Volume unit", ["MWh", "MW"], index=0, horizontal=True,
                           disabled=volume_col is None)
    amount_col = _sel("Amount $", "amount_col")
    location_col = _sel("Location / pnode (optional)", "location_col")

    st.header("2 · Reference price")
    market = st.radio("Market", ["RT", "DA"], horizontal=True,
                      help="RT = real-time hourly LMP, DA = day-ahead hourly LMP "
                           "(both from the local hub-price store).")
    location = st.selectbox("Trading hub", HUBS,
                            index=HUBS.index(PRIMARY_HUB) if PRIMARY_HUB in HUBS else 0)
    component = st.selectbox("LMP component", ["total_lmp", "energy", "congestion", "loss"],
                             index=0,
                             help="Bills normally settle on total LMP; the "
                                  "components help chase down a mismatch.")

    st.header("3 · Tolerance")
    abs_tol = st.number_input("Absolute ($, $/MWh)", value=0.01, min_value=0.0,
                              step=0.01, format="%.2f")
    rel_tol = st.number_input("Relative (%)", value=0.5, min_value=0.0, step=0.1,
                              format="%.2f") / 100.0
    run = st.button("▶ Validate", type="primary", width="stretch")

mapping = {
    "time_col": time_col, "time_basis": time_basis, "interval": interval,
    "dst_flag_col": dst_flag_col, "location_col": location_col,
    "price_col": price_col, "volume_col": volume_col, "volume_unit": volume_unit,
    "amount_col": amount_col,
}

if not run:
    st.info("Map the columns and pick a reference price, then press **▶ Validate**.")
    st.stop()

try:
    inv = INV.load_invoice(raw, mapping)
except ValueError as e:
    st.error(f"Could not read the invoice: {e}")
    st.stop()

# Pull cached reference prices over the invoice's window (+ a one-hour pad).
lo = inv["interval_start"].min().tz_convert(tz.EASTERN).tz_localize(None)
hi = inv["interval_start"].max().tz_convert(tz.EASTERN).tz_localize(None)
start = pd.Timestamp(lo) - pd.Timedelta(hours=1)
end_excl = pd.Timestamp(hi) + pd.Timedelta(hours=2)

price_df = PX.load_hub_prices([location], start, end_excl, market=market)
if price_df.empty:
    _common.empty_state(
        st, f"No cached **{market}** LMPs for **{location}** over "
            f"{lo.date()} → {hi.date()}.",
        hint="Run 'Update Hub Prices' on the API Keys page so this window is "
             "covered, then re-validate.",
        page="screens/0_API_Keys.py", page_label="Go to API Keys")

res = INV.reconcile(inv, price_df=price_df, location=location, market=market,
                    component=component, abs_tol=abs_tol, rel_tol=rel_tol)
d, s = res["intervals"], res["summary"]

# ── results ────────────────────────────────────────────────────────────────
st.subheader("Result")
c = st.columns(4)
c[0].metric("Intervals", f"{s['intervals']:,}")
c[1].metric("Matched", f"{s['n_match']:,}", help="Within tolerance on every "
            "compared field.")
c[2].metric("Flagged", f"{s['n_flagged']:,}",
            delta=None if not s["n_flagged"] else "needs review",
            delta_color="inverse")
c[3].metric("$ Variance (inv − exp)", f"${s['variance']:+,.2f}",
            delta=f"{s['variance_pct']:+.2f}%", delta_color="inverse",
            help="Invoiced − expected, signed from the payer's side: positive "
                 "⇒ you were overbilled (charged more than LMP × volume); "
                 "negative ⇒ underbilled (in your favor).")

cc = st.columns(2)
cc[0].metric("Invoiced total", f"${s['invoiced_total']:,.2f}")
cc[1].metric("Expected total", f"${s['expected_total']:,.2f}",
             help=f"Invoiced volume × {market} {component} at {location}.")

_var = s["variance"]
if abs(_var) < 0.005:
    st.caption("✅ **Payer view:** the invoice matches expected (within rounding).")
elif _var > 0:
    st.caption(f"🔴 **Payer view: overbilled** — the invoice charges **\\${_var:,.2f}** "
               "more than expected; as the payer you'd overpay by this much.")
else:
    st.caption(f"🟢 **Payer view: underbilled** — the invoice charges **\\${abs(_var):,.2f}** "
               "less than expected; in your favor as the payer.")

if s["status_counts"]:
    st.caption("  ·  ".join(f"**{k}**: {v}" for k, v in sorted(s["status_counts"].items())))

flagged_only = st.toggle("Show flagged intervals only", value=bool(s["n_flagged"]))
view = d[d["status"] != "match"] if flagged_only else d


def _style(col: pd.Series):
    return [f"color: {_STATUS_COLOR.get(v, '')}; font-weight: 600" for v in col]


def _style_payer_delta(col: pd.Series):
    # Payer view: positive delta = overbilled (you overpay) → red;
    # negative = underbilled (in your favor) → green.
    out = []
    for v in col:
        if pd.isna(v) or abs(v) < 0.005:
            out.append("")
        else:
            out.append(f"color: {'#d23f31' if v > 0 else '#34a853'}; font-weight: 600")
    return out


money = {c: "${:,.2f}" for c in ("inv_price", "exp_price", "price_delta",
                                 "inv_amount", "exp_amount", "amount_delta")
         if c in view.columns}
qty = {c: "{:,.3f}" for c in ("inv_volume_mwh",) if c in view.columns}
styler = view.style.apply(_style, subset=["status"]).format({**money, **qty})
# Red = overbilled, green = in the payer's favor — on the $ that actually bills.
for _dcol in ("amount_delta", "price_delta"):
    if _dcol in view.columns:
        styler = styler.apply(_style_payer_delta, subset=[_dcol])
st.dataframe(styler, width="stretch", height=460)

st.download_button(
    "⬇ Download reconciliation (CSV)",
    d.to_csv(index=False).encode(),
    file_name=f"invoice_reconciliation_{location.replace(' ', '_')}_{market}.csv",
    mime="text/csv",
)

_common.data_status(st, path=paths.HUB_PRICES_PARQUET, rows=len(price_df),
                    span=(start.date(), end_excl.date()))
