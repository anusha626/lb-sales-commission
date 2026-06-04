"""Real-world fixtures for the seller-note parser.

Every fixture here came from an actual order in sample_data.csv. Adding a
note that's been wrong in production? Drop it in here, set its expected
shape, and watch the test fail until the parser handles it.
"""
from __future__ import annotations

import pytest

from commission.models import PaymentMethod
from commission.parser import HOUSE_ACCOUNT, parse_seller_note


def _names(parsed):
    return [(s.name, round(s.share, 4)) for s in parsed.sa_shares]


def _methods(parsed):
    return [p.method for p in parsed.payments]


# ---------------------------------------------------------------------------
# Single-SA, single-payment
# ---------------------------------------------------------------------------

def test_walk_in_visa_credit_with_amount():
    p = parse_seller_note(
        "MINKEI\nWALKIN PJ\nVISA CREDIT 5644 RM1090", order_total=1090.0
    )
    assert _names(p) == [("MINKEI", 1.0)]
    assert _methods(p) == [PaymentMethod.VISA_CREDIT]
    assert p.payments[0].last4 == "5644"
    assert p.payments[0].amount == 1090.0
    assert not p.review_flags


def test_mastercard_implicit_amount():
    p = parse_seller_note("CHLOE WALK IN\nMASTERCARD 6692", order_total=9900.0)
    assert _names(p) == [("CHLOE", 1.0)]
    assert p.payments[0].last4 == "6692"
    assert p.payments[0].amount == 9900.0  # implicit -> remainder


def test_mydebit_not_confused_with_debit_card():
    p = parse_seller_note("LILY WALK IN\nMYDEBIT 7656", order_total=5390.0)
    assert _methods(p) == [PaymentMethod.MYDEBIT]
    assert p.payments[0].last4 == "7656"


def test_debit_mastercard_precedence():
    p = parse_seller_note(
        "COMPANY SALES WALK IN\nDEBIT MASTERCARD 6506", order_total=300.0
    )
    assert _methods(p) == [PaymentMethod.MASTERCARD_DEBIT]
    assert _names(p) == [(HOUSE_ACCOUNT, 1.0)]


def test_visa_short_keyword_with_last4():
    p = parse_seller_note("LILY WALK IN\n VISA 9109 ", order_total=1300.0)
    assert _methods(p) == [PaymentMethod.VISA_CREDIT]
    assert p.payments[0].last4 == "9109"
    assert p.payments[0].amount == 1300.0


# ---------------------------------------------------------------------------
# Multi-payment
# ---------------------------------------------------------------------------

def test_deposit_then_card():
    p = parse_seller_note(
        "EILEEN\nWHATSAPP\n5/5 ONLINE TRANSFER MBB 0150 KL DEPO-RM1000\n"
        "7/5 MASTERCARD 5620-RM9700",
        order_total=10700.0,
    )
    assert _methods(p) == [PaymentMethod.BANK_TRANSFER, PaymentMethod.MASTERCARD_CREDIT]
    assert [pp.amount for pp in p.payments] == [1000.0, 9700.0]
    assert [pp.last4 for pp in p.payments] == ["0150", "5620"]


def test_multi_amount_on_one_line_summed():
    p = parse_seller_note(
        "EILEEN\nCHATDADDY\n4/5 ONLINE TRANSFER MBB 0150 KL DEPO - RM1000\n"
        "6/5 ONLINE TRANSFER MBB 0150 KL RM5000+RM4000+RM700",
        order_total=10700.0,
    )
    assert [pp.amount for pp in p.payments] == [1000.0, 9700.0]


def test_two_card_split_payment():
    p = parse_seller_note(
        "CHLOE\nWALKIN PJ\nMASTERCARD 2675 RM15000\nVISA CREDIT 0016 RM6800",
        order_total=21800.0,
    )
    assert _methods(p) == [PaymentMethod.MASTERCARD_CREDIT, PaymentMethod.VISA_CREDIT]
    assert [pp.last4 for pp in p.payments] == ["2675", "0016"]
    assert [pp.amount for pp in p.payments] == [15000.0, 6800.0]


