# LB International — Sales Commission Calculator

Streamlit web app for monthly Sales Advisor (SA) commission calculation from
EasyStore order exports. Parses the seller-written `Note` field, applies the
Maybank merchant rate card to derive net sales, and computes per-SA
commission based on a whole-bracket tier table (with channel-specific
overrides like the TikTok flat rule).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

Then open the URL Streamlit prints (usually `http://localhost:8501`).

## Test

```bash
pytest tests/ -v
```

## Folder layout

```
app.py                     Streamlit UI (4 pages)
commission/
    __init__.py
    models.py              Pydantic models (ParsedNote, OrderResult, …)
    parser.py              Seller-note parser (the heart of the system)
    charges.py             Bank-charge calculator
    commission_engine.py   Tier lookup + per-SA aggregation
    incentive.py           SA Return Customer & Sales Growth Incentive
    costs.py               Accumulating SKU → cost-price store
    aggregator.py          CSV → list[OrderResult] pipeline
    excel_export.py        Multi-sheet workbook builder
    settings.py            Load/save data/*.json
data/
    sa_list.json           Active SAs
    tiers.json             Commission brackets + channel flat rules
    rates.json             Versioned merchant rate card
    incentive_scheme.json  SA incentive targets (Part A / Part B, M1–M12)
    incentive_history.json Saved monthly incentive figures (accumulates)
    sku_costs.json         SKU → cost price, for the 30% gross-profit gate
tests/
    test_parser.py         Real-note fixtures (21 cases, all from sample_data.csv)
    test_incentive.py      SA incentive: qualification, Part A/B, accumulation
sample_data.csv            Real EasyStore export for development
```

## How the parser works

The seller note is the **authoritative** source for SA attribution and
payment method — `Transaction gateway` and `Transaction method` columns from
EasyStore are ignored, since SAs frequently log split payments and trade-ins
that EasyStore can't represent.

For each note the parser produces a `ParsedNote` containing:

- `sa_shares`: list of `(SA name, share)` tuples summing to 1.0
- `payments`: list of `PaymentPortion` (method, amount, optional last 4 digits)
- `review_flags`: human-readable reasons the order needs manual attention

### SA detection

1. **Explicit split** — regex finds `NAME N% NAME N%` patterns and fuzzy-
   matches each name against the active SA list (rapidfuzz, threshold 85).
   `MINKEI 70% / LILY 30%` → `[("MINKEI", 0.7), ("LILY", 0.3)]`.
2. **House account** — if `COMPANY SALES` appears anywhere in the note, the
   order is attributed 100% to the house account (no commission paid).
3. **Single SA** — first 1–3 lines are tokenized; first token that fuzzy-
   matches the SA list wins.
4. **Empty note** — falls back to channel: `online_store` and `tiktok-shop`
   default to `COMPANY SALES` and flag the order for review. Otherwise the
   order goes to the review queue with no SA assigned.

### Payment detection

Each non-empty line is scanned for the **longest** matching keyword (so
`VISA CREDIT` wins over `VISA`, and `DEBIT MASTERCARD` wins over
`MASTERCARD`). Recognised keywords:

| Keyword(s)                             | Method               |
|----------------------------------------|----------------------|
| `DEBIT MASTERCARD`, `MASTERCARD DEBIT` | `MASTERCARD_DEBIT`   |
| `MASTERCARD CREDIT`, plain `MASTERCARD` | `MASTERCARD_CREDIT` |
| `DEBIT VISA`, `VISA DEBIT`             | `VISA_DEBIT`         |
| `VISA CREDIT`, plain `VISA`            | `VISA_CREDIT`        |
| `MYDEBIT`                              | `MYDEBIT`            |
| `AMEX`, `JCB`, `UPI`, `MAESTRO`        | (literal)            |
| `SENANGPAY`, `SENANG PAY`              | `SENANGPAY_CARD` (or `SENANGPAY_FPX` if `FPX` is mentioned) |
| `ONLINE TRANSFER`, `BANK TRANSFER`     | `BANK_TRANSFER`      |
| `TOUCH AND GO`, `TNG`                  | `TNG`                |
| `TIKTOK PAYMENT`, `TIKTOKPAY`          | `TIKTOK`             |
| `CASH`                                 | `CASH`               |
| `TRADE IN`                             | `TRADE_IN`           |

