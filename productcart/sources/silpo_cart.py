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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..catalog import parse_pack

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
    names: dict[str, str] = field(default_factory=dict)
    total: float | None = None
    checkout: str | None = None
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
    calc = _first(cart, "calculation") or {}
    st.total = _num(_first(calc, "total", "totalAfterDiscounts"))
    st.checkout = _find(data, "checkoutWebLink", "webLink")
    return st


def _step(p: dict) -> float:
    return _num(_first(p, "addToBasketStep", "step")) or 1.0


def quantity_for(want: Want, product: dict | None = None) -> float:
    """Скільки класти: штуки (упаковки) для штучного товару, кілограми — для вагового."""
    weighted = bool(product.get("weighted")) if product is not None else want.weighted
    step = _step(product) if product is not None else (0.1 if weighted else 1.0)
    if weighted:
        kg = want.need / 1000 if want.unit in ("г", "мл") else want.need * 0.15  # штуку вагового — ~150 г
        return round(max(step, math.ceil(round(kg / step, 6)) * step), 3)
    if product is None:
        return float(max(1, want.packs))
    ratio = product.get("displayRatio") or product.get("ratio") or product.get("name") or ""
    qty, unit = parse_pack(str(ratio))
    if qty and unit and want.unit in ("г", "мл", "шт") and {"г": "g", "мл": "ml", "шт": "pcs"}[want.unit] == unit:
        return float(max(1, math.ceil(want.need / qty - 1e-9)))
    return float(max(1, math.ceil(step)))


def pick_product(items: list[dict]) -> dict | None:
    """Найдешевший товар у наявності (за кг / л / шт, коли це можна порахувати)."""
    def ok(p: dict) -> bool:
        stock = _num(p.get("stock"))
        return _first(p, "id", "productId") and _num(_first(p, "price", "currentPrice")) and (stock is None or stock > 0) \
            and p.get("available", True) is not False

    def unit_price(p: dict) -> float:
        price = _num(_first(p, "price", "currentPrice")) or 1e9
        if p.get("weighted"):
            return price
        qty, _ = parse_pack(str(p.get("displayRatio") or p.get("ratio") or p.get("name") or ""))
        return price / qty * 1000 if qty else price * 1000

    good = [p for p in items if ok(p)]
    return min(good[:5], key=unit_price) if good else None


# ---------- сценарій ----------

async def current_cart(call: Call) -> CartState | None:
    mine = await call("silpo_get_my_shopping_cart", {}) or {}
    log.info("Сільпо: get_my_shopping_cart → %s", str(mine)[:600])
    cid = _find(mine, "cartId", "shoppingCartId") or (_first(mine, "id") if isinstance(mine, dict) else None)
    if not cid:
        if _find(mine, "exists") is False:
            return None
        log.warning("Сільпо: get_my_shopping_cart без id: %s", str(mine)[:500])
        return None
    return parse_cart(await call("silpo_get_shopping_cart_by_id", {"shoppingCartId": cid}) or {})


async def resolve(call: Call, cart: CartState, wants: list[Want]) -> tuple[list[tuple[Want, str, float, str, bool]], list[Want]]:
    """→ ([(want, productId, quantity, назва, ваговий)], не знайдено)."""
    found: list[tuple[Want, str, float, str, bool]] = []
    missing: list[Want] = []
    search = [w for w in wants if not w.product_id]
    for w in wants:
        if w.product_id:
            found.append((w, w.product_id, quantity_for(w), w.name, w.weighted))
    ctx = {k: v for k, v in {"branchId": cart.branch_id, "deliveryType": cart.delivery_type,
                             "timeslotStart": cart.slot_start, "timeslotEnd": cart.slot_end}.items() if v}
    by_query: dict[str, list[dict]] = {}
    for i in range(0, len(search), SEARCH_BATCH):
        batch = search[i:i + SEARCH_BATCH]
        try:
            res = await call("silpo_find_products_batch", dict(ctx, products=[w.name for w in batch])) or {}
        except Exception as e:
            log.warning("Сільпо: find_products_batch: %s", str(e)[:300])
            continue
        queries = _first(res, "queries", "results") if isinstance(res, dict) else res
        log.info("Сільпо: find_products_batch → %s", str(res)[:800])
        for q in queries or []:
            name = _first(q, "query", "name", "requestedName")
            if name:
                by_query.setdefault(str(name).lower(), _first(q, "products", "items") or [])
    for w in search:
        p = pick_product(by_query.get(w.name.lower(), []))
        if p is None:
            missing.append(w)
            continue
        found.append((w, str(_first(p, "id", "productId")), quantity_for(w, p), p.get("name") or w.name, bool(p.get("weighted"))))
    return found, missing


async def add_to_cart(call: Call, cart: CartState, found: list[tuple[Want, str, float, str, bool]]) -> list[Placed]:
    """Додає, не видаляючи наявного: кількість = те, що вже лежить, + наша."""
    merged: dict[str, Placed] = {}
    for w, pid, qty, name, weighted in found:
        if pid in merged:
            merged[pid].quantity = round(merged[pid].quantity + qty, 3)
        else:
            merged[pid] = Placed(w.key, name, pid, qty, weighted)
    if not merged:
        return []
    products = [{"productId": pl.product_id, "companyId": cart.company_id or "",
                 "branchId": cart.branch_id or "", "quantity": round(cart.products.get(pl.product_id, 0) + pl.quantity, 3)}
                for pl in merged.values()]
    log.info("Сільпо: add_or_update %d позицій у кошик %s", len(products), cart.cart_id)
    res = await call("silpo_add_or_update_cart_products", {"shoppingCartId": cart.cart_id, "products": products})
    log.info("Сільпо: add_or_update → %s", str(res)[:600])
    return list(merged.values())


def wants_from_menu(lines, bought: set[str] = frozenset()) -> list[Want]:
    """Рядки «до каси» з меню → що класти. Позначене як куплене пропускаємо."""
    out = []
    for l in lines:
        if l.key in bought:
            continue
        o = l.source.offer
        if o is not None and o.chain == "silpo" and o.product_id:
            out.append(Want(l.key, o.name, l.need, l.unit, product_id=o.product_id, packs=l.packs, weighted=o.weighted))
        else:
            out.append(Want(l.key, l.name, l.need, l.unit))
    return out


async def build(call: Call, wants: list[Want]) -> dict:
    """Повний сценарій: кошик → пошук → додавання → перечитати суму й посилання на оплату."""
    cart = await current_cart(call)
    if cart is None:
        raise CartError("У вашому акаунті «Сільпо» ще немає кошика. Відкрийте silpo.ua або застосунок «Сільпо», "
                        "оберіть адресу доставки чи магазин самовивозу — і натисніть кнопку ще раз.")
    before = cart.count
    found, missing = await resolve(call, cart, wants)
    placed = await add_to_cart(call, cart, found)
    after = parse_cart(await call("silpo_get_shopping_cart_by_id", {"shoppingCartId": cart.cart_id}) or {})
    return {"placed": placed, "missing": missing, "had": before, "total": after.total,
            "checkout": after.checkout, "count": after.count}
