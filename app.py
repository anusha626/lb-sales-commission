"""Streamlit UI for the LB International sales-commission calculator.

Three pages:
  1. Upload & Review  — load CSV, fix flagged orders, see parsed/excluded data
  2. Commission Report — per-SA cards, chart, Excel download
  3. Settings          — edit SAs, rate card, tier brackets, channel flat rules

Streamlit is only used for UI glue. All calculation lives in `commission/*`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from pathlib import Path

from commission.aggregator import build_order_results, read_easystore_csv
from commission.commission_engine import (
    apply_overachievement_bonuses,
    compute_commissions,
)
from commission.excel_export import build_workbook
from commission.github_sync import GitHubConfig, push_local_path
from commission.models import (
    OrderResult,
    ParsedNote,
    PaymentMethod,
    PaymentPortion,
    SAShare,
)
from commission.parser import HOUSE_ACCOUNT, parse_seller_note
from commission.settings import (
    AppSettings,
    ChannelFlatRule,
    CommissionTier,
    RATES_FILE,
    RateRow,
    RateTableVersion,
    SARecord,
    SA_FILE,
    TIERS_FILE,
    load_all,
    save_rates,
    save_sa_list,
    save_tiers,
)

PROJECT_ROOT = Path(__file__).parent

st.set_page_config(
    page_title="LB Commission Calculator",
    page_icon="💼",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Password gate
# ---------------------------------------------------------------------------
# The expected password is read from Streamlit secrets (Streamlit Cloud's
# Secrets manager, or a local .streamlit/secrets.toml file). If no secret is
# configured, the gate is bypassed — that lets you develop locally without
# typing a password every reload, while production deployments stay protected.

def _password_required() -> bool:
    try:
        return bool(st.secrets.get("app_password", ""))
    except Exception:
        return False


def _github_config() -> GitHubConfig:
    """Read GitHub-sync credentials from Streamlit secrets, if present."""
    try:
        return GitHubConfig(
            pat=str(st.secrets.get("github_pat", "")),
            repo=str(st.secrets.get("github_repo", "")),
            branch=str(st.secrets.get("github_branch", "main")),
        )
    except Exception:
        return GitHubConfig(pat="", repo="")


def _save_and_sync(local_path: Path, what_changed: str) -> None:
    """Render Save feedback. If GitHub creds are configured, also push the
    local JSON to the repo so the change survives container restarts on
    Streamlit Cloud's ephemeral disk."""
    cfg = _github_config()
    if not cfg.configured:
        st.success(f"{what_changed} saved locally.")
        st.warning(
            "⚠ This change **won't survive an app restart** on Streamlit Cloud's "
            "free tier. To make settings permanent, an admin must add a GitHub "
            "Personal Access Token to Streamlit Secrets — see DEPLOY.md → "
            "*Persistent Settings*."
        )
        return
    with st.spinner("Syncing to GitHub…"):
        result = push_local_path(
            cfg, local_path, PROJECT_ROOT, f"Settings update: {what_changed}"
        )
    if result.ok:
        st.success(
            f"{what_changed} saved & synced to GitHub. "
            "App will redeploy with the new settings in ~1 minute."
        )
    else:
        st.error(
            f"{what_changed} saved locally, but GitHub sync failed: {result.message}"
        )


