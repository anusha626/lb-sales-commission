"""Streamlit UI for the LB International sales-commission calculator.

Three pages:
  1. Upload & Review  — load CSV, fix flagged orders, see parsed/excluded data
  2. Commission Report — per-SA cards, chart, Excel download
  3. Settings          — edit SAs, rate card, tier brackets, channel flat rules

Streamlit is only used for UI glue. All calculation lives in `commission/*`.
"""
from __future__ import annotations

import io
import json
import re
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from pathlib import Path

from commission.aggregator import build_order_results, read_easystore_csv
from commission.commission_engine import (
    apply_overachievement_bonuses,
    compute_commissions,
)
from commission.costs import COSTS_FILE, CostStore
from commission.excel_export import build_workbook
from commission.incentive import (
    HISTORY_FILE,
    IncentiveHistory,
    IncentiveMonth,
    IncentiveScheme,
    MonthFigures,
    SCHEME_FILE,
    compute_incentives,
    load_scheme,
    save_scheme,
    seed_prior_customers,
)
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
    ChannelSaleSplit,
    CommissionTier,
    EventPeriod,
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
RECLASS_PATH = PROJECT_ROOT / "data" / "reclassifications.json"


def _load_reclassifications() -> dict:
    """order_number -> {'month': 'YYYY-MM', 'flat_paid': float}."""
    try:
        return json.loads(RECLASS_PATH.read_text())
    except Exception:
        return {}


def _save_reclassifications(rc: dict) -> None:
    RECLASS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECLASS_PATH.write_text(json.dumps(rc, indent=2))


FORCE_INCLUDE_PATH = PROJECT_ROOT / "data" / "force_include.json"


def _load_force_include() -> set:
    try:
        return set(json.loads(FORCE_INCLUDE_PATH.read_text()))
    except Exception:
        return set()


def _save_force_include(orders: set) -> None:
    FORCE_INCLUDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FORCE_INCLUDE_PATH.write_text(json.dumps(sorted(orders), indent=2))


