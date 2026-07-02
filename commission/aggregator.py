"""Read an EasyStore order export and produce a list of OrderResults.

Responsibilities:
  - Parse the CSV (handling BOM, multi-row orders).
  - Aggregate split-payment / multi-line orders by Order Number.
  - Apply Order Status / Financial Status exclusion filters.
  - Run the parser on the seller note.
  - Run the charge calculator with the rate version effective on the order
    date.
  - Apply manual overrides supplied from the Review queue.

Pure I/O on dataframes / dicts; no Streamlit.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from io import StringIO
from typing import IO

import pandas as pd

from .charges import calculate_charges
from .models import OrderResult, ParsedNote, PaymentMethod, PaymentPortion, SAShare
from .parser import parse_seller_note
from .settings import AppSettings


# Columns we actually consume — keep this list defensive.
REQUIRED_COLS = (
    "Order Number",
    "Date",
    "Channel",
    "Total Amount",
    "Note",
    "Order Status",
    "Financial Status",
)
TAG_COL = "Tag"


def _parse_tags(s: str) -> list[str]:
    """EasyStore exports tags as a comma-separated string in the Tag column.
    Normalise to a list of stripped, upper-cased tokens."""
    if not s:
        return []
    return [t.strip().upper() for t in s.split(",") if t.strip()]


def read_easystore_csv(source: str | IO[str] | bytes) -> pd.DataFrame:
    """Read an EasyStore export, tolerating BOM and Streamlit's UploadedFile."""
    if isinstance(source, bytes):
        df = pd.read_csv(StringIO(source.decode("utf-8-sig")), dtype=str)
    else:
        df = pd.read_csv(source, dtype=str, encoding="utf-8-sig")
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    df = df.fillna("")
    return df


def _settlement_date_for_group(group: pd.DataFrame) -> str:
    """The order's settlement date = the latest *successful* transaction date.

    EasyStore writes one row per payment; a split-paid order is fully settled
    when its final successful transaction clears. Returns the raw date string
    (max over successful rows), or "" when the export has no transaction date
    for this order (e.g. a manually-marked-paid bank transfer).
    """
    if "Transaction status" not in group.columns or "Transaction date" not in group.columns:
        return ""
    succ = group[group["Transaction status"].str.strip().str.lower() == "success"]
    dates = [d for d in succ["Transaction date"].astype(str) if d.strip()]
    return max(dates) if dates else ""


def _aggregate_rows(df: pd.DataFrame) -> list[dict]:
    """Collapse a multi-row order export to one record per Order Number.

    EasyStore writes split-payment / multi-line orders as several rows; only
    the first carries metadata (Note, Order Status, Financial Status, Total).
    The per-transaction rows still carry the data we use to derive the
    settlement date, so we fold that across the whole group.
    """
    out: list[dict] = []
    for order_number, group in df.groupby("Order Number", sort=False):
        head = next(
            (
                row
                for _, row in group.iterrows()
                if row["Note"] or row["Financial Status"] or row["Order Status"]
            ),
            group.iloc[0],
        )
        rec = head.to_dict()
        rec["__settlement_date__"] = _settlement_date_for_group(group)
        out.append(rec)
    return out


def _parse_total(s: str) -> float:
    try:
        return float(s.replace(",", "").strip()) if s else 0.0
    except ValueError:
        return 0.0


