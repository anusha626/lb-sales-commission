"""SA Return Customer & Sales Growth Incentive (Year 1, Sep 2026 – Aug 2027).

A *separate* scheme from the tier commission in `commission_engine.py`. It
pays nothing through the tier table and never touches `SACommission` —
`compute_incentives` returns its own report so the UI and the workbook can
show it as a standalone block.

How it pays, per SA, per month:

    Part A  accumulated returning customers >= target  ->  1x base
    Part B  accumulated qualifying sales     >= target  ->  1x base
            AND gross profit >= 30%
    payout = base * (parts hit)            0x / 1x / 2x

Both parts are measured on figures *accumulated since the scheme start month*,
not on the month alone, so the engine needs the months before the one being
reported. Those come from `IncentiveHistory`, which the app writes each time a
payout month is finalised (see `data/incentive_history.json`).

Qualifying rules (from the signed T&C):
  - the order must be kept by the main pipeline (fully paid, not cancelled)
  - order total >= RM1,000 — tested on the whole order, not the SA's share
  - service revenue (bag spa, polish, …) is stripped out, and a customer whose
    order is service-only is not a returning customer
  - event / sale stock DOES count — that exclusion was removed from the scheme
    on 25 Sep 2026
  - sales and customers are credited by the SA's share % on the order
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

from .models import OrderResult
from .parser import HOUSE_ACCOUNT

DATA_DIR = Path(__file__).parent.parent / "data"
SCHEME_FILE = DATA_DIR / "incentive_scheme.json"
HISTORY_FILE = DATA_DIR / "incentive_history.json"


# ---------------------------------------------------------------------------
# Scheme configuration
# ---------------------------------------------------------------------------

class IncentiveMonth(BaseModel):
    m: int                            # 1..12
    key: str                          # "YYYY-MM"
    monthly_sales_target: float
    accumulated_sales_target: float
    returning_target: int
    base_incentive: float


class IncentiveScheme(BaseModel):
    name: str = "SA Return Customer & Sales Growth Incentive"
    year_label: str = "Year 1"
    start_month: str = "2026-09"
    min_order_value: float = 1000.0
    gp_threshold_pct: float = 30.0
    # "accumulated" tests GP on everything since the start month (consistent
    # with Part B's accumulated sales); "month" tests the reported month alone.
    gp_basis: str = "accumulated"
    # Which figure counts toward the sales target: "gross" (order total, after
    # store credit and discount) or "net" (after merchant charges).
    sales_basis: str = "gross"
    # "same_sa": the customer's earlier qualifying purchase must also be this
    # SA's. "company": any earlier purchase at LB counts, and the SA who closes
    # the repeat order takes the credit.
    returning_scope: str = "same_sa"
    # How the accumulated Part A figure adds up when the same person returns in
    # more than one month. "distinct" counts that person once for the year (the
    # literal reading of "returning customers"); "repeat_visits" counts every
    # repeat purchase, so a loyal customer can be counted again each month.
    returning_count: str = "distinct"
    service_keywords: list[str] = Field(default_factory=list)
    months: list[IncentiveMonth] = Field(default_factory=list)

    def month_for(self, key: str) -> IncentiveMonth | None:
        for m in self.months:
            if m.key == key:
                return m
        return None

    def months_upto(self, key: str) -> list[IncentiveMonth]:
        """Scheme months from the start through `key` inclusive, in order."""
        out = [m for m in sorted(self.months, key=lambda x: x.key) if m.key <= key]
        return out

    def is_service_name(self, name: str) -> bool:
        n = (name or "").upper()
        return any(k.upper() in n for k in self.service_keywords if k.strip())


def load_scheme(path: Path = SCHEME_FILE) -> IncentiveScheme:
    return IncentiveScheme.model_validate_json(path.read_text(encoding="utf-8"))


def save_scheme(cfg: IncentiveScheme, path: Path = SCHEME_FILE) -> None:
    path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-month stored figures
# ---------------------------------------------------------------------------

class MonthFigures(BaseModel):
    """One SA's qualifying figures for one month — what gets accumulated."""

    qualifying_sales: float = 0.0
    qualifying_orders: int = 0
    # Gross profit on the qualifying sales, and how much of that revenue had a
    # known cost. gp_pct is computed on the covered portion only, so a low
    # coverage figure means the percentage is an estimate.
    gp_revenue: float = 0.0      # revenue with a known cost (the GP base)
    gp_profit: float = 0.0       # that revenue minus its cost
    uncosted_sales: float = 0.0  # qualifying revenue whose SKU had no cost
    # Customer keys with a qualifying order this month, credited to this SA.
    customers: list[str] = Field(default_factory=list)
    # Of those, the ones that were a repeat (counted for Part A this month).
    returning: list[str] = Field(default_factory=list)

    @property
    def gp_pct(self) -> float:
        return round(self.gp_profit / self.gp_revenue * 100.0, 2) if self.gp_revenue else 0.0

    @property
    def cost_coverage_pct(self) -> float:
        total = self.gp_revenue + self.uncosted_sales
        return round(self.gp_revenue / total * 100.0, 1) if total else 0.0


