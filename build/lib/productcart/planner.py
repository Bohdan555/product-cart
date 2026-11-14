"""Готовий список покупок: найкращі збіги з урахуванням різноманітності, бюджету й групування по магазинах."""
from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import product_kind
from .matcher import Match
from .models import Profile


@dataclass
class ShoppingList:
    profile: Profile
    groups: dict[str, list[Match]] = field(default_factory=dict)   # "мережа · магазин" → позиції
    total: float = 0.0
    regular_total: float = 0.0
    regulars_covered: list[str] = field(default_factory=list)
    regulars_missing: list[str] = field(default_factory=list)
    skipped_budget: int = 0

    @property
    def saving(self) -> float:
        return round(self.regular_total - self.total, 2)

    @property
    def items(self) -> list[Match]:
        return [m for ms in self.groups.values() for m in ms]


def group_key(m: Match) -> str:
    o = m.offer
    return f"{o.chain}" + (f" · {o.store_name}" if o.store_name else "")


def build(matches: list[Match], profile: Profile, per_kind: int = 1) -> ShoppingList:
    """Жадібно бере найкращі позиції: щонайбільше `per_kind` товарів одного виду (одне молоко, а не п'ять),
    спершу закриває «купую щотижня», не виходить за бюджет."""
    sl = ShoppingList(profile)
    kinds: dict[str, int] = {}
    covered: set[str] = set()
    budget = profile.budget_week

    # Спершу — по одному найкращому товару на кожен пункт «купую щотижня», потім решта за балом.
    first = []
    for r in profile.regulars:
        best = next((m for m in matches if m.regular == r), None)
        if best and best not in first:
            first.append(best)
    ordered = first + [m for m in matches if m not in first]

    chosen: list[Match] = []
    for m in ordered:
        if len(chosen) >= profile.max_items:
            break
        kind = m.regular or product_kind(m.offer.name)
        if m.regular and m.regular in covered:
            continue
        if kinds.get(kind, 0) >= per_kind:
            continue
        if budget is not None and sl.total + m.offer.price > budget:
            sl.skipped_budget += 1
            continue
        chosen.append(m)
        kinds[kind] = kinds.get(kind, 0) + 1
        if m.regular:
            covered.add(m.regular)
        sl.total = round(sl.total + m.offer.price, 2)
        sl.regular_total = round(sl.regular_total + (m.offer.old_price or m.offer.price), 2)

    for m in sorted(chosen, key=lambda m: (group_key(m), -m.score)):
        sl.groups.setdefault(group_key(m), []).append(m)
    sl.regulars_covered = [r for r in profile.regulars if r in covered]
    sl.regulars_missing = [r for r in profile.regulars if r not in covered]
    return sl