def _check_password() -> bool:
    """Return True if the visitor is authorised to use the app."""
    if not _password_required():
        return True
    if st.session_state.get("authenticated"):
        return True

    st.title("💼 LB Commission Calculator")
    st.caption("Enter the shared password to continue.")
    with st.form("login_form", clear_on_submit=False):
        pw = st.text_input("Password", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("Sign in")
        if submitted:
            try:
                expected = st.secrets["app_password"]
            except Exception:
                expected = ""
            if pw and pw == expected:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_money(v: float | None) -> str:
    if v is None:
        return ""
    return f"RM {v:,.2f}"


def _format_payment_summary(order) -> str:
    """Compact one-cell summary of all payment portions on an order."""
    if not order or not order.parsed.payments:
        return ""
    parts: list[str] = []
    for p in order.parsed.payments:
        s = p.method.value
        if p.last4:
            s += f" *{p.last4}"
        parts.append(s)
    return ", ".join(parts)


_LOCATION_RE = __import__("re").compile(r"\b(PJ|PG|KL)\b")
_LOCATION_TAGS = {"PJ", "PG", "KL"}


def _render_reconciliation(
    *,
    df: pd.DataFrame,
    date_from: date,
    date_to: date,
    in_range_count: int,
    parsed_clean_count: int,
    review_count: int,
    excluded_count: int,
) -> None:
    """Render an integrity panel: shows the user that every order in the
    uploaded CSV is accounted for — either in-range (and then in one of
    parsed / review / excluded) or out-of-range (filtered by the date
    picker)."""
    total_rows = len(df)
    if "Order Number" in df.columns:
        unique_orders = df["Order Number"].nunique()
    else:
        unique_orders = total_rows
    out_of_range = unique_orders - in_range_count
    sum_check = parsed_clean_count + review_count + excluded_count
    range_ok = sum_check == in_range_count
    total_ok = (in_range_count + out_of_range) == unique_orders

    badge = "✅ all orders accounted for" if (range_ok and total_ok) else "⚠️ count mismatch — please check"
    with st.expander(f"Order count reconciliation — {badge}", expanded=not (range_ok and total_ok)):
        rows = [
            ("CSV rows uploaded", total_rows, ""),
            ("Unique orders in CSV", unique_orders, "after collapsing split-payment rows"),
            (
                "Outside date range",
                out_of_range,
                f"order date not between {date_from} and {date_to}",
            ),
            ("In date range", in_range_count, "appears below in one of the three tabs"),
            ("  → Parsed cleanly", parsed_clean_count, ""),
            ("  → Needs review", review_count, ""),
            ("  → Excluded (cancelled / unpaid)", excluded_count, ""),
            (
                "  Sum check",
                sum_check,
                "✅ matches in-range" if range_ok else "⚠️ does NOT match in-range",
            ),
        ]
        st.dataframe(
            pd.DataFrame(rows, columns=["What", "Count", "Note"]),
            hide_index=True,
            use_container_width=True,
            column_config={
                "Count": st.column_config.NumberColumn(format="%d"),
            },
        )
        if not range_ok:
            st.error(
                f"Parsed + Review + Excluded = {sum_check}, but Orders in range = {in_range_count}. "
                "Something is being lost in the pipeline — please report."
            )


def _detect_locations(order) -> str:
    """Surface the store location for an order.

    Preferred source: EasyStore's Tag column (structured data, set by the
    seller via the tags chip UI — much more reliable than free-text). Falls
    back to a regex on the seller note for older orders that aren't tagged
    yet. Returns "" if neither yields a match (typical for online orders).
    """
    if order is None:
        return ""
    # 1. Tags (most reliable)
    from_tags = sorted({t for t in order.tags if t in _LOCATION_TAGS})
    if from_tags:
        return " + ".join(from_tags)
    # 2. Note fallback
    note = order.parsed.raw_note
    if not note:
        return ""
    found = sorted({m.group(1) for m in _LOCATION_RE.finditer(note.upper())})
    return " + ".join(found)


def _contribution_row(contribution, order, *, tier_rate_pct=None, tiers_cfg=None) -> dict:
    """One row in the per-SA / house breakdown table.

    The SA's slice of the order's bank charges = order total charges × share.
    When `tier_rate_pct` is given (SA rows), also show the actual commission
    this order earns and a Type marker — so a clearance order visibly earns the
    flat RM amount, not the tier %.
    """
    if order is not None:
        charges_share = round(order.total_charges * contribution.share_pct, 2)
    else:
        # Fall back to gross - net so the row stays consistent if the
        # underlying OrderResult somehow can't be looked up.
        charges_share = round(contribution.gross_share - contribution.net_share, 2)
    row = {
        "Order #": contribution.order_number,
        "Date": contribution.order_date.strftime("%Y-%m-%d"),
        "Share %": f"{contribution.share_pct * 100:.0f}%",
        "Gross share": contribution.gross_share,
        "Charges": charges_share,
        "Net share": contribution.net_share,
        "Payment method": _format_payment_summary(order),
        "Location": _detect_locations(order),
    }
    if tier_rate_pct is not None:
        is_clearance = bool(order and getattr(order, "is_clearance", False))
        flat_rule = (
            tiers_cfg.flat_rule_for(order.channel)
            if (order is not None and tiers_cfg is not None)
            else None
        )
        if is_clearance and tiers_cfg is not None:
            commission = round(tiers_cfg.clearance_flat_amount * contribution.share_pct, 2)
            kind = "Clearance (flat)"
        elif flat_rule is not None:
            commission = round(flat_rule.amount_per_order * contribution.share_pct, 2)
            kind = f"{flat_rule.label or 'Flat'} (flat)"
        else:
            commission = round(contribution.net_share * tier_rate_pct / 100.0, 2)
            kind = ""
        row["Commission"] = commission
        row["Type"] = kind
    return row


def previous_month_range(today: date) -> tuple[date, date]:
    first_this_month = today.replace(day=1)
    last_prev_month = first_this_month - timedelta(days=1)
    first_prev_month = last_prev_month.replace(day=1)
    return first_prev_month, last_prev_month


def _ensure_state() -> None:
    st.session_state.setdefault("settings", load_all())
    st.session_state.setdefault("df", None)
    st.session_state.setdefault("orders", None)
    st.session_state.setdefault("overrides", {})  # order_number -> ParsedNote
    # order_number -> date the payment cleared, entered by hand for paid
    # orders whose export carries no transaction date.
    st.session_state.setdefault("settlement_overrides", {})


def _reload_settings() -> None:
    st.session_state["settings"] = load_all()


def _recompute_orders(
    *,
    include_unpaid: bool,
    date_from: date | None,
    date_to: date | None,
) -> None:
    df = st.session_state.get("df")
    if df is None:
        st.session_state["orders"] = None
        return
    settings: AppSettings = st.session_state["settings"]
    orders = build_order_results(
        df,
        settings,
        include_unpaid=include_unpaid,
        date_from=date_from,
        date_to=date_to,
        overrides=st.session_state["overrides"],
    )
    st.session_state["orders"] = orders


# ---------------------------------------------------------------------------
# Page 1: Upload & Review
# ---------------------------------------------------------------------------

def page_upload() -> None:
    st.title("Upload & Review")
    st.caption(
        "Upload an EasyStore order export. Orders are aggregated by Order "
        "Number, then the seller note in each order is parsed for SA, split, "
        "and payment breakdown."
    )

    settings: AppSettings = st.session_state["settings"]

    upl = st.file_uploader("EasyStore order export (CSV)", type=["csv"])
    if upl is not None:
        try:
            df = read_easystore_csv(upl.getvalue())
            st.session_state["df"] = df
            st.success(f"Loaded {len(df)} rows.")
        except Exception as e:
            st.error(f"Couldn't read CSV: {e}")
            return

    if st.session_state["df"] is None:
        st.info("Drop a CSV above to get started.")
        return

    df = st.session_state["df"]

    today = date.today()
    default_from, default_to = previous_month_range(today)
    col1, col2, col3 = st.columns([1.2, 1.2, 1])
    with col1:
        date_from = st.date_input("From", value=default_from)
    with col2:
        date_to = st.date_input("To", value=default_to)
    with col3:
        include_unpaid = st.checkbox("Include unpaid (forecast)", value=False)

    _recompute_orders(
        include_unpaid=include_unpaid, date_from=date_from, date_to=date_to
    )
    orders = st.session_state["orders"] or []

    if not orders:
        st.warning("No orders fall in this date range.")
        return

    parsed_orders = [o for o in orders if not o.excluded]
    review_orders = [o for o in parsed_orders if o.needs_review]
    excluded_orders = [o for o in orders if o.excluded]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Orders in range", len(orders))
    m2.metric("Parsed cleanly", len(parsed_orders) - len(review_orders))
    m3.metric("Need review", len(review_orders))
    m4.metric("Excluded", len(excluded_orders))

    # Integrity check: show the user how every CSV order is accounted for so
    # they can audit without doing the arithmetic in their head.
    _render_reconciliation(
        df=df,
        date_from=date_from,
        date_to=date_to,
        in_range_count=len(orders),
        parsed_clean_count=len(parsed_orders) - len(review_orders),
        review_count=len(review_orders),
        excluded_count=len(excluded_orders),
    )

    tab_parsed, tab_review, tab_excl = st.tabs(
        ["Parsed orders", f"Review queue ({len(review_orders)})", f"Excluded ({len(excluded_orders)})"]
    )

    with tab_parsed:
        rows = []
        for o in parsed_orders:
            sa_str = " + ".join(
                f"{s.name} {s.share*100:.0f}%" for s in o.parsed.sa_shares
            ) or "(none)"
            pay_str = " | ".join(
                f"{p.method.value}"
                + (f" *{p.last4}" if p.last4 else "")
                + (f" {fmt_money(p.amount)}" if p.amount is not None else "")
                for p in o.parsed.payments
            ) or "(none)"
            rows.append(
                {
                    "Order #": o.order_number,
                    "Date": o.order_date.strftime("%Y-%m-%d"),
                    "Channel": o.channel,
                    "SA(s)": sa_str,
                    "Gross": o.gross_total,
                    "Charges": o.total_charges,
                    "Net": o.net_total,
                    "Payments": pay_str,
                }
            )
        if rows:
            df_view = pd.DataFrame(rows)
            st.dataframe(
                df_view,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Gross": st.column_config.NumberColumn(format="RM %.2f"),
                    "Charges": st.column_config.NumberColumn(format="RM %.2f"),
                    "Net": st.column_config.NumberColumn(format="RM %.2f"),
                },
            )
        else:
            st.info("No parsed orders.")

    with tab_review:
        if not review_orders:
            st.success("Nothing in the review queue.")
        else:
            st.caption(
                "These orders need a manual fix. Edit any field below; the "
                "engine will re-cost the order with your override when you "
                "click Save."
            )
            sa_options = settings.sa_list.active_names + [HOUSE_ACCOUNT]
            method_options = [m.value for m in PaymentMethod]
            for o in review_orders:
                with st.expander(
                    f"#{o.order_number} • {o.order_date.strftime('%Y-%m-%d')} "
                    f"• {fmt_money(o.gross_total)} • flags: "
                    + " / ".join(o.parsed.review_flags),
                    expanded=False,
                ):
                    st.code(o.parsed.raw_note or "(empty)", language="text")
                    _review_editor(o, sa_options, method_options)

    with tab_excl:
        if not excluded_orders:
            st.info("Nothing was excluded.")
        else:
            erows = [
                {
                    "Order #": o.order_number,
                    "Date": o.order_date.strftime("%Y-%m-%d"),
                    "Gross": o.gross_total,
                    "Channel": o.channel,
                    "Order Status": o.order_status,
                    "Financial Status": o.financial_status,
                    "Reason": o.excluded_reason or "",
                }
                for o in excluded_orders
            ]
            st.dataframe(
                pd.DataFrame(erows),
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Gross": st.column_config.NumberColumn(format="RM %.2f")
                },
            )