class IncentiveHistory(BaseModel):
    """Saved figures per month per SA, plus pre-scheme customer history.

    `prior_customers` seeds the returning-customer test with purchases made
    before the scheme started — without it nobody could be "returning" in M1.
    """

    months: dict[str, dict[str, MonthFigures]] = Field(default_factory=dict)
    prior_customers: dict[str, list[str]] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = HISTORY_FILE) -> "IncentiveHistory":
        if not path.exists():
            return cls()
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return cls()

    def save(self, path: Path = HISTORY_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")

    def figures(self, month_key: str, sa: str) -> MonthFigures | None:
        return self.months.get(month_key, {}).get(sa)

    def put(self, month_key: str, sa: str, fig: MonthFigures) -> None:
        self.months.setdefault(month_key, {})[sa] = fig

    def customers_before(self, month_key: str, sa: str, *, scope: str) -> set[str]:
        """Customer keys seen before `month_key`. With scope 'same_sa' only
        this SA's; with 'company' every SA's, plus the pre-scheme seed."""
        seen: set[str] = set()
        if scope == "same_sa":
            seen.update(self.prior_customers.get(sa, []))
        else:
            for keys in self.prior_customers.values():
                seen.update(keys)
        for mk, per_sa in self.months.items():
            if mk >= month_key:
                continue
            for name, fig in per_sa.items():
                if scope == "same_sa" and name != sa:
                    continue
                seen.update(fig.customers)
        return seen


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

class SAIncentive(BaseModel):
    """One SA's incentive outcome for one payout month."""

    sa_name: str
    month_key: str
    month_index: int                 # 1..12 (M1..M12)
    base_incentive: float

    # This month
    month_sales: float = 0.0
    month_orders: int = 0
    month_returning: int = 0
    month_gp_pct: float = 0.0

    # Accumulated since the scheme start
    accum_sales: float = 0.0
    accum_returning: int = 0
    accum_gp_pct: float = 0.0
    cost_coverage_pct: float = 0.0

    # Targets and outcome
    sales_target: float = 0.0
    returning_target: int = 0
    gp_threshold_pct: float = 30.0
    part_a_hit: bool = False
    part_b_hit: bool = False
    gp_gate_passed: bool = False
    payout: float = 0.0

    # Working detail for the UI
    qualifying_order_numbers: list[str] = Field(default_factory=list)
    returning_customers: list[str] = Field(default_factory=list)
    excluded_note: str = ""

    @property
    def parts_hit(self) -> int:
        return int(self.part_a_hit) + int(self.part_b_hit)

    @property
    def multiplier_label(self) -> str:
        return {0: "—", 1: "1x", 2: "2x"}[self.parts_hit]


class IncentiveReport(BaseModel):
    month_key: str
    month_index: int
    scheme_name: str = ""
    sa_results: list[SAIncentive] = Field(default_factory=list)
    # Figures computed for this month, ready to be written to history.
    month_figures: dict[str, MonthFigures] = Field(default_factory=dict)
    cost_store_size: int = 0

    @property
    def total_payout(self) -> float:
        return round(sum(r.payout for r in self.sa_results), 2)


# ---------------------------------------------------------------------------
# Customer identity
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def hash_identity(raw: str) -> str:
    """Stable, non-reversible key for one customer identity.

    The saved history is committed to the repo so the deployed app keeps its
    accumulated figures across restarts — and this repo must never carry
    customer PII. Returning-customer detection only ever asks "is this the
    same person as before", which equality on a hash answers just as well as
    the email itself. Nothing in the UI reads labels out of history; they come
    from the CSV currently loaded.
    """
    if not raw:
        return ""
    return "c:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def customer_key(email: str, phone: str, first: str, last: str) -> tuple[str, str]:
    """(hashed key, human label) for returning-customer matching.

    Email wins (88% populated in a real export), then phone (74%), then name.
    Phones are reduced to their last 9 digits so 0164108795 and 60164108795
    are the same person. The label is for display only and is never persisted.
    """
    email = (email or "").strip().lower()
    if email:
        label = f"{(first or '').strip()} {(last or '').strip()}".strip() or email
        return hash_identity(f"e:{email}"), label
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) >= 8:
        label = f"{(first or '').strip()} {(last or '').strip()}".strip() or digits
        return hash_identity(f"p:{digits[-9:]}"), label
    name = _WS.sub(" ", f"{(first or '').strip()} {(last or '').strip()}".strip()).upper()
    if name:
        return hash_identity(f"n:{name}"), name.title()
    return "", ""


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------

