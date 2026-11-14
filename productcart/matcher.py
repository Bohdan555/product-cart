"""Підбір акцій під профіль: жорсткі фільтри (дієта, «не показувати», мережі) і бал із поясненням «чому вам».

Бал рахує код, без LLM: так результат відтворюваний і його можна пояснити покупцю.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .catalog import TAG_LABELS
from .models import DIET_LABELS, DIETS, Offer, Profile

# Ваги балу. Знижка важлива, але «це те, що я й так купую» важить більше.
W_DISCOUNT = 0.06      # за кожен відсоток знижки
W_REGULAR = 4.0        # товар зі списку «купую щотижня»
W_LIKE = 1.5           # тег або категорія, яку людина любить
W_BRAND = 1.5          # улюблений бренд
W_SAVING = 0.02        # за кожну гривню економії (обмежено, щоб дорогі товари не перемагали лише ціною)
MAX_SAVING_BONUS = 1.5


@dataclass
class Match:
    offer: Offer
    score: float
    reasons: list[str] = field(default_factory=list)
    regular: str | None = None   # який пункт «купую щотижня» закриває


@dataclass
class Rejected:
    offer: Offer
    reason: str


def _norm(s: str) -> str:
    return (s or "").lower().replace("’", "'").replace("ʼ", "'").strip()


def _matches_regular(offer: Offer, regular: str) -> bool:
    """«молоко» ловить «Молоко 2,5% 900 г», але не «Йогурт» з категорії «Молочні продукти» і не «Молочний шоколад».
    Кожне слово пункту має бути початком якогось слова в назві товару (без закінчення)."""
    tokens = [t for t in re.split(r"[^\wʼ'-]+", _norm(offer.name)) if t]
    words = [w for w in _norm(regular).split() if w]
    if not words:
        return False

    def stem(w: str) -> str:
        return w if len(w) <= 4 else w[: len(w) - 1]

    return all(any(t.startswith(stem(w)) for t in tokens) for w in words)


def check(offer: Offer, profile: Profile) -> Rejected | None:
    """Жорсткі фільтри. Повертає причину відсіву або None."""
    if profile.chains and offer.chain not in profile.chains:
        return Rejected(offer, "мережа не вибрана")
    store = profile.stores.get(offer.chain)
    if store and offer.store_id and offer.store_id != store:
        return Rejected(offer, "інший магазин мережі")
    for diet in profile.diets:
        hit = DIETS[diet] & offer.tags
        if hit:
            return Rejected(offer, f"{DIET_LABELS[diet]}: {', '.join(TAG_LABELS.get(t, t) for t in sorted(hit))}")
    bad = set(profile.dislikes) & offer.tags
    if bad:
        return Rejected(offer, "не показувати: " + ", ".join(TAG_LABELS.get(t, t) for t in sorted(bad)))
    brand = _norm(offer.brand or "")
    if brand and any(_norm(b) == brand or _norm(b) in _norm(offer.name) for b in profile.brands_avoid):
        return Rejected(offer, f"бренд у списку «не купую»: {offer.brand}")
    if not offer.brand and any(_norm(b) in _norm(offer.name) for b in profile.brands_avoid if b):
        return Rejected(offer, "бренд у списку «не купую»")
    if offer.discount_pct < profile.min_discount:
        return Rejected(offer, f"знижка {offer.discount_pct:.0f}% менша за поріг {profile.min_discount:.0f}%")
    return None


def score(offer: Offer, profile: Profile) -> Match:
    reasons: list[str] = []
    s = offer.discount_pct * W_DISCOUNT
    reasons.append(f"−{offer.discount_pct:.0f}%")

    regular = next((r for r in profile.regulars if _matches_regular(offer, r)), None)
    if regular:
        s += W_REGULAR
        reasons.insert(0, f"ви купуєте «{regular}»")

    likes = {_norm(x) for x in profile.likes}
    liked_tags = sorted(t for t in offer.tags if t in likes)
    if offer.category and _norm(offer.category) in likes:
        liked_tags.append(offer.category)
    if liked_tags:
        s += W_LIKE
        reasons.append("вам подобається: " + ", ".join(TAG_LABELS.get(t, t) for t in liked_tags))

    name = _norm(offer.name)
    brand_hit = next((b for b in profile.brands_like if _norm(b) and (_norm(b) == _norm(offer.brand or "") or _norm(b) in name)), None)
    if brand_hit:
        s += W_BRAND
        reasons.append(f"улюблений бренд {brand_hit}")

    if offer.also_in:
        reasons.append(f"також ще в {offer.also_in} магаз. поруч")
    s += min(MAX_SAVING_BONUS, offer.saving * W_SAVING)
    if offer.saving >= 20:
        reasons.append(f"економія {offer.saving:.0f} ₴")
    return Match(offer, round(s, 3), reasons, regular)


def rank(offers: list[Offer], profile: Profile) -> tuple[list[Match], list[Rejected]]:
    matches, rejected = [], []
    for o in offers:
        r = check(o, profile)
        if r:
            rejected.append(r)
        else:
            matches.append(score(o, profile))
    matches.sort(key=lambda m: (-m.score, -m.offer.discount_pct, m.offer.price))
    return matches, rejected
