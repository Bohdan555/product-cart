"""Кошик «Сільпо» з меню: додаємо до наявного, нічого не видаляючи; ваговий — у кг."""
import asyncio

import pytest

from productcart.sources import silpo_cart as sc


def lookup(catalog: dict, q: str) -> list:
    """Як пошук «Сільпо»: без урахування регістру й за частиною назви."""
    ql = q.lower()
    for k, v in catalog.items():
        if k.lower() == ql:
            return v
    return next((v for k, v in catalog.items() if v and (ql in k.lower() or k.lower() in ql)), [])


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
            prods = [{"id": f"line-{k}", "productId": k, "name": f"товар {k}", "quantity": v} for k, v in self.items.items()]
            return {"cart": {"id": "c1", "deliveryType": "SelfPickup", "timeslot": {"start": "s", "end": "e"},
                             "shipments": [{"companyId": "co", "branchId": "b1", "products": prods}],
                             "calculation": {"total": 123.4}}, "checkoutWebLink": "https://silpo.ua/pay/c1"}
        if name == "silpo_find_products_batch":
            assert args["branchId"] == "b1" and args["deliveryType"] == "SelfPickup"
            catalog = {
                "Яйця C1 10 шт": [{"id": "egg10", "name": "Яйця C1 10 шт", "price": 52.9, "stock": 5}],
                "Філе куряче": [{"id": "fillet", "name": "Філе куряче", "price": 189, "weighted": True, "stock": 4}],
                "Кава": [{"id": "p2", "name": "Кава зернова", "price": 449, "stock": 3}],
                "Морква": [{"id": "carrot", "name": "Морква", "price": 25, "weighted": True, "addToBasketStep": 0.1, "stock": 9}],
                "Сметана": [{"id": "sm-big", "name": "Сметана 15% 400г", "price": 60, "displayRatio": "400г", "stock": 3},
                            {"id": "sm-small", "name": "Сметана 15% 200г", "price": 40, "displayRatio": "200г", "stock": 0}],
            }
            return {"queries": [{"query": q, "products": lookup(catalog, q)} for q in args["products"]]}
        if name == "silpo_add_cart_products":
            assert args["shoppingCartId"] == "c1"
            for p in args["products"]:
                assert p["companyId"] == "co" and p["branchId"] == "b1" and p["productId"] not in self.items
                self.items[p["productId"]] = p["quantity"]
            return {"success": True}
        if name == "silpo_update_cart_products":
            for p in args["products"]:
                pid = p["id"].removeprefix("line-")
                assert pid in self.items
                self.items[pid] = p["quantity"]
            return {"success": True}
        raise AssertionError(name)


def run(c):
    return asyncio.run(c)


def wants():
    # одиниці — як у меню (INGREDIENTS): g / ml / pcs
    return [sc.Want("eggs", "Яйця C1 10 шт", 8, "pcs", product_id="egg10", packs=1),
            sc.Want("eggs2", "Яйця C1 10 шт", 8, "pcs", product_id="egg10", packs=1),   # той самий товар — зливаємо
            sc.Want("chicken", "Філе куряче", 350, "g", product_id="fillet", weighted=True),
            sc.Want("carrot", "Морква", 80, "g"),
            sc.Want("smetana", "Сметана", 250, "g"),
            sc.Want("tofu", "Тофу", 200, "g")]


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
    assert {n for n, _ in mcp.calls} >= {"silpo_add_cart_products", "silpo_update_cart_products"}


def test_no_cart_and_no_way_to_create():
    with pytest.raises(sc.CartError, match="створити його не вдалося"):
        run(sc.build(CartMCP(has_cart=False), wants()))


def test_quantity_for_packs():
    w = sc.Want("milk", "Молоко", 1500, "ml")
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


def test_weighted_uses_grams_from_menu_and_sanity_cap():
    assert sc.quantity_for(sc.Want("pork", "Свинина", 375, "g", product_id="p", weighted=True)) == 0.4
    assert sc.quantity_for(sc.Want("x", "Цибуля", 2, "pcs", product_id="p", weighted=True)) == 0.3
    assert not sc.sane(56.3, True) and sc.sane(0.4, True) and not sc.sane(25, False)


def test_menu_lines_map_to_sane_cart_quantities():
    """Справжні рядки меню на демо-акціях → кількості в розумних межах."""
    from productcart import menu, planner
    from productcart.models import Profile
    from productcart.sources import demo
    offers = planner.merge_nearby(demo.load())
    for o in offers:
        o.chain = "silpo"
    m = menu.plan(offers, Profile(household=4), dinners=7, max_minutes=180)
    for w in sc.wants_from_menu(m.cart):
        if w.product_id:
            q = sc.quantity_for(w)
            assert sc.sane(q, w.weighted), (w, q)


