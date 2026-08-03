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


def test_split_sa_name_with_stray_space():
    """An SA name typed as two words ('MIN KEI') in a percentage split must be
    matched as MINKEI, not truncated to 'KEI' and dropped — which previously
    collapsed the order to SHASHA 100%."""
    p = parse_seller_note(
        "SHASHA 60% MIN KEI 40% ONLINE CHATDADDY + WALK IN PJ\n"
        "DEPOSIT TRANSFER RM1000 VISA 9252 RM15800",
        order_total=16800.0,
        sa_list=["SHASHA", "MINKEI", "LILY", "CHLOE", "EILEEN", "MP"],
    )
    assert {(s.name, round(s.share, 2)) for s in p.sa_shares} == {
        ("SHASHA", 0.6),
        ("MINKEI", 0.4),
    }


def test_amount_split_divides_by_total():
    # Each SA is followed by their own sales amount (no percentages). Shares
    # are amount / sum(amounts): CHLOE 1350/6400, MINKEI 5050/6400.
    p = parse_seller_note(
        "CHLOE RM1350 MINKEI RM5050 WALKIN PJ MASTER 4599 RM6400",
        order_total=6400.0,
    )
    assert _names(p) == [("CHLOE", round(1350 / 6400, 4)), ("MINKEI", round(5050 / 6400, 4))]
    # The payment is the MASTERCARD line, not mistaken for an SA amount.
    assert p.payments[0].method == PaymentMethod.MASTERCARD_CREDIT
    assert p.payments[0].last4 == "4599"


def test_single_sa_with_amount_stays_100pct():
    # One SA with an amount next to it is NOT an amount-split — still 100%.
    p = parse_seller_note("MINKEI WALK IN PJ MASTER 3680 RM4690", order_total=4690.0)
    assert _names(p) == [("MINKEI", 1.0)]


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


def test_multi_portion_large_mismatch_kept_and_flagged():
    """Significant drift (>10%) is NOT scaled — the written amounts are kept
    (no fabricated split) and the order is flagged for Review so a human can
    resolve the real gap (missing line, unpaid balance, or wrong total)."""
    p = parse_seller_note(
        "EILEEN\nONLINE TRANSFER MBB 0150 KL - RM500\nMASTERCARD 5620 - RM500",
        order_total=10000.0,
    )
    # Written amounts preserved, not scaled up to the order total.
    assert [pp.amount for pp in p.payments] == [500.0, 500.0]
    assert any("differs from order total" in f for f in p.review_flags)


def test_multi_portion_bank_transfer_not_scaled_to_total():
    """Real case: 7 transfers summing to RM8,900 against a RM9,900 order must
    keep their written amounts (RM1,000 etc.), not be scaled to RM1,112.36."""
    note = (
        "MINKEI\nWHATSAPP + WALK IN PJ\n"
        "19/5 DEPO ONLINE TRANSFER MBB 0150 KL - RM1000\n"
        "20/5 ONLINE TRANSFER MBB 0150 KL - RM2000\n"
        "23/5 ONLINE TRANSFER MBB 0150 KL - RM1000\n"
        "24/5 ONLINE TRANSFER MBB 0150 KL - RM1000\n"
        "12/6 ONLINE TRANSFER MBB 0150 KL - RM2000\n"
        "13/6 ONLINE TRANSFER MBB 0150 KL - RM1000\n"
        "14/6 ONLINE TRANSFER MB 0150 KL - RM900"
    )
    p = parse_seller_note(note, order_total=9900.0)
    assert [pp.amount for pp in p.payments] == [1000.0, 2000.0, 1000.0, 1000.0, 2000.0, 1000.0, 900.0]
    assert any("differs from order total" in f for f in p.review_flags)


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


# ---------------------------------------------------------------------------
# Deposit lines with no payment method → flagged for Review
# ---------------------------------------------------------------------------

def _has_deposit_flag(p):
    return any("Deposit line has no payment method" in f for f in p.review_flags)


