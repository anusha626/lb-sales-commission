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
) -> OrderResult:
    net = gross if net is None else net
    return OrderResult(
        order_number=order_number,
        order_date=date or datetime(2026, 5, 1),
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
