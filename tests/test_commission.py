"""Tests for the commission engine: tier brackets, splits, channel flat
rules, and the COMPANY SALES (house account) zero-commission rule."""
from __future__ import annotations

from datetime import datetime

from commission.commission_engine import compute_commissions
from commission.models import (
    OrderResult,
    ParsedNote,
    PaymentMethod,
    PaymentPortion,
    SAShare,
)
from commission.parser import HOUSE_ACCOUNT
from commission.settings import ChannelFlatRule, CommissionTier, TiersConfig


def _make_order(
    *,
    order_number: str,
    sa_shares: list[tuple[str, float]],
    gross: float,
    net: float | None = None,
    channel: str = "admin_panel",
    date: datetime | None = None,
    is_clearance: bool = False,
) -> OrderResult:
    net = gross if net is None else net
    return OrderResult(
        order_number=order_number,
        order_date=date or datetime(2026, 5, 1),
        is_clearance=is_clearance,
        clearance_amount=gross if is_clearance else 0.0,
        channel=channel,
        financial_status="Paid",
        order_status="Open",
        gross_total=gross,
        parsed=ParsedNote(
            sa_shares=[SAShare(name=n, share=s) for n, s in sa_shares],
            payments=[
                PaymentPortion(method=PaymentMethod.CASH, amount=gross, raw_line="CASH")
            ],
            raw_note="(test)",
        ),
        charges=[],
        total_charges=round(gross - net, 2),
        net_total=net,
    )


def _default_tiers() -> TiersConfig:
    return TiersConfig(
        tiers=[
            CommissionTier(min_net=0.0, max_net=199999.99, rate_pct=0.80),
            CommissionTier(min_net=200000.0, max_net=349999.99, rate_pct=1.00),
            CommissionTier(min_net=350000.0, max_net=None, rate_pct=1.20),
        ],
        channel_flat_commissions=[
            ChannelFlatRule(channel="tiktok-shop", amount_per_order=10.0, label="TikTok flat")
        ],
    )


def test_company_sales_not_in_sa_list_and_tracked_separately():
    """COMPANY SALES is the house account, not a Sales Advisor — it must
    not appear in sa_summaries and must show up in report.house."""
    orders = [
        _make_order(
            order_number="#9001",
            sa_shares=[(HOUSE_ACCOUNT, 1.0)],
            gross=24400.0,
        )
    ]
    report = compute_commissions(orders, _default_tiers())
    assert report.sa_summaries == []
    assert report.total_commission == 0.0
    assert report.house is not None
    assert report.house.order_count == 1
    assert report.house.total_gross_sales == 24400.0


def test_company_sales_split_only_real_sa_in_summary():
    """50% MINKEI + 50% COMPANY SALES on a RM 10,000 net order:
    MINKEI earns commission on her RM 5,000; COMPANY SALES is house-tracked."""
    orders = [
        _make_order(
            order_number="#9100",
            sa_shares=[("MINKEI", 0.5), (HOUSE_ACCOUNT, 0.5)],
            gross=10000.0,
        )
    ]
    report = compute_commissions(orders, _default_tiers())
    sa_names = [s.sa_name for s in report.sa_summaries]
    assert sa_names == ["MINKEI"]
    assert HOUSE_ACCOUNT not in sa_names
    minkei = report.sa_summaries[0]
    assert minkei.commission_amount == round(5000 * 0.008, 2)  # RM 40
    assert minkei.total_net_sales == 5000.0
    # House gets the other half
    assert report.house is not None
    assert report.house.total_net_sales == 5000.0


def test_no_house_sales_means_house_is_none():
    orders = [_make_order(order_number="#1", sa_shares=[("EILEEN", 1.0)], gross=100.0)]
    report = compute_commissions(orders, _default_tiers())
    assert report.house is None


def test_house_only_orders_with_flags_do_not_show_in_review():
    """A 100% COMPANY SALES order with parser flags shouldn't appear in
    review — its commission is RM 0 regardless of how the flag is resolved."""
    o = _make_order(
        order_number="#H1",
        sa_shares=[(HOUSE_ACCOUNT, 1.0)],
        gross=2990.0,
    )
    o.parsed.review_flags.append("SenangPay portion lacks card/FPX detail")
    assert o.parsed.needs_review is True  # flag is on the parsed note
    assert o.needs_review is False  # but order isn't actionable for review


