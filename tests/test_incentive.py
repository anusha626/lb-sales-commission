"""Tests for the SA Return Customer & Sales Growth Incentive."""
from __future__ import annotations

from datetime import datetime

import pytest

from commission.incentive import (
    IncentiveHistory,
    IncentiveMonth,
    IncentiveScheme,
    MonthFigures,
    compute_incentives,
    customer_key,
    hash_identity,
    month_figures_for,
    qualifying_amount,
    seed_prior_customers,
)
from commission.models import LineItem, OrderResult, ParsedNote, SAShare


def make_scheme(**kw) -> IncentiveScheme:
    base = dict(
        start_month="2026-09",
        min_order_value=1000.0,
        gp_threshold_pct=30.0,
        gp_basis="accumulated",
        part_b_gate="discount_rate",
        max_discount_rate_pct=30.0,
        discount_basis="month",
        discount_scope="all_orders",
        sales_basis="gross",
        returning_scope="same_sa",
        service_keywords=["SPA", "POLISH", "SERVICE"],
        months=[
            IncentiveMonth(
                m=1, key="2026-09", monthly_sales_target=280000.0,
                accumulated_sales_target=280000.0, returning_target=10,
                base_incentive=200.0,
            ),
            IncentiveMonth(
                m=2, key="2026-10", monthly_sales_target=330000.0,
                accumulated_sales_target=610000.0, returning_target=25,
                base_incentive=300.0,
            ),
        ],
    )
    base.update(kw)
    return IncentiveScheme(**base)


def make_order(
    number="#1", gross=10000.0, sa="MINKEI", share=1.0, items=None,
    when="2026-09-05", email="a@b.com", excluded=False, net=None,
    discount=0.0,
) -> OrderResult:
    return OrderResult(
        order_number=number,
        order_date=datetime.fromisoformat(when),
        channel="pos",
        financial_status="Paid",
        order_status="Open",
        gross_total=gross,
        net_total=gross if net is None else net,
        discount_total=discount,
        parsed=ParsedNote(sa_shares=[SAShare(name=sa, share=share)], raw_note=""),
        line_items=items if items is not None else [
            LineItem(sku="S1", name="PREOWNED BAG", price=gross, qty=1, cost=gross * 0.5)
        ],
        customer_key=hash_identity(f"e:{email}") if email else "",
        customer_label=email,
        excluded=excluded,
    )


# --- qualification --------------------------------------------------------

def test_order_below_minimum_does_not_qualify():
    scheme = make_scheme()
    assert qualifying_amount(make_order(gross=999.0), scheme) is None


def test_order_at_minimum_qualifies():
    scheme = make_scheme()
    q = qualifying_amount(make_order(gross=1000.0), scheme)
    assert q is not None and q["sales"] == 1000.0


def test_excluded_order_never_qualifies():
    scheme = make_scheme()
    assert qualifying_amount(make_order(excluded=True), scheme) is None


def test_service_revenue_is_stripped_out():
    scheme = make_scheme()
    order = make_order(gross=5000.0, items=[
        LineItem(sku="B1", name="PREOWNED BAG", price=4800.0, qty=1, cost=2000.0),
        LineItem(sku="SV", name="893808 polish service", price=200.0, qty=1, cost=0.0),
    ])
    q = qualifying_amount(order, scheme)
    assert q["service"] == pytest.approx(200.0)
    assert q["sales"] == pytest.approx(4800.0)


def test_service_only_order_counts_nobody():
    scheme = make_scheme()
    order = make_order(gross=1500.0, items=[
        LineItem(sku="SV", name="BAG SPA DELUXE", price=1500.0, qty=1, cost=100.0),
    ])
    q = qualifying_amount(order, scheme)
    assert q["is_service_only"] is True and q["sales"] == 0.0


