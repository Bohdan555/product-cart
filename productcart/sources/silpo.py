"""Акції «Сільпо» через офіційний MCP-сервер (https://mcp.silpo.ua/mcp).

Логіка не залежить від транспорту: на вхід — асинхронна функція `call(tool, args) -> dict`.
Справжній клієнт з OAuth — у silpo_mcp.py; у тестах — підробка з фікстурами.

Назви інструментів і поля відповідей звірено з відкритими проєктами хакатону «Сільпо» AI Factory
(див. docs/data-sources.md). Сервер може змінювати формат, тому мапінг толерантний до варіантів полів.
"""
from __future__ import annotations
import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ..catalog import enrich, parse_pack
from ..models import Offer

log = logging.getLogger(__name__)

Call = Callable[[str, dict], Awaitable[Any]]
CHAIN = "silpo"
IMAGE_BASE = "https://images.silpo.ua/products/300x300/webp/"
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
        log.warning("Сільпо: немає слота для магазину %s, відповідь: %s", branch_id, str(data)[:400])
    # Вільних слотів немає (вечір, усе розібрано), а акції без типу доставки й вікна «Сільпо» не віддає.
    # Акції від слота не залежать, тож беремо умовне вікно самовивозу на завтра.
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    return {"branchId": branch_id, "deliveryType": "SelfPickup",
            "timeslotStart": start.isoformat(), "timeslotEnd": (start + timedelta(hours=2)).isoformat()}


# ---------- товари ----------

def image_url(p: dict) -> str | None:
    """Фото товару: повна адреса або ідентифікатор файлу на images.silpo.ua. Великі фото зменшуємо до 300×300."""
    img = p.get("image") or p.get("image_url") or p.get("imageUrl") or p.get("mainImage")
    if not img and isinstance(p.get("images"), list) and p["images"]:
        first = p["images"][0]
        img = first.get("url") if isinstance(first, dict) else first
    if not img or not isinstance(img, str):
        return None
    img = img.strip()
    if img.startswith("//"):
        img = "https:" + img
    if not img.startswith("http"):
        img = IMAGE_BASE + img.split("/")[-1]
    return img.replace("/1600x1600/", "/300x300/")


def normalize_product(p: dict, *, branch: dict | None = None, promo: str | None = None) -> Offer | None:
    price = _num(p.get("price"), p.get("currentPrice"), p.get("current_price"))
    if price is None:
        return None
    old = _num(p.get("oldPrice"), p.get("priceOld"), p.get("old_price"))
    special = (p.get("specialPrices") or [None])[0] if isinstance(p.get("specialPrices"), list) else None
    promo_text = promo
    if special and special.get("price") and special.get("count"):
        promo_text = f"{special['count']} шт по {special['price']} ₴" + (f" · {promo}" if promo else "")
    name = p.get("name") or p.get("title") or "Товар"
    ratio = p.get("displayRatio") or p.get("display_ratio") or ""
    weighted = bool(p.get("weighted") or p.get("isWeighted"))
    qty, unit = parse_pack(ratio) if ratio else (None, None)
    image = image_url(p)
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
        url=PRODUCT_URL.format(slug=slug) if slug else (p.get("url") or p.get("product_url")),
        ean=p.get("barcode") or p.get("ean"),
        image=image,
        source="silpo-mcp",
    )
    return enrich(offer)


def _invalid_args(e: Exception) -> bool:
    msg = str(e)
    return "-32602" in msg or "Invalid arguments" in msg or "validation" in msg.lower()


def _ctx_variants(ctx: dict) -> list[dict]:
    """Контекст зі слота → той самий із самовивозом. «Сільпо» вимагає тип доставки й вікно, але може не прийняти тип зі слота."""
    out = [ctx]
    if ctx.get("deliveryType") not in (None, "SelfPickup"):
        out.append(dict(ctx, deliveryType="SelfPickup"))
    return out


async def _call_with_ctx(call: Call, name: str, ctx: dict, extra: dict) -> tuple[Any, dict]:
    """Викликає інструмент, спрощуючи контекст, якщо «Сільпо» відхиляє аргументи. Повертає (відповідь, робочий контекст)."""
    err: Exception | None = None
    for c in _ctx_variants(ctx):
        try:
            return await call(name, dict(c, **extra)), c
        except Exception as e:
            if not _invalid_args(e):
                raise
            log.warning("Сільпо: %s не прийняв %s: %s", name, {k: c[k] for k in c if k != "branchId"}, str(e)[:300])
            err = e
    raise err  # type: ignore[misc]