def test_mixed_house_and_real_sa_still_shows_in_review():
    """If a real SA has a stake in the order, parser flags still matter."""
    o = _make_order(
        order_number="#M1",
        sa_shares=[("MINKEI", 0.5), (HOUSE_ACCOUNT, 0.5)],
        gross=1000.0,
    )
    o.parsed.review_flags.append("SenangPay portion lacks card/FPX detail")
    assert o.needs_review is True


def test_no_sa_detected_still_shows_in_review():
    """When the parser couldn't attribute an order at all, the user must
    assign an SA — keep it in review even though no real SA is on it yet."""
    o = _make_order(
        order_number="#N1",
        sa_shares=[("MINKEI", 1.0)],  # placeholder
        gross=500.0,
    )
    # Simulate parser not finding anyone
    o.parsed.sa_shares.clear()
    o.parsed.review_flags.append("No SA detected")
    assert o.needs_review is True


def test_tier_bracket_whole_not_progressive():
    """SA with RM 250,000 net should get RM 2,500 (1.0% × 250k), not a blend."""
    orders = [
        _make_order(order_number="#1", sa_shares=[("EILEEN", 1.0)], gross=250000.0)
    ]
    report = compute_commissions(orders, _default_tiers())
    eileen = next(s for s in report.sa_summaries if s.sa_name == "EILEEN")
    assert eileen.tier_rate_pct == 1.0
    assert eileen.commission_amount == 2500.0


def test_tiktok_flat_plus_tier_on_other_orders():
    """TikTok order earns flat RM 10; other orders earn tier rate.
    TikTok net still feeds the SA's monthly net for tier purposes."""
    orders = [
        _make_order(
            order_number="#TT1",
            sa_shares=[("EILEEN", 1.0)],
            gross=3990.0,
            net=3189.42,
            channel="tiktok-shop",
        ),
        _make_order(
            order_number="#R1",
            sa_shares=[("EILEEN", 1.0)],
            gross=10700.0,
        ),
    ]
    report = compute_commissions(orders, _default_tiers())
    eileen = next(s for s in report.sa_summaries if s.sa_name == "EILEEN")
    expected = 10700.0 * 0.008 + 10.0
    assert eileen.commission_amount == round(expected, 2)
    assert eileen.total_net_sales == round(3189.42 + 10700.0, 2)


def test_split_70_30_commission_distributed_correctly():
    orders = [
        _make_order(
            order_number="#S1",
            sa_shares=[("MINKEI", 0.7), ("LILY", 0.3)],
            gross=10000.0,
        )
    ]
    report = compute_commissions(orders, _default_tiers())
    by_name = {s.sa_name: s for s in report.sa_summaries}
    # 0.8% tier: MINKEI 7000*0.008=56; LILY 3000*0.008=24
    assert by_name["MINKEI"].commission_amount == 56.0
    assert by_name["LILY"].commission_amount == 24.0


# ---------------------------------------------------------------------------
# Clearance-stock flat rule
# ---------------------------------------------------------------------------

def test_clearance_order_earns_flat_amount():
    """A clearance order earns the flat RM10, not the tier %."""
    orders = [
        _make_order(
            order_number="#C1",
            sa_shares=[("MINKEI", 1.0)],
            gross=500.0,
            is_clearance=True,
        )
    ]
    report = compute_commissions(orders, _default_tiers())
    minkei = report.sa_summaries[0]
    assert minkei.commission_amount == 10.0  # flat, not 500*0.008


def test_clearance_flat_split_by_ratio():
    """RM10 flat is divided by each SA's share — 70/30 → RM7 / RM3."""
    orders = [
        _make_order(
            order_number="#C2",
            sa_shares=[("MINKEI", 0.7), ("LILY", 0.3)],
            gross=1000.0,
            is_clearance=True,
        )
    ]
    report = compute_commissions(orders, _default_tiers())
    by = {s.sa_name: s for s in report.sa_summaries}
    assert by["MINKEI"].commission_amount == 7.0
    assert by["LILY"].commission_amount == 3.0


