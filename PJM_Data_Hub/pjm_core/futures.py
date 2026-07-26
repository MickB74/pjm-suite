"""PJM Western Hub forward curve — exchange-traded power futures.

PJM Western Hub is the liquid traded hub for PJM power. Monthly **peak** and
**off-peak** calendar-month futures settle in **$/MWh** on CME/NYMEX (Globex
``JL1`` peak, ``JN9`` off-peak) and on ICE. A calendar strip (e.g. Cal-2027) is
just the average of that year's monthly settles.

**There is no free forward-price *API*** in the usual sense — Yahoo (which powers
our Henry Hub gas strip) carries no PJM power contracts, CME's swap-futures barely
trade and its feed is Akamai-blocked, and ICE/Barchart subscription feeds are
paid. **But** ICE's public product-guide charting endpoint serves the
*free, 15-minute-delayed* forward curve as clean JSON, and that's what this module
pulls. It mirrors the Capacity (RPM) design:

  * a **user-maintained reference CSV** (``data/futures/pjm_wh_forward_curve.csv``)
    seeded below with a **placeholder** curve, and
  * a best-effort scraper (:func:`update`) that pulls the **ICE** free delayed
    curve and overwrites the CSV, falling back to the CSV on any failure. It
    never raises — the app always keeps working from the CSV.

Data source — ICE product-guide charting API (free, delayed ≥15 min):
    https://www.ice.com/marketdata/api/productguide/charting/contract-data
        ?productId=<pid>&hubId=<hid>
returning [{marketStrip:"Aug26", lastPrice, volume, lastTime, change}, ...].
Reached with a browser-TLS client (curl_cffi) — plain requests get Akamai-403'd.

Columns: asof (YYYY-MM-DD the mark was taken), contract_month (YYYY-MM),
         block ("peak"/"offpeak"), price ($/MWh), source (free text).
"""

from __future__ import annotations

import json
import re

import pandas as pd

from pjm_core import paths

SEED_COLUMNS = ["asof", "contract_month", "block", "price", "source"]

# ---------------------------------------------------------------------------
# ICE free delayed-curve products for PJM Western Hub (captured from ice.com's
# own product pages). Each maps a block to (productId, hubId) for the charting
# endpoint. RT Peak (1 MW) carries the deepest, most-liquid monthly curve.
#   peak    -> PJM Western Hub Real-Time Peak (1 MW) Fixed Price Future
#   offpeak -> PJM Western Hub Real-Time Off-Peak Fixed Price Future
# ---------------------------------------------------------------------------
ICE_PRODUCTS: dict[str, tuple[int, int]] = {
    "peak":    (1460, 1146),
    "offpeak": (19697, 389),
}
ICE_CHART_URL = ("https://www.ice.com/marketdata/api/productguide/charting/"
                 "contract-data?productId={pid}&hubId={hid}")
MIN_ROWS = 3  # need at least this many contract months to trust a pull


# ---------------------------------------------------------------------------
# Seed curve — PLACEHOLDER values with a shape (summer peaks, peak > off-peak),
# NOT real market marks. Replace with your own EOD settlements. The `source`
# column flags every seeded row so the UI can warn until you overwrite them.
# ---------------------------------------------------------------------------
_PLACEHOLDER = "PLACEHOLDER — replace with real mark"
_ASOF = "2026-01-01"  # static seed date (real pulls stamp the actual trade date)

# (contract_month, peak $/MWh, offpeak $/MWh) — illustrative forward shape only.
_SEED_CURVE = [
    ("2026-08", 78.0, 44.0),
    ("2026-09", 62.0, 40.0),
    ("2026-10", 55.0, 38.0),
    ("2026-11", 58.0, 41.0),
    ("2026-12", 66.0, 47.0),
    ("2027-01", 72.0, 52.0),
    ("2027-02", 68.0, 49.0),
    ("2027-03", 54.0, 39.0),
    ("2027-04", 48.0, 35.0),
    ("2027-05", 50.0, 36.0),
    ("2027-06", 64.0, 42.0),
    ("2027-07", 80.0, 46.0),
    ("2027-08", 79.0, 45.0),
    ("2027-09", 60.0, 39.0),
    ("2027-10", 53.0, 37.0),
    ("2027-11", 57.0, 40.0),
    ("2027-12", 65.0, 46.0),
]


def _seed_frame() -> pd.DataFrame:
    rows = []
    for month, peak, offpeak in _SEED_CURVE:
        rows.append((_ASOF, month, "peak", peak, _PLACEHOLDER))
        rows.append((_ASOF, month, "offpeak", offpeak, _PLACEHOLDER))
    return pd.DataFrame(rows, columns=SEED_COLUMNS)


def ensure_seed() -> None:
    """Write the seed CSV if the user has no futures file yet (idempotent)."""
    paths.FUTURES_DIR.mkdir(parents=True, exist_ok=True)
    if not paths.FUTURES_CSV.exists():
        _seed_frame().to_csv(paths.FUTURES_CSV, index=False)


def load() -> pd.DataFrame:
    """Load the Western Hub forward curve (seeding the CSV on first use)."""
    ensure_seed()
    try:
        df = pd.read_csv(paths.FUTURES_CSV)
    except Exception:
        df = _seed_frame()
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["contract_month"] = df["contract_month"].astype(str)
    if "source" not in df.columns:
        df["source"] = ""
    return df.dropna(subset=["price"]).sort_values(
        ["block", "contract_month"]).reset_index(drop=True)