async def fetch_offers(call: Call, branch: dict, max_promotions: int = 12, page: int = 100, max_pages: int = 5,
                       parallel: int = 3) -> list[Offer]:
    """Усі акційні товари магазину: список акцій → товари кожної акції (з пагінацією), без дублів.
    Акції завантажуємо паралельно (до `parallel` одночасно), порядок результатів зберігаємо."""
    ctx = await branch_context(call, branch["id"])
    data, ctx = await _call_with_ctx(call, "silpo_get_promotions", ctx, {})
    data = data or {}
    promotions = _list(data.get("promotions"), data.get("items"))
    if not promotions:
        log.warning("Сільпо: акцій не знайдено для магазину %s", branch.get("title"))
    sem = asyncio.Semaphore(max(1, parallel))

    async def promo_items(promo: dict) -> list[Offer]:
        code, title = promo.get("code"), promo.get("title") or promo.get("name") or promo.get("code")
        out: list[Offer] = []
        if not code:
            return out
        async with sem:
            for n in range(max_pages):
                res, _ = await _call_with_ctx(call, "silpo_get_products", ctx,
                                              dict(promotionCode=code, inStock=True, limit=page, offset=n * page,
                                                   sortBy="promotion"))
                res = res or {}
                items = _list(res.get("products"), res.get("items"))
                out += [o for o in (normalize_product(p, branch=branch, promo=title) for p in items) if o]
                total = int((res.get("meta") or {}).get("total") or res.get("total") or 0)
                if len(items) < page or (total and (n + 1) * page >= total):
                    break
        return out

    seen: dict[str, Offer] = {}
    for batch in await asyncio.gather(*(promo_items(p) for p in promotions[:max_promotions])):
        for o in batch:
            seen.setdefault(o.product_id, o)
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


# ---------- історія покупок → «що купуєте щотижня» ----------

_GENERIC_2 = {"крупа", "філе", "сир", "йогурт", "масло", "кава", "чай", "вода", "сік", "ковбаса", "олія", "соус",
              "хліб", "батон", "печиво", "цукерки", "сметана", "кефір", "молоко", "яйця", "борошно", "фарш"}
_SKIP = {"пакет", "пакетик", "доставка", "сертифікат", "бонус"}


def generic_name(name: str) -> str | None:
    """«Молоко Яготинське 2,6% 900г» → «молоко»; «Сир твердий Гауда 45%» → «сир твердий»; «Крупа гречана …» → «крупа гречана»."""
    words = [w.strip("«»\"'(),.") for w in (name or "").replace("’", "'").split()]
    words = [w for w in words if w and not any(ch.isdigit() for ch in w) and w not in ("%", "г", "кг", "мл", "л", "шт")]
    if not words:
        return None
    first = words[0].lower()
    if first.split("-")[0] in _SKIP or len(first) < 3:
        return None
    if first in _GENERIC_2 and len(words) > 1 and words[1][:1].islower() and len(words[1]) > 3:
        return f"{first} {words[1].lower()}"
    return first


def _product_dicts(x, out: list):
    """Усі словники, схожі на товар у чеку/замовленні (мають назву), з будь-якої вкладеності."""
    if isinstance(x, dict):
        name = x.get("name") or x.get("title") or x.get("productName")
        if isinstance(name, str) and (x.get("quantity") is not None or x.get("price") is not None or x.get("productId")):
            out.append(x)
        for v in x.values():
            if isinstance(v, (dict, list)):
                _product_dicts(v, out)
    elif isinstance(x, list):
        for v in x:
            _product_dicts(v, out)


def _orders(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("orders", "items", "receipts", "cheques", "checks", "results", "data"):
            if isinstance(data.get(k), list):
                return data[k]
    return [data] if data else []


async def purchase_hints(call: Call, top: int = 12) -> dict:
    """Що людина купує регулярно — з онлайн-замовлень і чеків «Сільпо». Повертаємо як пропозицію."""
    from collections import Counter
    names: Counter = Counter()
    brands: Counter = Counter()
    n_orders = 0
    for tool in ("silpo_get_my_online_orders", "silpo_get_my_offline_orders"):
        data = None
        for args in ({"limit": 20}, {}):
            try:
                data = await call(tool, args)
                break
            except Exception as e:
                log.warning("Сільпо: %s(%s): %s", tool, args, str(e)[:300])
        if not data:
            continue
        log.info("Сільпо: %s → %s", tool, str(data)[:1200])
        for order in _orders(data)[:30]:
            prods: list = []
            _product_dicts(order, prods)
            if not prods:
                continue
            n_orders += 1
            seen, seen_b = set(), set()
            for p in prods:
                g = generic_name(p.get("name") or p.get("title") or p.get("productName") or "")
                if g and g not in seen:
                    seen.add(g)
                    names[g] += 1
                b = p.get("brand") or p.get("brandName") or p.get("trademark")
                if isinstance(b, dict):
                    b = b.get("name") or b.get("title")
                if isinstance(b, str) and b.strip() and b.lower() not in seen_b:
                    seen_b.add(b.lower())
                    brands[b.strip()] += 1
    need = 2 if n_orders >= 3 else 1
    return {"orders": n_orders,
            "regulars": [n for n, c in names.most_common(40) if c >= need][:top],
            "brands": [b for b, c in brands.most_common(20) if c >= max(2, need)][:6]}