def test_event_stock_counts_since_the_exclusion_was_removed():
    """Event / sale stock was dropped from the exclusions on 25 Sep 2026, so an
    (EVENT)-titled item counts toward the sales target like any other."""
    scheme = make_scheme()
    order = make_order(gross=2890.0, items=[
        LineItem(sku="E1", name="(EVENT) PREOWNED PRADA GALLERIA",
                 price=2890.0, qty=1, cost=1300.0),
    ])
    q = qualifying_amount(order, scheme)
    assert q["sales"] == pytest.approx(2890.0)


def test_unknown_cost_lands_in_uncosted_not_in_gp():
    scheme = make_scheme()
    order = make_order(gross=4000.0, items=[
        LineItem(sku="X", name="PREOWNED BAG", price=4000.0, qty=1, cost=None),
    ])
    q = qualifying_amount(order, scheme)
    assert q["gp_revenue"] == 0.0 and q["uncosted"] == pytest.approx(4000.0)


def test_split_order_credits_each_sa_by_share():
    scheme = make_scheme()
    o = make_order(gross=10000.0)
    o.parsed.sa_shares = [SAShare(name="MINKEI", share=0.7), SAShare(name="LILY", share=0.3)]
    figs = month_figures_for([o], scheme, IncentiveHistory(), "2026-09")
    assert figs["MINKEI"].qualifying_sales == pytest.approx(7000.0)
    assert figs["LILY"].qualifying_sales == pytest.approx(3000.0)


def test_house_account_is_not_an_sa():
    scheme = make_scheme()
    o = make_order(sa="COMPANY SALES")
    assert month_figures_for([o], scheme, IncentiveHistory(), "2026-09") == {}


# --- returning customers --------------------------------------------------

def test_customer_key_prefers_email_then_phone_then_name():
    assert customer_key("A@B.com", "0123", "X", "Y")[0] == hash_identity("e:a@b.com")
    assert customer_key("", "60164108795", "X", "Y")[0] == hash_identity("p:164108795")
    # same person, local and international format
    assert customer_key("", "0164108795", "X", "Y")[0] == hash_identity("p:164108795")
    assert customer_key("", "", "Siti", "Hawa")[0] == hash_identity("n:SITI HAWA")
    assert customer_key("", "", "", "")[0] == ""


def test_stored_keys_carry_no_personal_data():
    """History is committed to the repo, so a key must not be reversible to
    the customer's email, phone or name."""
    key, label = customer_key("someone@gmail.com", "60123456789", "Jane", "Doe")
    assert key.startswith("c:") and len(key) == 22
    for secret in ("someone", "gmail", "60123456789", "jane", "doe"):
        assert secret not in key.lower()
    assert label == "Jane Doe"  # label is display-only, never persisted


def test_first_time_buyer_is_not_returning():
    scheme = make_scheme()
    figs = month_figures_for([make_order()], scheme, IncentiveHistory(), "2026-09")
    assert figs["MINKEI"].returning == []


def test_buyer_with_prior_history_is_returning():
    scheme = make_scheme()
    hist = IncentiveHistory(prior_customers={"MINKEI": [hash_identity("e:a@b.com")]})
    figs = month_figures_for([make_order()], scheme, hist, "2026-09")
    assert figs["MINKEI"].returning == [hash_identity("e:a@b.com")]


def test_same_sa_scope_ignores_another_sas_customer():
    scheme = make_scheme(returning_scope="same_sa")
    hist = IncentiveHistory(prior_customers={"LILY": [hash_identity("e:a@b.com")]})
    figs = month_figures_for([make_order(sa="MINKEI")], scheme, hist, "2026-09")
    assert figs["MINKEI"].returning == []


def test_company_scope_accepts_another_sas_customer():
    scheme = make_scheme(returning_scope="company")
    hist = IncentiveHistory(prior_customers={"LILY": [hash_identity("e:a@b.com")]})
    figs = month_figures_for([make_order(sa="MINKEI")], scheme, hist, "2026-09")
    assert figs["MINKEI"].returning == [hash_identity("e:a@b.com")]