def _review_editor(
    order, sa_options: list[str], method_options: list[str]
) -> None:
    """Inline editor for one review-queue order."""
    on = order.order_number
    parsed = order.parsed

    # SA shares editor
    sa_rows = (
        [{"Sales Advisor": s.name, "Share %": s.share * 100} for s in parsed.sa_shares]
        if parsed.sa_shares
        else [{"Sales Advisor": sa_options[0] if sa_options else "", "Share %": 100.0}]
    )
    sa_df = st.data_editor(
        pd.DataFrame(sa_rows),
        num_rows="dynamic",
        key=f"sa_editor_{on}",
        column_config={
            "Sales Advisor": st.column_config.SelectboxColumn(
                options=sa_options, required=True
            ),
            "Share %": st.column_config.NumberColumn(min_value=0, max_value=100, step=1),
        },
        use_container_width=True,
    )

    # Payments editor
    pay_rows = [
        {
            "Method": p.method.value,
            "Last 4": p.last4 or "",
            "Amount": p.amount or 0.0,
            "Foreign": p.is_foreign,
        }
        for p in parsed.payments
    ] or [{"Method": "CASH", "Last 4": "", "Amount": order.gross_total, "Foreign": False}]
    pay_df = st.data_editor(
        pd.DataFrame(pay_rows),
        num_rows="dynamic",
        key=f"pay_editor_{on}",
        column_config={
            "Method": st.column_config.SelectboxColumn(options=method_options, required=True),
            "Amount": st.column_config.NumberColumn(format="RM %.2f", min_value=0),
            "Foreign": st.column_config.CheckboxColumn(),
        },
        use_container_width=True,
    )

    if st.button("Save override", key=f"save_{on}"):
        try:
            shares: list[SAShare] = []
            for _, r in sa_df.iterrows():
                name = (r["Sales Advisor"] or "").strip()
                pct = float(r["Share %"] or 0)
                if name and pct > 0:
                    shares.append(SAShare(name=name, share=pct / 100.0))
            payments: list[PaymentPortion] = []
            for _, r in pay_df.iterrows():
                method_str = (r["Method"] or "").strip()
                if not method_str:
                    continue
                payments.append(
                    PaymentPortion(
                        method=PaymentMethod(method_str),
                        amount=float(r["Amount"] or 0),
                        last4=(r["Last 4"] or None) or None,
                        is_foreign=bool(r["Foreign"]),
                        raw_line="(manual override)",
                    )
                )
            override = ParsedNote(
                sa_shares=shares,
                payments=payments,
                raw_note=parsed.raw_note,
                review_flags=[],  # cleared by save
            )
            st.session_state["overrides"][on] = override
            st.success(f"Override saved for #{on}. Rerun report to see changes.")
        except Exception as e:
            st.error(f"Couldn't save: {e}")