def test_deposit_transfer_without_method_flagged():
    """'DEPOSIT TRANSFER RM1000' names no recognised method — flag for Review
    instead of letting the card portion absorb (and over-charge) it."""
    p = parse_seller_note(
        "LILY WALK IN PJ\nDEPOSIT TRANSFER RM1000\nVISA 8407 RM5990",
        order_total=6990.0,
    )
    assert _has_deposit_flag(p)


def test_bare_deposit_without_method_flagged():
    p = parse_seller_note(
        "SHASHA CHAT D\nDEPOSIT RM1000\nVISA 8407 RM5790",
        order_total=6790.0,
    )
    assert _has_deposit_flag(p)


def test_deposit_with_named_method_not_flagged():
    """A deposit that DOES name its method (cash / online transfer) is
    recognised and must NOT be flagged."""
    cash = parse_seller_note("MINKEI\nDEPOSIT CASH RM5000\nVISA 1234 RM7990", order_total=12990.0)
    assert not _has_deposit_flag(cash)
    xfer = parse_seller_note("EILEEN\nDEPO ONLINE TRANSFER MBB 0150 RM1000\nCASH RM2000", order_total=3000.0)
    assert not _has_deposit_flag(xfer)


def test_fullwidth_percent_split():
    """Full-width percent signs (phone / CJK keyboards) must still split:
    'MINKEI 70％ RISKA 30％' → MINKEI 70% / RISKA 30%, not MINKEI 100%."""
    p = parse_seller_note(
        "MINKEI 70％ RISKA 30％\nCHATDADDY + WALK IN PG\nCASH RM1690",
        order_total=1690.0,
        sa_list=["MINKEI", "RISKA", "LILY", "CHLOE"],
    )
    assert {(s.name, round(s.share, 2)) for s in p.sa_shares} == {
        ("MINKEI", 0.7),
        ("RISKA", 0.3),
    }


def test_amount_split_with_company_sales_remainder():
    """Named amounts + a COMPANY SALES portion: the SA amounts divide by the
    ORDER TOTAL and the shortfall goes to the house, not into the SAs.
    'MINKEI RM5003 MICHELLE RM3997 COMPANY SALE - BAG SPA RM250' on RM9,250."""
    p = parse_seller_note(
        "MINKEI RM 5003 MICHELLE RM3997 COMPANY SALE - BAG SPA RM250 "
        "WALK IN SS2 ONLINE TRANSFER RM9250",
        order_total=9250.0,
        sa_list=["MINKEI", "MICHELLE", "LILY", "CHLOE"],
    )
    shares = {s.name: round(s.share, 4) for s in p.sa_shares}
    assert shares == {
        "MINKEI": round(5003 / 9250, 4),
        "MICHELLE": round(3997 / 9250, 4),
        HOUSE_ACCOUNT: round(250 / 9250, 4),
    }


def test_pj_sales_outlet_tag_not_company_sales():
    """'PJ SALES' / 'PG SALES' are outlet tags, NOT the COMPANY SALES house
    account — the SA on line 1 must still be credited."""
    for tag in ("PJ SALES", "PG SALES"):
        p = parse_seller_note(
            f"MINKEI\nCHATDADDY - WALK IN\n{tag}\nONLINE TRANSFER RM4100",
            order_total=4100.0,
        )
        assert _names(p) == [("MINKEI", 1.0)], tag
    # Real house account still detected (full + common typo)
    assert _names(parse_seller_note("COMPANY SALES\nCASH RM100", order_total=100.0)) == [(HOUSE_ACCOUNT, 1.0)]
    assert _names(parse_seller_note("COMPANY SALE\nCASH RM100", order_total=100.0)) == [(HOUSE_ACCOUNT, 1.0)]


def test_tiktok_paylater_recognised():
    """'TIKTOK PAYLATER' (TikTok BNPL) is a TikTok payment — not an
    unrecognised method that flags 'no payment detected'."""
    p = parse_seller_note(
        "ANNABELL\nCHATDADDY\nTIKTOK PAYLATER RM1751.2",
        order_total=1990.0, channel="tiktok-shop",
    )
    assert _methods(p) == [PaymentMethod.TIKTOK]
    assert p.payments[0].amount == 1751.2
    assert not any("No payment method" in f for f in p.review_flags)


