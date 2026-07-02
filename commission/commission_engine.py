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


# ---------------------------------------------------------------------------
# Overachievement bonus (per-SA, month-season dependent). MINKEI only for now;
# move to settings if more SAs get a scheme.
# ---------------------------------------------------------------------------
_PEAK_MONTHS = {1, 2, 3, 9, 10, 11, 12}  # Jan-Mar, Sep-Dec
OVERACHIEVEMENT_SCHEMES: dict[str, dict] = {
    "MINKEI": {
        "non_peak_target": 280000.0,
        "peak_target": 380000.0,
        "step": 50000.0,   # every RM50,000 over target...
        "per_step": 500.0,  # ...earns RM500
    },
}


def overachievement_bonus(sa_name: str, month: int | None, achieved: float) -> dict | None:
    """Bonus = floor(max(0, achieved - target) / step) * per_step, where the
    target depends on the month's season. None if the SA has no scheme."""
    cfg = OVERACHIEVEMENT_SCHEMES.get((sa_name or "").upper())
    if cfg is None or not month:
        return None
    peak = month in _PEAK_MONTHS
    target = cfg["peak_target"] if peak else cfg["non_peak_target"]
    extra = max(0.0, round(achieved - target, 2))
    tiers = int(extra // cfg["step"])
    return {
        "season": "Peak" if peak else "Non-peak",
        "target": target,
        "extra": extra,
        "tiers": tiers,
        "amount": round(tiers * cfg["per_step"], 2),
    }


def apply_overachievement_bonuses(report: CommissionReport, month: int | None) -> CommissionReport:
    """Fold each SA's overachievement bonus (on their monthly net sales) into
    commission_amount and record the breakdown. Call after compute_commissions
    with the payout month."""
    for s in report.sa_summaries:
        b = overachievement_bonus(s.sa_name, month, s.total_net_sales)
        if b is None:
            continue
        s.bonus_season = b["season"]
        s.bonus_target = b["target"]
        s.bonus_achieved = s.total_net_sales
        s.bonus_tiers = b["tiers"]
        s.bonus_amount = b["amount"]
        s.commission_amount = round(s.commission_amount + b["amount"], 2)
    return report


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

        def _is_clearance(c: SAContribution) -> bool:
            o = by_number.get(c.order_number)
            return bool(o and o.is_clearance)

        # Clearance-stock orders are a separate flat-rate bucket: they are kept
        # OUT of the SA's sales totals (gross, net, order count, average) and
        # out of the tier — they only earn the flat amount.
        normal = [c for c in sa_contribs if not _is_clearance(c)]
        clearance = [c for c in sa_contribs if _is_clearance(c)]

        total_gross = round(sum(c.gross_share for c in normal), 2)
        total_net = round(sum(c.net_share for c in normal), 2)
        order_count = len(normal)
        avg = round(total_gross / order_count, 2) if order_count else 0.0

        tier, tier_label = _tier_for(total_net, tiers_cfg.tiers)

        # Commission: normal orders earn share * tier rate (or a flat-rule
        # channel amount, e.g. TikTok); clearance orders earn the flat amount.
        normal_commission = 0.0
        for c in normal:
            order = by_number.get(c.order_number)
            flat = _flat_rule_for_order(order, tiers_cfg) if order else None
            if flat is not None:
                normal_commission += flat.amount_per_order * c.share_pct
            else:
                normal_commission += c.net_share * tier.rate_pct / 100.0
        clearance_commission = round(
            sum(tiers_cfg.clearance_flat_amount * c.share_pct for c in clearance), 2
        )
        clearance_net = round(sum(c.net_share for c in clearance), 2)

        sa_summaries.append(
            SACommission(
                sa_name=sa,
                order_count=order_count,
                total_gross_sales=total_gross,
                total_net_sales=total_net,
                avg_order_value=avg,
                tier_rate_pct=tier.rate_pct,
                tier_label=tier_label,
                commission_amount=round(normal_commission + clearance_commission, 2),
                contributions=sorted(sa_contribs, key=lambda c: c.order_date),
                clearance_order_count=len(clearance),
                clearance_net_sales=clearance_net,
                clearance_commission=clearance_commission,
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