# ---------------------------------------------------------------------------
# Page 2: Commission Report
# ---------------------------------------------------------------------------

_MONTH_ABBR = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _effective_settlement_date(
    o: OrderResult, overrides: dict[str, date]
) -> date | None:
    """Date used to place an order in a payout month: a manual override if the
    user entered one, else the export's settlement (last-successful-transaction)
    date, else None when neither exists."""
    if o.order_number in overrides:
        return overrides[o.order_number]
    # getattr guard: an order built by a pre-settlement_date build of the code
    # and left in st.session_state across a redeploy won't carry the field, and
    # plain attribute access would raise AttributeError. Treat it as undated.
    settlement = getattr(o, "settlement_date", None)
    if settlement is not None:
        return settlement.date()
    return None


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _month_label(key: str) -> str:
    year, month = key.split("-")
    return f"{_MONTH_ABBR[int(month) - 1]} {year}"


def _render_settlement_entry(
    awaiting: list[OrderResult], overrides: dict[str, date]
) -> None:
    """Let the user hand-enter the date payment cleared for paid orders the
    export couldn't date (e.g. manual bank transfers). Until a date is set,
    these orders are held out of every month's totals."""
    with st.expander(
        f"⚠️ {len(awaiting)} paid order(s) need a settlement date", expanded=False
    ):
        st.caption(
            "These orders are marked **Paid** but the export carries no "
            "transaction date, so they can't be placed in a payout month "
            "automatically. Enter the date each payment actually cleared, then "
            "**Save**. Until saved, they are excluded from every month's totals "
            "(so nothing is silently counted in the wrong month)."
        )
        editor = pd.DataFrame(
            [
                {
                    "Order #": o.order_number,
                    "Order date": o.order_date.date(),
                    "Gross": o.gross_total,
                    "Settled on": overrides.get(o.order_number),
                }
                for o in awaiting
            ]
        )
        edited = st.data_editor(
            editor,
            hide_index=True,
            use_container_width=True,
            disabled=["Order #", "Order date", "Gross"],
            column_config={
                "Gross": st.column_config.NumberColumn(format="RM %.2f"),
                "Settled on": st.column_config.DateColumn(
                    "Settled on", help="Date the payment cleared"
                ),
            },
            key="settlement_editor",
        )
        if st.button("Save settlement dates"):
            saved = 0
            for _, r in edited.iterrows():
                val = r["Settled on"]
                if pd.notna(val):
                    d = val.date() if hasattr(val, "date") else val
                    overrides[r["Order #"]] = d
                    saved += 1
            st.success(f"Saved {saved} settlement date(s).")
            st.rerun()


