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

from commission.aggregator import build_order_results, read_easystore_csv
from commission.commission_engine import compute_commissions
from commission.excel_export import build_workbook
from commission.models import (
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
    RateRow,
    RateTableVersion,
    SARecord,
    load_all,
    save_rates,
    save_sa_list,
    save_tiers,
)

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

def page_report() -> None:
    st.title("Commission Report")

    orders = st.session_state.get("orders")
    settings: AppSettings = st.session_state["settings"]
    if not orders:
        st.info("Upload a CSV on the **Upload & Review** page first.")
        return

    report = compute_commissions(orders, settings.tiers)
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

    if summaries:
        st.subheader("Net sales by SA")
        chart_df = pd.DataFrame(
            {
                "SA": [s.sa_name for s in summaries],
                "Net sales (RM)": [s.total_net_sales for s in summaries],
            }
        ).set_index("SA")
        st.bar_chart(chart_df)

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

                with st.expander("Order-by-order breakdown"):
                    rows = [
                        {
                            "Order #": c.order_number,
                            "Date": c.order_date.strftime("%Y-%m-%d"),
                            "Share %": f"{c.share_pct*100:.0f}%",
                            "Gross share": c.gross_share,
                            "Net share": c.net_share,
                        }
                        for c in s.contributions
                    ]
                    if rows:
                        st.dataframe(
                            pd.DataFrame(rows),
                            hide_index=True,
                            use_container_width=True,
                            column_config={
                                "Gross share": st.column_config.NumberColumn(format="RM %.2f"),
                                "Net share": st.column_config.NumberColumn(format="RM %.2f"),
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
            rows = [
                {
                    "Order #": c.order_number,
                    "Date": c.order_date.strftime("%Y-%m-%d"),
                    "Share %": f"{c.share_pct*100:.0f}%",
                    "Gross share": c.gross_share,
                    "Net share": c.net_share,
                }
                for c in house.contributions
            ]
            if rows:
                st.dataframe(
                    pd.DataFrame(rows),
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "Gross share": st.column_config.NumberColumn(format="RM %.2f"),
                        "Net share": st.column_config.NumberColumn(format="RM %.2f"),
                    },
                )

    st.divider()
    xlsx = build_workbook(orders, report, settings)
    st.download_button(
        "Download Excel Report",
        data=xlsx,
        file_name=f"commission_report_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
            st.success("Saved.")

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
                settings.rates.versions[active_idx] = RateTableVersion(
                    effective_from=new_eff,
                    senangpay_card_pct=new_card,
                    senangpay_fpx_pct=new_fpx,
                    rates=new_rows,
                )
                save_rates(settings.rates)
                _reload_settings()
                st.success("Saved.")
        with c_new:
            if st.button("Add new version (copy of current)"):
                copy = version.model_copy(deep=True)
                copy.effective_from = date.today()
                settings.rates.versions.append(copy)
                save_rates(settings.rates)
                _reload_settings()
                st.success("New version added — switch to it via the dropdown.")
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
            st.success("Saved.")


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
