"""Кошик «Сільпо» з меню: додаємо до наявного, нічого не видаляючи; ваговий — у кг."""
import asyncio

import pytest

from productcart.sources import silpo_cart as sc


class CartMCP:
    def __init__(self, existing=None, has_cart=True):
        self.calls = []
        self.has_cart = has_cart
        self.items = dict(existing or {})          # productId → quantity

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if name == "silpo_get_my_shopping_cart":
            return {"cartId": "c1"} if self.has_cart else {"exists": False}
        if name == "silpo_get_shopping_cart_by_id":
            prods = [{"productId": k, "name": f"товар {k}", "quantity": v} for k, v in self.items.items()]
            return {"cart": {"id": "c1", "deliveryType": "SelfPickup", "timeslot": {"start": "s", "end": "e"},
                             "shipments": [{"companyId": "co", "branchId": "b1", "products": prods}],
                             "calculation": {"total": 123.4}}, "checkoutWebLink": "https://silpo.ua/pay/c1"}
        if name == "silpo_find_products_batch":
            assert args["branchId"] == "b1" and args["deliveryType"] == "SelfPickup"
            catalog = {
                "Морква": [{"id": "carrot", "name": "Морква", "price": 25, "weighted": True, "addToBasketStep": 0.1, "stock": 9}],
                "Сметана": [{"id": "sm-big", "name": "Сметана 15% 400г", "price": 60, "displayRatio": "400г", "stock": 3},
                            {"id": "sm-small", "name": "Сметана 15% 200г", "price": 40, "displayRatio": "200г", "stock": 0}],
            }
            return {"queries": [{"query": q, "products": catalog.get(q, [])} for q in args["products"]]}
        if name == "silpo_add_or_update_cart_products":
            assert args["shoppingCartId"] == "c1"
            for p in args["products"]:
                assert p["companyId"] == "co" and p["branchId"] == "b1"
                self.items[p["productId"]] = p["quantity"]
            return {"success": True}
        raise AssertionError(name)


def run(c):
    return asyncio.run(c)


def wants():
    return [sc.Want("eggs", "Яйця C1 10 шт", 8, "шт", product_id="egg10", packs=1),
            sc.Want("eggs2", "Яйця C1 10 шт", 8, "шт", product_id="egg10", packs=1),   # той самий товар — зливаємо
            sc.Want("chicken", "Філе куряче", 350, "г", product_id="fillet", weighted=True),
            sc.Want("carrot", "Морква", 80, "г"),
            sc.Want("smetana", "Сметана", 250, "г"),
            sc.Want("tofu", "Тофу", 200, "г")]


def test_build_adds_to_existing_without_removing():
    mcp = CartMCP(existing={"egg10": 2, "other": 1})
    res = run(sc.build(mcp, wants()))
    assert mcp.items["other"] == 1                         # чуже не чіпаємо
    assert mcp.items["egg10"] == 4                         # 2 вже було + 2 упаковки наших
    assert mcp.items["fillet"] == 0.4                      # 350 г → 0,4 кг (крок 0,1)
    assert mcp.items["carrot"] == 0.1                      # 80 г вагової моркви → 0,1 кг
    assert mcp.items["sm-big"] == 1                        # дрібна сметана не в наявності; 400 г покриває 250 г
    assert [w.key for w in res["missing"]] == ["tofu"]
    assert res["had"] == 2 and res["checkout"] == "https://silpo.ua/pay/c1" and res["total"] == 123.4
    assert not any(n in ("silpo_clear_shopping_cart", "silpo_remove_cart_products") for n, _ in mcp.calls)


def test_no_cart_and_no_way_to_create():
    with pytest.raises(sc.CartError, match="створити його не вдалося"):
        run(sc.build(CartMCP(has_cart=False), wants()))


def test_quantity_for_packs():
    w = sc.Want("milk", "Молоко", 1500, "мл")
    assert sc.quantity_for(w, {"displayRatio": "900мл"}) == 2
    assert sc.quantity_for(w, {"displayRatio": "1шт"}) == 1    # інша одиниця — мінімальний крок


class NoCartMCP(CartMCP):
    def __init__(self):
        super().__init__(has_cart=False)

    async def __call__(self, name, args):
        if name == "silpo_get_my_delivery_addresses":
            self.calls.append((name, args))
            return {"addresses": []}
        if name == "silpo_get_time_slots":
            self.calls.append((name, args))
            assert args["deliveryTypes"] == ["SelfPickup"] and args["branchId"] == "b1"
            return {"slots": [{"available": False, "start": "x", "end": "y"}, {"available": True, "start": "s", "end": "e"}]}
        if name == "silpo_create_shopping_cart":
            self.calls.append((name, args))
            assert args["deliveryType"] == "SelfPickup" and args["timeslot"] == {"start": "s", "end": "e"}
            assert args["latitude"] == 50.44 and args["addressType"] == "house"
            self.has_cart = True
            return {"success": True, "shoppingCartId": "c1"}
        return await super().__call__(name, args)


def test_creates_pickup_cart_when_none():
    mcp = NoCartMCP()
    res = run(sc.build(mcp, wants(), {"id": "b1", "title": "Франка", "lat": 50.44, "lon": 30.51, "city": "Київ",
                                      "address": "вул. Франка, 1"}))
    assert res["created"].startswith("Створили кошик із самовивозом") and res["had"] == 0
    assert mcp.items["egg10"] == 2
