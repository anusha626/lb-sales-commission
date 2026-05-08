"""Persistence and lookup for the configurable side of the engine.

Three JSON files live in `data/`:
- sa_list.json  : active SAs (parser uses this for fuzzy matching)
- tiers.json    : commission brackets + per-channel flat rules (e.g. TikTok)
- rates.json    : versioned merchant rate card (Maybank/SenangPay)

All three are user-editable from the Settings page in the Streamlit app.
Pure functions only — no Streamlit imports here.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .models import (
    CommissionTier,
    PaymentMethod,
    RateRow,
    RateTableVersion,
)

DATA_DIR = Path(__file__).parent.parent / "data"
SA_FILE = DATA_DIR / "sa_list.json"
TIERS_FILE = DATA_DIR / "tiers.json"
RATES_FILE = DATA_DIR / "rates.json"


# ---------------------------------------------------------------------------
# SA list
# ---------------------------------------------------------------------------

class SARecord(BaseModel):
    name: str
    active: bool = True


class SAListConfig(BaseModel):
    sas: list[SARecord]

    @property
    def active_names(self) -> list[str]:
        return [s.name.upper() for s in self.sas if s.active]


def load_sa_list(path: Path = SA_FILE) -> SAListConfig:
    return SAListConfig.model_validate_json(path.read_text(encoding="utf-8"))


def save_sa_list(cfg: SAListConfig, path: Path = SA_FILE) -> None:
    path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tiers + channel flat rules
# ---------------------------------------------------------------------------

class ChannelFlatRule(BaseModel):
    """A flat commission per order for a specific EasyStore channel."""

    channel: str  # matches Channel column, e.g. "tiktok-shop"
    amount_per_order: float
    label: str = ""


class TiersConfig(BaseModel):
    tiers: list[CommissionTier]
    channel_flat_commissions: list[ChannelFlatRule] = Field(default_factory=list)

    def flat_rule_for(self, channel: str) -> ChannelFlatRule | None:
        ch = (channel or "").lower()
        for r in self.channel_flat_commissions:
            if r.channel.lower() == ch:
                return r
        return None


def load_tiers(path: Path = TIERS_FILE) -> TiersConfig:
    return TiersConfig.model_validate_json(path.read_text(encoding="utf-8"))


def save_tiers(cfg: TiersConfig, path: Path = TIERS_FILE) -> None:
    path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Rate table (versioned)
# ---------------------------------------------------------------------------

class RatesConfig(BaseModel):
    versions: list[RateTableVersion]

    def version_for(self, on_date: date) -> RateTableVersion:
        """Pick the version whose effective_from is the latest <= on_date."""
        applicable = [v for v in self.versions if v.effective_from <= on_date]
        if not applicable:
            # No version covers this date — return earliest as a degraded default.
            return min(self.versions, key=lambda v: v.effective_from)
        return max(applicable, key=lambda v: v.effective_from)


def load_rates(path: Path = RATES_FILE) -> RatesConfig:
    return RatesConfig.model_validate_json(path.read_text(encoding="utf-8"))


def save_rates(cfg: RatesConfig, path: Path = RATES_FILE) -> None:
    path.write_text(cfg.model_dump_json(indent=2, exclude_none=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Rate lookup
# ---------------------------------------------------------------------------

# AMEX has multiple rows in the rate card (CR LOCAL, DR LOCAL, DR MAYBANK,
# FOREIGN). The parser currently emits a single PaymentMethod.AMEX value.
# Default the lookup to "AMEX CR LOCAL" — user can override in the review
# queue if a particular order was AMEX debit or AMEX foreign.
_DEFAULT_LABEL_HINTS: dict[PaymentMethod, str] = {
    PaymentMethod.AMEX: "AMEX CR LOCAL",
}


def lookup_rate_row(
    rates: RateTableVersion, method: PaymentMethod, is_foreign: bool
) -> RateRow | None:
    """Find the rate row that applies to a given (method, is_foreign).

    Strategy:
      1. Match method + is_foreign exactly.
      2. If method has a default label hint (e.g. AMEX → AMEX CR LOCAL), use that.
      3. Match method only (any is_foreign), preferring is_foreign=False.
      4. Return None if nothing matches.
    """
    exact = [r for r in rates.rates if r.method == method and r.is_foreign == is_foreign]
    if exact:
        return exact[0]
    hint = _DEFAULT_LABEL_HINTS.get(method)
    if hint:
        for r in rates.rates:
            if r.label == hint:
                return r
    by_method = [r for r in rates.rates if r.method == method]
    if by_method:
        local = [r for r in by_method if not r.is_foreign]
        return local[0] if local else by_method[0]
    return None


# ---------------------------------------------------------------------------
# Convenience: load all three at once
# ---------------------------------------------------------------------------

class AppSettings(BaseModel):
    sa_list: SAListConfig
    tiers: TiersConfig
    rates: RatesConfig


def load_all() -> AppSettings:
    return AppSettings(
        sa_list=load_sa_list(),
        tiers=load_tiers(),
        rates=load_rates(),
    )
