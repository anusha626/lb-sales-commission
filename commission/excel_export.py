"""Build the multi-sheet Excel report.

Sheets produced:
  - Summary           : per-SA totals, tier, commission
  - One sheet per SA  : full audit trail of contributing orders
  - Review log        : orders that needed manual attention
  - Excluded          : orders excluded by status filters
  - Settings snapshot : the rate table, tiers, SA list at run time
"""
from __future__ import annotations

from io import BytesIO
from typing import Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .models import (
    CommissionReport,
    HouseSalesSummary,
    OrderResult,
    SACommission,
    SAContribution,
)
from .settings import AppSettings

_HEADER_FILL = PatternFill("solid", fgColor="1F2937")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_MONEY_FMT = '"RM"#,##0.00'
_PCT_FMT = "0.00\\%"


def _write_header(ws: Worksheet, headers: Sequence[str]) -> None:
    for col, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=col, value=h)
        c.fill = _HEADER_FILL
        c.font = _HEADER_FONT
        c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 22


def _autosize(ws: Worksheet, min_w: int = 10, max_w: int = 60) -> None:
    for col_idx, col_cells in enumerate(ws.columns, start=1):
        longest = 0
        for cell in col_cells:
            v = "" if cell.value is None else str(cell.value)
            longest = max(longest, len(v))
        ws.column_dimensions[get_column_letter(col_idx)].width = max(
            min_w, min(max_w, longest + 2)
        )


def _build_summary_sheet(
    ws: Worksheet,
    summaries: list[SACommission],
    house: HouseSalesSummary | None,
) -> None:
    ws.title = "Summary"
    headers = [
        "Sales Advisor",
        "# Orders",
        "Total Gross",
        "Total Net",
        "Avg Order",
        "Tier",
        "Tier Rate",
        "Commission (RM)",
    ]
    _write_header(ws, headers)
    for i, s in enumerate(summaries, start=2):
        ws.cell(row=i, column=1, value=s.sa_name)
        ws.cell(row=i, column=2, value=s.order_count)
        ws.cell(row=i, column=3, value=s.total_gross_sales).number_format = _MONEY_FMT
        ws.cell(row=i, column=4, value=s.total_net_sales).number_format = _MONEY_FMT
        ws.cell(row=i, column=5, value=s.avg_order_value).number_format = _MONEY_FMT
        ws.cell(row=i, column=6, value=s.tier_label)
        ws.cell(row=i, column=7, value=s.tier_rate_pct).number_format = _PCT_FMT
        ws.cell(row=i, column=8, value=s.commission_amount).number_format = _MONEY_FMT

    sa_count = len(summaries)
    if summaries:
        last = sa_count + 2
        ws.cell(row=last, column=1, value="SA TOTAL").font = Font(bold=True)
        ws.cell(row=last, column=2, value=sum(s.order_count for s in summaries)).font = Font(bold=True)
        for col, attr in [(3, "total_gross_sales"), (4, "total_net_sales"), (8, "commission_amount")]:
            cell = ws.cell(row=last, column=col, value=sum(getattr(s, attr) for s in summaries))
            cell.number_format = _MONEY_FMT
            cell.font = Font(bold=True)

    if house:
        # Visual gap, then a separate "House sales" row in italic.
        row = sa_count + 4
        ws.cell(row=row, column=1, value="House sales (COMPANY SALES — no commission)").font = Font(
            italic=True, bold=True
        )
        row += 1
        ws.cell(row=row, column=1, value="COMPANY SALES").font = Font(italic=True)
        ws.cell(row=row, column=2, value=house.order_count).font = Font(italic=True)
        c3 = ws.cell(row=row, column=3, value=house.total_gross_sales)
        c3.number_format = _MONEY_FMT
        c3.font = Font(italic=True)
        c4 = ws.cell(row=row, column=4, value=house.total_net_sales)
        c4.number_format = _MONEY_FMT
        c4.font = Font(italic=True)
        avg = round(house.total_gross_sales / house.order_count, 2) if house.order_count else 0.0
        c5 = ws.cell(row=row, column=5, value=avg)
        c5.number_format = _MONEY_FMT
        c5.font = Font(italic=True)
        ws.cell(row=row, column=6, value="House account — no commission").font = Font(italic=True)
        c8 = ws.cell(row=row, column=8, value=0.0)
        c8.number_format = _MONEY_FMT
        c8.font = Font(italic=True)

    _autosize(ws)