def qualifying_amount(order: OrderResult, scheme: IncentiveScheme) -> dict | None:
    """The part of `order` that counts toward the incentive, or None if the
    whole order is disqualified.

    Returns {sales, gp_revenue, gp_profit, uncosted, service, is_service_only}.
    `sales` is before the SA split — callers apply each share.
    """
    if order.excluded:
        return None
    base = order.net_total if scheme.sales_basis == "net" else order.gross_total
    if order.gross_total < scheme.min_order_value:
        return None

    items = order.line_items or []
    if not items:
        # No line-item rows in the export — take the order at face value and
        # treat cost as unknown, so it lands in `uncosted` and drags coverage
        # down visibly rather than silently faking a margin.
        return {
            "sales": round(base, 2),
            "gp_revenue": 0.0,
            "gp_profit": 0.0,
            "uncosted": round(base, 2),
            "service": 0.0,
            "is_service_only": False,
        }

    item_gross = sum(i.gross for i in items)
    service = sum(i.gross for i in items if scheme.is_service_name(i.name))
    goods = item_gross - service
    if goods <= 0:
        # Service-only order: no qualifying sales and no returning customer.
        return {
            "sales": 0.0, "gp_revenue": 0.0, "gp_profit": 0.0,
            "uncosted": 0.0, "service": round(service, 2),
            "is_service_only": True,
        }

    # Scale line values onto the order's actual money (Total Amount less store
    # credit, or net of charges) so discounts and shipping don't skew the total.
    scale = (base / item_gross) if item_gross else 1.0

    gp_revenue = gp_profit = uncosted = 0.0
    for i in items:
        if scheme.is_service_name(i.name):
            continue
        rev = i.gross * scale
        ct = i.cost_total
        if ct is None:
            uncosted += rev
        else:
            gp_revenue += rev
            gp_profit += rev - ct
    return {
        "sales": round(goods * scale, 2),
        "gp_revenue": round(gp_revenue, 2),
        "gp_profit": round(gp_profit, 2),
        "uncosted": round(uncosted, 2),
        "service": round(service * scale, 2),
        "is_service_only": False,
    }


