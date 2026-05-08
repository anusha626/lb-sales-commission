"""Tests for the CSV → OrderResult aggregator."""
from __future__ import annotations

from io import StringIO

import pandas as pd

from commission.aggregator import build_order_results
from commission.settings import load_all


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows).fillna("")


def _row(**overrides) -> dict:
    base = {
        "Order Number": "#1",
        "Date": "2026-05-01 10:00:00",
        "Channel": "admin_panel",
        "Total Amount": "1000.00",
        "Note": "MINKEI\nCASH RM1000",
        "Order Status": "Open",
        "Financial Status": "Paid",
    }
    base.update(overrides)
    return base


def test_zero_net_orders_dropped_entirely():
    """Orders with net_total == 0 (typically empty notes or RM 2 test orders)
    must not appear in the results — neither active nor excluded."""
    settings = load_all()
    df = _df(
        [
            _row(**{"Order Number": "#REAL", "Note": "MINKEI\nCASH RM1000"}),
            _row(**{"Order Number": "#TEST_2", "Total Amount": "2.00", "Note": ""}),
            _row(
                **{
                    "Order Number": "#NO_PAY",
                    "Total Amount": "500.00",
                    "Note": "MINKEI WALK IN",
                }
            ),
        ]
    )
    orders = build_order_results(df, settings)
    nums = [o.order_number for o in orders]
    assert "#REAL" in nums
    assert "#TEST_2" not in nums  # empty note → no payment → net=0 → dropped
    assert "#NO_PAY" not in nums  # SA detected but no payment → net=0 → dropped


def test_excluded_orders_still_shown():
    """Excluded orders (cancelled, unpaid) must still appear in the results
    — only the zero-net rule causes a complete drop."""
    settings = load_all()
    df = _df(
        [
            _row(**{"Order Number": "#CANCEL", "Order Status": "Cancelled"}),
            _row(**{"Order Number": "#UNPAID", "Financial Status": "Unpaid"}),
            _row(**{"Order Number": "#KEEP"}),
        ]
    )
    orders = build_order_results(df, settings)
    nums = [o.order_number for o in orders]
    assert set(nums) == {"#CANCEL", "#UNPAID", "#KEEP"}
    by_num = {o.order_number: o for o in orders}
    assert by_num["#CANCEL"].excluded is True
    assert by_num["#UNPAID"].excluded is True
    assert by_num["#KEEP"].excluded is False