def _build_sa_sheet(
    ws: Worksheet,
    sa: SACommission,
    orders_by_number: dict[str, OrderResult],
) -> None:
    ws.title = f"SA - {sa.sa_name}"[:31]  # Excel sheet name limit
    headers = [
        "Order #",
        "Date",
        "Channel",
        "Order Gross",
        "Payment Method",
        "Last 4",
        "Portion Gross",
        "Rate Applied",
        "Charge Amount",
        "Portion Net",
        "Split %",
        "Contribution to SA (Net)",
    ]
    _write_header(ws, headers)
    row = 2
    for c in sa.contributions:
        order = orders_by_number.get(c.order_number)
        if order is None or order.excluded or not order.charges:
            ws.cell(row=row, column=1, value=c.order_number)
            ws.cell(row=row, column=2, value=c.order_date.strftime("%Y-%m-%d"))
            ws.cell(row=row, column=3, value=order.channel if order else "")
            ws.cell(row=row, column=4, value=order.gross_total if order else 0).number_format = _MONEY_FMT
            ws.cell(row=row, column=11, value=c.share_pct).number_format = "0.0%"
            ws.cell(row=row, column=12, value=c.net_share).number_format = _MONEY_FMT
            row += 1
            continue
        for ch in order.charges:
            ws.cell(row=row, column=1, value=order.order_number)
            ws.cell(row=row, column=2, value=order.order_date.strftime("%Y-%m-%d"))
            ws.cell(row=row, column=3, value=order.channel)
            ws.cell(row=row, column=4, value=order.gross_total).number_format = _MONEY_FMT
            ws.cell(row=row, column=5, value=ch.method.value)
            ws.cell(row=row, column=6, value=ch.last4 or "")
            ws.cell(row=row, column=7, value=ch.gross).number_format = _MONEY_FMT
            ws.cell(row=row, column=8, value=ch.rate_label)
            ws.cell(row=row, column=9, value=ch.charge).number_format = _MONEY_FMT
            ws.cell(row=row, column=10, value=ch.net).number_format = _MONEY_FMT
            ws.cell(row=row, column=11, value=c.share_pct).number_format = "0.0%"
            # Only emit the contribution figure on the first portion row of an order
            if ch is order.charges[0]:
                ws.cell(row=row, column=12, value=c.net_share).number_format = _MONEY_FMT
            row += 1

    # Totals
    ws.cell(row=row, column=1, value="TOTALS").font = Font(bold=True)
    ws.cell(row=row, column=12, value=sa.total_net_sales).number_format = _MONEY_FMT
    ws.cell(row=row, column=12).font = Font(bold=True)
    ws.cell(row=row + 1, column=11, value=f"Tier: {sa.tier_label}").font = Font(italic=True)
    ws.cell(row=row + 1, column=12, value=sa.commission_amount).number_format = _MONEY_FMT
    ws.cell(row=row + 1, column=12).font = Font(bold=True, color="0F766E")
    _autosize(ws)


def _build_review_sheet(ws: Worksheet, orders: list[OrderResult]) -> None:
    ws.title = "Review log"
    review = [o for o in orders if not o.excluded and o.parsed.needs_review]
    headers = ["Order #", "Date", "Gross", "Channel", "Note", "Flags"]
    _write_header(ws, headers)
    for i, o in enumerate(review, start=2):
        ws.cell(row=i, column=1, value=o.order_number)
        ws.cell(row=i, column=2, value=o.order_date.strftime("%Y-%m-%d"))
        ws.cell(row=i, column=3, value=o.gross_total).number_format = _MONEY_FMT
        ws.cell(row=i, column=4, value=o.channel)
        ws.cell(row=i, column=5, value=o.parsed.raw_note)
        ws.cell(row=i, column=6, value=" | ".join(o.parsed.review_flags))
    _autosize(ws, max_w=80)


def _build_excluded_sheet(ws: Worksheet, orders: list[OrderResult]) -> None:
    ws.title = "Excluded"
    excluded = [o for o in orders if o.excluded]
    headers = ["Order #", "Date", "Gross", "Channel", "Order Status", "Financial Status", "Reason"]
    _write_header(ws, headers)
    for i, o in enumerate(excluded, start=2):
        ws.cell(row=i, column=1, value=o.order_number)
        ws.cell(row=i, column=2, value=o.order_date.strftime("%Y-%m-%d"))
        ws.cell(row=i, column=3, value=o.gross_total).number_format = _MONEY_FMT
        ws.cell(row=i, column=4, value=o.channel)
        ws.cell(row=i, column=5, value=o.order_status)
        ws.cell(row=i, column=6, value=o.financial_status)
        ws.cell(row=i, column=7, value=o.excluded_reason or "")
    _autosize(ws)