def month_figures_for(
    orders: list[OrderResult],
    scheme: IncentiveScheme,
    history: IncentiveHistory,
    month_key: str,
) -> dict[str, MonthFigures]:
    """Per-SA qualifying figures for one payout month's orders."""
    figures: dict[str, MonthFigures] = defaultdict(MonthFigures)
    # Customers each SA had seen before this month — a repeat against this set
    # is what makes the buyer "returning".
    seen_before: dict[str, set[str]] = {}
    # Within the month, a customer's *first* qualifying order establishes them;
    # a second order in the same month doesn't count them twice.
    counted: dict[str, set[str]] = defaultdict(set)

    for o in sorted(orders, key=lambda x: x.order_date):
        q = qualifying_amount(o, scheme)
        if q is None or q["is_service_only"]:
            continue
        for share in o.parsed.sa_shares:
            sa = share.name
            if sa == HOUSE_ACCOUNT:
                continue
            f = figures[sa]
            f.qualifying_sales = round(f.qualifying_sales + q["sales"] * share.share, 2)
            f.gp_revenue = round(f.gp_revenue + q["gp_revenue"] * share.share, 2)
            f.gp_profit = round(f.gp_profit + q["gp_profit"] * share.share, 2)
            f.uncosted_sales = round(f.uncosted_sales + q["uncosted"] * share.share, 2)
            f.qualifying_orders += 1

            ck = o.customer_key
            if not ck or ck in counted[sa]:
                continue
            counted[sa].add(ck)
            if ck not in f.customers:
                f.customers.append(ck)
            if sa not in seen_before:
                seen_before[sa] = history.customers_before(
                    month_key, sa, scope=scheme.returning_scope
                )
            if ck in seen_before[sa] and ck not in f.returning:
                f.returning.append(ck)
    return dict(figures)


def seed_prior_customers(
    orders: list[OrderResult],
    scheme: IncentiveScheme,
    history: IncentiveHistory,
    *,
    before_month: str | None = None,
) -> dict[str, int]:
    """Record who each SA had already sold to *before* the scheme started.

    Without this nobody can be "returning" in M1 — every customer would look
    brand new. Feed it any pre-scheme orders you have (the same export usually
    reaches back a few months); it merges, so running it again with an older
    export only adds coverage. Returns {SA: customers known} after merging.
    """
    cutoff = before_month or scheme.start_month
    for o in orders:
        if o.order_date.strftime("%Y-%m") >= cutoff:
            continue
        q = qualifying_amount(o, scheme)
        if q is None or q["is_service_only"] or not o.customer_key:
            continue
        for share in o.parsed.sa_shares:
            if share.name == HOUSE_ACCOUNT:
                continue
            keys = history.prior_customers.setdefault(share.name, [])
            if o.customer_key not in keys:
                keys.append(o.customer_key)
    return {sa: len(v) for sa, v in sorted(history.prior_customers.items())}