def page_report() -> None:
    st.title("Commission Report")

    orders = st.session_state.get("orders")
    settings: AppSettings = st.session_state["settings"]
    if not orders:
        st.info("Upload a CSV on the **Upload & Review** page first.")
        return

    # Orders loaded before an app update won't carry settlement_date; force a
    # clean reload rather than silently dumping them all into manual entry.
    if not hasattr(orders[0], "settlement_date"):
        st.warning(
            "The app was updated since these orders were loaded. Please re-open "
            "**Upload & Review** (re-upload the CSV) to refresh the data, then "
            "come back here."
        )
        return

    # ---- Attribute orders to a payout month by *settlement* date -----------
    # Commission for a month is earned on orders whose payment fully cleared
    # that month — so an April order settled in May counts toward May. Orders
    # are grouped by settlement month (date of the last successful
    # transaction). Paid orders the export couldn't date wait in a manual-entry
    # panel until the user supplies the date the money cleared.
    settle_overrides: dict[str, date] = st.session_state["settlement_overrides"]
    kept = [o for o in orders if not o.excluded]
    by_month: dict[str, list[OrderResult]] = {}
    awaiting: list[OrderResult] = []
    for o in kept:
        sd = _effective_settlement_date(o, settle_overrides)
        if sd is None:
            awaiting.append(o)
        else:
            by_month.setdefault(_month_key(sd), []).append(o)

    if not by_month and not awaiting:
        st.warning("No kept orders to report.")
        return

    month_keys = sorted(by_month.keys(), reverse=True)
    st.caption(
        "Orders are grouped by the month their payment **fully settled** "
        "(date of the last successful transaction), not the order date — so an "
        "order placed in April but settled in May counts toward May."
    )

    if month_keys:
        sel_month = st.selectbox(
            "Payout month", options=month_keys, format_func=_month_label
        )
        month_orders = by_month[sel_month]
    else:
        sel_month = None
        month_orders = []
        st.warning(
            "Every paid order is still awaiting a settlement date (below)."
        )

    if awaiting:
        _render_settlement_entry(awaiting, settle_overrides)

    report = compute_commissions(month_orders, settings.tiers)
    # Fold in per-SA overachievement bonuses (e.g. MINKEI) for the payout month.
    if sel_month:
        apply_overachievement_bonuses(report, int(sel_month.split("-")[1]))
    summaries = report.sa_summaries
    house = report.house

    st.caption(
        "**Whole-bracket tier:** the SA's full monthly net is multiplied by "
        "the rate of the bracket containing it (not progressive). "
        "TikTok-shop orders earn a flat RM-per-order amount instead and "
        "still count toward the SA's monthly net for tier purposes. "
        "**COMPANY SALES** is the house account — tracked separately below, "
        "earns no commission."
    )

    if not summaries and not house:
        st.warning("No data to report (no kept orders or no SAs detected).")
        return

    g1, g2, g3, g4 = st.columns(4)
    g1.metric("SAs with sales", len(summaries))
    g2.metric("SA total gross", fmt_money(report.total_sa_gross))
    g3.metric("SA total net", fmt_money(report.total_sa_net))
    g4.metric("Total commission", fmt_money(report.total_commission))

    # Build the all-in-one workbook once (summary + one tab per SA + house +
    # review + excluded + settings) and offer it right here, so the whole
    # team's commission downloads as ONE Excel without scrolling past every SA.
    xlsx = build_workbook(
        month_orders, report, settings,
        payout_month=sel_month,
        payout_label=_month_label(sel_month) if sel_month else None,
    )
    month_tag = sel_month or datetime.now().strftime("%Y%m")
    xlsx_name = f"commission_report_{month_tag}.xlsx"
    st.download_button(
        "⬇️  Download full report — all SAs in one Excel",
        data=xlsx,
        file_name=xlsx_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        key="dl_top",
    )
    st.caption(
        "One workbook: a Summary tab plus one tab per SA (and House, Review, "
        "Excluded, Settings). The small ⬇ on each table below only exports that "
        "one SA — use this button for everyone at once."
    )

    if summaries:
        st.subheader("Net sales by SA")
        chart_df = pd.DataFrame(
            {
                "SA": [s.sa_name for s in summaries],
                "Net sales (RM)": [s.total_net_sales for s in summaries],
            }
        ).set_index("SA")
        st.bar_chart(chart_df)

        # Build a lookup so the per-SA breakdowns can show each order's
        # charges and payment-method summary.
        orders_by_num = {o.order_number: o for o in orders}

        st.subheader("Per-SA summary")
        for s in summaries:
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([1.2, 1, 1, 1])
                c1.markdown(f"### {s.sa_name}")
                c1.caption(s.tier_label)
                c2.metric("Orders", s.order_count)
                c2.metric("Avg order", fmt_money(s.avg_order_value))
                c3.metric("Gross", fmt_money(s.total_gross_sales))
                c3.metric("Net", fmt_money(s.total_net_sales))
                c4.metric("Commission", fmt_money(s.commission_amount))

                # getattr guards: if a stale SACommission from a not-yet-fully-
                # reloaded module lacks the clearance fields, degrade quietly
                # instead of crashing the whole report.
                clr_count = getattr(s, "clearance_order_count", 0)
                if clr_count:
                    clr_net = getattr(s, "clearance_net_sales", 0.0)
                    clr_comm = getattr(s, "clearance_commission", 0.0)
                    st.caption(
                        f"➕ {clr_count} clearance order(s) are "
                        f"**excluded** from the sales figures above "
                        f"({fmt_money(clr_net)} in sales) — they earn a "
                        f"flat **{fmt_money(clr_comm)}**, already included "
                        f"in Commission. See the “Clearance (flat)” rows below."
                    )

                if getattr(s, "bonus_season", "") and getattr(s, "bonus_amount", 0.0):
                    st.caption(
                        f"🎯 Overachievement bonus ({s.bonus_season} season): net "
                        f"{fmt_money(s.total_net_sales)} vs target "
                        f"{fmt_money(s.bonus_target)} → **{s.bonus_tiers} tier(s)** "
                        f"= **{fmt_money(s.bonus_amount)}**, included in Commission."
                    )

                with st.expander("Order-by-order breakdown"):
                    rows = [
                        _contribution_row(
                            c,
                            orders_by_num.get(c.order_number),
                            tier_rate_pct=s.tier_rate_pct,
                            tiers_cfg=settings.tiers,
                        )
                        for c in s.contributions
                    ]
                    if rows:
                        st.dataframe(
                            pd.DataFrame(rows),
                            hide_index=True,
                            use_container_width=True,
                            column_config={
                                "Gross share": st.column_config.NumberColumn(format="RM %.2f"),
                                "Charges": st.column_config.NumberColumn(format="RM %.2f"),
                                "Net share": st.column_config.NumberColumn(format="RM %.2f"),
                                "Commission": st.column_config.NumberColumn(format="RM %.2f"),
                            },
                        )

    if house:
        st.divider()
        st.subheader("House sales (COMPANY SALES — no commission)")
        st.caption(
            "Tracked for revenue visibility. Not attributed to any Sales Advisor."
        )
        h1, h2, h3 = st.columns(3)
        h1.metric("Orders", house.order_count)
        h2.metric("Gross", fmt_money(house.total_gross_sales))
        h3.metric("Net", fmt_money(house.total_net_sales))
        with st.expander("Order-by-order breakdown"):
            orders_by_num = {o.order_number: o for o in orders}
            rows = [
                _contribution_row(c, orders_by_num.get(c.order_number))
                for c in house.contributions
            ]
            if rows:
                st.dataframe(
                    pd.DataFrame(rows),
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "Gross share": st.column_config.NumberColumn(format="RM %.2f"),
                        "Charges": st.column_config.NumberColumn(format="RM %.2f"),
                        "Net share": st.column_config.NumberColumn(format="RM %.2f"),
                    },
                )

    st.divider()
    st.download_button(
        "Download Excel Report",
        data=xlsx,
        file_name=xlsx_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_bottom",
    )


