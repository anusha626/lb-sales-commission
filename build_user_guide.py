"""Build the LB Commission Calculator User Guide PDF.

One-shot generator. Run once to produce LB_Commission_User_Guide.pdf.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


OUT = Path(__file__).parent / "LB_Commission_User_Guide.pdf"
APP_URL = "https://lb-sales-commission.streamlit.app"
APP_PASSWORD = "Lbitesa88"

# ---- Styles ----------------------------------------------------------------
styles = getSampleStyleSheet()

H1 = ParagraphStyle(
    "H1",
    parent=styles["Heading1"],
    fontSize=20,
    leading=24,
    spaceAfter=4,
    textColor=colors.HexColor("#0F172A"),
)
SUBTITLE = ParagraphStyle(
    "Subtitle",
    parent=styles["Normal"],
    fontSize=10.5,
    leading=14,
    textColor=colors.HexColor("#475569"),
    spaceAfter=12,
)
H2 = ParagraphStyle(
    "H2",
    parent=styles["Heading2"],
    fontSize=14,
    leading=18,
    spaceBefore=12,
    spaceAfter=4,
    textColor=colors.HexColor("#0F766E"),
)
STEP_TITLE = ParagraphStyle(
    "StepTitle",
    parent=styles["Heading3"],
    fontSize=12,
    leading=16,
    spaceBefore=10,
    spaceAfter=4,
    textColor=colors.HexColor("#0F172A"),
)
BODY = ParagraphStyle(
    "Body",
    parent=styles["Normal"],
    fontSize=10.5,
    leading=15,
    alignment=TA_LEFT,
    spaceAfter=4,
)
BULLET = ParagraphStyle(
    "Bullet",
    parent=BODY,
    leftIndent=14,
    bulletIndent=2,
)
NOTE = ParagraphStyle(
    "Note",
    parent=BODY,
    fontSize=9.5,
    leading=13,
    backColor=colors.HexColor("#FEF3C7"),
    borderColor=colors.HexColor("#F59E0B"),
    borderWidth=0.5,
    borderPadding=6,
    spaceBefore=4,
    spaceAfter=8,
    leftIndent=0,
)
TIP = ParagraphStyle(
    "Tip",
    parent=BODY,
    fontSize=9.5,
    leading=13,
    backColor=colors.HexColor("#DBEAFE"),
    borderColor=colors.HexColor("#3B82F6"),
    borderWidth=0.5,
    borderPadding=6,
    spaceBefore=4,
    spaceAfter=8,
)
CODE_LIKE = ParagraphStyle(
    "CodeLike",
    parent=BODY,
    fontName="Courier",
    fontSize=9.5,
    backColor=colors.HexColor("#F1F5F9"),
    borderPadding=4,
    leftIndent=4,
)


def step(num: int, title: str) -> Paragraph:
    return Paragraph(
        f"<b>Step {num} &middot; {title}</b>",
        STEP_TITLE,
    )


def bullet(text: str) -> Paragraph:
    return Paragraph(f"&bull; {text}", BULLET)


def divider() -> HRFlowable:
    return HRFlowable(
        width="100%",
        thickness=0.5,
        color=colors.HexColor("#CBD5E1"),
        spaceBefore=8,
        spaceAfter=8,
    )


# ---- Build the story -------------------------------------------------------

story: list = []

# --- Cover ---
story.append(Paragraph("LB Commission Calculator", H1))
story.append(
    Paragraph(
        f"Step-by-step user guide &middot; Generated {date.today().isoformat()}",
        SUBTITLE,
    )
)
story.append(divider())

# Quick info table
info = Table(
    [
        ["App URL", APP_URL],
        ["Password", APP_PASSWORD],
        ["Source data", "EasyStore order export (.csv)"],
        ["Output", "Multi-sheet Excel report (.xlsx)"],
    ],
    colWidths=[40 * mm, 110 * mm],
    hAlign="LEFT",
)
info.setStyle(
    TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#475569")),
            ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]
    )
)
story.append(info)
story.append(Spacer(1, 8))
story.append(
    Paragraph(
        "<b>What this guide covers:</b> the full monthly workflow &mdash; "
        "from downloading the EasyStore CSV, through reviewing parsed orders "
        "and fixing flagged items, to downloading the final commission Excel "
        "for finance.",
        BODY,
    )
)
story.append(Spacer(1, 6))
story.append(
    Paragraph(
        "Read once end-to-end. After the first run you will only need this "
        "guide for the unusual cases.",
        BODY,
    )
)

# --- Part 1: get the CSV ---
story.append(H_intro := Paragraph("Part 1 &middot; Download the EasyStore CSV", H2))

story.append(step(1, "Sign in to EasyStore"))
story.append(
    Paragraph(
        "Open <b>admin.easystore.co</b> in a browser and sign in with your "
        "EasyStore admin account.",
        BODY,
    )
)

story.append(step(2, "Go to Orders &rarr; Export"))
story.append(bullet("In the left sidebar, click <b>Orders</b>."))
story.append(bullet("Top-right of the orders table, click <b>Export</b>."))
story.append(
    bullet(
        "Choose the date range for the month you are running commission for. "
        "(Easiest: pick the first to the last day of the previous calendar month.)"
    )
)
story.append(bullet("Leave the format on <b>CSV</b>."))
story.append(bullet("Click <b>Export</b>. EasyStore will email you the file or download it directly &mdash; usually within a minute."))

story.append(step(3, "Save the CSV somewhere you'll find it"))
story.append(
    Paragraph(
        "The file will be named something like "
        "<font face='Courier'>Export_Orders_YYYYMMDD_HHMMSS.csv</font>. "
        "Keep it in your <b>Downloads</b> folder &mdash; the next part uploads it directly.",
        BODY,
    )
)

# --- Part 2: open the app ---
story.append(Paragraph("Part 2 &middot; Open the Commission Calculator", H2))

story.append(step(4, "Open the app"))
story.append(
    Paragraph(
        f"In Chrome, go to <b>{APP_URL}</b>",
        BODY,
    )
)
story.append(
    Paragraph(
        "Tip: bookmark this URL so you don't have to type it every month.",
        TIP,
    )
)

story.append(step(5, "Sign in with the shared password"))
story.append(bullet(f"You will see a sign-in box. Enter the password: <b>{APP_PASSWORD}</b>"))
story.append(bullet("Click <b>Sign in</b>."))
story.append(
    Paragraph(
        "<b>Do not share the password publicly.</b> If a colleague leaves the team, "
        "ask the admin to rotate the password.",
        NOTE,
    )
)

story.append(PageBreak())

# --- Part 3: upload + review ---
story.append(Paragraph("Part 3 &middot; Upload the CSV and review", H2))

story.append(step(6, "Upload the CSV"))
story.append(bullet("Make sure you are on the <b>Upload &amp; Review</b> page (selected by default in the left sidebar)."))
story.append(bullet("Click the <b>Upload</b> button under the heading <b>EasyStore order export (CSV)</b>."))
story.append(bullet("Select the CSV you downloaded earlier and click <b>Open</b>."))
story.append(bullet("You will see a green message confirming how many rows were loaded."))

story.append(step(7, "Set the date range"))
story.append(
    Paragraph(
        "Just below the upload box, two date fields appear: <b>From</b> and <b>To</b>. "
        "By default they point to last calendar month. Adjust if you need a different period.",
        BODY,
    )
)
story.append(
    Paragraph(
        "<b>Important:</b> the date filter uses the order's <i>Date</i> column from "
        "EasyStore. Orders outside this range will not appear in any tab.",
        NOTE,
    )
)

story.append(step(8, "Read the four metric cards"))
story.append(
    Paragraph(
        "Across the top you will see four numbers:",
        BODY,
    )
)
metric_table = Table(
    [
        ["Orders in range", "How many orders fall in the date range"],
        ["Parsed cleanly", "Orders that were fully understood by the engine"],
        ["Need review", "Orders the engine couldn't fully cost &mdash; you must check"],
        ["Excluded", "Cancelled or unpaid orders (no commission impact)"],
    ],
    colWidths=[40 * mm, 110 * mm],
)
metric_table.setStyle(
    TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F1F5F9")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]
    )
)
story.append(metric_table)
story.append(Spacer(1, 8))

story.append(step(9, "Skim the Parsed orders tab"))
story.append(
    Paragraph(
        "This is the default tab. Each row is one order. Confirm:",
        BODY,
    )
)
story.append(bullet("The SA(s) column shows the right name(s) and percentages."))
story.append(bullet("The Payments column shows the methods you expect (cash, card, etc.)."))
story.append(bullet("The Net column matches your expectation for that order."))
story.append(
    Paragraph(
        "If a single row looks wrong, check the seller note in EasyStore for that "
        "order &mdash; usually a typo in the note.",
        BODY,
    )
)

story.append(step(10, "Fix anything in the Review queue"))
story.append(
    Paragraph(
        "Click the <b>Review queue</b> tab. Anything here needs your attention "
        "before the report is correct.",
        BODY,
    )
)
story.append(
    Paragraph(
        "For each order:",
        BODY,
    )
)
story.append(bullet("Click the order's row to expand it."))
story.append(bullet("Read the seller note shown at the top of the expanded row."))
story.append(
    bullet(
        "In the <b>Sales Advisor</b> table, set the right SA name(s) and "
        "share %. Shares must total 100."
    )
)
story.append(
    bullet(
        "In the <b>Method / Last 4 / Amount / Foreign</b> table, fix the payment "
        "details. The Amount column should sum to the order total."
    )
)
story.append(
    bullet(
        "Click <b>Save override</b>. The order is now corrected for this run."
    )
)
story.append(
    Paragraph(
        "Overrides apply to <b>this session only</b>. If you re-upload the CSV "
        "or close the browser, the corrections must be redone. The fix is to "
        "ask the SA to update the seller note in EasyStore for next month.",
        TIP,
    )
)

story.append(step(11, "Glance at the Excluded tab"))
story.append(
    Paragraph(
        "Orders here are <b>not</b> in the report. Common reasons: cancelled by "
        "customer, unpaid, or not yet fulfilled. If something is excluded that "
        "should count, fix the order's status in EasyStore and re-export the CSV.",
        BODY,
    )
)

story.append(PageBreak())

# --- Part 4: report ---
story.append(Paragraph("Part 4 &middot; Generate the Commission Report", H2))

story.append(step(12, "Switch to the Commission Report page"))
story.append(
    Paragraph(
        "In the left sidebar, click <b>Commission Report</b>.",
        BODY,
    )
)

story.append(step(13, "Read the top metrics and bar chart"))
story.append(bullet("Four header metrics: SAs with sales / total gross / total net / total commission."))
story.append(bullet("Bar chart shows net sales by SA at a glance."))

story.append(step(14, "Review each SA's card"))
story.append(
    Paragraph(
        "Below the chart, each Sales Advisor has their own card showing:",
        BODY,
    )
)
story.append(bullet("Tier label (e.g. <i>RM 0 &ndash; RM 200,000 @ 0.8%</i>)"))
story.append(bullet("Order count and average order value"))
story.append(bullet("Gross / Net totals"))
story.append(bullet("Commission earned for the month"))
story.append(
    Paragraph(
        "Click <b>Order-by-order breakdown</b> to expand the per-order detail. "
        "The seven columns are:",
        BODY,
    )
)
cols = Table(
    [
        ["1", "Order #", "EasyStore order number"],
        ["2", "Date", "Order date"],
        ["3", "Share %", "How much of this order belongs to this SA"],
        ["4", "Gross share", "Order gross &times; share %"],
        ["5", "Charges", "SA's slice of the bank/SenangPay fees"],
        ["6", "Net share", "Gross share &minus; charges"],
        ["7", "Payment method", "Compact summary of all payment portions"],
    ],
    colWidths=[10 * mm, 32 * mm, 108 * mm],
)
cols.setStyle(
    TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (1, -1), colors.HexColor("#F1F5F9")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
    )
)
story.append(cols)
story.append(Spacer(1, 8))

story.append(step(15, "Check the House sales section"))
story.append(
    Paragraph(
        "Below the SA cards, a <b>House sales (COMPANY SALES &mdash; no commission)</b> "
        "section shows orders attributed to the house account. These are tracked "
        "for revenue visibility but earn no commission.",
        BODY,
    )
)

story.append(step(16, "Download the Excel report"))
story.append(bullet("Scroll to the bottom of the Commission Report page."))
story.append(bullet("Click the <b>Download Excel Report</b> button."))
story.append(bullet("The file is named <font face='Courier'>commission_report_YYYYMMDD_HHMMSS.xlsx</font>."))
story.append(
    Paragraph(
        "The Excel workbook has multiple sheets:",
        BODY,
    )
)
story.append(bullet("<b>Summary</b> &mdash; one row per SA plus a totals row, plus a House row at the bottom."))
story.append(bullet("<b>SA &ndash; &lt;Name&gt;</b> &mdash; one sheet per SA with full audit trail (every order, every payment portion, the rate row applied, the charge, the net, the SA's share)."))
story.append(bullet("<b>House &ndash; COMPANY SALES</b> &mdash; equivalent audit trail for house orders."))
story.append(bullet("<b>Review log</b> &mdash; orders that needed manual fixing this run."))
story.append(bullet("<b>Excluded</b> &mdash; orders excluded by the cancelled/unpaid filter."))
story.append(bullet("<b>Settings snapshot</b> &mdash; SA list, tier brackets, channel flat rules, and the rate table version used. Useful for audit."))

story.append(step(17, "Sign out (optional)"))
story.append(
    Paragraph(
        "On a shared computer, click <b>Sign out</b> in the left sidebar before "
        "leaving. On your own laptop, you can leave the session open.",
        BODY,
    )
)

story.append(PageBreak())

# --- Part 5: troubleshooting ---
story.append(Paragraph("Part 5 &middot; Common questions", H2))

story.append(STEP_question1 := Paragraph("<b>What does &ldquo;Need review&rdquo; mean exactly?</b>", STEP_TITLE))
story.append(
    Paragraph(
        "The engine couldn't confidently parse the seller note. Common causes:",
        BODY,
    )
)
story.append(bullet("The SA name is misspelled in the note."))
story.append(bullet("The payment method is ambiguous (e.g. just <i>SENANGPAY</i> with no card/FPX hint)."))
story.append(bullet("Multiple payment portions in the note don't add up to the order total."))
story.append(
    Paragraph(
        "House-only orders (100% COMPANY SALES) with parser issues are <b>not</b> "
        "shown here, because they don't affect any SA's commission.",
        BODY,
    )
)

story.append(Paragraph("<b>Why is an order missing from the report?</b>", STEP_TITLE))
story.append(bullet("It's outside the date range you set."))
story.append(bullet("It's <i>Cancelled</i> or unpaid &mdash; check the <b>Excluded</b> tab."))
story.append(
    bullet(
        "Its calculated net = RM 0 (typically empty notes or RM 2.00 test "
        "orders). These are dropped silently to keep the report clean."
    )
)

story.append(Paragraph("<b>Why does the Net column not match the order total?</b>", STEP_TITLE))
story.append(
    Paragraph(
        "Net = Gross &minus; bank/SenangPay charges. Cards incur 0.45% &ndash; 2.5% depending "
        "on scheme; bank transfers, cash, trade-ins, MyDebit, TouchNGo and TikTok "
        "incur 0% in our rate setup.",
        BODY,
    )
)

story.append(Paragraph("<b>Why does TikTok-shop look different?</b>", STEP_TITLE))
story.append(
    Paragraph(
        "TikTok orders use the seller-note amount as net (already after TikTok's "
        "platform fee), and earn a flat <b>RM 10 per order</b> commission instead "
        "of the percentage tier.",
        BODY,
    )
)

story.append(Paragraph("<b>The seller wrote the wrong amount in the note &mdash; what happens?</b>", STEP_TITLE))
story.append(
    Paragraph(
        "The engine trusts the EasyStore-calculated <b>Total Amount</b> column over "
        "the typed amount. So if the note says <i>CASH RM750</i> but the order total "
        "is RM 950, the engine will record cash = RM 950 automatically.",
        BODY,
    )
)

story.append(Paragraph("<b>The bank rate changed &mdash; how do I update?</b>", STEP_TITLE))
story.append(
    Paragraph(
        "Go to the <b>Settings</b> page in the app &rarr; <b>Card rates</b> tab. "
        "Either edit the active rate version, or click <b>Add new version</b> "
        "with a new <i>effective from</i> date so historic months use the old rates.",
        BODY,
    )
)
story.append(
    Paragraph(
        "On Streamlit Cloud's free tier, edits made through the Settings page do not "
        "survive the next redeploy. The admin should also edit "
        "<font face='Courier'>data/rates.json</font> in the GitHub repo and push, "
        "so the rate persists.",
        NOTE,
    )
)

# --- Footer note ---
story.append(divider())
story.append(
    Paragraph(
        "<i>Engine version: 1.0 &middot; Built by LB International finance &middot; "
        "Source: github.com/anusha626/lb-sales-commission</i>",
        ParagraphStyle(
            "Footer",
            parent=BODY,
            fontSize=9,
            textColor=colors.HexColor("#94A3B8"),
            alignment=TA_LEFT,
        ),
    )
)


# ---- Render ---------------------------------------------------------------

doc = SimpleDocTemplate(
    str(OUT),
    pagesize=A4,
    leftMargin=18 * mm,
    rightMargin=18 * mm,
    topMargin=18 * mm,
    bottomMargin=18 * mm,
    title="LB Commission Calculator User Guide",
    author="LB International",
)
doc.build(story)
print(f"Wrote {OUT.name} ({OUT.stat().st_size:,} bytes)")