def test_three_way_split_with_bank_transfer():
    p = parse_seller_note(
        "COMPANY SALES WALK IN\nVISA 2394 - RM3000\nVISA 8108 - RM2000\n"
        "ONLINE TRANSFER MBB 9238 PG - RM4900",
        order_total=9900.0,
    )
    assert len(p.payments) == 3
    assert sum(pp.amount for pp in p.payments) == pytest.approx(9900.0)


# ---------------------------------------------------------------------------
# Splits
# ---------------------------------------------------------------------------

def test_split_70_30():
    p = parse_seller_note(
        "MINKEI 70% LILY 30%\nCHATDADDY - WALK IN\nMASTERCARD 1104",
        order_total=1090.0,
    )
    assert _names(p) == [("MINKEI", 0.7), ("LILY", 0.3)]
    assert p.payments[0].method == PaymentMethod.MASTERCARD_CREDIT
    assert p.payments[0].last4 == "1104"


# ---------------------------------------------------------------------------
# Channel-aware behaviour
# ---------------------------------------------------------------------------

def test_empty_note_online_store_inferred_to_house_account():
    p = parse_seller_note("", order_total=2990.0, channel="online_store")
    assert _names(p) == [(HOUSE_ACCOUNT, 1.0)]
    assert _methods(p) == [PaymentMethod.SENANGPAY_CARD]
    assert any("Empty note" in f for f in p.review_flags)


def test_tiktok_channel_suppresses_mismatch_flag():
    """TikTok channel: parsed amount is post-platform-fee net; the gap to the
    order total is expected and must NOT be flagged as data mismatch."""
    p = parse_seller_note(
        "EILEEN\nTIKTOK-WHATSAPP\nTIKTOK PAYMENT RM3189.42",
        order_total=3990.0,
        channel="tiktok-shop",
    )
    assert _methods(p) == [PaymentMethod.TIKTOK]
    assert p.payments[0].amount == 3189.42
    assert not any("differs from order total" in f for f in p.review_flags)


def test_single_portion_aligns_silently_to_order_total():
    """Seller writes RM750 but order total is RM950 (typo). Engine trusts the
    order total and adjusts the cash portion to RM950 — no review flag."""
    p = parse_seller_note(
        "MINKEI\nWHATSAPP\nCASH RM750", order_total=950.0, channel="admin_panel"
    )
    assert _methods(p) == [PaymentMethod.CASH]
    assert p.payments[0].amount == 950.0
    assert not p.review_flags


def test_multi_portion_minor_mismatch_aligned_silently():
    """Two portions sum slightly off the total — scale silently if drift < 10%."""
    p = parse_seller_note(
        "EILEEN\nONLINE TRANSFER MBB 0150 KL - RM1000\nMASTERCARD 5620 - RM9000",
        order_total=10300.0,
    )
    assert sum(pp.amount for pp in p.payments) == 10300.0
    assert not any("Scaled" in f for f in p.review_flags)


def test_multi_portion_large_mismatch_flagged():
    """Significant drift (>10%) triggers a flag so the user can verify."""
    p = parse_seller_note(
        "EILEEN\nONLINE TRANSFER MBB 0150 KL - RM500\nMASTERCARD 5620 - RM500",
        order_total=10000.0,
    )
    assert any("Scaled" in f for f in p.review_flags)
    assert sum(pp.amount for pp in p.payments) == 10000.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_company_sales_alone_no_payment():
    p = parse_seller_note("COMPANY SALES", order_total=6800.0)
    assert _names(p) == [(HOUSE_ACCOUNT, 1.0)]
    assert any("No payment method" in f for f in p.review_flags)


def test_company_sale_singular_typo_detected():
    """Real-world typo: 'COMPANY SALE' (singular). Engine should still
    recognise this as the house account."""
    p = parse_seller_note(
        "COMPANY SALE WALK IN\nVISA CREDIT 4255 RM200\nVISA CREDIT 4255 RM400",
        order_total=600.0,
    )
    assert _names(p) == [(HOUSE_ACCOUNT, 1.0)]