def test_two_orders_same_month_count_the_customer_once():
    scheme = make_scheme()
    hist = IncentiveHistory(prior_customers={"MINKEI": [hash_identity("e:a@b.com")]})
    orders = [make_order("#1"), make_order("#2", when="2026-09-20")]
    figs = month_figures_for(orders, scheme, hist, "2026-09")
    assert len(figs["MINKEI"].returning) == 1
    assert figs["MINKEI"].qualifying_orders == 2


def test_seeding_only_uses_pre_scheme_orders():
    scheme = make_scheme()
    hist = IncentiveHistory()
    orders = [
        make_order("#old", when="2026-06-10", email="old@x.com"),
        make_order("#new", when="2026-09-10", email="new@x.com"),
    ]
    seed_prior_customers(orders, scheme, hist)
    assert hist.prior_customers["MINKEI"] == [hash_identity("e:old@x.com")]


# --- payout ---------------------------------------------------------------

def _big(sa="MINKEI", n=30, when="2026-09-05", gp=0.5, discounted=0):
    """n orders of RM10,000 at the given gross margin; the first `discounted`
    of them carry a discount."""
    return [
        make_order(
            f"#{i}", gross=10000.0, sa=sa, when=when, email=f"c{i}@x.com",
            discount=50.0 if i < discounted else 0.0,
            items=[LineItem(sku=f"S{i}", name="BAG", price=10000.0, qty=1,
                            cost=10000.0 * (1 - gp))],
        )
        for i in range(n)
    ]


def test_neither_part_pays_nothing():
    scheme = make_scheme()
    rep = compute_incentives(_big(n=3), scheme, IncentiveHistory(), "2026-09")
    assert rep.total_payout == 0.0


def test_part_b_only_pays_one_times_base():
    scheme = make_scheme()
    rep = compute_incentives(_big(n=28), scheme, IncentiveHistory(), "2026-09")
    r = rep.sa_results[0]
    assert r.accum_sales == pytest.approx(280000.0)
    assert r.part_b_hit and not r.part_a_hit
    assert r.payout == 200.0


def test_both_parts_pay_two_times_base():
    scheme = make_scheme()
    hist = IncentiveHistory(
        prior_customers={"MINKEI": [hash_identity(f"e:c{i}@x.com") for i in range(12)]}
    )
    rep = compute_incentives(_big(n=28), scheme, hist, "2026-09")
    r = rep.sa_results[0]
    assert r.part_a_hit and r.part_b_hit
    assert r.accum_returning >= 10
    assert r.payout == 400.0


def test_gp_below_threshold_blocks_part_b():
    scheme = make_scheme(part_b_gate="gross_profit")
    rep = compute_incentives(_big(n=28, gp=0.25), scheme, IncentiveHistory(), "2026-09")
    r = rep.sa_results[0]
    assert r.accum_sales >= r.sales_target
    assert not r.gp_gate_passed and not r.part_b_hit and r.payout == 0.0
    assert "gross profit" in r.excluded_note


def test_part_a_alone_still_pays_when_gp_fails():
    """Part A is independent of the Part B quality gate."""
    scheme = make_scheme(part_b_gate="gross_profit")
    hist = IncentiveHistory(
        prior_customers={"MINKEI": [hash_identity(f"e:c{i}@x.com") for i in range(12)]}
    )
    rep = compute_incentives(_big(n=12, gp=0.05), scheme, hist, "2026-09")
    r = rep.sa_results[0]
    assert r.part_a_hit and not r.part_b_hit and r.payout == 200.0


