"""Parse EasyStore seller notes into structured data.

The seller note (free text the SA writes in the EasyStore Note field) is the
authoritative source for: which Sales Advisor(s) own the sale, how revenue is
split between them, and how the customer paid. The Transaction gateway and
Transaction method columns from EasyStore are NOT used for payment detection.

Public entry point: parse_seller_note().
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from .models import ParsedNote, PaymentMethod, PaymentPortion, SAShare

# Default SAs (loaded at runtime from data/sa_list.json by callers).
DEFAULT_SAS: list[str] = ["EILEEN", "MINKEI", "LILY", "CHLOE", "MP"]
HOUSE_ACCOUNT = "COMPANY SALES"

# Fuzzy-match threshold for SA name detection.
SA_FUZZY_THRESHOLD = 85

# Channel-based fallback when the seller note is empty.
ONLINE_CHANNELS = {"online_store", "tiktok-shop"}


# ---------------------------------------------------------------------------
# Payment-keyword detection
# ---------------------------------------------------------------------------

# Order matters: longest / most-specific keywords first so "VISA CREDIT" wins
# over "VISA", "DEBIT MASTERCARD" wins over "MASTERCARD", etc.
@dataclass(frozen=True)
class _Keyword:
    pattern: str
    method: PaymentMethod


_KEYWORDS: tuple[_Keyword, ...] = (
    _Keyword("DEBIT MASTERCARD", PaymentMethod.MASTERCARD_DEBIT),
    _Keyword("MASTERCARD DEBIT", PaymentMethod.MASTERCARD_DEBIT),
    _Keyword("CREDIT MASTERCARD", PaymentMethod.MASTERCARD_CREDIT),
    _Keyword("MASTERCARD CREDIT", PaymentMethod.MASTERCARD_CREDIT),
    _Keyword("DEBIT VISA", PaymentMethod.VISA_DEBIT),
    _Keyword("VISA DEBIT", PaymentMethod.VISA_DEBIT),
    _Keyword("CREDIT VISA", PaymentMethod.VISA_CREDIT),
    _Keyword("VISA CREDIT", PaymentMethod.VISA_CREDIT),
    _Keyword("TIKTOK PAYLATER", PaymentMethod.TIKTOK),
    _Keyword("TIKTOK PAY LATER", PaymentMethod.TIKTOK),
    _Keyword("TIKTOK PAYMENT", PaymentMethod.TIKTOK),
    _Keyword("TIKTOKPAY", PaymentMethod.TIKTOK),
    _Keyword("TIKTOK PAY", PaymentMethod.TIKTOK),
    _Keyword("ONLINE TRANSFER", PaymentMethod.BANK_TRANSFER),
    _Keyword("BANK TRANSFER", PaymentMethod.BANK_TRANSFER),
    _Keyword("TOUCH AND GO", PaymentMethod.TNG),
    _Keyword("TOUCH N GO", PaymentMethod.TNG),
    _Keyword("TRADE IN", PaymentMethod.TRADE_IN),
    _Keyword("TRADE-IN", PaymentMethod.TRADE_IN),
    _Keyword("SENANG PAY", PaymentMethod.SENANGPAY_CARD),
    _Keyword("SENANGPAY", PaymentMethod.SENANGPAY_CARD),
    _Keyword("MASTERCARD", PaymentMethod.MASTERCARD_CREDIT),
    # Common seller-shorthand for MASTERCARD. The longest-match keyword
    # search already prefers "MASTERCARD" when both are present, so this
    # only fires on notes like "MINKEI WALK IN PJ MASTER 3680 RM4690".
    _Keyword("MASTER", PaymentMethod.MASTERCARD_CREDIT),
    _Keyword("MYDEBIT", PaymentMethod.MYDEBIT),
    _Keyword("MAESTRO", PaymentMethod.MAESTRO),
    _Keyword("AMEX", PaymentMethod.AMEX),
    _Keyword("JCB", PaymentMethod.JCB),
    _Keyword("UPI", PaymentMethod.UPI),
    _Keyword("VISA", PaymentMethod.VISA_CREDIT),
    _Keyword("TNG", PaymentMethod.TNG),
    _Keyword("ALIPAY", PaymentMethod.ALIPAY),
    _Keyword("ALI PAY", PaymentMethod.ALIPAY),
    _Keyword("CASH", PaymentMethod.CASH),
)

# Synthetic keyword returned by the fuzzy "TRANSFER" fallback in _find_keyword.
_TRANSFER_KW = _Keyword("TRANSFER", PaymentMethod.BANK_TRANSFER)


_AMOUNT_RE = re.compile(r"RM\s?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_FOUR_DIGIT_RE = re.compile(r"\b(\d{4})\b")
# Split notation: "MINKEI 70% LILY 30%", "MINKEI 60% / LILY 40%", "MINKEI 60%/LILY 40%".
# The name is captured as up to TWO words so an SA name typed with a stray space
# ("MIN KEI 40%") is matched as a whole, not truncated to its last word ("KEI").
_SPLIT_RE = re.compile(
    r"([A-Z]+(?:\s+[A-Z]+)?)\s*(\d{1,3})\s*%",
    re.IGNORECASE,
)
# Amount-split notation: each SA followed by their sales amount in RM, e.g.
# "CHLOE RM1350 MINKEI RM5050". Each SA's share is amount / sum(amounts). The
# second word is only taken when it isn't the "RM<amount>" itself.
_AMOUNT_SHARE_RE = re.compile(
    r"([A-Z]+(?:\s+(?!RM\d)[A-Z]+)?)\s*RM\s?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
# Tokens that may sit before an "RM<amount>" but are never an SA name —
# payment methods, channels and location words. Fuzzy matching rejects most,
# but these are listed explicitly as a cheap guard.
_NON_SA_TOKENS = {
    "WALK", "WALKIN", "WHATSAPP", "CHATDADDY", "TIKTOK", "TIKTOKPAY",
    "ONLINE", "STORE", "CASH", "MASTER", "MASTERCARD", "VISA", "AMEX",
    "JCB", "UPI", "TNG", "MYDEBIT", "MAESTRO", "FPX", "MBB", "MAYBANK",
    "SENANGPAY", "TRADE", "DEPOSIT", "BALANCE", "TOTAL", "PJ", "KL", "ALIPAY",
}


def _find_keyword(line: str) -> tuple[_Keyword, int, int] | None:
    """Return (keyword, start, end) of the first matching keyword in `line`,
    preferring the longest/most-specific match. Case-insensitive whole-word.
    """
    upper = line.upper()
    best: tuple[_Keyword, int, int] | None = None
    for kw in _KEYWORDS:
        idx = upper.find(kw.pattern)
        if idx == -1:
            continue
        # Word boundary check on both sides
        if idx > 0 and upper[idx - 1].isalnum():
            continue
        end = idx + len(kw.pattern)
        if end < len(upper) and upper[end].isalnum():
            continue
        if best is None or (end - idx) > (best[2] - best[1]):
            best = (kw, idx, end)
    if best is None and ("ONLINE" in upper or "BANK" in upper):
        # Fuzzy fallback for a misspelled "TRANSFER" (e.g. "ONLINE TRASNFER",
        # "BANK TRANFER"). Only runs when no exact keyword matched AND the line
        # carries an ONLINE/BANK qualifier, so a bare "DEPOSIT TRANSFER" still
        # flags for Review and no card/e-wallet method gets overridden.
        # Online transfer == bank transfer.
        for m in re.finditer(r"[A-Z]{6,}", upper):
            if fuzz.ratio(m.group(0), "TRANSFER") >= 85:
                best = (_TRANSFER_KW, m.start(), m.end())
                break
    return best


def _find_all_keywords(line: str) -> list[tuple[_Keyword, int, int]]:
    """Return every payment keyword in the line, left to right, as
    (keyword, start, end). Overlapping matches keep the longest/most-specific
    one, so 'VISA CREDIT' wins over 'VISA' and 'MASTERCARD' over 'MASTER'.

    This lets a single line carry several payments — e.g. LILY's
    'DEPOSIT VISA CREDIT 6228 RM681 BALANCE ONLINE TRANSFER RM9000 ...' —
    instead of collapsing to one method (which would drop the card charge).
    """
    upper = line.upper()
    candidates: list[tuple[int, int, _Keyword]] = []
    for kw in _KEYWORDS:
        start = 0
        while True:
            idx = upper.find(kw.pattern, start)
            if idx == -1:
                break
            end = idx + len(kw.pattern)
            start = idx + 1
            if idx > 0 and upper[idx - 1].isalnum():
                continue
            if end < len(upper) and upper[end].isalnum():
                continue
            candidates.append((idx, end, kw))
    # Earliest first; on a tie, the longer match first so it wins the overlap.
    candidates.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    picked: list[tuple[_Keyword, int, int]] = []
    last_end = -1
    for st, en, kw in candidates:
        if st >= last_end:
            picked.append((kw, st, en))
            last_end = en
    return picked


def _parse_amounts_and_last4(
    line: str, keyword_span: tuple[int, int] | None
) -> tuple[list[float], str | None]:
    """Extract all RM amounts and the first standalone 4-digit number.

    The 4-digit search masks out RM-amount text so RM4590 doesn't get picked
    up as a "card last 4". Digits inside the keyword span are also masked
    so e.g. the "9" in "TIKTOK PAY9999" doesn't leak in.
    """
    masked = list(line)
    amounts: list[float] = []
    for m in _AMOUNT_RE.finditer(line):
        try:
            amounts.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
        for i in range(m.start(), m.end()):
            masked[i] = " "
    if keyword_span:
        for i in range(keyword_span[0], keyword_span[1]):
            if 0 <= i < len(masked):
                masked[i] = " "
    masked_text = "".join(masked)
    digit_match = _FOUR_DIGIT_RE.search(masked_text)
    last4 = digit_match.group(1) if digit_match else None
    return amounts, last4


def _best_sa_match(token: str, sa_pool: list[str]) -> str | None:
    """Fuzzy-match a captured token to a canonical SA name, tolerating a name
    typed as two words ('MIN KEI' -> 'MINKEI') and a stray channel/location
    word captured in front of it ('PJ SHASHA' -> 'SHASHA').

    Tries the token as written, with internal spaces removed, and just its
    last word, returning the best match at or above the fuzzy threshold.
    """
    token = token.strip()
    variants = {token, token.replace(" ", "")}
    parts = token.split()
    if parts:
        variants.add(parts[-1])
    pool = sa_pool + [HOUSE_ACCOUNT]
    best_name: str | None = None
    best_score = 0.0
    for v in variants:
        m = process.extractOne(v, pool, scorer=fuzz.ratio)
        if m and m[1] > best_score:
            best_name, best_score = m[0], m[1]
    return best_name if best_score >= SA_FUZZY_THRESHOLD else None


def _detect_split_shares(
    note: str, sa_pool: list[str]
) -> list[SAShare] | None:
    """Detect explicit split notation like 'MINKEI 70% LILY 30%'.

    Returns None if no explicit split was found. Returns a list of SAShare
    if at least two valid SA shares totalling ~100% are found.
    """
    candidates: list[tuple[str, float, int]] = []  # (raw_token, pct, position)
    for m in _SPLIT_RE.finditer(note):
        token = m.group(1).upper().strip()
        try:
            pct = float(m.group(2)) / 100.0
        except ValueError:
            continue
        # Reject tokens that look like generic words (channel keywords etc.)
        if token in {"WALK", "WALKIN", "WHATSAPP", "CHATDADDY", "TIKTOK", "ONLINE", "STORE"}:
            continue
        canonical = _best_sa_match(token, sa_pool)
        if canonical is None:
            continue
        candidates.append((canonical, pct, m.start()))

    if len(candidates) < 2:
        return None

    total = sum(pct for _, pct, _ in candidates)
    # Tolerate small rounding noise.
    if abs(total - 1.0) > 0.02:
        return None
    candidates.sort(key=lambda x: x[2])
    return [SAShare(name=name, share=pct) for name, pct, _ in candidates]


def _detect_amount_shares(
    note: str, sa_pool: list[str], order_total: float = 0.0
) -> list[SAShare] | None:
    """Detect amount-based splits like 'CHLOE RM1350 MINKEI RM5050', where each
    SA is followed by their own sales amount instead of a percentage.

    Each SA's share is amount / (sum of amounts). If the note also names
    COMPANY SALES and the SA amounts fall short of the order total, the
    shortfall is attributed to the house account (e.g.
    'MINKEI RM5003 MICHELLE RM3997 COMPANY SALE - BAG SPA RM250' on a RM9,250
    order → MINKEI 5003, MICHELLE 3997, COMPANY SALES 250, all over 9,250).
    Returns None unless at least two distinct SAs each carry an amount.
    """
    candidates: list[tuple[str, float, int]] = []  # (name, amount, position)
    seen: set[str] = set()
    for m in _AMOUNT_SHARE_RE.finditer(note):
        token = m.group(1).upper().strip()
        if token in _NON_SA_TOKENS:
            continue
        try:
            amount = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        if amount <= 0:
            continue
        canonical = _best_sa_match(token, sa_pool)
        if canonical is None:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        candidates.append((canonical, amount, m.start()))

    if len(candidates) < 2:
        return None

    sa_sum = sum(amt for _, amt, _ in candidates)
    if sa_sum <= 0:
        return None

    # A leftover to the order total, when COMPANY SALES is named, is the house
    # (non-commission) portion — so the SA amounts divide by the TOTAL, not
    # just their own sum, and the house takes the rest.
    house_amt = 0.0
    if order_total:
        remainder = round(order_total - sa_sum, 2)
        if remainder > 0 and re.search(r"\bCOMPANY\s+SALES?\b", note):
            house_amt = remainder

    total = sa_sum + house_amt
    candidates.sort(key=lambda x: x[2])
    shares = [
        SAShare(name=name, share=round(amt / total, 6))
        for name, amt, _ in candidates
    ]
    if house_amt > 0:
        shares.append(SAShare(name=HOUSE_ACCOUNT, share=round(house_amt / total, 6)))
    return shares


def _detect_single_sa(note: str, sa_pool: list[str]) -> SAShare | None:
    """Detect a single SA name in the note (fuzzy match, first match wins)."""
    upper = note.upper()
    if "COMPANY SALES" in upper:
        return SAShare(name=HOUSE_ACCOUNT, share=1.0)

    lines = [ln.strip() for ln in upper.split("\n") if ln.strip()]

    # House account — "COMPANY" leading the note is the seller identity and
    # means company sales, with or without a following "SALES" ("COMPANY WALK
    # IN ..." counts). Anchoring on the FIRST token (fuzzy, catches typos like
    # "COMPNAY") keeps a mid-note phrase such as "COMPANY POLICY DISCOUNT" — or
    # an outlet tag "PJ SALES" / "PG SALES" — from being mistaken for the house
    # account.
    first_words = re.split(r"[\s,/&\-]+", lines[0]) if lines else []
    if first_words and fuzz.ratio("COMPANY", first_words[0]) >= 82:
        return SAShare(name=HOUSE_ACCOUNT, share=1.0)
    # Also honour a "COMPANY SALES" tag that appears mid-note (fuzzy on both the
    # COMPANY-like word and a nearby SALE), e.g. a trailing "... COMPANY SALES".
    for line in lines[:3]:
        words = re.split(r"[\s,/&\-]+", line)
        if any(fuzz.ratio("COMPANY", w) >= 82 for w in words) and "SALE" in line:
            return SAShare(name=HOUSE_ACCOUNT, share=1.0)

    # Check the first 3 lines for an SA token (SA name is conventionally first)
    for line in lines[:3]:
        for raw_token in re.split(r"[\s,/&\-]+", line):
            token = raw_token.strip().strip(":.;-")
            if len(token) < 2:
                continue
            match = process.extractOne(token, sa_pool, scorer=fuzz.ratio)
            if match is None:
                continue
            canonical, score, _ = match
            if score >= SA_FUZZY_THRESHOLD:
                return SAShare(name=canonical, share=1.0)
    return None


def _classify_senangpay(line: str) -> PaymentMethod:
    """Heuristic: classify SenangPay line as card vs FPX. Default: card."""
    upper = line.upper()
    if "FPX" in upper or "ONLINE BANKING" in upper or "ONLINE BANK" in upper:
        return PaymentMethod.SENANGPAY_FPX
    return PaymentMethod.SENANGPAY_CARD


def parse_seller_note(
    note: str,
    order_total: float,
    sa_list: list[str] | None = None,
    channel: str | None = None,
) -> ParsedNote:
    """Parse a seller note into structured SA shares + payment portions.

    Args:
        note: Raw text from the EasyStore Note column.
        order_total: Order's Total Amount (RM). Used to allocate the implicit
            remainder portion when a payment line has no amount.
        sa_list: Active SA names. Defaults to DEFAULT_SAS.
        channel: EasyStore channel (e.g. "online_store"); used as fallback
            when the note is empty.

    Returns:
        ParsedNote with sa_shares, payments, and any review_flags.
    """
    sa_pool = sa_list if sa_list is not None else DEFAULT_SAS
    raw = note or ""
    flags: list[str] = []

    # ---- Empty note: fall back to channel ---------------------------------
    if not raw.strip():
        if channel and channel.lower() in ONLINE_CHANNELS:
            flags.append("Empty note: inferred online sale to COMPANY SALES")
            return ParsedNote(
                sa_shares=[SAShare(name=HOUSE_ACCOUNT, share=1.0)],
                payments=[
                    PaymentPortion(
                        method=PaymentMethod.SENANGPAY_CARD,
                        amount=order_total,
                        raw_line="(empty note; inferred SenangPay)",
                    )
                ],
                raw_note=raw,
                review_flags=flags,
            )
        flags.append("Empty note: SA and payment unknown")
        return ParsedNote(raw_note=raw, review_flags=flags)

    # ---- Normalise --------------------------------------------------------
    # Full-width punctuation from phone / CJK keyboards: fold to ASCII so
    # "MINKEI 70％ RISKA 30％" is read as a 70/30 split, not one SA at 100%.
    upper_note = raw.upper().translate(str.maketrans("％＋，．", "%+,."))
    lines = [ln.strip() for ln in upper_note.split("\n") if ln.strip()]

    # ---- SA shares --------------------------------------------------------
    sa_shares: list[SAShare] = []
    split = _detect_split_shares(upper_note, sa_pool)
    amount_split = split or _detect_amount_shares(upper_note, sa_pool, order_total)
    if amount_split:
        sa_shares = amount_split
    else:
        single = _detect_single_sa(upper_note, sa_pool)
        if single:
            sa_shares = [single]
        else:
            flags.append("No SA detected in note")

    # ---- Payment portions -------------------------------------------------
    # A line can carry several payments (e.g. a card deposit then bank-transfer
    # balances all typed on one line). Split it at each payment keyword so every
    # method keeps its own amount and charge; a line with one keyword behaves as
    # before (all its amounts summed into a single portion).
    payments: list[PaymentPortion] = []
    for line in lines:
        kws = _find_all_keywords(line)
        if not kws:
            kw_hit = _find_keyword(line)  # fuzzy fallback (misspelled TRANSFER)
            if not kw_hit:
                continue
            kws = [kw_hit]
        # Segment i spans from its own start (segment 0 starts at the line start,
        # so any leading amount attaches to the first method) to the next
        # keyword's start.
        seg_starts = [0] + [kws[i][1] for i in range(1, len(kws))]
        for i, (kw, kst, ken) in enumerate(kws):
            seg_start = seg_starts[i]
            seg_end = seg_starts[i + 1] if i + 1 < len(seg_starts) else len(line)
            seg = line[seg_start:seg_end]
            method = kw.method
            if method == PaymentMethod.SENANGPAY_CARD:
                method = _classify_senangpay(seg)
            amounts, last4 = _parse_amounts_and_last4(
                seg, (kst - seg_start, ken - seg_start)
            )
            amount = sum(amounts) if amounts else None
            payments.append(
                PaymentPortion(
                    method=method,
                    amount=amount,
                    last4=last4,
                    raw_line=seg.strip(),
                )
            )

    if not payments:
        flags.append("No payment method detected in note")

    # ---- Allocate implicit remainder & validate sum -----------------------
    explicit_sum = sum(p.amount for p in payments if p.amount is not None)
    implicit_idx = [i for i, p in enumerate(payments) if p.amount is None]

    if len(implicit_idx) == 1:
        remainder = round(order_total - explicit_sum, 2)
        if remainder < 0:
            flags.append(
                f"Implicit payment remainder is negative (RM{remainder:.2f})"
            )
        payments[implicit_idx[0]] = payments[implicit_idx[0]].model_copy(
            update={"amount": remainder}
        )
    elif len(implicit_idx) > 1:
        flags.append(
            f"{len(implicit_idx)} payment lines have no amount; cannot auto-allocate"
        )

    # Align parsed amounts to the order total. The seller note is human-typed
    # and prone to typos (e.g. "CASH RM750" written when the order was RM950);
    # the order's Total Amount is auto-calculated by EasyStore and is the
    # source of truth. We trust the total and reconcile the portions to it.
    #
    # Special case: TikTok-shop orders intentionally use the seller-note
    # amount as net (Q2=B — the gap to order total IS the TikTok platform
    # fee), so alignment is skipped there.
    is_tiktok_channel = (channel or "").lower() == "tiktok-shop"
    has_tiktok_portion = any(p.method == PaymentMethod.TIKTOK for p in payments)
    skip_alignment = is_tiktok_channel and has_tiktok_portion

    if payments and not skip_alignment:
        explicit_idx = [i for i, p in enumerate(payments) if p.amount is not None]
        has_implicit = len(explicit_idx) != len(payments)
        if not has_implicit and explicit_idx:
            parsed_sum = sum(payments[i].amount for i in explicit_idx)
            diff = round(order_total - parsed_sum, 2)
            if abs(diff) > 1.0:
                if len(explicit_idx) == 1:
                    # Single portion absorbs the order total — common typo case.
                    i = explicit_idx[0]
                    payments[i] = payments[i].model_copy(
                        update={"amount": round(order_total, 2)}
                    )
                elif parsed_sum > 0:
                    # Multiple portions: only reconcile SMALL drift (within
                    # 10%), which is almost always rounding or a minor typo.
                    # A large gap is more likely a missing payment line, an
                    # unpaid balance, or a wrong order total — scaling there
                    # would fabricate a fake per-payment split (e.g. RM1,000
                    # shown as RM1,112.36). Instead we keep the amounts exactly
                    # as written and let the final-sum check below flag the
                    # order for Review so a human can resolve the real gap.
                    ratio = order_total / parsed_sum
                    if abs(ratio - 1.0) <= 0.10:
                        for i in explicit_idx:
                            payments[i] = payments[i].model_copy(
                                update={"amount": round(payments[i].amount * ratio, 2)}
                            )

    # Final sanity check — should only fire for TikTok (where alignment was
    # skipped) or pathological multi-implicit cases.
    final_sum = sum(p.amount for p in payments if p.amount is not None)
    suppress_mismatch = is_tiktok_channel and has_tiktok_portion and final_sum <= order_total
    if payments and not suppress_mismatch and abs(final_sum - order_total) > 1.0:
        flags.append(
            f"Payment total RM{final_sum:.2f} differs from order total "
            f"RM{order_total:.2f} by RM{final_sum - order_total:+.2f}"
        )

    # SenangPay with no detail flagged for review
    for p in payments:
        if p.method == PaymentMethod.SENANGPAY_CARD and "SENANG" in p.raw_line.upper():
            # Only flag if no FPX/card hint was present
            ru = p.raw_line.upper()
            if not any(k in ru for k in ("FPX", "CARD", "VISA", "MASTERCARD", "ONLINE BANK")):
                flags.append(
                    "SenangPay portion lacks card/FPX detail (defaulted to card)"
                )
                break

    # Deposit line with no identifiable payment method → send to Review rather
    # than letting another portion silently absorb it (which over-charges the
    # card rate on the deposit). Covers "DEPOSIT TRANSFER RM1000" and a bare
    # "DEPOSIT RM1000"; a deposit that DOES name a method ("DEPOSIT CASH RM500",
    # "DEPO ONLINE TRANSFER ...") is recognised and not flagged.
    for line in lines:
        if re.search(r"\bDEPO", line) and _AMOUNT_RE.search(line) and _find_keyword(line) is None:
            flags.append(
                f"Deposit line has no payment method — confirm in Review: '{line.strip()}'"
            )

    return ParsedNote(
        sa_shares=sa_shares,
        payments=payments,
        raw_note=raw,
        review_flags=flags,
    )
