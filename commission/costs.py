"""SKU → cost-price store, used for the gross-profit test on the SA incentive.

The EasyStore *orders* export carries no cost column; the *products* export
does (`SKU`, `Cost Price`). A single product snapshot only covers items still
listed, so items sold and delisted drop out and their cost is lost — matching
roughly three quarters of a month's line items in practice.

The fix is to keep an accumulating store: every product CSV the user uploads
is merged into `data/sku_costs.json` and remembered. Coverage therefore grows
month by month and never regresses when an item leaves the catalogue. A SKU
seen again with a new cost is updated (the latest upload wins), so a corrected
cost price propagates.
"""
from __future__ import annotations

import json
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import IO

import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "data"
COSTS_FILE = DATA_DIR / "sku_costs.json"

# Column names in the EasyStore product export.
SKU_COL = "SKU"
COST_COL = "Cost Price"
PRICE_COL = "Price"
TITLE_COL = "Title"


def _to_float(s) -> float | None:
    try:
        v = float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v


class CostStore:
    """SKU → {cost, title, updated}. Persisted as a flat JSON dict."""

    def __init__(self, costs: dict[str, dict] | None = None) -> None:
        self.costs: dict[str, dict] = costs or {}

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str = COSTS_FILE) -> "CostStore":
        path = Path(path)
        if not path.exists():
            return cls({})
        try:
            return cls(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return cls({})

    def save(self, path: Path | str = COSTS_FILE) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.costs, indent=1, sort_keys=True), encoding="utf-8")

    # -- ingest ------------------------------------------------------------
    def merge_product_csv(self, source: str | IO[str] | bytes) -> dict:
        """Merge an EasyStore product export into the store.

        Returns a small summary dict: rows read, SKUs added, SKUs updated,
        rows skipped for a missing SKU or an unusable cost.
        """
        if isinstance(source, bytes):
            df = pd.read_csv(StringIO(source.decode("utf-8-sig")), dtype=str)
        else:
            df = pd.read_csv(source, dtype=str, encoding="utf-8-sig")
        df = df.fillna("")
        if SKU_COL not in df.columns or COST_COL not in df.columns:
            raise ValueError(
                f"Not an EasyStore product export — needs '{SKU_COL}' and "
                f"'{COST_COL}' columns. Found: {list(df.columns)[:8]}…"
            )

        stamp = datetime.now().strftime("%Y-%m-%d")
        added = updated = skipped = 0
        for _, r in df.iterrows():
            sku = str(r.get(SKU_COL, "")).strip()
            if not sku:
                skipped += 1
                continue
            cost = _to_float(r.get(COST_COL, ""))
            if cost is None or cost < 0:
                skipped += 1
                continue
            rec = {
                "cost": round(cost, 2),
                "title": str(r.get(TITLE_COL, "")).strip()[:120],
                "updated": stamp,
            }
            if sku in self.costs:
                updated += 1
            else:
                added += 1
            self.costs[sku] = rec
        return {
            "rows": len(df),
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "total_skus": len(self.costs),
        }

    # -- lookup ------------------------------------------------------------
    def cost_for(self, sku: str) -> float | None:
        rec = self.costs.get((sku or "").strip())
        return rec.get("cost") if rec else None

    def __len__(self) -> int:
        return len(self.costs)