def test_company_sales_transposition_typo_detected():
    p = parse_seller_note(
        "COMPNAY SALES WALK IN\nMASTERCARD 1234", order_total=500.0
    )
    assert _names(p) == [(HOUSE_ACCOUNT, 1.0)]


def test_company_policy_does_not_false_match_house():
    """Defensive: 'COMPANY POLICY' must NOT trip the fuzzy house detector."""
    p = parse_seller_note(
        "EILEEN\nWHATSAPP\nCOMPANY POLICY DISCOUNT\nCASH RM500",
        order_total=500.0,
    )
    assert _names(p) == [("EILEEN", 1.0)]


def test_decimal_amount():
    p = parse_seller_note(
        "EILEEN\nTIKTOK PAYMENT RM3189.42", order_total=3189.42, channel="tiktok-shop"
    )
    assert p.payments[0].amount == 3189.42


def test_trade_in_capped_at_order_total():
    """Trade-in note shows RM10000 but order total is RM4590 (the trade-in
    has leftover credit for a future order). Per 'trust order total' rule
    the TRADE_IN amount is capped at RM4590; leftover credit is the
    customer's concern, not this order's."""
    p = parse_seller_note(
        "CHLOE WALK IN\nTRADE IN CHANEL BAG RM10000\nRM10000 - RM4590 = BALANCE RM5410",
        order_total=4590.0,
    )
    assert any(pp.method == PaymentMethod.TRADE_IN for pp in p.payments)
    trade_in = next(pp for pp in p.payments if pp.method == PaymentMethod.TRADE_IN)
    assert trade_in.amount == 4590.0


def test_senangpay_bare_flagged_for_review():
    p = parse_seller_note(
        "COMPANY SALES ONLINE WEBSITE\nSENANGPAY",
        order_total=2990.0,
        channel="online_store",
    )
    assert _methods(p)[0] == PaymentMethod.SENANGPAY_CARD
    assert any("SenangPay" in f for f in p.review_flags)


def test_mp_two_letter_sa_detected():
    p = parse_seller_note(
        "MP WALK IN\nONLINE TRANSFER MBB 9238 PG - RM6800",
        order_total=6800.0,
    )
    assert _names(p) == [("MP", 1.0)]


def test_no_sa_detected_in_unrecognisable_note():
    p = parse_seller_note(
        "RANDOMNAME WALKIN PJ\nCASH RM100", order_total=100.0
    )
    assert any("No SA detected" in f for f in p.review_flags)


def test_touch_and_go_normalised_to_tng():
    p = parse_seller_note(
        "COMPANY SALES WALK IN\nTOUCH AND GO", order_total=1050.0
    )
    assert _methods(p) == [PaymentMethod.TNG]


def test_last4_not_confused_with_rm_amount():
    """RM4590 contains a 4-digit number; ensure it isn't pulled as 'card last4'."""
    p = parse_seller_note(
        "CHLOE WALK IN\nMASTERCARD 5403 RM4590", order_total=4590.0
    )
    assert p.payments[0].last4 == "5403"


def test_master_shorthand_detected_as_mastercard():
    """Real-world abbreviation: 'MASTER' in place of 'MASTERCARD'.
    Single-line note with no newlines must still parse cleanly."""
    p = parse_seller_note(
        "MINKEI WALK IN PJ MASTER 3680 RM4690", order_total=4690.0
    )
    assert _names(p) == [("MINKEI", 1.0)]
    assert _methods(p) == [PaymentMethod.MASTERCARD_CREDIT]
    assert p.payments[0].last4 == "3680"
    assert p.payments[0].amount == 4690.0


def test_full_mastercard_still_preferred_over_master_shorthand():
    """When both 'MASTERCARD' and 'MASTER' substrings are present (i.e. a
    normal MASTERCARD note), the longer keyword wins."""
    p = parse_seller_note(
        "CHLOE WALK IN\nMASTERCARD 5403 RM4590", order_total=4590.0
    )
    assert _methods(p) == [PaymentMethod.MASTERCARD_CREDIT]
    assert p.payments[0].last4 == "5403"