def test_misspelled_online_transfer_recognised():
    """'ONLINE TRASNFER' (TRANSFER misspelled) is still a bank transfer, not an
    unrecognised method — online transfer == bank transfer."""
    p = parse_seller_note(
        "ANNABELL\nCHATDADDY\nONLINE TRASNFER RM1990", order_total=1990.0
    )
    assert _methods(p) == [PaymentMethod.BANK_TRANSFER]
    assert p.payments[0].amount == 1990.0


def test_bank_transfer_typo_recognised():
    p = parse_seller_note("LILY\nBANK TRANFER RM800", order_total=800.0)
    assert _methods(p) == [PaymentMethod.BANK_TRANSFER]


def test_company_leading_token_is_house_without_sales_word():
    """'COMPANY' leading the note means company sales even without a following
    'SALES' word ('COMPANY WALK IN ...')."""
    p = parse_seller_note(
        "COMPANY WALK IN VISA CREDIT 7490 RM400", order_total=400.0
    )
    assert _names(p) == [(HOUSE_ACCOUNT, 1.0)]
    assert _methods(p) == [PaymentMethod.VISA_CREDIT]


def test_alipay_recognised_zero_charge_wallet():
    """ALIPAY is a recognised (zero-charge) e-wallet method, and 'ALIPAY' is
    never mistaken for an SA name."""
    p = parse_seller_note("LILY DEAL IN CHATDADDY\nALIPAY", order_total=2500.0)
    assert _names(p) == [("LILY", 1.0)]
    assert _methods(p) == [PaymentMethod.ALIPAY]
    assert p.payments[0].amount == 2500.0
    assert not p.review_flags


def test_multiple_methods_on_one_line_split_into_portions():
    """A whole note typed on ONE line with several payment methods must split
    into one portion per method — not collapse into a single method (which drops
    the card charge). LILY's #10253: card deposit + four transfer balances."""
    p = parse_seller_note(
        "LILY WALK IN PG DEPOSIT VISA CREDIT 6228 RM681 BALANCE ONLINE "
        "TRANSFER RM9000 BALANCE ONLINE TRANSFER RM5000 BALANCE ONLINE "
        "TRANSFER RM9000 BALANCE ONLINE TRANSFER RM4000",
        order_total=27681.0,
    )
    assert _methods(p) == [
        PaymentMethod.VISA_CREDIT,
        PaymentMethod.BANK_TRANSFER,
        PaymentMethod.BANK_TRANSFER,
        PaymentMethod.BANK_TRANSFER,
        PaymentMethod.BANK_TRANSFER,
    ]
    assert [pp.amount for pp in p.payments] == [681.0, 9000.0, 5000.0, 9000.0, 4000.0]
    # The card portion keeps its last4 so it can be charged the card rate.
    assert p.payments[0].last4 == "6228"
    assert not p.review_flags


def test_split_without_percent_sign():
    """A split written without '%' signs still splits — LILY's #10125
    'LILY 50 / COMPANY SALES 50' is LILY 50% + house 50%, not 100% house."""
    p = parse_seller_note(
        "LILY 50 \nCOMPANY SALES 50\nCHATDADDY\nDEPOSIT ONLINE TRANSFER RM4700\n"
        "BALANCE CASH RM6500\nBALANCE ONLINE TRANSFER RM1300",
        order_total=12500.0,
    )
    assert {(s.name, round(s.share, 2)) for s in p.sa_shares} == {
        ("LILY", 0.5),
        (HOUSE_ACCOUNT, 0.5),
    }


def test_bare_number_split_three_way():
    p = parse_seller_note("MINKEI 50 MICHELLE 30 LILY 20\nCASH RM1000", order_total=1000.0)
    assert {(s.name, round(s.share, 2)) for s in p.sa_shares} == {
        ("MINKEI", 0.5), ("MICHELLE", 0.3), ("LILY", 0.2),
    }


def test_rm_amount_not_read_as_percent_split():
    """Guard: 'CASH RM50' must not be read as a 50% share for a lone SA."""
    p = parse_seller_note("MINKEI\nWALK IN\nCASH RM50", order_total=50.0)
    assert _names(p) == [("MINKEI", 1.0)]
