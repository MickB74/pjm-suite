"""Regional gas basis — delivered price to power generators vs Henry Hub.

DOM Hub units do not burn Henry Hub gas. They burn Mid-Atlantic gas, which in
winter can trade far above it: February 2026 settled at $3.62 at Henry Hub and
$7.09 delivered to Virginia power burners. A forecast that prices power off
Henry Hub alone has to explain that $3.47 gap somewhere, and it ends up in the
implied heat rate — 24.1 against Henry Hub for that month, against a normal
12.3 on the gas actually burned.

Source is EIA's "natural gas sold to electric power consumers" series, one per
state (`N3045<ST>3`), monthly, published in $/Mcf. Two limits worth carrying:

  * It is a *delivered* price including transport, not a traded hub print like
    Transco Zone 5. Directionally right, but it is a proxy.
  * It lags roughly three months. That is fine for fitting the heat rate on
    what generators actually paid, and it means the forward basis has to come
    from the seasonal distribution rather than from a current quote.

A daily hub feed (Platts/Argus) would supersede both limits; this exists to
establish whether basis earns its place before anyone pays for one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import requests

from pjm_core import credentials, paths

EIA_URL = "https://api.eia.gov/v2/natural-gas/pri/sum/data/"

# EIA quotes these in dollars per thousand cubic feet; the forecast works in
# $/MMBtu throughout. Pipeline-quality gas runs ~1.037 MMBtu per Mcf.
MCF_TO_MMBTU = 1.037

# States carrying PJM load. Only VA feeds the DOM Hub forecast today, but the
# rest cost one request each and make the store useful for other zones later.
PJM_STATES = {
    "VA": "Virginia", "PA": "Pennsylvania", "MD": "Maryland",
    "NJ": "New Jersey", "OH": "Ohio", "IL": "Illinois",
    "WV": "West Virginia", "NC": "North Carolina",
}

PRIMARY_STATE = "VA"          # the DOM Hub's own state


def _series_id(state: str) -> str:
    return f"N3045{state}3"


def _fetch_state(state: str, api_key: str, timeout: int = 30) -> pd.Series:
    """Monthly $/MMBtu delivered to electric power consumers in `state`."""
    r = requests.get(EIA_URL, params={
        "api_key": api_key,
        "frequency": "monthly",
        "data[0]": "value",
        "facets[series][]": _series_id(state),
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 500,
    }, timeout=timeout)
    r.raise_for_status()
    rows = r.json()["response"]["data"]
    s = pd.Series({
        pd.Timestamp(x["period"]): float(x["value"]) / MCF_TO_MMBTU
        for x in rows if x.get("value") is not None
    })
    return s.sort_index()


def _henry_hub(api_key: str, timeout: int = 30) -> pd.Series:
    """Monthly Henry Hub spot, the reference leg of the basis."""
    r = requests.get("https://api.eia.gov/v2/natural-gas/pri/fut/data/", params={
        "api_key": api_key,
        "frequency": "monthly",
        "data[0]": "value",
        "facets[series][]": "RNGWHHD",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 500,
    }, timeout=timeout)
    r.raise_for_status()
    rows = r.json()["response"]["data"]
    s = pd.Series({pd.Timestamp(x["period"]): float(x["value"])
                   for x in rows if x.get("value") is not None})
    return s.sort_index()


def update(states=None, api_key: str | None = None, log=print) -> pd.DataFrame:
    """Refresh the basis store. Returns the written frame.

    One row per (month, state) with the delivered price, Henry Hub, and the
    basis between them. A state whose request fails is logged and skipped
    rather than failing the whole pull — the primary state is what the
    forecast needs, and a missing secondary is not worth an exception.
    """
    key = api_key or credentials.get_eia_api_key()
    if not key:
        raise RuntimeError(
            "No EIA API key — set it on the API Keys screen or in config.json.")
    states = list(states or PJM_STATES)

    hh = _henry_hub(key)
    if hh.empty:
        raise RuntimeError("EIA returned no Henry Hub history.")

    frames = []
    for st in states:
        try:
            s = _fetch_state(st, key)
        except Exception as e:                      # noqa: BLE001 - see docstring
            log(f"  {st}: {e} — skipped")
            continue
        if s.empty:
            log(f"  {st}: no data returned — skipped")
            continue
        df = pd.DataFrame({"delivered": s})
        df["henry_hub"] = df.index.map(hh)
        df = df.dropna()
        df["basis"] = df["delivered"] - df["henry_hub"]
        df["state"] = st
        df = df.reset_index(names="month")
        frames.append(df)
        log(f"  {st}: {len(df):,} months, {df['month'].min():%Y-%m}"
            f"–{df['month'].max():%Y-%m}, mean basis {df['basis'].mean():+.2f}")

    if not frames:
        raise RuntimeError("No states returned usable data.")

    out = (pd.concat(frames, ignore_index=True)
           .sort_values(["state", "month"])
           .reset_index(drop=True))
    paths.GAS_DIR.mkdir(parents=True, exist_ok=True)
    out.to_parquet(paths.GAS_BASIS_PARQUET, index=False)
    paths.GAS_BASIS_STATE.write_text(json.dumps({
        "last_success": datetime.now(timezone.utc).isoformat(),
        "rows": int(len(out)),
        "states": sorted(out["state"].unique().tolist()),
        "first_month": f"{out['month'].min():%Y-%m}",
        "last_month": f"{out['month'].max():%Y-%m}",
        "parquet": str(paths.GAS_BASIS_PARQUET),
    }, indent=2))
    return out


if __name__ == "__main__":
    update()
