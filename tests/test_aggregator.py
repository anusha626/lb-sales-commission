"""Tests for the CSV → OrderResult aggregator."""
from __future__ import annotations

from io import StringIO

import pandas as pd

from commission.aggregator import build_order_results
from commission.parser import HOUSE_ACCOUNT
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


from datetime import date as _date  # noqa: E402


def test_clearance_sku_gated_by_effective_date():
    """A clearance-SKU item counts as clearance only for orders on/after the
    effective date; earlier (full-price) sales are untouched."""
    settings = load_all()
    skus = {"CLR-SKU-1"}
    rows = [
        _row(**{"Order Number": "#MAY", "Date": "2026-05-10 10:00:00",
                "Total Amount": "5000", "Note": "MINKEI\nCASH RM5000",
                "Item SKU": "CLR-SKU-1", "Item Price": "5000", "Quantity": "1"}),
        _row(**{"Order Number": "#JUL", "Date": "2026-07-10 10:00:00",
                "Total Amount": "5000", "Note": "MINKEI\nCASH RM5000",
                "Item SKU": "CLR-SKU-1", "Item Price": "5000", "Quantity": "1"}),
    ]
    orders = {o.order_number: o for o in build_order_results(
        _df(rows), settings, clearance_skus=skus, clearance_from=_date(2026, 7, 1))}
    assert orders["#MAY"].clearance_amount == 0.0      # full price, before cutoff
    assert orders["#JUL"].clearance_amount == 5000.0   # clearance, on/after cutoff


def test_tiktok_channel_sale_split():
    """tiktok-shop: the SA keeps 30% of the sale, 70% goes to COMPANY SALES;
    no SA on the note → whole order is house."""
    settings = load_all()
    if settings.tiers.sale_split_for("tiktok-shop") is None:
        import pytest
        pytest.skip("no tiktok sale-split configured")
    with_sa = build_order_results(_df([_row(
        **{"Order Number": "#T1", "Channel": "tiktok-shop",
           "Note": "MINKEI\nTIKTOK PAYMENT RM1000", "Total Amount": "1000"})]), settings)[0]
    shares = {s.name: round(s.share, 4) for s in with_sa.parsed.sa_shares}
    assert shares == {"MINKEI": 0.3, HOUSE_ACCOUNT: 0.7}
    no_sa = build_order_results(_df([_row(
        **{"Order Number": "#T2", "Channel": "tiktok-shop",
           "Note": "COMPANY SALES\nTIKTOK PAYMENT RM1000", "Total Amount": "1000"})]), settings)[0]
    assert [(s.name, s.share) for s in no_sa.parsed.sa_shares] == [(HOUSE_ACCOUNT, 1.0)]


def test_discount_captured_and_split_across_sas():
    """'Order Discount' + 'Item Discount' are captured as a positive
    discount_total on the order and split per-SA into discount_share."""
    from commission.commission_engine import build_contributions

    settings = load_all()
    df = _df(
        [
            _row(**{
                "Order Number": "#D1",
                "Total Amount": "250.00",
                "Note": "MINKEI 60% LILY 40%\nCASH RM250",
                "Order Discount": "-240.00",
                "Item Discount": "-10.00",
            }),
        ]
    )
    orders = build_order_results(df, settings, include_unpaid=False)
    o = next(o for o in orders if o.order_number == "#D1")
    assert o.discount_total == 250.0  # 240 order + 10 item, as a magnitude

    contribs = {c.sa_name: c for c in build_contributions(orders)}
    assert contribs["MINKEI"].discount_share == 150.0  # 250 * 0.6
    assert contribs["LILY"].discount_share == 100.0    # 250 * 0.4


def test_fully_paid_counts_regardless_of_fulfillment():
    """As long as an order is fully Paid it counts — fulfillment status
    (Fulfilled / Unfulfilled / Partially Fulfilled / Restocked) is not a gate.
    Not-fully-paid orders (Partially Paid) are still held out."""
    settings = load_all()
    df = _df(
        [
            _row(**{"Order Number": "#PF", "Financial Status": "Paid",
                    "Fulfillment Status": "Fulfilled"}),
            _row(**{"Order Number": "#PU", "Financial Status": "Paid",
                    "Fulfillment Status": "Unfulfilled"}),
            _row(**{"Order Number": "#PP", "Financial Status": "Paid",
                    "Fulfillment Status": "Partially Fulfilled"}),
            _row(**{"Order Number": "#HALF", "Financial Status": "Partially Paid",
                    "Fulfillment Status": "Fulfilled"}),
        ]
    )
    by = {o.order_number: o for o in build_order_results(df, settings)}
    assert not by["#PF"].excluded
    assert not by["#PU"].excluded             # unfulfilled but paid -> counts
    assert not by["#PP"].excluded             # partially fulfilled but paid -> counts
    assert by["#HALF"].excluded               # not fully paid -> held out


def test_blank_fulfillment_status_is_not_excluded():
    """A CSV without a Fulfillment Status value must not silently drop every
    order (backward compatibility)."""
    settings = load_all()
    df = _df([_row(**{"Order Number": "#NB", "Financial Status": "Paid"})])
    o = build_order_results(df, settings)[0]
    assert not o.excluded


def test_restocked_order_counts_as_sale():
    """A fully-paid 'Restocked' order is a completed sale and must count
    (per the user's rule), unlike Unfulfilled / Partially Fulfilled."""
    settings = load_all()
    df = _df(
        [
            _row(**{"Order Number": "#RS", "Financial Status": "Paid",
                    "Fulfillment Status": "Restocked"}),
        ]
    )
    o = build_order_results(df, settings)[0]
    assert not o.excluded


def test_force_include_overrides_status_exclusion():
    """A not-fully-paid order the user force-includes is counted (and costed)
    despite the paid filter; others stay excluded."""
    settings = load_all()
    df = _df(
        [
            _row(**{"Order Number": "#KEEP", "Financial Status": "Partially Paid"}),
            _row(**{"Order Number": "#DROP", "Financial Status": "Partially Paid"}),
        ]
    )
    by = {o.order_number: o for o in build_order_results(df, settings, force_include={"#KEEP"})}
    assert not by["#KEEP"].excluded
    assert by["#KEEP"].net_total > 0          # re-costed, not zeroed
    assert by["#DROP"].excluded               # others unaffected