def _build_settings_sheet(ws: Worksheet, settings: AppSettings) -> None:
    ws.title = "Settings snapshot"
    row = 1
    ws.cell(row=row, column=1, value="Sales Advisors").font = Font(bold=True, size=12)
    row += 1
    for sa in settings.sa_list.sas:
        ws.cell(row=row, column=1, value=sa.name)
        ws.cell(row=row, column=2, value="active" if sa.active else "inactive")
        row += 1

    row += 1
    ws.cell(row=row, column=1, value="Tiers").font = Font(bold=True, size=12)
    row += 1
    for t in settings.tiers.tiers:
        ws.cell(row=row, column=1, value=f"RM {t.min_net:,.2f}")
        ws.cell(row=row, column=2, value=f"RM {t.max_net:,.2f}" if t.max_net is not None else "and above")
        ws.cell(row=row, column=3, value=f"{t.rate_pct}%")
        row += 1

    row += 1
    ws.cell(row=row, column=1, value="Channel flat commissions").font = Font(bold=True, size=12)
    row += 1
    for r in settings.tiers.channel_flat_commissions:
        ws.cell(row=row, column=1, value=r.channel)
        ws.cell(row=row, column=2, value=r.amount_per_order).number_format = _MONEY_FMT
        ws.cell(row=row, column=3, value=r.label)
        row += 1

    row += 1
    ws.cell(row=row, column=1, value="Active rate version").font = Font(bold=True, size=12)
    row += 1
    if settings.rates.versions:
        v = max(settings.rates.versions, key=lambda v: v.effective_from)
        ws.cell(row=row, column=1, value=f"Effective from {v.effective_from.isoformat()}")
        row += 1
        ws.cell(row=row, column=1, value="SenangPay card").font = Font(italic=True)
        ws.cell(row=row, column=2, value=v.senangpay_card_pct).number_format = _PCT_FMT
        row += 1
        ws.cell(row=row, column=1, value="SenangPay FPX").font = Font(italic=True)
        ws.cell(row=row, column=2, value=v.senangpay_fpx_pct).number_format = _PCT_FMT
        row += 1
        for rate in v.rates:
            ws.cell(row=row, column=1, value=rate.label)
            if rate.rate_pct is not None:
                ws.cell(row=row, column=2, value=rate.rate_pct).number_format = _PCT_FMT
            else:
                ws.cell(row=row, column=2, value="(not configured)")
            row += 1
    _autosize(ws)


def _build_house_sheet(
    ws: Worksheet,
    house: HouseSalesSummary,
    orders_by_number: dict[str, OrderResult],
) -> None:
    """Audit-trail sheet for COMPANY SALES (no commission column)."""
    ws.title = "House - COMPANY SALES"[:31]
    headers = [
        "Order #",
        "Date",
        "Channel",
        "Order Gross",
        "Payment Method",
        "Last 4",
        "Portion Gross",
        "Rate Applied",
        "Charge Amount",
        "Portion Net",
        "Share %",
        "Net to House",
    ]
    _write_header(ws, headers)
    row = 2
    for c in house.contributions:
        order = orders_by_number.get(c.order_number)
        if order is None or order.excluded or not order.charges:
            ws.cell(row=row, column=1, value=c.order_number)
            ws.cell(row=row, column=2, value=c.order_date.strftime("%Y-%m-%d"))
            ws.cell(row=row, column=3, value=order.channel if order else "")
            ws.cell(row=row, column=4, value=order.gross_total if order else 0).number_format = _MONEY_FMT
            ws.cell(row=row, column=11, value=c.share_pct).number_format = "0.0%"
            ws.cell(row=row, column=12, value=c.net_share).number_format = _MONEY_FMT
            row += 1
            continue
        for ch in order.charges:
            ws.cell(row=row, column=1, value=order.order_number)
            ws.cell(row=row, column=2, value=order.order_date.strftime("%Y-%m-%d"))
            ws.cell(row=row, column=3, value=order.channel)
            ws.cell(row=row, column=4, value=order.gross_total).number_format = _MONEY_FMT
            ws.cell(row=row, column=5, value=ch.method.value)
            ws.cell(row=row, column=6, value=ch.last4 or "")
            ws.cell(row=row, column=7, value=ch.gross).number_format = _MONEY_FMT
            ws.cell(row=row, column=8, value=ch.rate_label)
            ws.cell(row=row, column=9, value=ch.charge).number_format = _MONEY_FMT
            ws.cell(row=row, column=10, value=ch.net).number_format = _MONEY_FMT
            ws.cell(row=row, column=11, value=c.share_pct).number_format = "0.0%"
            if ch is order.charges[0]:
                ws.cell(row=row, column=12, value=c.net_share).number_format = _MONEY_FMT
            row += 1
    ws.cell(row=row, column=1, value="TOTAL HOUSE NET").font = Font(bold=True)
    ws.cell(row=row, column=12, value=house.total_net_sales).number_format = _MONEY_FMT
    ws.cell(row=row, column=12).font = Font(bold=True)
    _autosize(ws)


def build_workbook(
    orders: list[OrderResult],
    report: CommissionReport,
    settings: AppSettings,
) -> bytes:
    """Render the full report and return raw .xlsx bytes."""
    wb = Workbook()
    summary_ws = wb.active
    _build_summary_sheet(summary_ws, report.sa_summaries, report.house)

    by_number = {o.order_number: o for o in orders}
    for s in report.sa_summaries:
        ws = wb.create_sheet()
        _build_sa_sheet(ws, s, by_number)

    if report.house:
        _build_house_sheet(wb.create_sheet(), report.house, by_number)

    _build_review_sheet(wb.create_sheet(), orders)
    _build_excluded_sheet(wb.create_sheet(), orders)
    _build_settings_sheet(wb.create_sheet(), settings)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