def test_targets_accumulate_across_months():
    """M2 is tested on Sep + Oct together, not October alone."""
    scheme = make_scheme()
    hist = IncentiveHistory()
    hist.put("2026-09", "MINKEI", MonthFigures(
        qualifying_sales=300000.0, qualifying_orders=30,
        gp_revenue=300000.0, gp_profit=150000.0,
        customers=[hash_identity(f"e:s{i}@x.com") for i in range(20)],
        returning=[hash_identity(f"e:s{i}@x.com") for i in range(20)],
    ))
    # 31 October orders: the first 5 are September buyers coming back, the
    # rest are new faces.
    oct_orders = _big(n=31, when="2026-10-05")
    for i in range(5):
        oct_orders[i].customer_key = hash_identity(f"e:s{i}@x.com")
    rep = compute_incentives(oct_orders, scheme, hist, "2026-10")
    r = rep.sa_results[0]
    assert r.accum_sales == pytest.approx(610000.0)   # 300k carried + 310k
    assert r.part_b_hit
    assert r.month_returning == 5                     # the 5 who came back
    # Default "distinct": those 5 already counted in September, so the
    # accumulated figure stays at 20 distinct returning customers.
    assert r.accum_returning == 20
    assert not r.part_a_hit                           # M2 target is 25
    assert r.payout == 300.0                          # Part B only


def test_repeat_visits_mode_counts_a_customer_again_each_month():
    """With returning_count='repeat_visits', a customer who comes back in two
    months counts twice toward the accumulated Part A figure."""
    scheme = make_scheme(returning_count="repeat_visits")
    hist = IncentiveHistory()
    hist.put("2026-09", "MINKEI", MonthFigures(
        qualifying_sales=300000.0, qualifying_orders=30,
        gp_revenue=300000.0, gp_profit=150000.0,
        customers=[hash_identity(f"e:s{i}@x.com") for i in range(20)],
        returning=[hash_identity(f"e:s{i}@x.com") for i in range(20)],
    ))
    oct_orders = _big(n=31, when="2026-10-05")
    for i in range(5):
        oct_orders[i].customer_key = hash_identity(f"e:s{i}@x.com")
    r = compute_incentives(oct_orders, scheme, hist, "2026-10").sa_results[0]
    assert r.accum_returning == 25                    # 20 + the 5 repeats
    assert r.part_a_hit and r.part_b_hit
    assert r.payout == 600.0                          # 2x RM300


def test_month_outside_the_scheme_window_returns_nothing():
    scheme = make_scheme()
    rep = compute_incentives(_big(n=40), scheme, IncentiveHistory(), "2027-12")
    assert rep.sa_results == [] and rep.total_payout == 0.0


def test_history_round_trips_through_json(tmp_path):
    hist = IncentiveHistory(prior_customers={"MINKEI": [hash_identity("e:a@b.com")]})
    hist.put("2026-09", "MINKEI", MonthFigures(qualifying_sales=1234.0))
    p = tmp_path / "h.json"
    hist.save(p)
    back = IncentiveHistory.load(p)
    assert back.figures("2026-09", "MINKEI").qualifying_sales == 1234.0
    assert back.prior_customers["MINKEI"] == [hash_identity("e:a@b.com")]


# --- the Part B discount-rate gate ------------------------------------------

def test_discount_rate_within_ceiling_lets_part_b_pay():
    """28 orders, 8 discounted = 29% — just inside the 30% ceiling."""
    scheme = make_scheme(max_discount_rate_pct=30.0)
    r = compute_incentives(
        _big(n=28, discounted=8), scheme, IncentiveHistory(), "2026-09"
    ).sa_results[0]
    assert r.discount_rate_pct == pytest.approx(28.57, abs=0.01)
    assert r.discount_gate_passed and r.part_b_hit and r.payout == 200.0


def test_too_many_discounted_orders_blocks_part_b():
    """Same sales, but 15 of 28 discounted = 54% — over the ceiling."""
    scheme = make_scheme(max_discount_rate_pct=30.0)
    r = compute_incentives(
        _big(n=28, discounted=15), scheme, IncentiveHistory(), "2026-09"
    ).sa_results[0]
    assert r.accum_sales >= r.sales_target      # the sales target was met
    assert not r.discount_gate_passed and not r.part_b_hit and r.payout == 0.0
    assert "15 of 28 orders were discounted" in r.excluded_note


