"""Invoice reconciliation.

Covers what the module says its purpose is: (1) header-role inference for the
upload UI, (2) hour-flooring join between a sub-hourly invoice and hourly LMPs,
(3) status classification with abs/rel tolerances, (4) DST-safe joining across
the fall-back hour.
"""

from __future__ import annotations

import pandas as pd
import pytest

from pjm_core import invoice


class TestSuggestMapping:
    def test_hour_ending_headers_are_recognized(self):
        m = invoice.suggest_mapping(
            ["Hour Ending", "PNODE", "LMP", "MWh", "Amount ($)"]
        )
        assert m["time_col"] == "Hour Ending"
        assert m["location_col"] == "PNODE"
        assert m["price_col"] == "LMP"
        assert m["volume_col"] == "MWh"
        assert m["amount_col"] == "Amount ($)"
        assert m["time_basis"] == "ending"    # "hour ending" → interval-END
        assert m["interval"] == "hour"

    def test_interval_start_headers_switch_basis(self):
        m = invoice.suggest_mapping(["Interval Start", "Total LMP", "MWh"])
        assert m["time_col"] == "Interval Start"
        assert m["time_basis"] == "beginning"


class TestLoadInvoice:
    def test_hour_ending_is_shifted_back_to_interval_start(self):
        df = pd.DataFrame({
            "Hour Ending": [pd.Timestamp("2025-07-15 15:00")],
            "MWh": [10.0],
            "Amount": [500.0],
        })
        m = invoice.suggest_mapping(df.columns)
        out = invoice.load_invoice(df, m)
        # HE 15:00 → interval-beginning 14:00 EDT
        assert out.loc[0, "interval_start"].hour == 14
        assert out.loc[0, "inv_volume_mwh"] == 10.0
        assert out.loc[0, "inv_amount"] == 500.0

    def test_interval_beginning_is_not_shifted(self):
        df = pd.DataFrame({
            "Interval Start": [pd.Timestamp("2025-07-15 14:00")],
            "MWh": [10.0],
            "Amount": [500.0],
        })
        m = invoice.suggest_mapping(df.columns)
        out = invoice.load_invoice(df, m)
        assert out.loc[0, "interval_start"].hour == 14

    def test_mw_volume_unit_is_scaled_to_mwh(self):
        # 10 MW for an hour = 10 MWh, but at 15-min interval, 10 MW = 2.5 MWh.
        df = pd.DataFrame({
            "Interval Start": [pd.Timestamp("2025-07-15 14:00")],
            "MW": [10.0],
        })
        m = invoice.suggest_mapping(df.columns)
        m["volume_unit"] = "MW"
        m["interval"] = "15min"
        out = invoice.load_invoice(df, m)
        assert out.loc[0, "inv_volume_mwh"] == pytest.approx(2.5)

    def test_missing_value_columns_raises(self):
        df = pd.DataFrame({"time": [pd.Timestamp("2025-07-15 14:00")]})
        m = {"time_col": "time", "time_basis": "beginning", "interval": "hour"}
        with pytest.raises(ValueError):
            invoice.load_invoice(df, m)


def _prices(times, prices, location="DOMINION HUB"):
    return pd.DataFrame({
        "datetime_beginning_ept": times,
        "pnode_name": location,
        "total_lmp": prices,
    })


