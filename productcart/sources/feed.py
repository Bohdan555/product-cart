"""Фід акцій від мережі-партнера (CSV або JSON). Формат описано в docs/feed-format.md."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ..catalog import enrich
from ..models import Offer

REQUIRED = ("chain", "product_id", "name", "price")
NUMERIC = ("price", "old_price", "pack_qty")


def _clean(row: dict) -> dict:
    d = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
    d = {k: v for k, v in d.items() if v not in ("", None)}
    for k in NUMERIC:
        if k in d:
            d[k] = float(str(d[k]).replace(",", "."))
    if "weighted" in d:
        d["weighted"] = str(d["weighted"]).lower() in ("1", "true", "так", "yes")
    if "tags" in d:
        d["tags"] = set(d["tags"] if isinstance(d["tags"], list) else str(d["tags"]).split("|"))
    return d


def load(path: str | Path) -> list[Offer]:
    path = Path(path)
    if path.suffix.lower() == ".json":
        rows = json.loads(path.read_text(encoding="utf-8"))
        rows = rows.get("offers", rows) if isinstance(rows, dict) else rows
    else:
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    offers, errors = [], []
    for i, row in enumerate(rows, start=1):
        d = _clean(row)
        missing = [k for k in REQUIRED if k not in d]
        if missing:
            errors.append(f"рядок {i}: немає {', '.join(missing)}")
            continue
        known = {k: v for k, v in d.items() if k in Offer.__dataclass_fields__}
        known.setdefault("source", f"feed:{d['chain']}")
        offers.append(enrich(Offer(**known)))
    if errors and not offers:
        raise ValueError("Фід не прочитано: " + "; ".join(errors[:5]))
    return offers
