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
app.py                     Streamlit UI (3 pages)
commission/
    __init__.py
    models.py              Pydantic models (ParsedNote, OrderResult, …)
    parser.py              Seller-note parser (the heart of the system)
    charges.py             Bank-charge calculator
    commission_engine.py   Tier lookup + per-SA aggregation
    aggregator.py          CSV → list[OrderResult] pipeline
    excel_export.py        Multi-sheet workbook builder
    settings.py            Load/save data/*.json
data/
    sa_list.json           Active SAs
    tiers.json             Commission brackets + channel flat rules
    rates.json             Versioned merchant rate card
tests/
    test_parser.py         Real-note fixtures (21 cases, all from sample_data.csv)
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