def test_discount_magnitude_is_not_graded():
    """A RM1 discount counts the same as a RM5,000 one — the gate is a count."""
    scheme = make_scheme(max_discount_rate_pct=30.0)
    orders = _big(n=28, discounted=0)
    for o in orders[:15]:
        o.discount_total = 1.0
    r = compute_incentives(orders, scheme, IncentiveHistory(), "2026-09").sa_results[0]
    assert not r.discount_gate_passed


def test_poor_margin_no_longer_blocks_part_b_under_the_discount_gate():
    """A 5%-margin month passes Part B as long as discounting was restrained —
    the GP test is no longer the gate."""
    scheme = make_scheme(part_b_gate="discount_rate")
    r = compute_incentives(
        _big(n=28, gp=0.05, discounted=2), scheme, IncentiveHistory(), "2026-09"
    ).sa_results[0]
    assert not r.gp_gate_passed          # margin really is bad
    assert r.part_b_hit and r.payout == 200.0


def test_both_gates_require_both():
    scheme = make_scheme(part_b_gate="both", max_discount_rate_pct=30.0)
    good_gp_bad_disc = compute_incentives(
        _big(n=28, gp=0.5, discounted=20), scheme, IncentiveHistory(), "2026-09"
    ).sa_results[0]
    assert not good_gp_bad_disc.part_b_hit
    bad_gp_good_disc = compute_incentives(
        _big(n=28, gp=0.05, discounted=1), scheme, IncentiveHistory(), "2026-09"
    ).sa_results[0]
    assert not bad_gp_good_disc.part_b_hit
    both_good = compute_incentives(
        _big(n=28, gp=0.5, discounted=1), scheme, IncentiveHistory(), "2026-09"
    ).sa_results[0]
    assert both_good.part_b_hit


def test_gate_none_pays_on_the_sales_target_alone():
    scheme = make_scheme(part_b_gate="none")
    r = compute_incentives(
        _big(n=28, gp=0.01, discounted=28), scheme, IncentiveHistory(), "2026-09"
    ).sa_results[0]
    assert r.part_b_hit and r.payout == 200.0


def test_month_basis_ignores_an_earlier_bad_month():
    """With discount_basis='month', a heavy-discount September does not follow
    the SA into October."""
    scheme = make_scheme(discount_basis="month", max_discount_rate_pct=30.0)
    hist = IncentiveHistory()
    hist.put("2026-09", "MINKEI", MonthFigures(
        qualifying_sales=300000.0, qualifying_orders=30, discounted_orders=30,
        gp_revenue=300000.0, gp_profit=150000.0,
    ))
    r = compute_incentives(
        _big(n=31, when="2026-10-05", discounted=0), scheme, hist, "2026-10"
    ).sa_results[0]
    assert r.discount_rate_pct == 0.0 and r.discount_gate_passed and r.part_b_hit


def test_accumulated_basis_carries_an_earlier_bad_month():
    scheme = make_scheme(discount_basis="accumulated", max_discount_rate_pct=30.0)
    hist = IncentiveHistory()
    hist.put("2026-09", "MINKEI", MonthFigures(
        qualifying_sales=300000.0, qualifying_orders=30, discounted_orders=30,
        gp_revenue=300000.0, gp_profit=150000.0,
    ))
    r = compute_incentives(
        _big(n=31, when="2026-10-05", discounted=0), scheme, hist, "2026-10"
    ).sa_results[0]
    # 30 of 61 accumulated orders discounted = 49%
    assert r.discount_rate_pct == pytest.approx(49.18, abs=0.01)
    assert not r.discount_gate_passed and not r.part_b_hit