def test_clearance_net_excluded_from_tier():
    """A huge clearance sale must not push the SA into a higher bracket on
    their normal full-price sale."""
    orders = [
        _make_order(order_number="#N1", sa_shares=[("MINKEI", 1.0)], gross=10000.0),
        _make_order(order_number="#C3", sa_shares=[("MINKEI", 1.0)],
                    gross=900000.0, is_clearance=True),
    ]
    report = compute_commissions(orders, _default_tiers())
    minkei = report.sa_summaries[0]
    # Tier picked on 10,000 (0.8%), NOT on 910,000 (which would be 1.2%).
    assert minkei.tier_rate_pct == 0.80
    # Commission = 10000*0.8% (normal) + RM10 (clearance flat)
    assert minkei.commission_amount == round(10000 * 0.008 + 10.0, 2)


def test_clearance_tag_detection_year_optional():
    """Tag matches with or without the trailing year, tolerates spacing, and
    does not false-match on COMPANY SALES."""
    cfg = _default_tiers()
    assert cfg.is_clearance_note("MINKEI\nSALES JUNE 2026\nCASH RM500") is True
    assert cfg.is_clearance_note("MINKEI\nSALES JUNE\nCASH RM500") is True
    assert cfg.is_clearance_note("minkei sales  june 2026") is True   # case + spacing
    assert cfg.is_clearance_note("MINKEI\nWALK IN PJ\nCASH RM500") is False
    assert cfg.is_clearance_note("COMPANY SALES\nCASH RM500") is False


def test_clearance_excluded_from_total_sales():
    """Clearance orders are kept OUT of the SA's sales totals (gross, net,
    order count) and summarised separately; commission still includes the
    flat amount."""
    orders = [
        _make_order(order_number="#N1", sa_shares=[("MINKEI", 1.0)], gross=10000.0),
        _make_order(order_number="#C1", sa_shares=[("MINKEI", 1.0)],
                    gross=5000.0, is_clearance=True),
    ]
    report = compute_commissions(orders, _default_tiers())
    m = report.sa_summaries[0]
    assert m.total_net_sales == 10000.0          # clearance 5,000 NOT included
    assert m.total_gross_sales == 10000.0
    assert m.order_count == 1                     # only the normal order
    assert m.clearance_order_count == 1
    assert m.clearance_net_sales == 5000.0
    assert m.clearance_commission == 10.0
    assert m.commission_amount == round(10000 * 0.008 + 10.0, 2)
    # Report-level totals also exclude clearance sales
    assert report.total_sa_net == 10000.0


# ---------------------------------------------------------------------------
# Overachievement bonus (MINKEI scheme)
# ---------------------------------------------------------------------------
from commission.commission_engine import (  # noqa: E402
    apply_overachievement_bonuses,
    overachievement_bonus,
)


def test_overachievement_matches_letter_examples():
    assert overachievement_bonus("MINKEI", 6, 330000)["amount"] == 500.0   # non-peak
    assert overachievement_bonus("MINKEI", 1, 430000)["amount"] == 500.0   # peak
    assert overachievement_bonus("MINKEI", 6, 430000)["amount"] == 1500.0  # 3 tiers
    assert overachievement_bonus("MINKEI", 6, 279999)["amount"] == 0.0     # below target
    assert overachievement_bonus("LILY", 6, 900000) is None                # no scheme


def test_bonus_folded_into_commission():
    orders = [_make_order(order_number="#1", sa_shares=[("MINKEI", 1.0)], gross=330000.0)]
    report = compute_commissions(orders, _default_tiers())
    apply_overachievement_bonuses(report, 6)  # June = non-peak, target 280k
    m = report.sa_summaries[0]
    assert m.bonus_season == "Non-peak"
    assert m.bonus_tiers == 1
    assert m.bonus_amount == 500.0
    # tier commission (330k @ 1.0%) + RM500 bonus
    assert m.commission_amount == round(330000 * 0.01 + 500.0, 2)


def test_partial_clearance_order_split():
    """An order that is part normal + part clearance (e.g. #10052: RM490
    normal + RM799 clearance in a RM1289 order) splits: the normal portion
    earns tier %, the clearance portion earns the flat RM10."""
    o = _make_order(order_number="#10052", sa_shares=[("MINKEI", 1.0)], gross=1289.0)
    o.clearance_amount = 799.0          # partial clearance
    o.is_clearance = False
    report = compute_commissions([o], _default_tiers())
    m = report.sa_summaries[0]
    # Sales total = only the RM490 normal portion
    assert m.total_net_sales == 490.0
    assert m.clearance_net_sales == 799.0
    assert m.clearance_order_count == 1
    # Commission = 490 × 0.8% (tier) + RM10 flat clearance
    assert m.commission_amount == round(490 * 0.008 + 10.0, 2)
