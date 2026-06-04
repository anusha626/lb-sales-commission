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


def _aggregate_rows(df: pd.DataFrame) -> list[dict]:
    """Collapse a multi-row order export to one record per Order Number.

    EasyStore writes split-payment / multi-line orders as several rows; only
    the first carries metadata (Note, Order Status, Financial Status, Total).
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
        out.append(head.to_dict())
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

        gross = _parse_total(row["Total Amount"])
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