class EditMCP(CartMCP):
    async def __call__(self, name, args):
        if name == "silpo_clear_shopping_cart":
            self.calls.append((name, args))
            assert args["shoppingCartId"] == "c1"
            self.items.clear()
            return {"success": True}
        if name == "silpo_remove_cart_products":
            self.calls.append((name, args))
            for p in args["products"]:
                self.items.pop(p["id"].removeprefix("line-"))
            return {"success": True}
        return await super().__call__(name, args)


def test_set_quantity_and_remove():
    mcp = EditMCP(existing={"meat": 56.3, "eggs": 2})
    cart = run(sc.current_cart(mcp))
    run(sc.set_quantity(mcp, cart, "line-meat", 0.5))
    assert mcp.items["meat"] == 0.5
    run(sc.remove_line(mcp, cart, "line-eggs"))
    assert "eggs" not in mcp.items and mcp.items["meat"] == 0.5
    with pytest.raises(sc.CartError):
        run(sc.set_quantity(mcp, cart, "line-nope", 1))


class OutOfStockMCP(CartMCP):
    async def __call__(self, name, args):
        if name == "silpo_find_products_batch":
            self.calls.append((name, args))
            cat = {"Сметана Галичина 15% 350г": [{"id": "sm-promo", "name": "Сметана Галичина 15% 350г", "price": 49, "stock": 0}],
                   "Сметана": [{"id": "sm-other", "name": "Сметана Простоквашино 20% 350г", "price": 62, "displayRatio": "350г", "stock": 7}],
                   "Тофу Х 300г": []}
            return {"queries": [{"query": q, "products": lookup(cat, q)} for q in args["products"]]}
        return await super().__call__(name, args)


def test_out_of_stock_promo_is_replaced_or_reported():
    mcp = OutOfStockMCP()
    wants = [sc.Want("sour_cream", "Сметана Галичина 15% 350г", 250, "g", product_id="sm-promo", generic="Сметана"),
             sc.Want("tofu", "Тофу Х 300г", 200, "g", product_id="tofu-1")]
    res = run(sc.build(mcp, wants))
    assert "sm-promo" not in mcp.items and mcp.items["sm-other"] == 1          # нема в наявності → замінили
    assert res["replaced"] == [("Сметана Галичина 15% 350г", "Сметана Простоквашино 20% 350г")]
    assert [w.key for w in res["missing"]] == ["tofu"]                        # без заміни → «немає в наявності»


def test_cart_totals_split_products_and_delivery():
    st = sc.parse_cart({"cart": {"id": "c1", "shipments": [{"products": [
        {"id": "l1", "productId": "a", "name": "A", "quantity": 2, "price": 10, "total": 20}]}],
        "calculation": {"total": 119, "totalAfterDiscounts": 119, "productsTotal": 20, "delivery": {"total": 99}}}})
    assert (st.products_total, st.delivery, st.total) == (20, 99, 119)


class FallbackMCP(CartMCP):
    """«хек» у магазині нема, а «минтай» є; на «сир твердий» каталог повертає й плавлений, і гауду."""
    async def __call__(self, name, args):
        if name == "silpo_find_products_batch":
            self.calls.append((name, args))
            cat = {"минтай": [{"id": "pollock", "name": "Минтай філе 1кг", "price": 199, "weighted": True, "stock": 5}],
                   "сир твердий": [{"id": "melted", "name": "Сирок плавлений 90г", "price": 25, "stock": 9},
                                   {"id": "gouda", "name": "Сир твердий Гауда 45% 200г", "price": 89, "displayRatio": "200г", "stock": 4}]}
            return {"queries": [{"query": q, "products": cat.get(q, [])} for q in args["products"]]}
        return await super().__call__(name, args)


def test_fallback_queries_and_matching_product():
    from productcart import menu
    mcp = FallbackMCP()
    t = menu.search_terms
    wants = [sc.Want("fish", t("fish")[0], 800, "g", alts=t("fish")[1:], label="Філе риби"),
             sc.Want("cheese", t("cheese")[0], 150, "g", alts=t("cheese")[1:], label="Сир твердий")]
    res = run(sc.build(mcp, wants))
    assert mcp.items.get("pollock") and mcp.items.get("gouda") == 1 and "melted" not in mcp.items
    assert not res["missing"]
    queried = [q for n, a in mcp.calls if n == "silpo_find_products_batch" for q in a["products"]]
    assert queried[:2] == ["хек", "сир твердий"] and "минтай" in queried     # друге коло — наступний запит