# ---------------------------------------------------------------------------
# Page 3: Settings
# ---------------------------------------------------------------------------

def page_settings() -> None:
    st.title("Settings")
    st.caption("Changes are written to JSON in `data/` and persist across runs.")

    settings: AppSettings = st.session_state["settings"]

    sa_tab, rate_tab, tier_tab = st.tabs(
        ["Sales Advisors", "Card rates", "Tiers & channel flat rules"]
    )

    # ---- SAs ---------------------------------------------------------------
    with sa_tab:
        st.subheader("Active sales advisors")
        sa_df = pd.DataFrame(
            [{"Name": s.name, "Active": s.active} for s in settings.sa_list.sas]
        )
        edited = st.data_editor(
            sa_df,
            num_rows="dynamic",
            key="sa_editor_settings",
            use_container_width=True,
            column_config={
                "Name": st.column_config.TextColumn(required=True),
                "Active": st.column_config.CheckboxColumn(default=True),
            },
        )
        if st.button("Save SA list"):
            new_sas = []
            for _, r in edited.iterrows():
                name = (r["Name"] or "").strip().upper()
                if not name:
                    continue
                new_sas.append(SARecord(name=name, active=bool(r["Active"])))
            settings.sa_list.sas = new_sas
            save_sa_list(settings.sa_list)
            _reload_settings()
            _save_and_sync(SA_FILE, "Sales Advisor list")

    # ---- Rates -------------------------------------------------------------
    with rate_tab:
        st.subheader("Maybank merchant rate card (versioned)")
        st.caption(
            "Each version has an `effective_from` date; the engine picks the "
            "version active on each order's date."
        )
        version_labels = [
            f"{v.effective_from.isoformat()}" for v in settings.rates.versions
        ]
        active_idx = st.selectbox(
            "Edit version",
            options=list(range(len(version_labels))),
            format_func=lambda i: version_labels[i],
        )
        version = settings.rates.versions[active_idx]

        cv1, cv2, cv3 = st.columns(3)
        with cv1:
            new_eff = st.date_input("Effective from", value=version.effective_from)
        with cv2:
            new_card = st.number_input(
                "SenangPay card %", value=float(version.senangpay_card_pct), step=0.01
            )
        with cv3:
            new_fpx = st.number_input(
                "SenangPay FPX %", value=float(version.senangpay_fpx_pct), step=0.01
            )

        rate_df = pd.DataFrame(
            [
                {
                    "Label": r.label,
                    "Method": r.method.value,
                    "Foreign": r.is_foreign,
                    "Rate %": r.rate_pct,
                }
                for r in version.rates
            ]
        )
        edited_rates = st.data_editor(
            rate_df,
            num_rows="fixed",
            key=f"rates_editor_{active_idx}",
            use_container_width=True,
            column_config={
                "Method": st.column_config.SelectboxColumn(
                    options=[m.value for m in PaymentMethod], required=True
                ),
                "Foreign": st.column_config.CheckboxColumn(),
                "Rate %": st.column_config.NumberColumn(min_value=0.0, step=0.01),
            },
        )
        c_save, c_new = st.columns(2)
        with c_save:
            if st.button("Save changes to this version"):
                new_rows = []
                for _, r in edited_rates.iterrows():
                    rate_pct = r["Rate %"]
                    if pd.isna(rate_pct):
                        rate_pct = None
                    new_rows.append(
                        RateRow(
                            label=str(r["Label"]).strip(),
                            method=PaymentMethod(str(r["Method"]).strip()),
                            is_foreign=bool(r["Foreign"]),
                            rate_pct=rate_pct,
                        )
                    )
                # Reject duplicate dates so we don't silently create another
                # 2026-05-08 vs 2026-05-08 ambiguity when the user is editing.
                others = [
                    v.effective_from
                    for i, v in enumerate(settings.rates.versions)
                    if i != active_idx
                ]
                if new_eff in others:
                    st.error(
                        f"A version dated {new_eff} already exists. "
                        "Pick a different effective-from date or delete the duplicate first."
                    )
                else:
                    settings.rates.versions[active_idx] = RateTableVersion(
                        effective_from=new_eff,
                        senangpay_card_pct=new_card,
                        senangpay_fpx_pct=new_fpx,
                        rates=new_rows,
                    )
                    save_rates(settings.rates)
                    _reload_settings()
                    _save_and_sync(RATES_FILE, "Card rate table")
        with c_new:
            new_ver_date = st.date_input(
                "Effective from (for the new version)",
                value=date.today(),
                key=f"new_ver_date_{active_idx}",
            )
            if st.button("Add new version (copy of current)"):
                if any(v.effective_from == new_ver_date for v in settings.rates.versions):
                    st.error(
                        f"A version dated {new_ver_date} already exists. "
                        "Pick a different date."
                    )
                else:
                    copy = version.model_copy(deep=True)
                    copy.effective_from = new_ver_date
                    settings.rates.versions.append(copy)
                    save_rates(settings.rates)
                    _reload_settings()
                    _save_and_sync(
                        RATES_FILE, f"Added rate version {new_ver_date.isoformat()}"
                    )
                    st.rerun()

        # Delete button — only when there's more than one version, so the
        # user can never accidentally end up with zero rate versions.
        if len(settings.rates.versions) > 1:
            st.divider()
            del_col1, del_col2 = st.columns([3, 1])
            confirm_del = del_col1.checkbox(
                f"Confirm: I want to permanently delete the "
                f"**{version.effective_from.isoformat()}** version",
                key=f"del_confirm_{active_idx}",
            )
            if del_col2.button("Delete version", disabled=not confirm_del, type="secondary"):
                gone = settings.rates.versions.pop(active_idx)
                save_rates(settings.rates)
                _reload_settings()
                _save_and_sync(
                    RATES_FILE,
                    f"Deleted rate version {gone.effective_from.isoformat()}",
                )
                st.rerun()

    # ---- Tiers + channel flat rules ---------------------------------------
    with tier_tab:
        st.subheader("Commission tiers (whole-bracket)")
        tier_df = pd.DataFrame(
            [
                {
                    "Min net (RM)": t.min_net,
                    "Max net (RM)": t.max_net if t.max_net is not None else float("inf"),
                    "Rate %": t.rate_pct,
                }
                for t in settings.tiers.tiers
            ]
        )
        tier_edit = st.data_editor(
            tier_df,
            num_rows="dynamic",
            key="tier_editor",
            use_container_width=True,
            column_config={
                "Min net (RM)": st.column_config.NumberColumn(min_value=0.0, step=1000.0),
                "Max net (RM)": st.column_config.NumberColumn(min_value=0.0, step=1000.0),
                "Rate %": st.column_config.NumberColumn(min_value=0.0, step=0.01),
            },
        )

        st.subheader("Channel flat-commission rules")
        st.caption(
            "Orders on these channels earn a flat RM amount per order instead "
            "of the tier rate. Net sales still count toward the SA's monthly "
            "tier total."
        )
        flat_df = pd.DataFrame(
            [
                {"Channel": r.channel, "RM per order": r.amount_per_order, "Label": r.label}
                for r in settings.tiers.channel_flat_commissions
            ]
        )
        flat_edit = st.data_editor(
            flat_df,
            num_rows="dynamic",
            key="flat_editor",
            use_container_width=True,
            column_config={
                "RM per order": st.column_config.NumberColumn(format="RM %.2f", min_value=0.0),
            },
        )

        if st.button("Save tiers + flat rules"):
            new_tiers: list[CommissionTier] = []
            for _, r in tier_edit.iterrows():
                min_net = float(r["Min net (RM)"] or 0)
                raw_max = r["Max net (RM)"]
                max_net = None if (pd.isna(raw_max) or raw_max == float("inf")) else float(raw_max)
                rate_pct = float(r["Rate %"] or 0)
                new_tiers.append(
                    CommissionTier(min_net=min_net, max_net=max_net, rate_pct=rate_pct)
                )
            new_flat: list[ChannelFlatRule] = []
            for _, r in flat_edit.iterrows():
                ch = (r["Channel"] or "").strip()
                if not ch:
                    continue
                new_flat.append(
                    ChannelFlatRule(
                        channel=ch,
                        amount_per_order=float(r["RM per order"] or 0),
                        label=str(r["Label"] or ""),
                    )
                )
            settings.tiers.tiers = new_tiers
            settings.tiers.channel_flat_commissions = new_flat
            save_tiers(settings.tiers)
            _reload_settings()
            _save_and_sync(TIERS_FILE, "Tiers and channel flat rules")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _ensure_state()
    if not _check_password():
        return
    st.sidebar.title("LB Commission")
    if _password_required():
        if st.sidebar.button("Sign out"):
            st.session_state.pop("authenticated", None)
            st.rerun()
    page = st.sidebar.radio(
        "Navigation",
        options=["Upload & Review", "Commission Report", "Settings"],
        label_visibility="collapsed",
    )
    st.sidebar.divider()
    settings: AppSettings = st.session_state["settings"]
    st.sidebar.caption(
        f"**Active SAs:** {', '.join(settings.sa_list.active_names) or '(none)'}\n\n"
        f"**Tiers:** "
        + " / ".join(
            (
                f"≥{t.min_net:,.0f}@{t.rate_pct}%"
                if t.max_net is None
                else f"<{t.max_net:,.0f}@{t.rate_pct}%"
            )
            for t in settings.tiers.tiers
        )
    )

    if page == "Upload & Review":
        page_upload()
    elif page == "Commission Report":
        page_report()
    else:
        page_settings()


if __name__ == "__main__":
    main()
