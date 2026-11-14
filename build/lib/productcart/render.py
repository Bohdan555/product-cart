"""Виведення списку: текст (для терміналу й Telegram), Markdown і JSON."""
from __future__ import annotations

import json

from .models import DIET_LABELS
from .planner import ShoppingList

CHAIN_NAMES = {"silpo": "Сільпо", "varus": "VARUS", "atb": "АТБ", "fora": "Фора", "novus": "NOVUS",
               "demo-a": "Демо-мережа А", "demo-b": "Демо-мережа Б", "demo-c": "Демо-мережа В"}


def uah(x: float) -> str:
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return s.removesuffix(",00") + " ₴"


def _chain(key: str) -> str:
    chain, _, store = key.partition(" · ")
    return CHAIN_NAMES.get(chain, chain) + (f" · {store}" if store else "")


def text(sl: ShoppingList, markdown: bool = False) -> str:
    b = "**" if markdown else ""
    p = sl.profile
    out = [f"{b}Ваш список з акцій{b} — {p.name}"]
    if p.diets:
        out.append("Фільтри: " + ", ".join(DIET_LABELS[d] for d in p.diets) + " — за назвою товару, перевіряйте склад")
    if not sl.items:
        out.append("Під ваш профіль цього тижня акцій не знайшлося. Спробуйте знизити поріг знижки або додати мережі.")
        return "\n".join(out)
    for key, items in sl.groups.items():
        out.append("")
        out.append(f"{b}{_chain(key)}{b}")
        for m in items:
            o = m.offer
            old = f" (було {uah(o.old_price)})" if o.old_price else ""
            valid = f", до {o.valid_to}" if o.valid_to else ""
            out.append(f"{'-' if markdown else '•'} {o.name} — {uah(o.price)}{old}{valid}")
            out.append(f"  {'  ' if markdown else ''}↳ " + "; ".join(m.reasons))
    out.append("")
    out.append(f"Разом: {uah(sl.total)}, економія {uah(sl.saving)} проти звичайних цін")
    if sl.profile.budget_week:
        out.append(f"Бюджет: {uah(sl.profile.budget_week)}" + (f", не влізло ще {sl.skipped_budget} акцій" if sl.skipped_budget else ""))
    if sl.regulars_missing:
        out.append("Без акції цього тижня: " + ", ".join(sl.regulars_missing))
    return "\n".join(out)


def to_json(sl: ShoppingList) -> str:
    return json.dumps({
        "profile": sl.profile.name,
        "total": sl.total,
        "saving": sl.saving,
        "regulars_covered": sl.regulars_covered,
        "regulars_missing": sl.regulars_missing,
        "groups": {k: [dict(m.offer.to_dict(), score=m.score, reasons=m.reasons) for m in v] for k, v in sl.groups.items()},
    }, ensure_ascii=False, indent=2)