def test_missing_shows_human_name():
    mcp = OutOfStockMCP()
    res = run(sc.build(mcp, [sc.Want("tofu", "тофу", 200, "g", label="Тофу")]))
    assert [w.title for w in res["missing"]] == ["Тофу"]


class StaleSlotMCP(CartMCP):
    """Кошик зі старим вікном: із ним пошук «Сільпо» порожній, зі свіжим — знаходить."""
    def __init__(self, end="2020-01-01T10:00:00Z"):
        super().__init__()
        self.end = end

    async def __call__(self, name, args):
        if name == "silpo_get_shopping_cart_by_id":
            r = await super().__call__(name, args)
            r["cart"]["timeslot"] = {"start": "2020-01-01T08:00:00Z", "end": self.end}
            return r
        if name == "silpo_get_time_slots":
            self.calls.append((name, args))
            return {"slots": [{"available": True, "start": "2099-01-01T08:00:00Z", "end": "2099-01-01T10:00:00Z"}]}
        if name == "silpo_find_products_batch":
            self.calls.append((name, args))
            fresh = args.get("timeslotStart", "").startswith("2099")
            cat = {"Паштет Одеський 240г": [{"id": "pate", "name": "Паштет Одеський 240г", "price": 40, "stock": 3}]}
            return {"queries": [{"query": q, "products": lookup(cat, q) if fresh else []} for q in args["products"]]}
        return await super().__call__(name, args)


def test_expired_cart_slot_searches_with_fresh_slot():
    mcp = StaleSlotMCP()
    res = run(sc.build(mcp, [sc.Want("deal", "Паштет Одеський 240г", 1, "pcs", product_id="pate", packs=1)]))
    assert [p.product_id for p in res["placed"]] == ["pate"] and not res["missing"]
    searches = [a for n, a in mcp.calls if n == "silpo_find_products_batch"]
    assert len(searches) == 1 and searches[0]["timeslotStart"].startswith("2099")   # одразу зі свіжим вікном


def test_empty_search_retries_with_fresh_slot():
    mcp = StaleSlotMCP(end="2099-12-31T10:00:00Z")      # вікно ще не минуло, але пошук порожній
    res = run(sc.build(mcp, [sc.Want("deal", "Паштет Одеський 240г", 1, "pcs", product_id="pate", packs=1)]))
    assert [p.product_id for p in res["placed"]] == ["pate"]
    assert len([1 for n, _ in mcp.calls if n == "silpo_find_products_batch"]) == 2


def test_validation_codes_become_human_text():
    st = sc.parse_cart({"cart": {"id": "c1", "shipments": [{"products": [{"id": "l1", "productId": "cola", "name": "Кола", "quantity": 3}]}],
                                 "calculation": {"validations": [
                                     {"level": "error", "message": "product.offer.stock.max", "context": {"productId": "cola"}},
                                     {"level": "error", "message": "order.weird.thing", "context": {}}]}}})
    assert st.validations[0].startswith("Деяких товарів у магазині менше") and "cola" in st.flagged
    assert all("." not in v.split()[0] for v in st.validations)          # жодних сирих кодів
    assert sc.validation_text("product.offer.stock.zero").startswith("Деяких товарів у магазині менше")


class _Tool:
    def __init__(self, name, schema):
        self.name, self.inputSchema = name, schema


