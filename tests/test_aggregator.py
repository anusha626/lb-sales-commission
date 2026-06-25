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


def test_no_orders_silently_dropped():
    """Every order must end up SOMEWHERE — in Parsed, Review queue, or
    Excluded — even if the parser couldn't extract a payment portion. The
    user explicitly asked for this so total counts always match the source
    CSV. Parse-failed orders surface in the Review queue via their flags."""
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
    nums = {o.order_number for o in orders}
    assert nums == {"#REAL", "#TEST_2", "#NO_PAY"}

    by_num = {o.order_number: o for o in orders}
    # The parse-failed orders are not excluded — they need user attention,
    # so they land in the Review queue via needs_review=True.
    assert by_num["#NO_PAY"].excluded is False
    assert by_num["#NO_PAY"].needs_review is True
    assert by_num["#TEST_2"].excluded is False
    assert by_num["#TEST_2"].needs_review is True
    # The clean order is not in review.
    assert by_num["#REAL"].needs_review is False


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


def test_paid_on_month_note_overrides_settlement_month():
    """A 'PAID ON <month> <year>' note attributes the order to that settlement
    month, overriding EasyStore transaction timestamps recorded at deal time.
    e.g. a May deal whose balance is paid in June counts in June."""
    settings = load_all()
    df = _df([_row(
        **{
            "Order Number": "#9867",
            "Date": "2026-05-21 13:52:46",
            "Total Amount": "11350",
            "Note": "SHASHA\nWALK IN PG\nMYDEBIT 5043 RM5000\n"
                    "MYDEBIT 3953 RM5000\nCASH RM1350 PAID ON JUN 2026",
            "Transaction status": "Success",
            "Transaction date": "2026-05-21 14:25:33",
            "Transaction amount": "5000",
        }
    )])
    o = build_order_results(df, settings)[0]
    assert o.order_date.date().isoformat() == "2026-05-21"   # ordered in May
    assert o.settlement_date is not None
    assert o.settlement_date.strftime("%Y-%m") == "2026-06"  # but settles in June


def test_no_paid_on_note_keeps_transaction_settlement():
    """Without a 'PAID ON' note, settlement stays the last successful
    transaction date."""
    settings = load_all()
    df = _df([_row(
        **{
            "Note": "MINKEI\nCASH RM1000",
            "Transaction status": "Success",
            "Transaction date": "2026-05-02 11:00:00",
            "Transaction amount": "1000",
        }
    )])
    o = build_order_results(df, settings)[0]
    assert o.settlement_date.strftime("%Y-%m") == "2026-05"