class TestReconcile:
    def test_matched_intervals_within_tolerance_are_match(self):
        # Invoice bills 10 MWh @ $50 = $500. Market LMP is $50 → clean match.
        t = pd.Series([pd.Timestamp("2025-07-15 14:00")])
        inv_df = pd.DataFrame({
            "Interval Start": t, "PNODE": ["DOMINION HUB"],
            "LMP": [50.0], "MWh": [10.0], "Amount": [500.0],
        })
        inv = invoice.load_invoice(inv_df, invoice.suggest_mapping(inv_df.columns))
        res = invoice.reconcile(inv, price_df=_prices(t, [50.0]),
                                location="DOMINION HUB")
        assert res["summary"]["n_match"] == 1
        assert res["summary"]["variance"] == pytest.approx(0.0)

    def test_amount_mismatch_when_invoice_overbills(self):
        # Invoice: $600 for 10 MWh at claimed $60/MWh. Market LMP $50 → expected $500.
        t = pd.Series([pd.Timestamp("2025-07-15 14:00")])
        inv_df = pd.DataFrame({
            "Interval Start": t, "PNODE": ["DOMINION HUB"],
            "LMP": [60.0], "MWh": [10.0], "Amount": [600.0],
        })
        inv = invoice.load_invoice(inv_df, invoice.suggest_mapping(inv_df.columns))
        res = invoice.reconcile(inv, price_df=_prices(t, [50.0]),
                                location="DOMINION HUB")
        # Price is checked first in the classifier, so this fails there.
        assert res["summary"]["n_flagged"] == 1
        assert res["summary"]["variance"] == pytest.approx(100.0)  # invoiced - expected

    def test_sub_hourly_invoice_floors_to_hour_of_hub_lmp(self):
        # Two 15-min slices at 14:15 and 14:45 both match the 14:00 hourly LMP.
        t = pd.Series([pd.Timestamp("2025-07-15 14:15"),
                       pd.Timestamp("2025-07-15 14:45")])
        inv_df = pd.DataFrame({
            "Interval Start": t, "PNODE": ["DOMINION HUB"] * 2,
            "LMP": [50.0, 50.0], "MWh": [2.5, 2.5], "Amount": [125.0, 125.0],
        })
        m = invoice.suggest_mapping(inv_df.columns)
        m["interval"] = "15min"
        inv = invoice.load_invoice(inv_df, m)
        prices = _prices(pd.Series([pd.Timestamp("2025-07-15 14:00")]), [50.0])
        res = invoice.reconcile(inv, price_df=prices, location="DOMINION HUB")
        # Both rows resolve to the same hub hour and reconcile as matches.
        assert res["summary"]["n_match"] == 2

    def test_missing_price_row_flags_extra_in_invoice(self):
        # Invoice bills an hour with no cached hub LMP.
        t = pd.Series([pd.Timestamp("2025-07-15 14:00")])
        inv_df = pd.DataFrame({
            "Interval Start": t, "PNODE": ["DOMINION HUB"],
            "MWh": [10.0], "Amount": [500.0],
        })
        inv = invoice.load_invoice(inv_df, invoice.suggest_mapping(inv_df.columns))
        empty = _prices(pd.Series(dtype="datetime64[ns]"), [])
        res = invoice.reconcile(inv, price_df=empty, location="DOMINION HUB")
        counts = res["summary"]["status_counts"]
        assert counts.get("extra_in_invoice", 0) == 1

    def test_fall_back_duplicate_hour_is_joined_by_absolute_instant(self):
        # 2025-11-02 01:00 happens twice. The invoice carries a DST flag; the
        # hub-price frame stores both passes as separate naive rows in order.
        # After tz-lifting on both sides, they must reconcile as distinct hours.
        inv_df = pd.DataFrame({
            "Interval Start": [
                pd.Timestamp("2025-11-02 00:00"),
                pd.Timestamp("2025-11-02 01:00"),   # 1st pass (EDT)
                pd.Timestamp("2025-11-02 01:00"),   # 2nd pass (EST)
                pd.Timestamp("2025-11-02 02:00"),
            ],
            "DST": [False, False, True, False],     # True = 2nd pass
            "PNODE": ["DOMINION HUB"] * 4,
            "LMP":   [40.0, 45.0, 42.0, 38.0],
            "MWh":   [10.0, 10.0, 10.0, 10.0],
            "Amount":[400.0, 450.0, 420.0, 380.0],
        })
        m = invoice.suggest_mapping(inv_df.columns)
        inv = invoice.load_invoice(inv_df, m)
        # Price frame stores the same repeating pattern (order defines pass).
        prices = _prices(
            pd.Series([
                pd.Timestamp("2025-11-02 00:00"),
                pd.Timestamp("2025-11-02 01:00"),
                pd.Timestamp("2025-11-02 01:00"),
                pd.Timestamp("2025-11-02 02:00"),
            ]),
            [40.0, 45.0, 42.0, 38.0],
        )
        res = invoice.reconcile(inv, price_df=prices, location="DOMINION HUB")
        # Four invoice rows, four match rows — the duplicated wall-clock hour
        # doesn't collapse.
        assert res["summary"]["intervals"] == 4
        assert res["summary"]["n_match"] == 4