def compute_incentives(
    orders: list[OrderResult],
    scheme: IncentiveScheme,
    history: IncentiveHistory,
    month_key: str,
    *,
    sa_names: list[str] | None = None,
    cost_store_size: int = 0,
) -> IncentiveReport:
    """Assess Part A and Part B for every SA for the payout month `month_key`.

    `history` supplies the earlier months. The month being reported is computed
    from `orders` and is NOT read from history, so re-running a month always
    reflects the current CSV and settings.
    """
    sched = scheme.month_for(month_key)
    report = IncentiveReport(
        month_key=month_key,
        month_index=sched.m if sched else 0,
        scheme_name=f"{scheme.name} — {scheme.year_label}",
        cost_store_size=cost_store_size,
    )
    if sched is None:
        return report  # month is outside the scheme window

    this_month = month_figures_for(orders, scheme, history, month_key)
    report.month_figures = this_month

    earlier = scheme.months_upto(month_key)[:-1]
    names = set(this_month) | {
        sa for m in earlier for sa in history.months.get(m.key, {})
    }
    if sa_names:
        names &= {n.upper() for n in sa_names}

    qual_orders: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, str] = {}
    for o in orders:
        labels[o.customer_key] = o.customer_label or o.customer_key
        q = qualifying_amount(o, scheme)
        if q is None or q["is_service_only"]:
            continue
        for s in o.parsed.sa_shares:
            if s.name != HOUSE_ACCOUNT:
                qual_orders[s.name].append(o.order_number)

    for sa in sorted(names):
        cur = this_month.get(sa, MonthFigures())
        accum_sales = cur.qualifying_sales
        accum_gp_rev = cur.gp_revenue
        accum_gp_profit = cur.gp_profit
        accum_uncosted = cur.uncosted_sales
        returning_keys: set[str] = set(cur.returning)
        returning_visits = len(cur.returning)
        for m in earlier:
            prev = history.figures(m.key, sa)
            if prev is None:
                continue
            accum_sales = round(accum_sales + prev.qualifying_sales, 2)
            accum_gp_rev = round(accum_gp_rev + prev.gp_revenue, 2)
            accum_gp_profit = round(accum_gp_profit + prev.gp_profit, 2)
            accum_uncosted = round(accum_uncosted + prev.uncosted_sales, 2)
            returning_keys.update(prev.returning)
            returning_visits += len(prev.returning)

        gp_num, gp_den = (
            (cur.gp_profit, cur.gp_revenue)
            if scheme.gp_basis == "month"
            else (accum_gp_profit, accum_gp_rev)
        )
        gp_pct = round(gp_num / gp_den * 100.0, 2) if gp_den else 0.0
        covered_total = accum_gp_rev + accum_uncosted
        coverage = round(accum_gp_rev / covered_total * 100.0, 1) if covered_total else 0.0

        accum_returning = (
            returning_visits
            if scheme.returning_count == "repeat_visits"
            else len(returning_keys)
        )
        part_a = accum_returning >= sched.returning_target
        sales_ok = accum_sales >= sched.accumulated_sales_target
        gp_ok = gp_pct >= scheme.gp_threshold_pct
        part_b = sales_ok and gp_ok

        note = ""
        if sales_ok and not gp_ok:
            note = (
                f"Accumulated sales target met, but gross profit "
                f"{gp_pct:.1f}% is below the {scheme.gp_threshold_pct:.0f}% "
                f"minimum — Part B does not pay."
            )
        if covered_total and coverage < 90.0:
            note = (note + " " if note else "") + (
                f"Cost price is known for only {coverage:.0f}% of qualifying "
                f"revenue — the GP figure is an estimate. Upload a newer "
                f"product export to improve it."
            )

        report.sa_results.append(
            SAIncentive(
                sa_name=sa,
                month_key=month_key,
                month_index=sched.m,
                base_incentive=sched.base_incentive,
                month_sales=cur.qualifying_sales,
                month_orders=cur.qualifying_orders,
                month_returning=len(cur.returning),
                month_gp_pct=cur.gp_pct,
                accum_sales=accum_sales,
                accum_returning=accum_returning,
                accum_gp_pct=gp_pct,
                cost_coverage_pct=coverage,
                sales_target=sched.accumulated_sales_target,
                returning_target=sched.returning_target,
                gp_threshold_pct=scheme.gp_threshold_pct,
                part_a_hit=part_a,
                part_b_hit=part_b,
                gp_gate_passed=gp_ok,
                payout=round(sched.base_incentive * (int(part_a) + int(part_b)), 2),
                qualifying_order_numbers=sorted(set(qual_orders.get(sa, []))),
                returning_customers=sorted(
                    labels.get(k, k) for k in cur.returning
                ),
                excluded_note=note,
            )
        )

    report.sa_results.sort(key=lambda r: (-r.payout, -r.accum_sales, r.sa_name))
    return report
