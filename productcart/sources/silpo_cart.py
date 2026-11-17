"""Кошик «Сільпо» зі списку покупок меню — від імені покупця, через офіційний MCP.

Правила:
- нічого не видаляємо: якщо товар уже лежить у кошику, додаємо нашу кількість до наявної;
- акційні товари кладемо за їхнім productId, решту шукаємо пакетним пошуком у магазині кошика;
- вагові товари: quantity — це кілограми (ціна за кг), а не кількість упаковок.
Назви інструментів і формати — як у живих клієнтах («SilpoRestockAI»); незнайомі відповіді пишемо в лог.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..catalog import parse_pack
from . import substitute

log = logging.getLogger(__name__)
Call = Callable[[str, dict], Awaitable[Any]]

SEARCH_BATCH = 30


@dataclass
class Want:
    """Рядок списку, який треба покласти в кошик."""
    key: str
    name: str                 # як шукати в каталозі, якщо немає productId
    need: float               # скільки треба: г | мл | шт
    unit: str                 # "г" | "мл" | "шт"
    product_id: str | None = None
    packs: int = 1            # для акційного штучного товару
    weighted: bool = False
    generic: str | None = None  # загальна назва («Морква») — шукаємо заміну, якщо акційного товару немає в наявності
    alts: tuple = ()            # інші прості запити по черзі: «хек» → «минтай» → «філе риби»
    label: str | None = None    # як показати людині («Макарони»), коли name — пошуковий запит

    @property
    def title(self) -> str:
        return self.label or self.name


@dataclass
class Placed:
    key: str
    name: str
    product_id: str
    quantity: float
    weighted: bool


@dataclass
class CartState:
    cart_id: str
    branch_id: str | None
    company_id: str | None
    delivery_type: str | None
    slot_start: str | None
    slot_end: str | None
    products: dict[str, float] = field(default_factory=dict)   # productId → quantity
    line_ids: dict[str, str] = field(default_factory=dict)     # productId → id рядка кошика (для зміни кількості)
    lines: list[dict] = field(default_factory=list)            # для показу: name, quantity, weighted, price, total…
    validations: list[str] = field(default_factory=list)
    min_order: float | None = None          # мінімальна сума замовлення, якщо «Сільпо» її називає
    names: dict[str, str] = field(default_factory=dict)
    total: float | None = None            # разом із доставкою
    products_total: float | None = None   # лише товари
    delivery: float | None = None
    checkout: str | None = None
    checkout_mobile: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.products)


class CartError(RuntimeError):
    """Зрозуміла людині причина, чому кошик не зібрався."""


# ---------- розбір відповідей ----------

def _first(d: Any, *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def _find(d: Any, *keys: str) -> Any:
    """Перше значення з ключем серед вкладених словників (відповіді «Сільпо» буває загорнуто в cart / data)."""
    if isinstance(d, dict):
        v = _first(d, *keys)
        if v is not None:
            return v
        for inner in d.values():
            if isinstance(inner, (dict, list)):
                v = _find(inner, *keys)
                if v is not None:
                    return v
    elif isinstance(d, list):
        for x in d:
            v = _find(x, *keys)
            if v is not None:
                return v
    return None


def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_cart(data: Any) -> CartState:
    cart = data.get("cart") if isinstance(data, dict) and isinstance(data.get("cart"), dict) else data
    cid = _first(cart, "id", "cartId", "shoppingCartId") or _find(data, "cartId", "shoppingCartId")
    if not cid:
        raise CartError("«Сільпо» не повернуло кошик.")
    timeslot = _first(cart, "timeslot", "timeSlot") or {}
    shipments = _first(cart, "shipments") or []
    st = CartState(cart_id=str(cid), branch_id=_find(shipments, "branchId") or _first(cart, "branchId"),
                   company_id=_find(shipments, "companyId") or _first(cart, "companyId"),
                   delivery_type=_first(cart, "deliveryType"),
                   slot_start=_first(timeslot, "start", "from"), slot_end=_first(timeslot, "end", "to"), raw=cart)
    for sh in shipments if isinstance(shipments, list) else []:
        for p in _first(sh, "products") or []:
            pid = _first(p, "productId", "id")
            if pid:
                st.products[str(pid)] = (_num(p.get("quantity")) or 0) + st.products.get(str(pid), 0)
                st.names[str(pid)] = p.get("name") or ""
                if p.get("id") and p.get("productId"):
                    st.line_ids[str(pid)] = str(p["id"])
                st.lines.append({"line_id": str(p.get("id") or ""), "product_id": str(pid), "name": p.get("name") or "товар", "ratio": p.get("ratio") or p.get("displayRatio"),
                                 "quantity": _num(p.get("quantity")) or 0, "weighted": bool(p.get("weighted")),
                                 "price": _num(p.get("price")), "old": _num(p.get("oldPrice")),
                                 "total": _num(_first(p, "total", "subTotal")), "image": p.get("image"),
                                 "slug": p.get("slug"), "stock": _num(p.get("stock"))})
    calc = _first(cart, "calculation") or {}
    st.total = _num(_first(calc, "totalAfterDiscounts", "total"))
    st.products_total = _num(_first(calc, "productsTotal"))
    deliv = _first(calc, "delivery")
    st.delivery = _num(deliv.get("total")) if isinstance(deliv, dict) else _num(deliv)
    if st.products_total is None and st.lines:   # «Сільпо» не дало суми товарів — рахуємо самі з рядків
        st.products_total = round(sum(l["total"] or (l["price"] or 0) * l["quantity"] for l in st.lines), 2)
    for v in _first(calc, "validations") or []:
        msg, c = v.get("message") or "", v.get("context") or {}
        if msg == "order.cost.min" and _num(c.get("orderCostMin")):
            st.min_order = _num(c.get("orderCostMin"))
        if v.get("level") == "error":
            st.validations.append(VALIDATION_TEXT.get(msg, msg).format(**{k: c.get(k, "") for k in ("orderCostMin",)}))
    st.checkout = _find(data, "checkoutWebLink", "webLink")
    st.checkout_mobile = _find(data, "checkoutMobileLink", "mobileLink")
    return st


VALIDATION_TEXT = {
    "order.cost.min": "Мінімальна сума замовлення в «Сільпо» — {orderCostMin} ₴. Додайте товарів, щоб оформити.",
    "product.offer.not_found": "Деяких товарів немає в цьому магазині — «Сільпо» прибере їх при оформленні.",
}


def _step(p: dict) -> float:
    return _num(_first(p, "addToBasketStep", "step")) or 1.0


UNIT = {"g": "g", "г": "g", "ml": "ml", "мл": "ml", "pcs": "pcs", "шт": "pcs"}
MAX_KG = 5.0      # запобіжник: на тиждень вечерь більше 5 кг одного вагового товару — це помилка
MAX_PACKS = 10


def quantity_for(want: Want, product: dict | None = None) -> float:
    """Скільки класти: штуки (упаковки) для штучного товару, кілограми — для вагового."""
    unit = UNIT.get(want.unit, want.unit)
    weighted = bool(product.get("weighted")) if product is not None else want.weighted
    step = _step(product) if product is not None else (0.1 if weighted else 1.0)
    if weighted:
        kg = want.need / 1000 if unit in ("g", "ml") else want.need * 0.15  # штуку вагового — ~150 г
        return round(max(step, math.ceil(round(kg / step, 6)) * step), 3)
    if product is None:
        return float(max(1, want.packs))
    ratio = product.get("displayRatio") or product.get("ratio") or product.get("name") or ""
    qty, punit = parse_pack(str(ratio))
    if qty and punit == unit:
        return float(max(1, math.ceil(want.need / qty - 1e-9)))
    return float(max(1, math.ceil(step)))


def sane(qty: float, weighted: bool) -> bool:
    return 0 < qty <= (MAX_KG if weighted else MAX_PACKS)


def _sellable(p: dict) -> bool:
    stock = _num(p.get("stock"))
    return bool(_first(p, "id", "productId") and _num(_first(p, "price", "currentPrice")) and (stock is None or stock > 0)
                and p.get("available", True) is not False)


def unit_price(p: dict) -> float:
    price = _num(_first(p, "price", "currentPrice")) or 1e9
    if p.get("weighted"):
        return price
    qty, _ = parse_pack(str(p.get("displayRatio") or p.get("ratio") or p.get("name") or ""))
    return price / qty * 1000 if qty else price * 1000


def pick_product(items: list[dict]) -> dict | None:
    """Найдешевший товар у наявності (за кг / л / шт, коли це можна порахувати)."""
    good = [p for p in items if _sellable(p)]
    return min(good[:5], key=unit_price) if good else None


GOOD_MATCH = 0.6      # такий кандидат беремо одразу; слабший — лише якщо кращого не знайдеться в наступних запитах
PRICE_BAND = 0.08     # серед майже однаково схожих — найдешевший за кг / л / шт


def score_candidate(p: dict, original: str, key: str, orig_weighted: bool | None = None) -> float:
    """Наскільки товар годиться: збіг з інгредієнтом меню + схожість на оригінал (substitute.relevance)."""
    rel = substitute.relevance(original, str(p.get("name") or ""), orig_weighted=orig_weighted,
                               cand_weighted=bool(p.get("weighted")))
    if _known(key):
        if _matches(p, key):
            if not rel and substitute.changes_form(original, str(p.get("name") or "")):
                return 0.0                                # «соус з куркою» для курки — ні
            return round(0.5 + 0.5 * (rel or 0.3), 3)     # інгредієнт той; чим схожіше на оригінал — тим вище
        return rel if rel >= GOOD_MATCH else 0.0          # не той інгредієнт — лише якщо дуже схоже на оригінал
    return rel


def best_candidate(items: list[dict], original: str, key: str, orig_weighted: bool | None = None) -> tuple[dict | None, float]:
    """Найрелевантніший товар у наявності; серед майже однаково схожих — найдешевший за одиницю."""
    scored = [(score_candidate(p, original, key, orig_weighted), p) for p in items if _sellable(p)]
    scored = [(sc, p) for sc, p in scored if sc >= substitute.MIN_SCORE]
    if not scored:
        return None, 0.0
    top = max(sc for sc, _ in scored)
    band = [(sc, p) for sc, p in scored if sc >= top - PRICE_BAND]
    sc, p = min(band, key=lambda x: (unit_price(x[1]), -x[0]))
    return p, sc


# ---------- сценарій ----------

async def current_cart(call: Call) -> CartState | None:
    mine = await call("silpo_get_my_shopping_cart", {}) or {}
    log.debug("Сільпо: get_my_shopping_cart → %s", str(mine)[:600])
    cid = _find(mine, "cartId", "shoppingCartId") or (_first(mine, "id") if isinstance(mine, dict) else None)
    if not cid:
        if _find(mine, "exists") is False:
            return None
        log.warning("Сільпо: get_my_shopping_cart без id: %s", str(mine)[:500])
        return None
    data = await call("silpo_get_shopping_cart_by_id", {"shoppingCartId": cid}) or {}
    cart = parse_cart(data)
    log.debug("Сільпо: кошик %s: позицій %d, сума %s, ключі %s, web-посилання %s, mobile-посилання %s, перевірки %s",
             cart.cart_id, cart.count, cart.total, sorted(data)[:15] if isinstance(data, dict) else type(data).__name__,
             (cart.checkout or "")[:40], (cart.checkout_mobile or "")[:40], cart.validations)
    return cart


def _stock_ok(p: dict, qty: float) -> bool:
    if p.get("available") is False:
        return False
    stock = _num(p.get("stock"))
    return stock is None or stock >= qty


async def _search_once(call: Call, ctx: dict, names: list[str]) -> tuple[dict[str, list[dict]], int, str]:
    """Один прохід пакетного пошуку → (назва запиту lower → товари, скільки знайдено, зразок сирої відповіді)."""
    by_query: dict[str, list[dict]] = {}
    total, sample = 0, ""
    for i in range(0, len(names), SEARCH_BATCH):
        chunk = names[i:i + SEARCH_BATCH]
        try:
            res = await call("silpo_find_products_batch", dict(ctx, products=chunk)) or {}
        except Exception as e:
            log.warning("Сільпо: find_products_batch: %s", str(e)[:300])
            sample = sample or f"помилка: {str(e)[:300]}"
            continue
        sample = sample or str(res)[:600]
        queries = _first(res, "queries", "results") if isinstance(res, dict) else res
        log.debug("Сільпо: find_products_batch → %s", str(res)[:800])
        for j, q in enumerate(queries or []):
            name = _first(q, "query", "name", "requestedName") if isinstance(q, dict) else None
            items = (_first(q, "products", "items") if isinstance(q, dict) else None) or []
            total += len(items)
            if name:
                by_query.setdefault(str(name).lower(), items)
            if j < len(chunk):      # якщо API переформулювало запит — тримаємо й за порядком
                by_query.setdefault(chunk[j].lower(), items)
    return by_query, total, sample


def _slot_over(ctx: dict) -> bool:
    end = ctx.get("timeslotEnd")
    if not end:
        return False
    try:
        t = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) < datetime.now(timezone.utc)


async def _fresh_ctx(call: Call, ctx: dict) -> dict | None:
    """Свіже вікно магазину кошика: зі старим (минулим) вікном «Сільпо» віддає порожній пошук."""
    if not ctx.get("branchId"):
        return None
    try:
        from .silpo import branch_context
        fresh = await branch_context(call, ctx["branchId"])
    except Exception as e:
        log.warning("Сільпо: свіже вікно для пошуку: %s", str(e)[:300])
        return None
    return fresh if fresh != ctx else None


async def _search(call: Call, ctx: dict, names: list[str]) -> dict[str, list[dict]]:
    """Пакетний пошук у магазині кошика: назва запиту (lower) → знайдені товари.
    Якщо вікно кошика вже минуло або пошук не дав нічого взагалі — повторюємо зі свіжим вікном магазину
    (ctx оновлюємо на місці, щоб наступні кола шукали вже з ним)."""
    names = list(dict.fromkeys(n for n in names if n))
    if not names:
        return {}
    if _slot_over(ctx) and not ctx.get("_fresh"):
        fresh = await _fresh_ctx(call, ctx)
        ctx["_fresh"] = True
        if fresh:
            log.info("Сільпо: вікно кошика минуло (%s) — шукаємо з новим %s", ctx.get("timeslotEnd"), fresh.get("timeslotStart"))
            ctx.update(fresh)
    args = {k: v for k, v in ctx.items() if not k.startswith("_")}
    by_query, total, sample = await _search_once(call, args, names)
    if total or ctx.get("_fresh"):
        if not total:
            log.warning("Сільпо: пошук порожній (%d запитів, контекст %s): %s", len(names),
                        {k: v for k, v in args.items() if k != "branchId"}, sample)
        return by_query
    ctx["_fresh"] = True
    fresh = await _fresh_ctx(call, args)
    if not fresh:
        log.warning("Сільпо: пошук порожній (%d запитів, контекст %s): %s", len(names),
                    {k: v for k, v in args.items() if k != "branchId"}, sample)
        return by_query
    by_query2, total2, sample2 = await _search_once(call, fresh, names)
    log.warning("Сільпо: пошук порожній з вікном кошика %s–%s → зі свіжим %s: знайдено %d%s",
                args.get("timeslotStart"), args.get("timeslotEnd"), fresh.get("timeslotStart"), total2,
                "" if total2 else f"; відповідь: {sample2}")
    if total2:
        ctx.update(fresh)
        return by_query2
    return by_query


def _known(key: str) -> bool:
    try:
        from ..menu import INGREDIENTS
        return key in INGREDIENTS
    except Exception:
        return False


def _matches(p: dict, key: str) -> bool:
    try:
        from ..menu import INGREDIENTS, matches_ingredient
        return key in INGREDIENTS and matches_ingredient(str(p.get("name") or ""), key)
    except Exception:
        return False


async def resolve(call: Call, cart: CartState, wants: list[Want]) -> dict:
    """Перевіряє наявність у магазині кошика й підбирає товари.
    → {"found": [(want, productId, quantity, назва, ваговий)], "missing": [Want], "replaced": [(старе, нове)]}
    Акційний товар — саме той, якщо є й вистачає залишку; інакше заміна. Решту шукаємо кількома запитами
    по черзі (від найточнішого), беремо найсхожіше на оригінал (substitute.relevance), серед майже однаково схожих — найдешевше."""
    ctx = {k: v for k, v in {"branchId": cart.branch_id, "deliveryType": cart.delivery_type,
                             "timeslotStart": cart.slot_start, "timeslotEnd": cart.slot_end}.items() if v}
    found, missing, replaced = [], [], []
    swap_from: dict[str, str] = {}   # ключ рядка → назва акційного товару, якого не виявилось
    why: dict[str, str] = {}         # ключ → чому не знайшли (у лог, для діагностики)
    origin: dict[str, tuple[str, bool | None]] = {}   # ключ → на що має бути схожа заміна
    weak: dict[str, tuple[float, dict, Want]] = {}    # найкращий слабкий кандидат, якщо сильного не буде
    # 1) акційні товари: чи є саме цей товар у магазині кошика і чи вистачає залишку
    by_id_query = await _search(call, ctx, [w.name for w in wants if w.product_id])
    pending: list[tuple[Want, list[str]]] = []      # (рядок, черга запитів)
    for w in wants:
        queue = [q for q in dict.fromkeys([w.name] + list(w.alts)) if q]
        if not w.product_id:
            pending.append((w, queue))
            continue
        qty = quantity_for(w)
        same = next((p for p in by_id_query.get(w.name.lower(), []) if str(_first(p, "id", "productId")) == w.product_id), None)
        if same is not None and _stock_ok(same, qty):
            found.append((w, w.product_id, qty, w.name, w.weighted))
            continue
        why[w.key] = "акційного немає" if same is None else "акційного мало на залишку"
        alt_queue = substitute.queries(w.name, w.generic, tuple(w.alts))
        if not alt_queue:
            missing.append(w)
            continue
        log.info("Сільпо: «%s» немає в наявності — шукаємо заміну: %s", w.name, ", ".join(alt_queue[:4]))
        swap_from[w.key] = w.name
        origin[w.key] = (w.name, w.weighted)
        pending.append((Want(w.key, alt_queue[0], w.need, w.unit, alts=tuple(alt_queue[1:]), label=w.title), alt_queue))
    # 2) пошук за назвами: кожне коло — наступний запит для тих, що ще не знайдені
    tries: dict[str, list[str]] = {}
    for _round in range(6):
        if not pending:
            break
        names = [queue[0] for _, queue in pending]
        by_query = await _search(call, ctx, names)
        nxt = []
        for w, queue in pending:
            q = queue[0]
            tries.setdefault(w.key, []).append(q)
            got = by_query.get(q.lower(), [])
            ok_stock = [p for p in got if _stock_ok(p, quantity_for(w, p))]
            orig, ow = origin.get(w.key, (w.label or w.name, None))
            p, sc = best_candidate(ok_stock, orig, w.key, ow)
            if p is not None and sc < GOOD_MATCH and len(queue) > 1:
                if sc > weak.get(w.key, (0.0,))[0]:
                    weak[w.key] = (sc, p, w)            # слабкувато — спершу спробуємо точніші запити далі
                p = None
            if p is None:
                prev = weak.get(w.key)
                if len(queue) > 1:
                    why[w.key] = "нічого не знайшли" if not got else (f"знайшли {len(got)}, але немає в наявності" if not ok_stock
                                                                      else f"знайшли {len(ok_stock)}, але не схожі")
                    nxt.append((w, queue[1:]))
                    continue
                if prev is None:
                    why[w.key] = "нічого не знайшли" if not got else (f"знайшли {len(got)}, але немає в наявності" if not ok_stock
                                                                      else f"знайшли {len(ok_stock)}, але не схожі на «{orig}»")
                    missing.append(w)
                    continue
                sc, p, _ = prev
            elif weak.get(w.key) and weak[w.key][0] > sc:
                sc, p, _ = weak[w.key]                  # останній запит дав гірше, ніж попередній
            weak.pop(w.key, None)
            name = p.get("name") or w.name
            log.info("Сільпо: для «%s» беремо «%s» (схожість %.2f, запит «%s»)", orig, name, sc, q)
            if w.key in swap_from:
                replaced.append((swap_from[w.key], name))
            found.append((w, str(_first(p, "id", "productId")), quantity_for(w, p), name, bool(p.get("weighted"))))
        pending = nxt
    for w, _ in pending:
        if w.key in weak:                           # черга не скінчилась, але щось годяще вже знайшли
            sc, p, _ = weak.pop(w.key)
            name = p.get("name") or w.name
            if w.key in swap_from:
                replaced.append((swap_from[w.key], name))
            found.append((w, str(_first(p, "id", "productId")), quantity_for(w, p), name, bool(p.get("weighted"))))
        else:
            missing.append(w)
    ok = []
    for item in found:
        if sane(item[2], item[4]):
            ok.append(item)
        else:
            log.warning("Сільпо: підозріла кількість %s для «%s» (%s %s) — не кладемо", item[2], item[3], item[0].need, item[0].unit)
            why[item[0].key] = "підозріла кількість"
            missing.append(item[0])
    if missing:
        log.warning("Сільпо: не знайшли в магазині %s: %s", cart.branch_id,
                    "; ".join(f"{w.title} ({why.get(w.key, '?')}; шукали: {', '.join(tries.get(w.key, []) or [w.name])})"
                              for w in missing)[:1500])
    return {"found": ok, "missing": missing, "replaced": replaced}


_schemas: dict[str, dict] = {}


async def tool_schema(call: Call, name: str) -> dict:
    """Схема інструмента з tools/list (кешуємо в процесі). Порожня, якщо сесія недоступна."""
    if not _schemas:
        session = getattr(call, "session", None)
        if session is not None:
            try:
                tools = (await session.list_tools()).tools
                _schemas.update({t.name: t.inputSchema or {} for t in tools})
                log.debug("Сільпо: кошикові інструменти: %s", sorted(n for n in _schemas if "cart" in n))
            except Exception as e:
                log.warning("Сільпо: tools/list: %s", str(e)[:300])
    return _schemas.get(name, {})


def _item_props(schema: dict) -> dict:
    items = ((schema.get("properties") or {}).get("products") or {}).get("items") or {}
    return items.get("properties") or {}


async def add_to_cart(call: Call, cart: CartState, found: list[tuple[Want, str, float, str, bool]]) -> list[Placed]:
    """Додає, не видаляючи наявного. Нове — silpo_add_cart_products; те, що вже лежить, —
    silpo_update_cart_products за id рядка кошика, кількість = наявна + наша."""
    merged: dict[str, Placed] = {}
    for w, pid, qty, name, weighted in found:
        if pid in merged:
            merged[pid].quantity = round(merged[pid].quantity + qty, 3)
        else:
            merged[pid] = Placed(w.key, name, pid, qty, weighted)
    if not merged:
        return []
    new = [pl for pl in merged.values() if pl.product_id not in cart.products]
    old = [pl for pl in merged.values() if pl.product_id in cart.products]
    if new:
        add_schema = await tool_schema(call, "silpo_add_cart_products")
        if add_schema:
            log.debug("Сільпо: схема silpo_add_cart_products: %s", str(add_schema)[:1200])
        products = [{"productId": pl.product_id, "companyId": cart.company_id or "", "branchId": cart.branch_id or "",
                     "quantity": pl.quantity} for pl in new]
        log.info("Сільпо: add_cart_products %d позицій у кошик %s", len(products), cart.cart_id)
        res = await call("silpo_add_cart_products", {"shoppingCartId": cart.cart_id, "products": products})
        log.debug("Сільпо: add_cart_products → %s", str(res)[:600])
    if old:
        upd_schema = await tool_schema(call, "silpo_update_cart_products")
        if upd_schema:
            log.debug("Сільпо: схема silpo_update_cart_products: %s", str(upd_schema)[:1200])
        props = _item_props(upd_schema)
        id_key = next((k for k in ("id", "cartProductId", "lineItemId", "itemId") if k in props), "id")
        lines = []
        for pl in old:
            line_id = cart.line_ids.get(pl.product_id)
            if not line_id:
                log.warning("Сільпо: немає id рядка кошика для %s", pl.product_id)
                continue
            item = {id_key: line_id, "quantity": round(cart.products[pl.product_id] + pl.quantity, 3)}
            if "productId" in props:
                item["productId"] = pl.product_id
            lines.append(item)
        if lines:
            res = await call("silpo_update_cart_products", {"shoppingCartId": cart.cart_id, "products": lines})
            log.debug("Сільпо: update_cart_products → %s", str(res)[:600])
    return list(merged.values())


def search_terms(key: str) -> tuple:
    from ..menu import search_terms as _terms
    return _terms(key) if not key.startswith("x:") else ()


def wants_from_menu(lines, bought: set[str] = frozenset()) -> list[Want]:
    """Рядки «до каси» з меню → що класти. Позначене як куплене пропускаємо."""
    out = []
    for l in lines:
        if l.key in bought:
            continue
        o = l.source.offer
        terms = search_terms(l.key)
        if o is not None and o.chain == "silpo" and o.product_id:
            out.append(Want(l.key, o.name, l.need, l.unit, product_id=o.product_id, packs=l.packs, weighted=o.weighted,
                            generic=terms[0] if terms else l.name, alts=tuple(terms[1:]), label=o.name))
        elif terms:
            out.append(Want(l.key, terms[0], l.need, l.unit, alts=tuple(terms[1:]), label=l.name))
        else:
            out.append(Want(l.key, l.name, l.need, l.unit))
    return out


# ---------- створення кошика, якщо його ще немає ----------

HOME_TYPES = ("DeliveryHome", "WideAssortDelivery", "B2B")


async def _slot(call: Call, branch_id: str, delivery_type: str) -> tuple[str, str]:
    data = await call("silpo_get_time_slots", {"branchId": branch_id, "deliveryTypes": [delivery_type], "limit": 100}) or {}
    slots = _first(data, "slots", "timeSlots", "timeslots", "items") or []
    slot = next((x for x in slots if x.get("available", True) and _first(x, "start", "from") and _first(x, "end", "to")), None)
    if not slot:
        log.warning("Сільпо: немає слотів %s для %s: %s", delivery_type, branch_id, str(data)[:500])
        raise CartError(f"немає вільних вікон ({delivery_type})")
    return _first(slot, "start", "from"), _first(slot, "end", "to")


async def _create(call: Call, args: dict) -> CartState:
    log.info("Сільпо: create_shopping_cart branch=%s type=%s", args.get("branchId"), args.get("deliveryType"))
    created = await call("silpo_create_shopping_cart", args) or {}
    log.debug("Сільпо: create_shopping_cart → %s", str(created)[:600])
    cid = _find(created, "shoppingCartId", "cartId") or _first(created.get("cart") if isinstance(created, dict) else None, "id")
    if not cid:
        raise CartError("«Сільпо» не створило кошик")
    return parse_cart(await call("silpo_get_shopping_cart_by_id", {"shoppingCartId": cid}) or {})


async def _saved_address(call: Call) -> dict | None:
    try:
        data = await call("silpo_get_my_delivery_addresses", {}) or {}
    except Exception as e:
        log.warning("Сільпо: адреси: %s", str(e)[:300])
        return None
    items = _first(data, "addresses", "items", "results") if isinstance(data, dict) else data
    return items[0] if items else None


def _address_args(a: dict, lat: float, lon: float) -> dict:
    out = {"addressType": _first(a, "addressType") or "house", "latitude": lat, "longitude": lon}
    for k in ("city", "street", "house", "district"):
        if _first(a, k):
            out[k] = _first(a, k)
    return out


async def create_cart(call: Call, branch: dict | None) -> tuple[CartState, str]:
    """Кошик в акаунті один; якщо його немає — створюємо. Спершу самовивіз із магазину покупця,
    далі доставка на збережену в «Сільпо» адресу. → (кошик, опис для людини)."""
    problems = []
    saved = await _saved_address(call)
    if branch and branch.get("id"):
        try:
            lat = _num(branch.get("lat")) or _num(_first(saved or {}, "latitude", "lat"))
            lon = _num(branch.get("lon")) or _num(_first(saved or {}, "longitude", "lon"))
            if lat is None or lon is None:
                raise CartError("немає координат магазину")
            start, end = await _slot(call, branch["id"], "SelfPickup")
            addr = saved or {"city": branch.get("city"), "street": branch.get("address")}
            cart = await _create(call, dict(_address_args(addr, lat, lon), deliveryType="SelfPickup",
                                            branchId=branch["id"], timeslot={"start": start, "end": end}))
            return cart, f"Створили кошик із самовивозом: {branch.get('title') or 'ваш магазин'}"
        except Exception as e:
            problems.append(f"самовивіз: {e}")
            log.warning("Сільпо: кошик із самовивозом не створився: %s", str(e)[:800])
    if saved:
        try:
            lat, lon = _num(_first(saved, "latitude", "lat")), _num(_first(saved, "longitude", "lon"))
            types = await call("silpo_get_available_delivery_types", {"latitude": lat, "longitude": lon}) or {}
            opts = _first(types, "deliveryTypes", "types", "results", "options") or []
            opt = next((o for o in opts if _first(o, "deliveryType", "type") in HOME_TYPES and _first(o, "branchId")), None)
            if not opt:
                log.warning("Сільпо: типи доставки: %s", str(types)[:500])
                raise CartError("немає доставки на збережену адресу")
            dtype, bid = _first(opt, "deliveryType", "type"), _first(opt, "branchId")
            start, end = await _slot(call, bid, dtype)
            cart = await _create(call, dict(_address_args(saved, lat, lon), deliveryType=dtype, branchId=bid,
                                            timeslot={"start": start, "end": end}))
            return cart, "Створили кошик із доставкою на вашу адресу в «Сільпо»"
        except Exception as e:
            problems.append(f"доставка: {e}")
            log.warning("Сільпо: кошик із доставкою не створився: %s", str(e)[:800])
    raise CartError("У вашому акаунті «Сільпо» ще немає кошика, і створити його не вдалося"
                    + (f" ({'; '.join(problems)[:200]})" if problems else "")
                    + ". Відкрийте silpo.ua або застосунок «Сільпо», додайте будь-який товар у кошик — і натисніть кнопку ще раз.")


async def build(call: Call, wants: list[Want], branch: dict | None = None) -> dict:
    """Повний сценарій: кошик (або новий) → пошук → додавання → перечитати суму й посилання на оплату."""
    cart = await current_cart(call)
    created = None
    if cart is None:
        cart, created = await create_cart(call, branch)
    before = cart.count
    r = await resolve(call, cart, wants)
    found, missing = r["found"], r["missing"]
    placed = await add_to_cart(call, cart, found)
    after = parse_cart(await call("silpo_get_shopping_cart_by_id", {"shoppingCartId": cart.cart_id}) or {})
    return {"placed": placed, "missing": missing, "had": before, "total": after.total,
            "checkout": after.checkout, "count": after.count, "created": created, "replaced": r["replaced"],
            "products_total": after.products_total, "delivery": after.delivery}


# ---------- зміна кошика зі сторінки «Мій кошик» ----------

async def set_quantity(call: Call, cart: CartState, line_id: str, quantity: float) -> None:
    line = next((l for l in cart.lines if l["line_id"] == line_id), None)
    if line is None:
        raise CartError("Цього товару вже немає в кошику — оновіть сторінку.")
    schema = await tool_schema(call, "silpo_update_cart_products")
    log.debug("Сільпо: схема silpo_update_cart_products: %s", str(schema)[:1200])
    props = _item_props(schema)
    id_key = next((k for k in ("id", "cartProductId", "lineItemId", "itemId") if k in props), "id")
    item = {id_key: line_id, "quantity": quantity}
    if "productId" in props:
        item["productId"] = line["product_id"]
    res = await call("silpo_update_cart_products", {"shoppingCartId": cart.cart_id, "products": [item]})
    log.debug("Сільпо: update_cart_products → %s", str(res)[:400])


async def remove_line(call: Call, cart: CartState, line_id: str) -> None:
    line = next((l for l in cart.lines if l["line_id"] == line_id), None)
    if line is None:
        return
    schema = await tool_schema(call, "silpo_remove_cart_products")
    log.debug("Сільпо: схема silpo_remove_cart_products: %s", str(schema)[:1200])
    props = schema.get("properties") or {}
    args: dict = {"shoppingCartId": cart.cart_id}
    if "products" in props or not props:
        ip = _item_props(schema)
        if "productId" in ip and not any(k in ip for k in ("id", "cartProductId", "lineItemId", "itemId")):
            args["products"] = [{"productId": line["product_id"], "companyId": cart.company_id or "",
                                 "branchId": cart.branch_id or ""}]
        else:
            key = next((k for k in ("id", "cartProductId", "lineItemId", "itemId") if k in ip), "id")
            args["products"] = [{key: line_id}]
    elif "productIds" in props:
        args["productIds"] = [line["product_id"]]
    else:
        key = next((k for k in ("ids", "cartProductIds", "lineItemIds", "itemIds") if k in props), "ids")
        args[key] = [line_id]
    res = await call("silpo_remove_cart_products", args)
    log.debug("Сільпо: remove_cart_products → %s", str(res)[:400])


async def clear(call: Call, cart: CartState) -> None:
    res = await call("silpo_clear_shopping_cart", {"shoppingCartId": cart.cart_id})
    log.debug("Сільпо: clear_shopping_cart → %s", str(res)[:300])


async def count(call: Call) -> int:
    cart = await current_cart(call)
    return cart.count if cart else 0
