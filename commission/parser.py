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
    _Keyword("MYDEBIT", PaymentMethod.MYDEBIT),
    _Keyword("MAESTRO", PaymentMethod.MAESTRO),
    _Keyword("AMEX", PaymentMethod.AMEX),
    _Keyword("JCB", PaymentMethod.JCB),
    _Keyword("UPI", PaymentMethod.UPI),
    _Keyword("VISA", PaymentMethod.VISA_CREDIT),
    _Keyword("TNG", PaymentMethod.TNG),
    _Keyword("CASH", PaymentMethod.CASH),
)


_AMOUNT_RE = re.compile(r"RM\s?([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_FOUR_DIGIT_RE = re.compile(r"\b(\d{4})\b")
# Split notation: "MINKEI 70% LILY 30%", "MINKEI 60% / LILY 40%", "MINKEI 60%/LILY 40%"
_SPLIT_RE = re.compile(
    r"([A-Z][A-Z]+)\s*(\d{1,3})\s*%",
    re.IGNORECASE,
)


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
    return best


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


def _detect_split_shares(
    note: str, sa_pool: list[str]
) -> list[SAShare] | None:
    """Detect explicit split notation like 'MINKEI 70% LILY 30%'.

    Returns None if no explicit split was found. Returns a list of SAShare
    if at least two valid SA shares totalling ~100% are found.
    """
    candidates: list[tuple[str, float, int]] = []  # (raw_token, pct, position)
    for m in _SPLIT_RE.finditer(note):
        token = m.group(1).upper()
        try:
            pct = float(m.group(2)) / 100.0
        except ValueError:
            continue
        # Reject tokens that look like generic words (channel keywords etc.)
        if token in {"WALK", "WALKIN", "WHATSAPP", "CHATDADDY", "TIKTOK", "ONLINE", "STORE"}:
            continue
        match = process.extractOne(token, sa_pool + [HOUSE_ACCOUNT], scorer=fuzz.ratio)
        if match is None:
            continue
        canonical, score, _ = match
        if score < SA_FUZZY_THRESHOLD:
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


def _detect_single_sa(note: str, sa_pool: list[str]) -> SAShare | None:
    """Detect a single SA name in the note (fuzzy match, first match wins)."""
    upper = note.upper()
    if "COMPANY SALES" in upper:
        return SAShare(name=HOUSE_ACCOUNT, share=1.0)

    # Check the first 3 lines for an SA token (SA name is conventionally first)
    lines = [ln.strip() for ln in upper.split("\n") if ln.strip()]
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
    upper_note = raw.upper()
    lines = [ln.strip() for ln in upper_note.split("\n") if ln.strip()]

    # ---- SA shares --------------------------------------------------------
    sa_shares: list[SAShare] = []
    split = _detect_split_shares(upper_note, sa_pool)
    if split:
        sa_shares = split
    else:
        single = _detect_single_sa(upper_note, sa_pool)
        if single:
            sa_shares = [single]
        else:
            flags.append("No SA detected in note")

    # ---- Payment portions -------------------------------------------------
    payments: list[PaymentPortion] = []
    for line in lines:
        kw_hit = _find_keyword(line)
        if not kw_hit:
            continue
        kw, start, end = kw_hit
        method = kw.method
        if method == PaymentMethod.SENANGPAY_CARD:
            method = _classify_senangpay(line)
        amounts, last4 = _parse_amounts_and_last4(line, (start, end))
        amount = sum(amounts) if amounts else None
        payments.append(
            PaymentPortion(
                method=method,
                amount=amount,
                last4=last4,
                raw_line=line,
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
                    # Multiple portions: scale proportionally to fit the total.
                    # Flag for review if the scaling factor is meaningfully
                    # off — likely a real data error rather than rounding.
                    ratio = order_total / parsed_sum
                    for i in explicit_idx:
                        payments[i] = payments[i].model_copy(
                            update={"amount": round(payments[i].amount * ratio, 2)}
                        )
                    if abs(ratio - 1.0) > 0.10:
                        flags.append(
                            f"Scaled multi-portion payments by {ratio:.3f} "
                            f"(parsed sum RM{parsed_sum:.2f} → order total RM{order_total:.2f})"
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

    return ParsedNote(
        sa_shares=sa_shares,
        payments=payments,
        raw_note=raw,
        review_flags=flags,
    )