Per portion, the parser extracts:

- **Amount(s)**: every `RM<number>` on the line, summed. So
  `RM5000+RM4000+RM700` becomes `9700.00`.
- **Last 4 digits**: the first standalone 4-digit number on the line, after
  masking out the RM amounts so e.g. `RM4590` is not picked up. So
  `MASTERCARD 5403 RM4590` → `last4="5403"`.
- If the line has no amount, that portion is implicit and absorbs
  `order_total − sum(other portions)`.

### Validation flags

- Sum of parsed amounts != order total (>RM1) → flagged. **Suppressed** for
  TikTok-shop orders, where the seller-note amount is the post-platform-fee
  net and the gap is expected.
- Bare `SENANGPAY` with no card/FPX hint → defaults to card and is flagged.
- Multiple implicit-amount portions → flagged (cannot auto-allocate).
- No SA detected → flagged.
- No payment method detected → flagged.

## How the charge calculator works

For each `PaymentPortion`:

- Methods in `ZERO_CHARGE_METHODS` (`BANK_TRANSFER`, `CASH`, `TRADE_IN`,
  `TIKTOK`, `TNG`) → 0% charge.
- `SENANGPAY_CARD` / `SENANGPAY_FPX` → `senangpay_card_pct` /
  `senangpay_fpx_pct` from the rate version active on the order's date.
- Card methods → looked up in the active rate version by
  `(method, is_foreign)`. Defaults to LOCAL when the note doesn't specify.
- Unconfigured rate (rate_pct is `null` in `data/rates.json`) → 0% with a
  warning that surfaces in the UI's review queue.

## How the commission engine works

1. **Build contributions**: each kept order is exploded into one
   `SAContribution` per `SAShare`. A 70/30 split on a RM 10,000 net order
   produces RM 7,000 to one SA and RM 3,000 to the other.
2. **Per-SA monthly net total** = sum of all the SA's `net_share` values.
3. **Tier**: the SA's monthly net is matched against the tier table
   (whole-bracket — net of RM 250,000 falls in the 1.00% bracket and earns
   RM 2,500, *not* a progressive blend).
4. **Channel flat rules**: orders whose `Channel` matches a configured rule
   (default: `tiktok-shop` → RM 10/order) bypass the tier rate for *that
   order's commission only*; their net still feeds the SA's monthly tier
   total (so a TikTok-heavy month can still push an SA into a higher
   bracket on their non-TikTok orders).
5. **Output**: per-SA commission is `Σ(non-flat order shares × tier_rate) +
   Σ(flat orders × flat_amount × share_pct)`.

## Configuration

All three JSON files in `data/` are user-editable from the **Settings** page
of the app. Edits persist across runs.

- `sa_list.json`: SAs known to the parser. Fuzzy-matching threshold is 85
  (typos that are 1–2 characters off the canonical name will still match).
- `tiers.json`: commission brackets (any number, in any order) plus channel
  flat rules.
- `rates.json`: versioned merchant rate card. Each version has an
  `effective_from` date; the engine picks the latest version whose date is
  ≤ the order's date. Rate fields can be `null` when not yet known — those
  default to 0% with a warning.

Default rates (at first run) — **only `MYDEBIT (0.45%)` is filled in**.
Maybank-issued rates need to be entered on the Settings → Card rates page
before charges become non-zero for cards.

## Excluded vs review queue

- **Excluded** (no commission impact, shown for transparency):
  - `Order Status == Cancelled`
  - `Financial Status` is anything other than `Paid` (toggle in the UI to
    include unpaid orders for forecasting)
- **Review queue** (still counted, but flagged for manual fix):
  - any of the parser flags listed above

