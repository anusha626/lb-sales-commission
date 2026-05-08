"""Bank-charge calculator.

Applies the merchant rate card to each PaymentPortion of a parsed note and
returns a charge breakdown plus the total net for the order.
"""
from __future__ import annotations

from datetime import date

from .models import (
    CARD_METHODS,
    ChargeLine,
    PaymentMethod,
    PaymentPortion,
    ZERO_CHARGE_METHODS,
)
from .settings import RatesConfig, lookup_rate_row


def _rate_for_portion(
    portion: PaymentPortion, rates: RatesConfig, on_date: date
) -> tuple[float, str]:
    """Return (rate_pct, label) for a payment portion on a given date.

    Methods that never incur a charge (cash, bank transfer, trade-in, TNG,
    TikTok) return (0.0, "Zero-charge method"). SenangPay uses the version's
    senangpay_card_pct / senangpay_fpx_pct fields rather than the rates table.
    Card methods are looked up against the active rate version.
    """
    version = rates.version_for(on_date)
    method = portion.method

    if method in ZERO_CHARGE_METHODS:
        return 0.0, "Zero-charge method"

    if method == PaymentMethod.SENANGPAY_CARD:
        return version.senangpay_card_pct, "SenangPay (card)"
    if method == PaymentMethod.SENANGPAY_FPX:
        return version.senangpay_fpx_pct, "SenangPay (FPX)"

    if method in CARD_METHODS:
        row = lookup_rate_row(version, method, portion.is_foreign)
        if row is None:
            return 0.0, f"No rate row for {method.value} (defaulted 0%)"
        if row.rate_pct is None:
            return 0.0, f"{row.label} (rate not configured; defaulted 0%)"
        return row.rate_pct, row.label

    if method == PaymentMethod.UNKNOWN:
        return 0.0, "Unknown method"
    return 0.0, f"{method.value} (no rate)"


def calculate_charges(
    payments: list[PaymentPortion], rates: RatesConfig, on_date: date
) -> tuple[list[ChargeLine], float, float]:
    """Compute per-portion charges plus order-level totals.

    Returns:
        (charge_lines, total_charges, net_total)
    """
    lines: list[ChargeLine] = []
    total_charges = 0.0
    net_total = 0.0
    for p in payments:
        gross = p.amount or 0.0
        rate_pct, label = _rate_for_portion(p, rates, on_date)
        charge = round(gross * rate_pct / 100.0, 2)
        net = round(gross - charge, 2)
        lines.append(
            ChargeLine(
                method=p.method,
                last4=p.last4,
                gross=round(gross, 2),
                rate_pct=rate_pct,
                rate_label=label,
                charge=charge,
                net=net,
            )
        )
        total_charges += charge
        net_total += net
    return lines, round(total_charges, 2), round(net_total, 2)


def has_unconfigured_rate(charge_lines: list[ChargeLine]) -> list[str]:
    """Return labels of any rate rows that defaulted to 0% because they're
    not configured. Used to surface a banner in the UI."""
    out: list[str] = []
    for cl in charge_lines:
        if "rate not configured" in cl.rate_label and cl.gross > 0:
            out.append(cl.rate_label)
    return out
