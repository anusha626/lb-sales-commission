"""Commission engine.

Two responsibilities:
  1. Build per-SA contributions from a list of OrderResults (apply splits).
  2. Aggregate contributions into a per-SA monthly summary, applying:
       - the whole-bracket tier table on the SA's monthly net total, OR
       - a flat-per-order rule for channels that have one (e.g. TikTok).

Per the agreed rules:
  - TikTok channel orders earn RM 10 flat per order (split by share for
    multi-SA orders); they DO contribute to the SA's monthly net total
    used to pick the tier on non-TikTok orders.
  - Cancelled / unpaid orders are excluded upstream by the aggregator.
"""
from __future__ import annotations

from collections import defaultdict

from .models import (
    CommissionReport,
    CommissionTier,
    HouseSalesSummary,
    OrderResult,
    SACommission,
    SAContribution,
)
from .parser import HOUSE_ACCOUNT
from .settings import ChannelFlatRule, TiersConfig


def _tier_for(net: float, tiers: list[CommissionTier]) -> tuple[CommissionTier, str]:
    """Return the tier whose [min_net, max_net] bracket contains `net`,
    plus a human label describing it."""
    sorted_tiers = sorted(tiers, key=lambda t: t.min_net)
    for t in sorted_tiers:
        upper = t.max_net if t.max_net is not None else float("inf")
        if t.min_net <= net <= upper:
            label = (
                f"RM{t.min_net:,.0f} – RM{t.max_net:,.0f} @ {t.rate_pct}%"
                if t.max_net is not None
                else f"≥ RM{t.min_net:,.0f} @ {t.rate_pct}%"
            )
            return t, label
    # Fall back to the lowest bracket (shouldn't happen if tiers cover 0+).
    t = sorted_tiers[0]
    return t, f"Defaulted to lowest tier @ {t.rate_pct}%"


def build_contributions(orders: list[OrderResult]) -> list[SAContribution]:
    """Expand each non-excluded order into one SAContribution per SA share.

    Splits apply to net sales (not commission). MINKEI 70%/LILY 30% on a
    RM10,000 net order produces:
      - SAContribution(MINKEI, gross=RM10000*0.7=RM7000, net=...same logic)
      - SAContribution(LILY,   gross=RM10000*0.3=RM3000, net=...)
    """
    out: list[SAContribution] = []
    for o in orders:
        if o.excluded:
            continue
        for share in o.parsed.sa_shares:
            out.append(
                SAContribution(
                    sa_name=share.name,
                    order_number=o.order_number,
                    order_date=o.order_date,
                    gross_share=round(o.gross_total * share.share, 2),
                    net_share=round(o.net_total * share.share, 2),
                    share_pct=share.share,
                )
            )
    return out


def _flat_rule_for_order(
    order: OrderResult, tiers_cfg: TiersConfig
) -> ChannelFlatRule | None:
    return tiers_cfg.flat_rule_for(order.channel)


def compute_commissions(
    orders: list[OrderResult], tiers_cfg: TiersConfig
) -> CommissionReport:
    """Build the per-SA summary cards plus a separate house-sales summary.

    COMPANY SALES is the house account, NOT a Sales Advisor. Its sales are
    tracked in `report.house` for revenue visibility but never appear in the
    per-SA commission list and never earn commission.

    Orders earning a flat-per-channel commission (e.g. TikTok) contribute
    their net to the SA monthly total (so the tier on OTHER orders can rise),
    but their commission is the flat amount, not a percentage.
    """
    # Index orders by number for quick lookup of channel/flat-rule status
    by_number = {o.order_number: o for o in orders}

    # Contributions across all kept orders
    contribs = build_contributions(orders)

    by_sa: dict[str, list[SAContribution]] = defaultdict(list)
    for c in contribs:
        by_sa[c.sa_name].append(c)

    sa_summaries: list[SACommission] = []
    house_contribs: list[SAContribution] = []

    for sa, sa_contribs in by_sa.items():
        # House account — track separately, never compute commission.
        if sa == HOUSE_ACCOUNT:
            house_contribs.extend(sa_contribs)
            continue

        total_gross = round(sum(c.gross_share for c in sa_contribs), 2)
        total_net = round(sum(c.net_share for c in sa_contribs), 2)
        order_count = len(sa_contribs)
        avg = round(total_gross / order_count, 2) if order_count else 0.0

        tier, tier_label = _tier_for(total_net, tiers_cfg.tiers)

        # Walk contributions: orders on a flat-rule channel earn the flat
        # amount (split by share); other orders earn share * tier_rate.
        commission_amount = 0.0
        for c in sa_contribs:
            order = by_number.get(c.order_number)
            flat = _flat_rule_for_order(order, tiers_cfg) if order else None
            if flat is not None:
                commission_amount += flat.amount_per_order * c.share_pct
            else:
                commission_amount += c.net_share * tier.rate_pct / 100.0

        sa_summaries.append(
            SACommission(
                sa_name=sa,
                order_count=order_count,
                total_gross_sales=total_gross,
                total_net_sales=total_net,
                avg_order_value=avg,
                tier_rate_pct=tier.rate_pct,
                tier_label=tier_label,
                commission_amount=round(commission_amount, 2),
                contributions=sorted(sa_contribs, key=lambda c: c.order_date),
            )
        )

    sa_summaries.sort(key=lambda s: s.total_net_sales, reverse=True)

    house: HouseSalesSummary | None = None
    if house_contribs:
        house = HouseSalesSummary(
            order_count=len(house_contribs),
            total_gross_sales=round(sum(c.gross_share for c in house_contribs), 2),
            total_net_sales=round(sum(c.net_share for c in house_contribs), 2),
            contributions=sorted(house_contribs, key=lambda c: c.order_date),
        )

    return CommissionReport(sa_summaries=sa_summaries, house=house)