The Review queue lets you edit SAs, payment method, last-4 and amount inline
via `st.data_editor`. Saving an override replaces the parser output for that
order; recompute the report to see the new numbers.

## Excel export

The **Download Excel Report** button on the Commission Report page builds a
multi-sheet `.xlsx`:

- `Summary` — per-SA totals and commission, with a totals row.
- One sheet per SA — full audit trail: every order, every payment portion,
  the rate row applied, the charge, the net, the share %, and the
  contribution to the SA's monthly total.
- `Review log` — every flagged order with the raw note and flags.
- `Excluded` — every excluded order with the reason.
- `Settings snapshot` — the SA list, tiers, channel flat rules, and active
  rate version at the moment the report was generated.


## SA Return Customer & Sales Growth Incentive

A **separate** scheme from the tier commission above, paid on top of it and on
top of the Year-End Sales Bonus. Runs M1 = Sep 2026 → M12 = Aug 2027. Lives in
`commission/incentive.py` and gets its own page in the app; it never changes a
`SACommission` figure.

Each month, per SA, two parts are assessed **independently** on totals
**accumulated since Sep 2026** — not on the month alone:

| Part | Test | Pays |
|------|------|------|
| A | accumulated returning customers ≥ target (10 → 220) | 1× base |
| B | accumulated qualifying sales ≥ target (280K → 4.57M) **and** the month's discount rate ≤ 30% | 1× base |

Part B's quality gate is the **discount rate**: of the SA's orders that month,
how many carried any discount (order-level or line-item). Size is not graded —
an order either carried a discount or it did not.

Note the two denominators differ on purpose. The sales target counts only
*qualifying* orders (≥ RM1,000, non-service, fully paid). The discount rate
counts **every paid order including those under RM1,000** — such an order can
never reach the sales target, but discounting on it is still discounting.
Service-only orders are out of both. Set `discount_scope` to `qualifying` to
make the two sets match again.

The gross-profit test it replaced is still implemented and still shown; switch
`part_b_gate` in Settings to `gross_profit`, `both` or `none` to change which
gate applies.

One part = 1× base, both = 2× base, neither = nothing (no carry-forward as a
debt). Base runs RM200 (M1) → RM1,700 (M12).

### What qualifies

- the order must be kept by the main pipeline (fully paid, not cancelled)
- order total ≥ RM1,000 — tested on the whole order, not the SA's share
- service revenue (bag spa, polish, …) is stripped out; a service-only order
  counts no sales and makes nobody a returning customer
- **event / sale stock counts** — that exclusion was removed from the scheme on
  25 Sep 2026
- sales and customers are credited by the SA's share % on the order

### The two data sources it needs

**Cost prices.** Only needed when the Part B gate includes gross profit. The
orders export has no cost column; the *products* export has `SKU` +
`Cost Price`. Upload one on the SA Incentive page and it is merged into
`data/sku_costs.json`, which **accumulates** — an item sold and delisted keeps
its cost, so coverage grows month by month. The page shows what percentage of
qualifying revenue has a known cost; below 90% the GP figure is flagged as an
estimate. The discount-rate gate needs none of this: discounts come straight
off the order export.

**Customer history.** A buyer is only *returning* if the SA sold to them
before, so purchases from before Sep 2026 have to be seeded once (a button on
the same page reads them out of any loaded export that reaches back far
enough). Without the seed nobody can be returning in M1.

### Accumulation

Part A and Part B need the earlier months, so each finalised month is saved to
`data/incentive_history.json` from the SA Incentive page. The month being
reported is always recomputed from the current CSV — only prior months come
from history — so re-running a month reflects the latest data and settings.

### Configurable in Settings → SA incentive scheme

Start month, minimum order value, which quality gate Part B carries
(`discount_rate`, `gross_profit`, `both`, `none`), the maximum discount rate
and whether it is measured on the month or accumulated, the GP threshold and
its basis; whether sales count gross or net;
whether "returning" means returning to *this SA* or to LB generally; whether a
customer returning in several months counts once or every time; the service
keyword list; and the full M1–M12 target table.