def _build_version() -> str:
    """Short git commit + date of the running build, so a reboot is visible.
    Computed once per app start (module load); falls back to 'unknown'."""
    import subprocess
    try:
        sha = subprocess.check_output(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
        dt = subprocess.check_output(
            ["git", "-C", str(PROJECT_ROOT), "log", "-1",
             "--format=%cd", "--date=format:%Y-%m-%d %H:%M"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
        return f"{sha} · {dt}"
    except Exception:
        pass
    # Fallback (no git in the container): newest source-file mtime, which on
    # Streamlit Cloud equals the deploy/clone time — still changes each reboot.
    try:
        import datetime as _dt
        files = list((PROJECT_ROOT / "commission").glob("*.py")) + [PROJECT_ROOT / "app.py"]
        newest = max(f.stat().st_mtime for f in files if f.exists())
        return "deployed " + _dt.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "unknown"


_BUILD_VERSION = _build_version()


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
    # Charges from this contribution's own gross − net, so a partially-clearance
    # order's normal and clearance portions each show the right charge.
    charges_share = round(contribution.gross_share - contribution.net_share, 2)
    row = {
        "Order #": contribution.order_number,
        "Date": contribution.order_date.strftime("%Y-%m-%d"),
        "Share %": f"{contribution.share_pct * 100:.0f}%",
        "Gross share": contribution.gross_share,
        "Discount": getattr(contribution, "discount_share", 0.0),
        "Charges": charges_share,
        "Net share": contribution.net_share,
        "Payment method": _format_payment_summary(order),
        "Location": _detect_locations(order),
    }
    if tier_rate_pct is not None:
        is_clearance = getattr(contribution, "is_clearance", False)
        flat_rule = (
            tiers_cfg.flat_rule_for(order.channel)
            if (order is not None and tiers_cfg is not None)
            else None
        )
        event_rate = getattr(order, "event_rate", None) if order is not None else None
        if event_rate is not None:
            commission = round(contribution.net_share * event_rate / 100.0, 2)
            kind = f"Event ({event_rate}%)"
        elif is_clearance and tiers_cfg is not None:
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
    st.session_state.setdefault("clearance_skus", set())  # from products export
    # order_number -> {'month': 'YYYY-MM', 'flat_paid': float}: carry-forward
    # reclassification (clearance -> normal + move payout month).
    st.session_state.setdefault("reclassifications", _load_reclassifications())
    # Order numbers to force-count even if a status filter would drop them.
    st.session_state.setdefault("force_include", _load_force_include())
    # Metadata about the currently-loaded CSV (name, rows, load time) so the
    # user can confirm which file the figures come from.
    st.session_state.setdefault("data_meta", None)
    st.session_state.setdefault("data_sig", None)
    # SKU -> cost price, accumulated across every product export ever
    # uploaded. Feeds the 30% gross-profit gate on the SA incentive.
    st.session_state.setdefault("cost_store", CostStore.load())
    # Saved per-month incentive figures (Part A / Part B accumulate on them).
    st.session_state.setdefault("incentive_history", IncentiveHistory.load())


def _reload_settings() -> None:
    st.session_state["settings"] = load_all()


def _recompute_orders(
    *,
    include_unpaid: bool,
    date_from: date | None,
    date_to: date | None,
) -> None:
    # Remember the last inputs so the report page can rebuild after editing the
    # force-include list.
    st.session_state["_recompute_args"] = {
        "include_unpaid": include_unpaid,
        "date_from": date_from,
        "date_to": date_to,
    }
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
        clearance_skus=st.session_state.get("clearance_skus") or set(),
        clearance_from=st.session_state.get("clearance_from"),
        force_include=st.session_state.get("force_include") or set(),
        costs=st.session_state.get("cost_store"),
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
            sig = (upl.name, getattr(upl, "size", len(upl.getvalue())))
            if st.session_state.get("data_sig") != sig:
                st.session_state["data_sig"] = sig
                st.session_state["data_meta"] = {
                    "name": upl.name,
                    "rows": len(df),
                    "loaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
            st.success(f"Loaded {len(df)} rows from **{upl.name}**.")
        except Exception as e:
            st.error(f"Couldn't read CSV: {e}")
            return

    # Optional clearance-products export: any order line-item whose SKU is in
    # this list is treated as clearance (flat RM10), on top of the note tag.
    clr_upl = st.file_uploader(
        "Clearance products export (optional) — items sold at flat RM10",
        type=["csv"],
        key="clr_upl",
    )
    if clr_upl is not None:
        try:
            cdf = pd.read_csv(io.BytesIO(clr_upl.getvalue()), dtype=str).fillna("")
            sku_col = next((c for c in cdf.columns if c.strip().lower() in ("sku", "variant sku")), None)
            skus = {s.strip() for s in cdf[sku_col] if s.strip()} if sku_col else set()
            st.session_state["clearance_skus"] = skus
            st.caption(f"🏷️ {len(skus)} clearance SKU(s) loaded — matching order items earn flat RM10.")
        except Exception as e:
            st.warning(f"Couldn't read clearance products CSV: {e}")
    if st.session_state.get("clearance_skus"):
        st.session_state["clearance_from"] = st.date_input(
            "Clearance effective from",
            value=st.session_state.get("clearance_from") or date.today().replace(day=1),
            help="SKU-matched items count as clearance only for orders on/after this "
                 "date. Earlier sales (full price before the item went on clearance) "
                 "are untouched. The 'SALES JUNE' note tag always applies regardless.",
        )
        st.caption(
            f"🏷️ {len(st.session_state['clearance_skus'])} clearance SKU(s) active "
            f"for orders on/after {st.session_state['clearance_from']}."
        )

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
        include_unpaid = st.checkbox(
            "Include unpaid (forecast)", value=False,
            help="By default an order counts once it is fully Paid, regardless "
                 "of fulfillment. Tick to also include not-fully-paid orders "
                 "(Unpaid / Partially Paid / COD) as a forecast.",
        )

    _recompute_orders(
        include_unpaid=include_unpaid,
        date_from=date_from,
        date_to=date_to,
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


def _apply_reclassifications(
    orders: list[OrderResult], reclass: dict
) -> list[OrderResult]:
    """Carry-forward reclassification: for each order the user flagged, return a
    copy that is treated as a NORMAL sale (clearance off), moved to the target
    payout month, and carrying the flat commission already paid last month so it
    can be deducted. Orders not flagged pass through unchanged."""
    if not reclass:
        return orders
    out: list[OrderResult] = []
    for o in orders:
        rc = reclass.get(o.order_number)
        if not rc:
            out.append(o)
            continue
        base = getattr(o, "settlement_date", None) or o.order_date
        try:
            y, m = int(str(rc["month"])[:4]), int(str(rc["month"])[5:7])
            new_settle = datetime(y, m, 15)
        except Exception:
            new_settle = base
        out.append(o.model_copy(update={
            "is_clearance": False,
            "clearance_amount": 0.0,
            "settlement_date": new_settle,
            "prior_flat_paid": float(rc.get("flat_paid", 0.0) or 0.0),
            "carried_from_month": _month_key(base.date()),
        }))
    return out


def _render_force_include_editor() -> None:
    """Add specific order numbers to count even when a status filter (e.g.
    Unfulfilled) is holding them out. They flow into the report normally."""
    fi: set = st.session_state["force_include"]
    with st.expander(
        f"➕ Force-include orders (count despite Paid/Fulfilled filter)  ·  {len(fi)} set",
        expanded=False,
    ):
        st.caption(
            "List order numbers that should be counted even though a status "
            "filter is dropping them (e.g. a paid-but-Unfulfilled sale in the "
            "Excluded tab). They are re-costed and included normally."
        )
        rows = [{"Order #": o} for o in sorted(fi)] or [{"Order #": ""}]
        edited = st.data_editor(
            pd.DataFrame(rows),
            num_rows="dynamic",
            hide_index=True,
            use_container_width=True,
            key="force_include_editor",
            column_config={"Order #": st.column_config.TextColumn(help="e.g. #10459")},
        )
        if st.button("Save force-include"):
            new: set = set()
            for _, r in edited.iterrows():
                on = str(r["Order #"] or "").strip()
                if not on:
                    continue
                if not on.startswith("#"):
                    on = "#" + on
                new.add(on)
            st.session_state["force_include"] = new
            _save_force_include(new)
            args = st.session_state.get("_recompute_args") or {
                "include_unpaid": False, "date_from": None, "date_to": None,
            }
            _recompute_orders(**args)
            st.success(f"Saved {len(new)} force-included order(s).")
            st.rerun()


def _render_reclass_editor() -> None:
    """Editor to carry orders forward: reclassify clearance → normal sale, move
    the payout month, and record any flat commission already paid last month."""
    rc: dict = st.session_state["reclassifications"]
    with st.expander(
        f"↪️ Carry-forward / reclassify orders  ·  {len(rc)} set", expanded=False
    ):
        st.caption(
            "Move an order to a different payout month and treat it as a normal "
            "sale (e.g. a clearance order that should earn normal commission this "
            "month). If a flat clearance commission was already paid last month, "
            "enter it under **Flat already paid** — it is deducted from the new "
            "commission so the SA is topped up, not paid twice."
        )
        rows = [
            {"Order #": k,
             "Move to month (YYYY-MM)": v.get("month", ""),
             "Flat already paid (RM)": float(v.get("flat_paid", 10.0) or 0.0)}
            for k, v in rc.items()
        ] or [{"Order #": "", "Move to month (YYYY-MM)": "", "Flat already paid (RM)": 10.0}]
        edited = st.data_editor(
            pd.DataFrame(rows),
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            key="reclass_editor",
            column_config={
                "Order #": st.column_config.TextColumn(help="e.g. #10048"),
                "Move to month (YYYY-MM)": st.column_config.TextColumn(help="e.g. 2026-07"),
                "Flat already paid (RM)": st.column_config.NumberColumn(format="RM %.2f"),
            },
        )
        if st.button("Save reclassifications"):
            new: dict = {}
            for _, r in edited.iterrows():
                on = str(r["Order #"] or "").strip()
                mo = str(r["Move to month (YYYY-MM)"] or "").strip()
                if not on or not mo:
                    continue
                if not on.startswith("#"):
                    on = "#" + on
                try:
                    fp = float(r["Flat already paid (RM)"] or 0.0)
                except (TypeError, ValueError):
                    fp = 0.0
                new[on] = {"month": mo, "flat_paid": fp}
            st.session_state["reclassifications"] = new
            _save_reclassifications(new)
            st.success(f"Saved {len(new)} reclassification(s).")
            st.rerun()


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


def _render_data_loaded_line() -> None:
    """Show which CSV the current figures come from (name, rows, load time)."""
    meta = st.session_state.get("data_meta")
    if meta:
        st.caption(
            f"📄 Data loaded: **{meta['name']}** · {meta['rows']} rows · "
            f"loaded {meta['loaded_at']}"
        )
    else:
        st.caption("📄 No CSV metadata — re-upload on **Upload & Review** to stamp it.")


def page_report() -> None:
    st.title("Commission Report")
    _render_data_loaded_line()

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

    # ---- Carry-forward / reclassify orders ---------------------------------
    _render_reclass_editor()
    _render_force_include_editor()
    orders = _apply_reclassifications(orders, st.session_state["reclassifications"])

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
    refunded_orders = [
        o for o in orders if (o.financial_status or "").strip().lower() == "refunded"
    ]
    # The SA incentive is a separate scheme, but it rides along in the same
    # workbook as its own sheet so one download covers the whole payout.
    inc_report = None
    if sel_month and settings.incentive.month_for(sel_month):
        inc_report = compute_incentives(
            month_orders,
            settings.incentive,
            st.session_state["incentive_history"],
            sel_month,
            sa_names=settings.sa_list.active_names,
            cost_store_size=len(st.session_state["cost_store"]),
        )
    xlsx = build_workbook(
        month_orders, report, settings,
        payout_month=sel_month,
        payout_label=_month_label(sel_month) if sel_month else None,
        refunded_orders=refunded_orders,
        all_orders=orders,  # full set → Review/Excluded tabs stay complete
        incentive_report=inc_report,
    )
    if inc_report is not None and inc_report.total_payout:
        st.success(
            f"➕ **SA Incentive (separate scheme): "
            f"{fmt_money(inc_report.total_payout)}** paid on top of the "
            f"commission above — "
            + ", ".join(
                f"{r.sa_name} {fmt_money(r.payout)} ({r.multiplier_label})"
                for r in inc_report.sa_results
                if r.payout
            )
            + ". See the **SA Incentive** page for the breakdown."
        )
    elif inc_report is not None:
        st.info(
            "➕ **SA Incentive (separate scheme): nothing payable this month.** "
            "See the **SA Incentive** page for how close each SA is."
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

                prior_flat = getattr(s, "prior_flat_deducted", 0.0)
                if prior_flat:
                    st.caption(
                        f"↪️ Carried-forward orders: **−{fmt_money(prior_flat)}** flat "
                        f"commission already paid last month has been **deducted** from "
                        f"Commission (they now earn the normal tier rate)."
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
                                "Discount": st.column_config.NumberColumn(format="RM %.2f"),
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
                        "Discount": st.column_config.NumberColumn(format="RM %.2f"),
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
# Page 3: SA Incentive (separate scheme — see commission/incentive.py)
# ---------------------------------------------------------------------------

def _incentive_month_orders() -> tuple[str | None, list[OrderResult]]:
    """Payout month picker, grouped by settlement date exactly like the
    Commission Report page so both pages report the same set of orders."""
    orders = st.session_state.get("orders") or []
    orders = _apply_reclassifications(orders, st.session_state["reclassifications"])
    settle_overrides: dict[str, date] = st.session_state["settlement_overrides"]
    by_month: dict[str, list[OrderResult]] = {}
    for o in orders:
        if o.excluded:
            continue
        sd = _effective_settlement_date(o, settle_overrides)
        if sd is not None:
            by_month.setdefault(_month_key(sd), []).append(o)
    if not by_month:
        return None, []
    keys = sorted(by_month, reverse=True)
    sel = st.selectbox(
        "Payout month", options=keys, format_func=_month_label, key="inc_month"
    )
    return sel, by_month[sel]


def _render_cost_upload() -> None:
    """Product-export upload that feeds the SKU cost store."""
    store: CostStore = st.session_state["cost_store"]
    st.markdown("**Cost prices** — needed for the 30% gross-profit gate on Part B")
    st.caption(
        f"The store currently holds **{len(store):,} SKUs**. It accumulates: "
        "each product export you upload is merged and kept, so an item sold "
        "and delisted keeps its cost. Upload a fresh EasyStore **product** "
        "export (Products → Export) whenever coverage looks low."
    )
    up = st.file_uploader(
        "EasyStore product export (needs SKU + Cost Price columns)",
        type=["csv"],
        key="cost_csv",
    )
    if up is not None and st.button("Merge into cost store", key="btn_merge_costs"):
        try:
            summary = store.merge_product_csv(up.getvalue())
        except ValueError as exc:
            st.error(str(exc))
            return
        store.save()
        _save_and_sync(COSTS_FILE, f"cost store: +{summary['added']} SKUs")
        st.success(
            f"Merged {summary['rows']:,} rows — {summary['added']:,} new SKUs, "
            f"{summary['updated']:,} updated, {summary['skipped']:,} skipped. "
            f"Store now holds {summary['total_skus']:,} SKUs. "
            "Re-upload the order CSV to apply the new costs."
        )
        st.rerun()


def _render_prior_seed() -> None:
    """Seed the pre-Sep-2026 purchase history used by Part A."""
    history: IncentiveHistory = st.session_state["incentive_history"]
    scheme: IncentiveScheme = st.session_state["settings"].incentive
    known = sum(len(v) for v in history.prior_customers.values())
    st.markdown("**Customer history before the scheme started**")
    st.caption(
        "A buyer only counts as *returning* if the SA had sold to them before. "
        f"Purchases before {_month_label(scheme.start_month)} have to be seeded "
        "from older exports, or nobody can be returning in M1. "
        f"Currently seeded: **{known:,} customer records** across "
        f"{len(history.prior_customers)} SA(s)."
    )
    orders = st.session_state.get("orders") or []
    pre = [
        o for o in orders
        if not o.excluded and o.order_date.strftime("%Y-%m") < scheme.start_month
    ]
    c1, c2 = st.columns([1, 2])
    with c1:
        disabled = not pre
        if st.button(
            f"Seed from loaded CSV ({len(pre)} pre-scheme orders)",
            key="btn_seed_prior",
            disabled=disabled,
        ):
            result = seed_prior_customers(pre, scheme, history)
            history.save()
            _save_and_sync(HISTORY_FILE, "incentive: seeded prior customers")
            st.success(
                "Seeded: "
                + ", ".join(f"{sa} {n}" for sa, n in sorted(result.items()))
            )
            st.rerun()
    with c2:
        if not pre:
            st.caption(
                "The loaded CSV has no orders dated before the scheme start — "
                "upload an export reaching further back to seed more history."
            )


def _render_incentive_history_editor(month_key: str, figures: dict) -> None:
    """Save this month into history, and show / clear what is stored."""
    history: IncentiveHistory = st.session_state["incentive_history"]
    saved = history.months.get(month_key, {})
    st.markdown("**Month history**")
    st.caption(
        "Part A and Part B are measured on totals accumulated since "
        "the scheme start, so each finalised month has to be saved. Saving "
        "overwrites this month's stored figures with the ones shown above."
    )
    c1, c2 = st.columns([1, 1])
    with c1:
        label = "Update saved figures" if saved else "Save this month to history"
        if st.button(f"💾 {label} — {_month_label(month_key)}", key="btn_save_hist"):
            for sa, fig in figures.items():
                history.put(month_key, sa, fig)
            history.save()
            _save_and_sync(HISTORY_FILE, f"incentive: saved {month_key}")
            st.success(f"Saved {len(figures)} SA figure(s) for {_month_label(month_key)}.")
            st.rerun()
    with c2:
        if saved and st.button(
            f"🗑 Remove {_month_label(month_key)} from history", key="btn_del_hist"
        ):
            history.months.pop(month_key, None)
            history.save()
            _save_and_sync(HISTORY_FILE, f"incentive: removed {month_key}")
            st.rerun()

    if history.months:
        rows = []
        for mk in sorted(history.months):
            for sa, fig in sorted(history.months[mk].items()):
                rows.append(
                    {
                        "Month": _month_label(mk),
                        "SA": sa,
                        "Qualifying sales": fig.qualifying_sales,
                        "Orders": fig.qualifying_orders,
                        "GP %": fig.gp_pct,
                        "Returning": len(fig.returning),
                    }
                )
        with st.expander(f"Stored history — {len(rows)} row(s)"):
            st.dataframe(
                pd.DataFrame(rows),
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Qualifying sales": st.column_config.NumberColumn(format="RM %.2f"),
                    "GP %": st.column_config.NumberColumn(format="%.1f%%"),
                },
            )


def page_incentive() -> None:
    st.title("SA Incentive")
    scheme: IncentiveScheme = st.session_state["settings"].incentive
    gate_text = {
        "discount_rate": (
            f"no more than {scheme.max_discount_rate_pct:.0f}% of "
            f"{'accumulated' if scheme.discount_basis == 'accumulated' else 'the month’s'}"
            " orders discounted"
        ),
        "gross_profit": f"{scheme.gp_threshold_pct:.0f}% gross profit",
        "both": (
            f"{scheme.gp_threshold_pct:.0f}% gross profit **and** no more than "
            f"{scheme.max_discount_rate_pct:.0f}% of orders discounted"
        ),
        "none": "no quality gate",
    }.get(scheme.part_b_gate, scheme.part_b_gate)
    st.caption(
        f"**{scheme.name} — {scheme.year_label}.** A separate scheme, paid on "
        "top of the tier commission and the Year-End Sales Bonus. Two parts "
        "are assessed each month on figures **accumulated since "
        f"{_month_label(scheme.start_month)}**: Part A (returning customers) "
        f"and Part B (accumulated sales, gated on {gate_text}). "
        "One part = 1× base, both = 2×, neither = nothing."
    )
    _render_data_loaded_line()

    with st.expander("Setup — cost prices and customer history", expanded=False):
        _render_cost_upload()
        st.divider()
        _render_prior_seed()

    orders = st.session_state.get("orders")
    if not orders:
        st.info("Upload a CSV on the **Upload & Review** page first.")
        return

    month_key, month_orders = _incentive_month_orders()
    if not month_key:
        st.warning("No settled orders to report.")
        return

    sched = scheme.month_for(month_key)
    if sched is None:
        st.warning(
            f"{_month_label(month_key)} is outside the Year 1 window "
            f"({_month_label(scheme.months[0].key)} – "
            f"{_month_label(scheme.months[-1].key)}). Nothing to assess."
        )
        return

    settings: AppSettings = st.session_state["settings"]
    history: IncentiveHistory = st.session_state["incentive_history"]
    store: CostStore = st.session_state["cost_store"]
    report = compute_incentives(
        month_orders,
        scheme,
        history,
        month_key,
        sa_names=settings.sa_list.active_names,
        cost_store_size=len(store),
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Scheme month", f"M{sched.m} of 12")
    m2.metric("Base incentive", fmt_money(sched.base_incentive))
    m3.metric("SAs assessed", len(report.sa_results))
    m4.metric("Total incentive payout", fmt_money(report.total_payout))

    st.caption(
        f"**M{sched.m} targets** — Part A: {sched.returning_target} accumulated "
        f"returning customers · Part B: {fmt_money(sched.accumulated_sales_target)} "
        f"accumulated qualifying sales with {gate_text} · "
        f"base {fmt_money(sched.base_incentive)} per part "
        f"(max {fmt_money(sched.base_incentive * 2)})."
    )

    if not report.sa_results:
        st.warning("No SA has qualifying orders for this month.")
        return

    st.subheader("Per-SA incentive")
    for r in report.sa_results:
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([1.3, 1.1, 1.1, 1])
            c1.markdown(f"### {r.sa_name}")
            c1.caption(
                f"M{r.month_index} · base {fmt_money(r.base_incentive)} · "
                f"{r.multiplier_label}"
            )

            a_icon = "✅" if r.part_a_hit else "❌"
            c2.metric(
                f"{a_icon} Part A — returning",
                f"{r.accum_returning} / {r.returning_target}",
                delta=f"+{r.month_returning} this month" if r.month_returning else None,
            )
            b_icon = "✅" if r.part_b_hit else "❌"
            c3.metric(
                f"{b_icon} Part B — accum. sales",
                fmt_money(r.accum_sales),
                delta=f"target {fmt_money(r.sales_target)}",
                delta_color="off",
            )
            if r.part_b_gate == "gross_profit":
                c3.caption(
                    f"GP {r.accum_gp_pct:.1f}% "
                    f"({'passes' if r.gp_gate_passed else 'below'} "
                    f"{r.gp_threshold_pct:.0f}%)"
                )
            elif r.part_b_gate == "none":
                c3.caption("no quality gate")
            else:
                mark = "✓" if r.discount_gate_passed else "✗"
                c3.caption(
                    f"{mark} discount rate {r.discount_numerator}/"
                    f"{r.discount_denominator} = {r.discount_rate_pct:.0f}% "
                    f"(max {r.max_discount_rate_pct:.0f}%)"
                    + (
                        f" · GP {r.accum_gp_pct:.1f}%"
                        if r.part_b_gate == "both" else ""
                    )
                )
            c4.metric("Incentive", fmt_money(r.payout))

            gap_sales = max(0.0, r.sales_target - r.accum_sales)
            gap_ret = max(0, r.returning_target - r.accum_returning)
            bits = []
            if gap_ret:
                bits.append(f"**{gap_ret}** more returning customer(s) for Part A")
            if gap_sales:
                bits.append(f"**{fmt_money(gap_sales)}** more sales for Part B")
            if bits:
                st.caption("Still needed: " + " · ".join(bits) + ".")
            if r.excluded_note:
                st.caption(f"⚠️ {r.excluded_note}")

            with st.expander("This month's detail"):
                d1, d2, d3 = st.columns(3)
                d1.metric("Qualifying sales", fmt_money(r.month_sales))
                d1.caption(f"{r.month_orders} qualifying order(s)")
                if r.part_b_gate in ("discount_rate", "both"):
                    d2.metric(
                        "Month discount rate", f"{r.month_discount_rate_pct:.0f}%"
                    )
                    d2.caption(
                        f"{r.month_discounted_orders} of "
                        f"{r.month_discount_base_orders} order(s) discounted"
                    )
                else:
                    d2.metric("Month GP", f"{r.month_gp_pct:.1f}%")
                    d2.caption(
                        f"cost known for {r.cost_coverage_pct:.0f}% of revenue"
                    )
                d3.metric("New returning customers", r.month_returning)
                if r.returning_customers:
                    st.caption(
                        "Returning this month: " + ", ".join(r.returning_customers)
                    )
                if r.qualifying_order_numbers:
                    st.caption(
                        f"Qualifying orders ({len(r.qualifying_order_numbers)}): "
                        + ", ".join(r.qualifying_order_numbers)
                    )

    st.divider()
    _render_incentive_history_editor(month_key, report.month_figures)

    st.divider()
    rows = [
        {
            "SA": r.sa_name,
            "Month sales": r.month_sales,
            "Accum. sales": r.accum_sales,
            "Sales target": r.sales_target,
            "GP %": r.accum_gp_pct,
            "Discount rate %": r.discount_rate_pct,
            "Returning": r.accum_returning,
            "Return target": r.returning_target,
            "Part A": "Yes" if r.part_a_hit else "No",
            "Part B": "Yes" if r.part_b_hit else "No",
            "Base": r.base_incentive,
            "Incentive": r.payout,
        }
        for r in report.sa_results
    ]
    df_out = pd.DataFrame(rows)
    st.subheader("Summary")
    st.dataframe(
        df_out,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Month sales": st.column_config.NumberColumn(format="RM %.2f"),
            "Accum. sales": st.column_config.NumberColumn(format="RM %.2f"),
            "Sales target": st.column_config.NumberColumn(format="RM %.2f"),
            "GP %": st.column_config.NumberColumn(format="%.1f%%"),
            "Discount rate %": st.column_config.NumberColumn(format="%.0f%%"),
            "Base": st.column_config.NumberColumn(format="RM %.2f"),
            "Incentive": st.column_config.NumberColumn(format="RM %.2f"),
        },
    )
    st.download_button(
        "⬇️  Download incentive summary (CSV)",
        data=df_out.to_csv(index=False).encode("utf-8"),
        file_name=f"sa_incentive_{month_key}.csv",
        mime="text/csv",
    )


# ---------------------------------------------------------------------------
# Page 4: Settings
# ---------------------------------------------------------------------------

def page_settings() -> None:
    st.title("Settings")
    st.caption("Changes are written to JSON in `data/` and persist across runs.")

    settings: AppSettings = st.session_state["settings"]

    sa_tab, rate_tab, tier_tab, inc_tab = st.tabs(
        [
            "Sales Advisors",
            "Card rates",
            "Tiers & channel flat rules",
            "SA incentive scheme",
        ]
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

        st.subheader("Channel sale-split rules")
        st.caption(
            "Orders on these channels split the sale: the SA(s) named on the "
            "order keep the SA % (and earn their tier rate on that portion); the "
            "rest goes to COMPANY SALES (no commission). No SA on the note → the "
            "whole order is company. E.g. tiktok-shop at 30% → SA 30%, company 70%."
        )
        split_df = pd.DataFrame(
            [
                {"Channel": r.channel, "SA %": round(r.sa_fraction * 100, 2), "Label": r.label}
                for r in settings.tiers.channel_sale_splits
            ]
        )
        split_edit = st.data_editor(
            split_df,
            num_rows="dynamic",
            key="split_editor",
            use_container_width=True,
            column_config={
                "SA %": st.column_config.NumberColumn(format="%.0f%%", min_value=0.0, max_value=100.0),
            },
        )

        st.subheader("Event periods (flat-rate promo windows)")
        st.caption(
            "Every order DATED inside a window earns the flat rate below "
            "regardless of tier. Clearance orders in the window are treated as "
            "normal sales and counted in the total; event sales also count "
            "toward the monthly net that sets the tier for non-event orders. "
            "Dates are inclusive, format YYYY-MM-DD."
        )
        event_df = pd.DataFrame(
            [
                {"Start": str(e.start), "End": str(e.end),
                 "Rate %": e.rate_pct, "Label": e.label}
                for e in settings.tiers.event_periods
            ]
            or [{"Start": "", "End": "", "Rate %": 0.8, "Label": ""}]
        )
        event_edit = st.data_editor(
            event_df,
            num_rows="dynamic",
            key="event_editor",
            use_container_width=True,
            column_config={
                "Start": st.column_config.TextColumn(help="e.g. 2026-08-27"),
                "End": st.column_config.TextColumn(help="e.g. 2026-08-31"),
                "Rate %": st.column_config.NumberColumn(format="%.2f%%"),
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
            new_splits: list[ChannelSaleSplit] = []
            for _, r in split_edit.iterrows():
                ch = (r["Channel"] or "").strip()
                if not ch:
                    continue
                new_splits.append(
                    ChannelSaleSplit(
                        channel=ch,
                        sa_fraction=float(r["SA %"] or 0) / 100.0,
                        label=str(r["Label"] or ""),
                    )
                )
            from datetime import date as _date
            new_events: list[EventPeriod] = []
            for _, r in event_edit.iterrows():
                start_s = str(r["Start"] or "").strip()
                end_s = str(r["End"] or "").strip()
                if not start_s or not end_s:
                    continue
                try:
                    start_d = _date.fromisoformat(start_s)
                    end_d = _date.fromisoformat(end_s)
                except ValueError:
                    st.error(f"Bad event date: {start_s} / {end_s} (use YYYY-MM-DD)")
                    continue
                new_events.append(
                    EventPeriod(
                        start=start_d, end=end_d,
                        rate_pct=float(r["Rate %"] or 0),
                        label=str(r["Label"] or ""),
                    )
                )
            settings.tiers.tiers = new_tiers
            settings.tiers.channel_flat_commissions = new_flat
            settings.tiers.channel_sale_splits = new_splits
            settings.tiers.event_periods = new_events
            save_tiers(settings.tiers)
            _reload_settings()
            _save_and_sync(TIERS_FILE, "Tiers and channel flat rules")



    # ---- SA incentive scheme ----------------------------------------------
    with inc_tab:
        scheme: IncentiveScheme = settings.incentive
        st.subheader(f"{scheme.name} — {scheme.year_label}")
        st.caption(
            "A separate scheme from the tier commission. Two parts assessed "
            "each month on figures accumulated since the start month; one part "
            "pays 1× the base, both pay 2×."
        )

        r1, r2, r3 = st.columns(3)
        start_month = r1.text_input(
            "Start month (YYYY-MM)", value=scheme.start_month, key="inc_start"
        )
        min_order = r2.number_input(
            "Minimum order value (RM)",
            value=float(scheme.min_order_value),
            step=100.0,
            key="inc_minorder",
            help="An order below this counts neither toward sales nor as a "
                 "returning customer.",
        )
        _GATES = ["discount_rate", "gross_profit", "both", "none"]
        part_b_gate = r3.selectbox(
            "Part B quality gate",
            options=_GATES,
            index=_GATES.index(scheme.part_b_gate)
            if scheme.part_b_gate in _GATES else 0,
            format_func=lambda v: {
                "discount_rate": "Discount rate (share of orders discounted)",
                "gross_profit": "Gross profit %",
                "both": "Both — discount rate and gross profit",
                "none": "None — accumulated sales alone",
            }[v],
            key="inc_gate",
            help="What Part B must satisfy on top of the accumulated sales "
                 "target.",
        )

        d1, d2 = st.columns(2)
        max_discount = d1.number_input(
            "Maximum discount rate (% of orders)",
            value=float(scheme.max_discount_rate_pct),
            step=5.0,
            min_value=0.0,
            max_value=100.0,
            key="inc_maxdisc",
            help="Part B is blocked when more than this share of the SA's "
                 "qualifying orders carried any discount. Magnitude is not "
                 "graded — an order is either discounted or it is not.",
        )
        discount_scope = d2.selectbox(
            "Discount rate measured across",
            options=["all_orders", "qualifying"],
            index=0 if scheme.discount_scope == "all_orders" else 1,
            format_func=lambda v: (
                "every paid order, including under the minimum"
                if v == "all_orders"
                else "only orders counting toward the sales target"
            ),
            key="inc_discscope",
            help="Service-only orders are left out either way.",
        )
        discount_basis = d2.selectbox(
            "Discount rate measured on",
            options=["month", "accumulated"],
            index=0 if scheme.discount_basis == "month" else 1,
            key="inc_discbasis",
            help="'month' judges each month on its own orders; 'accumulated' "
                 "runs the rate since the scheme start.",
        )

        r4, r5, r6 = st.columns(3)
        gp_threshold = r4.number_input(
            "Gross-profit gate (%)",
            value=float(scheme.gp_threshold_pct),
            step=1.0,
            key="inc_gp",
            help="Used when the Part B gate above includes gross profit.",
        )
        gp_basis = r4.selectbox(
            "Gross profit measured on",
            options=["accumulated", "month"],
            index=0 if scheme.gp_basis == "accumulated" else 1,
            key="inc_gpbasis",
            help="'accumulated' matches Part B's accumulated sales; 'month' "
                 "tests the reported month on its own.",
        )
        sales_basis = r5.selectbox(
            "Sales counted as",
            options=["gross", "net"],
            index=0 if scheme.sales_basis == "gross" else 1,
            key="inc_salesbasis",
            help="'gross' = order total after store credit and discount. "
                 "'net' = after merchant card charges, like the tier commission.",
        )
        returning_scope = r6.selectbox(
            "A customer is returning when",
            options=["same_sa", "company"],
            index=0 if scheme.returning_scope == "same_sa" else 1,
            format_func=lambda v: (
                "they bought from this SA before" if v == "same_sa"
                else "they bought from LB before (any SA)"
            ),
            key="inc_scope",
        )

        returning_count = st.selectbox(
            "When the same customer returns in more than one month, the "
            "accumulated Part A figure counts them",
            options=["distinct", "repeat_visits"],
            index=0 if scheme.returning_count == "distinct" else 1,
            format_func=lambda v: (
                "once for the year (distinct customers)" if v == "distinct"
                else "again every month they come back (repeat visits)"
            ),
            key="inc_retcount",
            help="The scheme document says '220 returning customers by Aug 27' "
                 "without settling this. 'distinct' is the literal reading and "
                 "the stricter target.",
        )

        service_kw = st.text_area(
            "Service keywords (one per line) — matching line items are stripped "
            "out of qualifying sales, and a service-only order counts nobody as "
            "returning",
            value="\n".join(scheme.service_keywords),
            key="inc_services",
            height=120,
        )

        st.markdown("**Monthly targets**")
        st.caption(
            "Accumulated columns are what Part A and Part B are actually "
            "tested against. The monthly column is shown for reference only."
        )
        month_df = pd.DataFrame(
            [
                {
                    "M": m.m,
                    "Month": m.key,
                    "Monthly sales": m.monthly_sales_target,
                    "Accum. sales": m.accumulated_sales_target,
                    "Returning": m.returning_target,
                    "Base": m.base_incentive,
                    "Max (2x)": m.base_incentive * 2,
                }
                for m in sorted(scheme.months, key=lambda x: x.m)
            ]
        )
        month_edit = st.data_editor(
            month_df,
            num_rows="dynamic",
            key="inc_months",
            use_container_width=True,
            disabled=["Max (2x)"],
            column_config={
                "Monthly sales": st.column_config.NumberColumn(format="RM %.0f"),
                "Accum. sales": st.column_config.NumberColumn(format="RM %.0f"),
                "Base": st.column_config.NumberColumn(format="RM %.0f"),
                "Max (2x)": st.column_config.NumberColumn(format="RM %.0f"),
            },
        )

        if st.button("Save incentive scheme"):
            new_months: list[IncentiveMonth] = []
            bad = False
            for _, r in month_edit.iterrows():
                key = str(r["Month"] or "").strip()
                if not key:
                    continue
                if not re.fullmatch(r"\d{4}-\d{2}", key):
                    st.error(f"Bad month key '{key}' — use YYYY-MM.")
                    bad = True
                    continue
                new_months.append(
                    IncentiveMonth(
                        m=int(r["M"] or 0),
                        key=key,
                        monthly_sales_target=float(r["Monthly sales"] or 0),
                        accumulated_sales_target=float(r["Accum. sales"] or 0),
                        returning_target=int(r["Returning"] or 0),
                        base_incentive=float(r["Base"] or 0),
                    )
                )
            if not re.fullmatch(r"\d{4}-\d{2}", start_month.strip()):
                st.error("Start month must be YYYY-MM.")
                bad = True
            if not bad:
                scheme.start_month = start_month.strip()
                scheme.min_order_value = float(min_order)
                scheme.gp_threshold_pct = float(gp_threshold)
                scheme.gp_basis = gp_basis
                scheme.part_b_gate = part_b_gate
                scheme.max_discount_rate_pct = float(max_discount)
                scheme.discount_basis = discount_basis
                scheme.discount_scope = discount_scope
                scheme.sales_basis = sales_basis
                scheme.returning_scope = returning_scope
                scheme.returning_count = returning_count
                scheme.service_keywords = [
                    k.strip() for k in service_kw.splitlines() if k.strip()
                ]
                scheme.months = sorted(new_months, key=lambda x: x.m)
                save_scheme(scheme)
                _reload_settings()
                _save_and_sync(SCHEME_FILE, "SA incentive scheme")


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
        options=[
            "Upload & Review",
            "Commission Report",
            "SA Incentive",
            "Settings",
        ],
        label_visibility="collapsed",
    )
    st.sidebar.divider()
    st.sidebar.caption(f"🔖 Build `{_BUILD_VERSION}`")
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
    elif page == "SA Incentive":
        page_incentive()
    else:
        page_settings()


if __name__ == "__main__":
    main()
