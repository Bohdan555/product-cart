"""Адаптер «Сільпо» на підробленому MCP. Формати відповідей — як у відкритих клієнтах хакатону AI Factory."""
import asyncio

from productcart import matcher, planner
from productcart.models import Profile
from productcart.sources import silpo

BRANCHES = {"branches": [
    {"branchId": "b1", "city": "Київ", "address": "вул. Івана Франка, 1", "latitude": "50.44", "longitude": "30.51"},
    {"branchId": "b2", "city": "Львів", "address": "пр. Свободи, 5"},
    {"branchId": "b3", "city": "Київ", "address": ""},
    {"branchId": "b4", "city": "Київ", "address": "вул. Хрещатик, 2", "latitude": "50.445", "longitude": "30.515"},
    {"branchId": "b5", "city": "Київ", "address": "просп. Перемоги, 99", "latitude": "50.46", "longitude": "30.40"},
], "meta": {"total": 5}}

PRODUCTS = {
    "cinotyzhyky": [
        {"id": "p1", "name": "Молоко 2,5%", "displayRatio": "900г", "price": 36.9, "oldPrice": 45.9, "slug": "moloko-25",
         "categoryName": "Молоко"},
        {"id": "p2", "title": "Кава зернова Арабіка", "displayRatio": "1кг", "currentPrice": 459, "priceOld": 639},
        {"id": "p3", "name": "Банани", "displayRatio": "1кг", "price": 54.9, "oldPrice": 54.9, "weighted": True},
    ],
    "2for1": [
        {"id": "p1", "name": "Молоко 2,5%", "displayRatio": "900г", "price": 36.9, "oldPrice": 45.9},  # дубль
        {"id": "p4", "name": "Йогурт питний", "displayRatio": "270г", "price": 29.9, "oldPrice": 34.9,
         "specialPrices": [{"price": 25.0, "count": 2}]},
        {"name": "Без ціни"},
    ],
}


class FakeMCP:
    def __init__(self):
        self.calls = []

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if name == "silpo_list_branches":
            return BRANCHES
        if name == "silpo_get_time_slots":
            return {"slots": [{"available": True, "start": "2026-11-15T10:00", "end": "2026-11-15T12:00",
                               "deliveryType": "DeliveryHome"}]}
        if name == "silpo_get_promotions":
            return {"promotions": [{"code": "cinotyzhyky", "title": "Цінотижики"}, {"code": "2for1", "title": "Вигідно разом"},
                                   {"title": "без коду"}]}
        if name == "silpo_get_products":
            return {"products": PRODUCTS[args["promotionCode"]], "meta": {"total": len(PRODUCTS[args["promotionCode"]])}}
        if name == "silpo_get_my_food_restrictions":
            return {"restrictions": [{"title": "Без глютену"}, {"title": "Вегетаріанське харчування"}]}
        if name == "silpo_get_my_family":
            return {"members": [{"name": "Дитина"}, {"name": "Партнер"}]}
        raise AssertionError(name)


def run(coro):
    return asyncio.run(coro)


def test_branches_filter():
    call = FakeMCP()
    kyiv = run(silpo.list_branches(call, city="Київ"))
    assert sorted(b["id"] for b in kyiv) == ["b1", "b4", "b5"]  # без адреси відкидаємо
    assert run(silpo.list_branches(call, query="Франка"))[0]["title"] == "Київ, вул. Івана Франка, 1"


def test_fetch_offers_maps_and_dedupes():
    call = FakeMCP()
    branch = {"id": "b1", "title": "Київ, вул. Івана Франка, 1"}
    offers = {o.product_id: o for o in run(silpo.fetch_offers(call, branch))}
    assert set(offers) == {"p1", "p2", "p3", "p4"}
    milk = offers["p1"]
    assert milk.name == "Молоко 2,5% 900г" and milk.pack_qty == 900 and milk.discount_pct == 19.6
    assert milk.url == "https://silpo.ua/product/moloko-25" and milk.store_id == "b1" and "lactose" in milk.tags
    assert offers["p2"].price == 459 and offers["p2"].old_price == 639      # альтернативні назви полів
    assert offers["p3"].old_price is None and offers["p3"].weighted           # стара ціна = нова → не акція
    assert offers["p4"].promo.startswith("2 шт по 25.0 ₴")
    products_call = next(a for n, a in call.calls if n == "silpo_get_products")
    assert products_call["deliveryType"] == "DeliveryHome" and products_call["inStock"] is True


def test_profile_hints_and_end_to_end():
    call = FakeMCP()
    hints = run(silpo.profile_hints(call))
    assert hints["diets"] == ["no_gluten", "vegetarian"] and hints["household"] == 3
    offers = run(silpo.fetch_offers(call, {"id": "b1", "title": "Франка"}))
    p = Profile(regulars=["кава зернова"], min_discount=10)
    sl = planner.build(matcher.rank(offers, p)[0], p)
    assert sl.regulars_covered == ["кава зернова"] and "silpo · Франка" in sl.groups


def test_nearest_by_distance():
    branches = [{"id": "far", "title": "Львів", "lat": 49.84, "lon": 24.03},
                {"id": "near", "title": "Київ, Франка", "lat": 50.44, "lon": 30.51},
                {"id": "nocoords", "title": "Без координат", "lat": None, "lon": None}]
    got = silpo.nearest(branches, 50.45, 30.52, n=2)
    assert [b["id"] for b in got] == ["near", "far"] and got[0]["km"] < 2 and 460 < got[1]["km"] < 480