def test_no_qualifying_orders_is_not_a_discount_failure():
    """An empty denominator must not divide by zero or fail the gate."""
    scheme = make_scheme()
    r = compute_incentives(
        _big(n=1, discounted=0), scheme, IncentiveHistory(), "2026-09"
    ).sa_results[0]
    assert r.discount_rate_pct == 0.0 and r.discount_gate_passed


# --- which orders the discount rate is measured across -----------------------

def test_small_orders_count_toward_the_discount_rate():
    """An order under RM1,000 can't reach the sales target, but discounting on
    it is still discounting — so it lands in the rate."""
    scheme = make_scheme(discount_scope="all_orders")
    orders = _big(n=28, discounted=0)                     # 28 clean big orders
    orders += [
        make_order(f"#s{i}", gross=500.0, discount=50.0, email=f"s{i}@x.com",
                   items=[LineItem(sku=f"T{i}", name="BAG", price=500.0,
                                   qty=1, cost=200.0)])
        for i in range(12)
    ]
    figs = month_figures_for(orders, scheme, IncentiveHistory(), "2026-09")
    f = figs["MINKEI"]
    assert f.qualifying_orders == 28        # sales target ignores the small ones
    assert f.discount_base_orders == 40     # the rate does not
    assert f.discounted_orders == 12
    assert f.discount_rate_pct == pytest.approx(30.0)


def test_qualifying_scope_ignores_small_orders():
    """The old behaviour is still available."""
    scheme = make_scheme(discount_scope="qualifying")
    orders = _big(n=28, discounted=0) + [
        make_order(f"#s{i}", gross=500.0, discount=50.0, email=f"s{i}@x.com")
        for i in range(12)
    ]
    f = month_figures_for(orders, scheme, IncentiveHistory(), "2026-09")["MINKEI"]
    assert f.discount_base_orders == 28 and f.discounted_orders == 0
    assert f.discount_rate_pct == 0.0


def test_small_orders_never_reach_the_sales_total():
    """Including them in the rate must not leak them into Part B's sales."""
    scheme = make_scheme(discount_scope="all_orders")
    orders = _big(n=28, discounted=0) + [
        make_order("#s1", gross=999.0, discount=10.0, email="s1@x.com")
    ]
    r = compute_incentives(orders, scheme, IncentiveHistory(), "2026-09").sa_results[0]
    assert r.accum_sales == pytest.approx(280000.0)   # the RM999 is not in there
    assert r.discount_denominator == 29               # but it is in the rate


def test_discounted_service_order_is_never_counted():
    """A discounted bag spa is not a pricing failure — service is out of the
    rate at any order value."""
    scheme = make_scheme(discount_scope="all_orders")
    orders = _big(n=10, discounted=0) + [
        make_order(f"#sv{i}", gross=190.0, discount=20.0, email=f"v{i}@x.com",
                   items=[LineItem(sku=f"SV{i}", name="893808 polish service",
                                   price=190.0, qty=1, cost=10.0)])
        for i in range(10)
    ]
    f = month_figures_for(orders, scheme, IncentiveHistory(), "2026-09")["MINKEI"]
    assert f.discount_base_orders == 10 and f.discounted_orders == 0


def test_cancelled_orders_stay_out_of_the_rate():
    scheme = make_scheme(discount_scope="all_orders")
    orders = _big(n=10, discounted=0) + [
        make_order("#x", gross=5000.0, discount=500.0, excluded=True)
    ]
    f = month_figures_for(orders, scheme, IncentiveHistory(), "2026-09")["MINKEI"]
    assert f.discount_base_orders == 10 and f.discounted_orders == 0


def test_old_history_without_the_split_still_reads():
    """Figures saved before the denominators were split fall back to the
    qualifying count rather than dividing by zero."""
    fig = MonthFigures(qualifying_orders=20, discounted_orders=5)
    assert fig.discount_base == 20
    assert fig.discount_rate_pct == pytest.approx(25.0)