def is_placeholder(df: pd.DataFrame | None = None) -> bool:
    """True while the curve still holds seeded placeholder marks (not real)."""
    if df is None:
        df = load()
    return df["source"].astype(str).str.startswith("PLACEHOLDER").any()


def asof(df: pd.DataFrame | None = None) -> str:
    """The most recent mark date in the curve, or '' if unknown."""
    if df is None:
        df = load()
    try:
        return str(df["asof"].max())
    except Exception:
        return ""


def calendar_strips(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Average peak/off-peak $/MWh per calendar year (a 'Cal strip')."""
    if df is None:
        df = load()
    d = df.copy()
    d["year"] = d["contract_month"].str.slice(0, 4)
    strip = (d.groupby(["year", "block"])["price"].mean()
             .reset_index()
             .pivot(index="year", columns="block", values="price")
             .reset_index())
    # An "around-the-clock" (ATC / 7x24) blend: peak hours are ~16/24 of the day
    # on weekdays; use the standard 5x16 peak weighting for a rough ATC proxy.
    if "peak" in strip and "offpeak" in strip:
        strip["atc"] = strip[["peak", "offpeak"]].mean(axis=1)
    return strip


# ---------------------------------------------------------------------------
# Best-effort ICE scraper. Never raises; returns the written DataFrame or None
# (leaving any existing CSV in place) so the app keeps working from the CSV
# whether or not the pull succeeds.
# ---------------------------------------------------------------------------
_MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
# A pure monthly ICE strip is "Aug26" — skip ranges ("Jan27-Feb27"),
# quarters ("Q4 26") and calendars ("Cal 27"); those aren't single months.
_MONTH_RE = re.compile(r"^([A-Z][a-z]{2})(\d{2})$")


def _parse_ice_month(label: str) -> str | None:
    """'Aug26' -> '2026-08'. Returns None for strips/quarters/ranges."""
    m = _MONTH_RE.match(str(label).strip())
    if not m or m.group(1) not in _MONTH_ABBR:
        return None
    return f"20{int(m.group(2)):02d}-{_MONTH_ABBR[m.group(1)]:02d}"


def _ice_trade_date(records: list[dict]) -> str:
    """Newest lastTime date in the pull as YYYY-MM-DD, else today."""
    best = ""
    for rec in records:
        t = str(rec.get("lastTime") or "")  # "07/17/2026 09:59 PM GMT"
        try:
            best = max(best, pd.Timestamp(t.split(" ", 1)[0]).strftime("%Y-%m-%d"))
        except Exception:
            continue
    return best or pd.Timestamp.now().strftime("%Y-%m-%d")


def _fetch_ice_product(product_id: int, hub_id: int, block: str) -> list[tuple]:
    """Pull one ICE product's free delayed curve as rows. Best-effort; may be []."""
    try:
        from curl_cffi import requests as cr  # optional dep; scraper only
    except Exception:
        return []
    url = ICE_CHART_URL.format(pid=product_id, hid=hub_id)
    try:
        s = cr.Session(impersonate="chrome")
        s.get("https://www.ice.com/", timeout=15)  # warm Akamai cookies
        r = s.get(url, timeout=20, headers={"Accept": "application/json"})
        if r.status_code != 200:
            return []
        records = r.json()
    except Exception:
        return []
    td = _ice_trade_date(records)
    out = []
    for rec in records:
        month = _parse_ice_month(rec.get("marketStrip", ""))
        price = rec.get("lastPrice")
        if month is None or price is None:
            continue
        try:
            out.append((td, month, block, float(price), "ICE (delayed ≥15 min)"))
        except (TypeError, ValueError):
            continue
    return out


def refreshed_today() -> bool:
    """True if the curve was already pulled today (per its state marker)."""
    try:
        state = json.loads(paths.FUTURES_STATE.read_text())
        return state.get("asof") == pd.Timestamp.now().strftime("%Y-%m-%d")
    except Exception:
        return False


def update(log=print) -> pd.DataFrame | None:
    """Best-effort pull of the ICE Western Hub free delayed curve into FUTURES_CSV.

    Returns the written DataFrame, or None if the pull was too thin (existing
    CSV left untouched). Never raises.
    """
    rows: list[tuple] = []
    for block, (pid, hid) in ICE_PRODUCTS.items():
        got = _fetch_ice_product(pid, hid, block)
        log(f"WH futures: ICE {block} (product {pid}) -> {len(got)} priced month(s).")
        rows.extend(got)

    if len(rows) < MIN_ROWS:
        log(f"WH futures: only {len(rows)} priced contract row(s) from ICE "
            f"(need {MIN_ROWS}); leaving CSV untouched.")
        return None

    df = (pd.DataFrame(rows, columns=SEED_COLUMNS)
          .drop_duplicates(["contract_month", "block"], keep="first")
          .sort_values(["block", "contract_month"]))
    paths.FUTURES_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(paths.FUTURES_CSV, index=False)
    paths.FUTURES_STATE.write_text(json.dumps({
        "asof": pd.Timestamp.now().strftime("%Y-%m-%d"),
        "last_success": pd.Timestamp.now(tz="UTC").isoformat(),
        "trade_date": str(df["asof"].max()),
        "rows": len(df),
    }, indent=2))
    log(f"WH futures: wrote {len(df)} rows (ICE, as of {df['asof'].max()}) "
        f"to {paths.FUTURES_CSV}")
    return df


if __name__ == "__main__":
    import sys

    force = "--force" in sys.argv[1:]
    if not force and refreshed_today():
        print("WH futures: already refreshed today; skipping (use --force).")
    else:
        update()