def _parse_date(s: str) -> datetime:
    """EasyStore writes 'YYYY-MM-DD HH:MM:SS' — be tolerant of missing time."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return datetime.min


_MONTH_NAMES = {
    "JAN": 1, "JANUARY": 1, "FEB": 2, "FEBRUARY": 2, "MAR": 3, "MARCH": 3,
    "APR": 4, "APRIL": 4, "MAY": 5, "JUN": 6, "JUNE": 6, "JUL": 7, "JULY": 7,
    "AUG": 8, "AUGUST": 8, "SEP": 9, "SEPT": 9, "SEPTEMBER": 9, "OCT": 10,
    "OCTOBER": 10, "NOV": 11, "NOVEMBER": 11, "DEC": 12, "DECEMBER": 12,
}
# "PAID ON JUN 2026", "PAID JUNE", "BALANCE PAID ON 15 JUN 2026" — a payment
# month stated by the seller, with optional leading day and trailing year.
_PAID_MONTH_RE = re.compile(
    r"PAID\s+(?:ON\s+)?(?:\d{1,2}(?:ST|ND|RD|TH)?\s+)?([A-Z]+)\.?\s*(\d{4})?",
    re.IGNORECASE,
)


def _paid_month_from_note(note: str, order_date: datetime) -> datetime | None:
    """If the seller note states the month a (balance) payment was made — e.g.
    'CASH RM1350 PAID ON JUN 2026' — return a date in that month to use as the
    settlement date. This lets staff override the EasyStore transaction
    timestamps (often recorded at deal time) when a balance is actually settled
    in a later month. Returns None if no such phrase is present."""
    for m in _PAID_MONTH_RE.finditer((note or "").upper()):
        month = _MONTH_NAMES.get(m.group(1))
        if month is None:
            continue
        year = int(m.group(2)) if m.group(2) else order_date.year
        if m.group(2) is None and month < order_date.month:
            year += 1  # balance carried into early next year
        # Day is irrelevant for month attribution; use a safe mid-month date.
        return datetime(year, month, 15)
    return None


def _excluded_reason(
    order_status: str, financial_status: str, include_unpaid: bool
) -> str | None:
    if order_status.lower() == "cancelled":
        return "Order cancelled"
    fin = financial_status.lower()
    if not include_unpaid and fin and fin != "paid":
        return f"Financial status: {financial_status}"
    return None


def build_order_results(
    df: pd.DataFrame,
    settings: AppSettings,
    *,
    include_unpaid: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
    overrides: dict[str, ParsedNote] | None = None,
) -> list[OrderResult]:
    """Run the full pipeline: aggregate → filter → parse → cost.

    `overrides` is an optional dict mapping Order Number → manually-edited
    ParsedNote (from the Review queue). When provided for an order, the
    parser output is replaced wholesale.
    """
    overrides = overrides or {}
    aggregated = _aggregate_rows(df)
    sa_pool = settings.sa_list.active_names

    out: list[OrderResult] = []
    for row in aggregated:
        order_number = row["Order Number"]
        order_date = _parse_date(row["Date"])
        if date_from and order_date.date() < date_from:
            continue
        if date_to and order_date.date() > date_to:
            continue

        settlement_raw = (row.get("__settlement_date__") or "").strip()
        settlement_date: datetime | None = (
            _parse_date(settlement_raw) if settlement_raw else None
        )
        if settlement_date == datetime.min:
            settlement_date = None

        note_text = row["Note"] or ""
        # A seller-note "PAID ON <month> <year>" overrides the transaction
        # timestamps for payout-month attribution: the order counts in the
        # month its balance was actually settled, as stated by the seller.
        note_paid = _paid_month_from_note(note_text, order_date)
        if note_paid is not None:
            settlement_date = note_paid

        gross = _parse_total(row["Total Amount"])
        clearance_amount = settings.tiers.clearance_amount_from_note(note_text, gross)
        is_clearance = clearance_amount >= gross > 0  # fully clearance
        channel = row.get("Channel", "") or ""
        order_status = row.get("Order Status", "") or ""
        financial_status = row.get("Financial Status", "") or ""
        tags = _parse_tags(row.get(TAG_COL, "") or "")

        excluded_reason = _excluded_reason(
            order_status, financial_status, include_unpaid
        )

        if order_number in overrides:
            parsed = overrides[order_number]
        else:
            parsed = parse_seller_note(
                row["Note"] or "",
                order_total=gross,
                sa_list=sa_pool,
                channel=channel,
            )

        if excluded_reason:
            out.append(
                OrderResult(
                    order_number=order_number,
                    order_date=order_date,
                    settlement_date=settlement_date,
                    is_clearance=is_clearance,
                    clearance_amount=clearance_amount,
                    channel=channel,
                    financial_status=financial_status,
                    order_status=order_status,
                    gross_total=gross,
                    parsed=parsed,
                    tags=tags,
                    charges=[],
                    total_charges=0.0,
                    net_total=0.0,
                    excluded=True,
                    excluded_reason=excluded_reason,
                )
            )
            continue

        charge_lines, total_charges, net_total = calculate_charges(
            parsed.payments, settings.rates, order_date.date()
        )
        # User rule (refined): every order must appear in one of the three
        # tabs — Parsed, Review or Excluded — even if the parser couldn't
        # compute a net for it. Silently dropping orders breaks the user's
        # trust in the totals ("did the report miss anything?"). Orders that
        # couldn't be parsed surface in Review via the flags the parser
        # already set ("No payment method detected", etc.) and the Net
        # column reads RM 0 until the user fixes the data.
        out.append(
            OrderResult(
                order_number=order_number,
                order_date=order_date,
                settlement_date=settlement_date,
                is_clearance=is_clearance,
                clearance_amount=clearance_amount,
                channel=channel,
                financial_status=financial_status,
                order_status=order_status,
                gross_total=gross,
                parsed=parsed,
                tags=tags,
                charges=charge_lines,
                total_charges=total_charges,
                net_total=net_total,
                excluded=False,
                excluded_reason=None,
            )
        )
    out.sort(key=lambda o: o.order_date, reverse=True)
    return out
