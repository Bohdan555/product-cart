"""Акції «Сільпо» через офіційний MCP-сервер (https://mcp.silpo.ua/mcp).

Логіка не залежить від транспорту: на вхід — асинхронна функція `call(tool, args) -> dict`.
Справжній клієнт з OAuth — у silpo_mcp.py; у тестах — підробка з фікстурами.

Назви інструментів і поля відповідей звірено з відкритими проєктами хакатону «Сільпо» AI Factory
(див. docs/data-sources.md). Сервер може змінювати формат, тому мапінг толерантний до варіантів полів.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ..catalog import enrich, parse_pack
from ..models import Offer

log = logging.getLogger(__name__)

Call = Callable[[str, dict], Awaitable[Any]]
CHAIN = "silpo"
PRODUCT_URL = "https://silpo.ua/product/{slug}"


def _list(*candidates) -> list:
    for c in candidates:
        if isinstance(c, list):
            return c
    return []


def _num(*vals) -> float | None:
    for v in vals:
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


# ---------- магазини ----------

def normalize_branch(b: dict) -> dict:
    city, address = (b.get("city") or "").strip(), (b.get("address") or "").strip()
    return {"id": str(b.get("branchId") or b.get("id") or ""), "city": city, "address": address,
            "title": ", ".join(x for x in (city, address) if x) or "Сільпо",
            "lat": _num(b.get("latitude")), "lon": _num(b.get("longitude"))}


async def list_branches(call: Call, city: str | None = None, query: str | None = None) -> list[dict]:
    out, offset = [], 0
    while True:
        data = await call("silpo_list_branches", {"limit": 500, "offset": offset}) or {}
        batch = _list(data.get("branches"), data.get("items"))
        out += [normalize_branch(b) for b in batch]
        offset += len(batch)
        total = int((data.get("meta") or {}).get("total") or offset)
        if not batch or offset >= total:
            break
    out = [b for b in out if b["id"] and b["address"]]
    if city:
        out = [b for b in out if b["city"].lower() == city.lower()]
    if query:
        terms = [t for t in query.lower().replace(",", " ").split() if len(t) > 2]
        out = [b for b in out if all(t[:-1] in b["title"].lower() for t in terms)]
    return sorted(out, key=lambda b: b["title"])


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Відстань по прямій (гаверсинус), км."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest(branches: list[dict], lat: float, lon: float, n: int = 5) -> list[dict]:
    """Найближчі магазини з відстанню (поле km). Магазини без координат пропускаємо."""
    out = [dict(b, km=round(distance_km(lat, lon, b["lat"], b["lon"]), 1))
           for b in branches if b.get("lat") is not None and b.get("lon") is not None]
    return sorted(out, key=lambda b: b["km"])[:n]


async def branch_context(call: Call, branch_id: str) -> dict:
    """Більшість інструментів каталогу хочуть контекст: магазин + тип доставки + вікно доставки.
    Беремо перший доступний слот; без слотів — лише branchId."""
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    attempts = [
        {"branchId": branch_id, "limit": 100},
        {"branchId": branch_id, "limit": 100, "deliveryTypes": ["SelfPickup"]},
        {"branchId": branch_id, "limit": 100, "start": tomorrow},
    ]
    for args in attempts:
        try:
            data = await call("silpo_get_time_slots", args) or {}
        except Exception as e:  # слоти — не критично
            log.warning("time slots: %s", e)
            continue
        slots = _list(data.get("slots"), data.get("items"))
        slot = next((s for s in slots if s.get("available")), slots[0] if slots else None)
        if slot and slot.get("start") and slot.get("end"):
            return {"branchId": branch_id, "deliveryType": slot.get("deliveryType") or "SelfPickup",
                    "timeslotStart": slot["start"], "timeslotEnd": slot["end"]}
    return {"branchId": branch_id}


# ---------- товари ----------

def normalize_product(p: dict, *, branch: dict | None = None, promo: str | None = None) -> Offer | None:
    price = _num(p.get("price"), p.get("currentPrice"))
    if price is None:
        return None
    old = _num(p.get("oldPrice"), p.get("priceOld"))
    special = (p.get("specialPrices") or [None])[0] if isinstance(p.get("specialPrices"), list) else None
    promo_text = promo
    if special and special.get("price") and special.get("count"):
        promo_text = f"{special['count']} шт по {special['price']} ₴" + (f" · {promo}" if promo else "")
    name = p.get("name") or p.get("title") or "Товар"
    ratio = p.get("displayRatio") or ""
    weighted = bool(p.get("weighted") or p.get("isWeighted"))
    qty, unit = parse_pack(ratio) if ratio else (None, None)
    image = p.get("image") or p.get("mainImage") or p.get("imageUrl")
    if not image and isinstance(p.get("images"), list) and p["images"]:
        first = p["images"][0]
        image = first.get("url") if isinstance(first, dict) else first
    slug = p.get("slug")
    offer = Offer(
        chain=CHAIN,
        product_id=str(p.get("id") or p.get("productId") or p.get("externalProductId") or name),
        name=name if not ratio or ratio in name else f"{name} {ratio}",
        price=price,
        old_price=old if old and old > price else None,
        brand=p.get("brandTitle") or p.get("brand") or None,
        category=p.get("category") or p.get("categoryName") or p.get("sectionTitle"),
        pack_qty=qty, pack_unit=unit, weighted=weighted,
        store_id=(branch or {}).get("id") or (str(p["branchId"]) if p.get("branchId") else None),
        store_name=(branch or {}).get("title"),
        promo=promo_text,
        url=PRODUCT_URL.format(slug=slug) if slug else None,
        ean=p.get("barcode") or p.get("ean"),
        image=image,
        source="silpo-mcp",
    )
    return enrich(offer)


async def fetch_offers(call: Call, branch: dict, max_promotions: int = 12, page: int = 100, max_pages: int = 5) -> list[Offer]:
    """Усі акційні товари магазину: список акцій → товари кожної акції (з пагінацією), без дублів."""
    ctx = await branch_context(call, branch["id"])
    data = await call("silpo_get_promotions", ctx) or {}
    promotions = _list(data.get("promotions"), data.get("items"))
    if not promotions:
        log.warning("Сільпо: акцій не знайдено для магазину %s", branch.get("title"))
    seen: dict[str, Offer] = {}
    for promo in promotions[:max_promotions]:
        code, title = promo.get("code"), promo.get("title") or promo.get("name") or promo.get("code")
        if not code:
            continue
        for n in range(max_pages):
            args = dict(ctx, promotionCode=code, inStock=True, limit=page, offset=n * page, sortBy="promotion")
            res = await call("silpo_get_products", args) or {}
            items = _list(res.get("products"), res.get("items"))
            for p in items:
                o = normalize_product(p, branch=branch, promo=title)
                if o and o.product_id not in seen:
                    seen[o.product_id] = o
            total = int((res.get("meta") or {}).get("total") or res.get("total") or 0)
            if len(items) < page or (total and (n + 1) * page >= total):
                break
    return list(seen.values())


# ---------- профіль із «Сільпо» ----------

RESTRICTION_TO_DIET = [
    (("глютен", "gluten", "целіак"), "no_gluten"),
    (("лактоз", "lactose", "молоч"), "no_lactose"),
    (("веган", "vegan"), "vegan"),
    (("вегетар", "vegetar"), "vegetarian"),
    (("свинин", "pork", "халял", "halal", "кошер"), "no_pork"),
    (("алкогол", "alcohol"), "no_alcohol"),
    (("цукор", "цукр", "sugar", "діабет"), "no_sugar"),
]


async def profile_hints(call: Call) -> dict:
    """Підказки для анкети з профілю «Сільпо»: харчові обмеження і розмір сім'ї.
    Повертаємо як пропозицію — покупець підтверджує їх сам."""
    hints: dict = {"diets": [], "household": None, "raw": {}}
    try:
        r = await call("silpo_get_my_food_restrictions", {}) or {}
        hints["raw"]["food_restrictions"] = r
        text = str(r).lower()
        hints["diets"] = sorted({diet for keys, diet in RESTRICTION_TO_DIET if any(k in text for k in keys)})
    except Exception as e:
        log.warning("food restrictions: %s", e)
    try:
        f = await call("silpo_get_my_family", {}) or {}
        hints["raw"]["family"] = f
        members = _list(f.get("members"), f.get("family"), f.get("items"))
        if members:
            hints["household"] = len(members) + 1
    except Exception as e:
        log.warning("family: %s", e)
    return hints
