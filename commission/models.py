"""Pydantic models for the commission engine."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class PaymentMethod(str, Enum):
    """Canonical payment method codes used throughout the engine."""

    VISA_CREDIT = "VISA_CREDIT"
    VISA_DEBIT = "VISA_DEBIT"
    MASTERCARD_CREDIT = "MASTERCARD_CREDIT"
    MASTERCARD_DEBIT = "MASTERCARD_DEBIT"
    AMEX = "AMEX"
    JCB = "JCB"
    UPI = "UPI"
    MAESTRO = "MAESTRO"
    MYDEBIT = "MYDEBIT"
    SENANGPAY_CARD = "SENANGPAY_CARD"
    SENANGPAY_FPX = "SENANGPAY_FPX"
    BANK_TRANSFER = "BANK_TRANSFER"
    CASH = "CASH"
    TRADE_IN = "TRADE_IN"
    TIKTOK = "TIKTOK"
    TNG = "TNG"
    UNKNOWN = "UNKNOWN"


CARD_METHODS: set[PaymentMethod] = {
    PaymentMethod.VISA_CREDIT,
    PaymentMethod.VISA_DEBIT,
    PaymentMethod.MASTERCARD_CREDIT,
    PaymentMethod.MASTERCARD_DEBIT,
    PaymentMethod.AMEX,
    PaymentMethod.JCB,
    PaymentMethod.UPI,
    PaymentMethod.MAESTRO,
    PaymentMethod.MYDEBIT,
    PaymentMethod.SENANGPAY_CARD,
}

ZERO_CHARGE_METHODS: set[PaymentMethod] = {
    PaymentMethod.BANK_TRANSFER,
    PaymentMethod.CASH,
    PaymentMethod.TRADE_IN,
    PaymentMethod.TIKTOK,
    PaymentMethod.TNG,
}


class PaymentPortion(BaseModel):
    """One payment line within a seller note."""

    method: PaymentMethod
    amount: float | None = None  # None = "remainder of order total"
    last4: str | None = None
    is_foreign: bool = False  # default LOCAL when origin can't be inferred
    raw_line: str

    @field_validator("amount")
    @classmethod
    def _round_amount(cls, v: float | None) -> float | None:
        return round(v, 2) if v is not None else None


class SAShare(BaseModel):
    """An SA's share of an order."""

    name: str  # canonical uppercase, e.g. "EILEEN" or "COMPANY SALES"
    share: float = Field(ge=0.0, le=1.0)


class ParsedNote(BaseModel):
    """Result of parsing one seller note."""

    sa_shares: list[SAShare] = Field(default_factory=list)
    payments: list[PaymentPortion] = Field(default_factory=list)
    raw_note: str
    review_flags: list[str] = Field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.review_flags)


class ChargeLine(BaseModel):
    """One row of charge breakdown for a payment portion."""

    method: PaymentMethod
    last4: str | None
    gross: float
    rate_pct: float  # e.g. 1.25 = 1.25%
    rate_label: str  # human-readable label of the rate row used
    charge: float
    net: float


class OrderResult(BaseModel):
    """Aggregated, parsed and costed result for one order."""

    order_number: str
    order_date: datetime
    channel: str
    financial_status: str
    order_status: str
    gross_total: float
    parsed: ParsedNote
    charges: list[ChargeLine] = Field(default_factory=list)
    total_charges: float = 0.0
    net_total: float = 0.0
    excluded: bool = False
    excluded_reason: str | None = None

    @property
    def needs_review(self) -> bool:
        """Whether this order should appear in the human Review queue.

        House-only orders (100% COMPANY SALES) earn zero commission no matter
        how their payment method is parsed, so we treat their parser flags as
        non-actionable noise — surfacing them just adds clicks for the user.
        Mixed orders (real SA + house) still surface, since their parsing
        affects the real SA's commission.
        """
        if self.excluded or not self.parsed.needs_review:
            return False
        # Empty SA list means parsing couldn't attribute the order — keep
        # in review so the user can assign one.
        if not self.parsed.sa_shares:
            return True
        # All shares are the house account → no commission impact → skip.
        if all(s.name == "COMPANY SALES" for s in self.parsed.sa_shares):
            return False
        return True


class SAContribution(BaseModel):
    """One SA's net contribution from one order."""

    sa_name: str
    order_number: str
    order_date: datetime
    gross_share: float
    net_share: float
    share_pct: float  # 0..1


class CommissionTier(BaseModel):
    """One tier in the commission bracket table."""

    min_net: float
    max_net: float | None  # None = open-ended top bracket
    rate_pct: float


class SACommission(BaseModel):
    """Per-SA monthly commission summary."""

    sa_name: str
    order_count: int
    total_gross_sales: float
    total_net_sales: float
    avg_order_value: float
    tier_rate_pct: float
    tier_label: str
    commission_amount: float
    contributions: list[SAContribution] = Field(default_factory=list)


class HouseSalesSummary(BaseModel):
    """Tracks COMPANY SALES (house account) totals — not a sales advisor.

    Kept separate from the SA list so the per-SA commission report only
    contains people who actually earn commission. House sales still need
    visibility for finance reporting (revenue mix, channel performance).
    """

    order_count: int
    total_gross_sales: float
    total_net_sales: float
    contributions: list[SAContribution] = Field(default_factory=list)


class CommissionReport(BaseModel):
    """Top-level result of the commission engine."""

    sa_summaries: list[SACommission] = Field(default_factory=list)
    house: HouseSalesSummary | None = None

    @property
    def total_commission(self) -> float:
        return round(sum(s.commission_amount for s in self.sa_summaries), 2)

    @property
    def total_sa_gross(self) -> float:
        return round(sum(s.total_gross_sales for s in self.sa_summaries), 2)

    @property
    def total_sa_net(self) -> float:
        return round(sum(s.total_net_sales for s in self.sa_summaries), 2)


class RateRow(BaseModel):
    """One row in a card-rate version."""

    label: str  # e.g. "VISA CREDIT LOCAL"
    method: PaymentMethod
    is_foreign: bool = False
    rate_pct: float | None  # None means "not configured yet"


class RateTableVersion(BaseModel):
    """One dated version of the merchant rate card."""

    effective_from: date
    rates: list[RateRow]
    senangpay_card_pct: float = 2.5
    senangpay_fpx_pct: float = 1.5