class OldSlotMCP(CartMCP):
    """Кошик із минулим вікном. with_tool — є інструмент зміни вікна; без нього — лише створення нового кошика."""
    def __init__(self, with_tool):
        super().__init__(existing={"egg10": 2})
        self.with_tool, self.slot, self.cid = with_tool, ("2020-01-01T08:00:00Z", "2020-01-01T09:00:00Z"), "c1"
        tools = [_Tool(n, {}) for n in ("silpo_get_my_shopping_cart", "silpo_add_cart_products", "silpo_create_shopping_cart")]
        if with_tool:
            tools.append(_Tool("silpo_update_shopping_cart_timeslot",
                               {"properties": {"shoppingCartId": {}, "timeslot": {}, "deliveryType": {}}}))
        outer = self

        class S:
            async def list_tools(self):
                return type("R", (), {"tools": tools})()
        self.session = S()

    async def __call__(self, name, args):
        self.calls.append((name, args))
        if name == "silpo_get_my_shopping_cart":
            return {"cartId": self.cid}
        if name == "silpo_get_shopping_cart_by_id":
            prods = [{"id": f"line-{k}", "productId": k, "name": k, "quantity": v} for k, v in self.items.items()]
            return {"cart": {"id": self.cid, "deliveryType": "SelfPickup", "timeslot": {"start": self.slot[0], "end": self.slot[1]},
                             "shipments": [{"companyId": "co", "branchId": "b1", "products": prods}], "calculation": {}}}
        if name == "silpo_get_time_slots":
            return {"slots": [{"available": True, "start": "2099-01-01T08:00:00Z", "end": "2099-01-01T09:00:00Z"}]}
        if name == "silpo_update_shopping_cart_timeslot":
            assert args["shoppingCartId"] == "c1" and args["timeslot"]["start"].startswith("2099")
            self.slot = (args["timeslot"]["start"], args["timeslot"]["end"])
            return {"success": True}
        if name == "silpo_get_my_delivery_addresses":
            return {"addresses": []}
        if name == "silpo_create_shopping_cart":
            assert args["branchId"] == "b1" and args["deliveryType"] == "SelfPickup"
            moved, self.items = self.items, {}
            self.moved, self.cid, self.slot = moved, "c2", (args["timeslot"]["start"], args["timeslot"]["end"])
            return {"shoppingCartId": "c2"}
        if name == "silpo_add_cart_products":
            for p in args["products"]:
                self.items[p["productId"]] = p["quantity"]
            return {"success": True}
        raise AssertionError(name)


def test_expired_cart_slot_is_refreshed_with_tool():
    sc._schemas.clear(); sc._logged_tools = False
    mcp = OldSlotMCP(with_tool=True)
    cart = run(sc.current_cart(mcp))
    assert sc.slot_expired(cart)
    fresh = run(sc.ensure_fresh_slot(mcp, cart))
    assert not sc.slot_expired(fresh) and fresh.cart_id == "c1" and fresh.products == {"egg10": 2}
    sc._schemas.clear()


def test_expired_cart_slot_new_cart_keeps_items():
    sc._schemas.clear(); sc._logged_tools = False
    mcp = OldSlotMCP(with_tool=False)
    cart = run(sc.current_cart(mcp))
    fresh = run(sc.ensure_fresh_slot(mcp, cart, {"b1": {"id": "b1", "title": "Франка", "lat": 50.4, "lon": 30.5}}))
    assert fresh.cart_id == "c2" and not sc.slot_expired(fresh) and mcp.items == {"egg10": 2}
    sc._schemas.clear()


def test_update_shopping_cart_args_follow_schema():
    """Як справжній silpo_update_shopping_cart: вимагає address і shipments; зайвих полів не приймає."""
    schema = {"type": "object", "required": ["shoppingCartId", "address", "shipments"], "properties": {
        "shoppingCartId": {"type": "string"},
        "address": {"type": "object", "properties": {"latitude": {}, "longitude": {}, "city": {}, "addressType": {}}},
        "shipments": {"type": "array", "items": {"type": "object", "properties": {
            "branchId": {}, "deliveryType": {}, "timeslot": {"type": "object", "properties": {"start": {}, "end": {}}}}}}}}
    sc._schemas.clear(); sc._logged_tools = False
    mcp = OldSlotMCP(with_tool=False)
    sc._schemas.update({"silpo_update_shopping_cart": schema, "silpo_get_my_shopping_cart": {}, "silpo_create_shopping_cart": {}})
    seen = {}

    async def call(name, args):
        if name == "silpo_update_shopping_cart":
            assert set(args) == {"shoppingCartId", "address", "shipments"} and args["address"]["latitude"] == 50.4
            sh = args["shipments"][0]
            assert sh["branchId"] == "b1" and sh["timeslot"]["start"].startswith("2099") and "products" not in sh
            seen["ok"] = True
            mcp.slot = (sh["timeslot"]["start"], sh["timeslot"]["end"])
            return {"success": True}
        return await mcp(name, args)
    call.session = mcp.session
    cart = run(sc.current_cart(call))
    fresh = run(sc.ensure_fresh_slot(call, cart, {"b1": {"id": "b1", "lat": 50.4, "lon": 30.5, "city": "Київ"}}))
    assert seen.get("ok") and not sc.slot_expired(fresh) and fresh.cart_id == "c1"
    sc._schemas.clear()
