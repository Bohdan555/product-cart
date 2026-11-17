"""Заміна відсутнього товару: найсхожіше, а не просто найдешевше; чуже — не кладемо."""
import asyncio

from productcart.sources import silpo_cart as sc
from productcart.sources.substitute import queries, relevance

from test_silpo_cart import CartMCP, lookup


def best(orig, cands):
    return max(cands, key=lambda c: relevance(orig, c))


def test_same_kind_wins_over_cheaper_other_product():
    o = "Сир Пирятин Гауда твердий нарізаний скибочками 45% 300г"
    assert best(o, ["Сир кисломолочний 5% 350г", "Сирок плавлений 90г", "Соус сирний 200г", "Сир Гауда Комо 45% 250г"]) \
        == "Сир Гауда Комо 45% 250г"
    assert relevance(o, "Сир кисломолочний 5% 350г") < 0.45 and relevance(o, "Соус сирний 200г") == 0


def test_form_changing_products_rejected():
    assert relevance("Куряче філе 100г", "Корм для котів з куркою 85г") == 0
    assert relevance("Куряче філе 100г", "Стегно куряче 1кг") == 0          # інша частина
    assert relevance("Молоко Бурьонка 2,5% 1,5л", "Молоко згущене 8,5% 370г") == 0
    assert relevance("Креветка королівська 100г", "Чипси зі смаком креветки 70г") == 0
    assert relevance("Паштет печінковий з вершковим маслом 240г", "Масло вершкове 82% 200г") == 0


def test_fat_and_pack_closer_is_better():
    o = "Сметана Молокія 15% стакан 500г"
    assert relevance(o, "Сметана 15% 400г") > relevance(o, "Сметана 30% 200г")
    o = "Молоко «Бурьонка» питне ультрапастеризоване 2,5% 1,5л"
    assert relevance(o, "Молоко Галичина ультрапастеризоване 2,5% 900г") > relevance(o, "Молоко Яготинське 3,2% 900мл")


def test_queries_from_precise_to_generic():
    q = queries("Сир Пирятин Гауда твердий 45% 300г", "Сир твердий")
    assert q[0].lower().startswith("сир гауда") and q[-1] == "Сир твердий"
    assert all("Пирятин" not in x for x in q)                                   # без бренду


class ShelfMCP(CartMCP):
    """Акційного Гауди немає; у пошуку — дешевий кисломолочний, плавлений і схожа Гауда."""
    async def __call__(self, name, args):
        if name == "silpo_find_products_batch":
            self.calls.append((name, args))
            shelf = [{"id": "kyslo", "name": "Сир кисломолочний 5% 350г", "price": 45, "stock": 9},
                     {"id": "plav", "name": "Сирок плавлений 90г", "price": 15, "stock": 9},
                     {"id": "gouda2", "name": "Сир Гауда Комо 45% 250г", "price": 95, "stock": 4},
                     {"id": "gouda3", "name": "Сир Гауда Клуб Сиру 45% 200г", "price": 99, "stock": 4}]
            cat = {"Сир Пирятин Гауда твердий 45% 300г": [], "сир": shelf, "сир гауда": shelf[2:]}
            return {"queries": [{"query": q, "products": lookup(cat, q) or shelf} for q in args["products"]]}
        return await super().__call__(name, args)


def test_deal_replacement_is_most_similar_then_cheapest():
    mcp = ShelfMCP()
    res = asyncio.run(sc.build(mcp, [sc.Want("pid1", "Сир Пирятин Гауда твердий 45% 300г", 1, "pcs", product_id="pid1", packs=1)]))
    assert [p.product_id for p in res["placed"]] == ["gouda2"]      # Гауда; з двох схожих — дешевша за кг (95/250 < 99/200)
    assert res["replaced"] == [("Сир Пирятин Гауда твердий 45% 300г", "Сир Гауда Комо 45% 250г")]


def test_nothing_similar_reports_missing():
    class Junk(CartMCP):
        async def __call__(self, name, args):
            if name == "silpo_find_products_batch":
                junk = [{"id": "s", "name": "Соус сирний 200г", "price": 30, "stock": 5}]
                return {"queries": [{"query": q, "products": junk} for q in args["products"]]}
            return await super().__call__(name, args)
    res = asyncio.run(sc.build(Junk(), [sc.Want("pid1", "Сир Пирятин Гауда твердий 45% 300г", 1, "pcs", product_id="pid1")]))
    assert not res["placed"] and len(res["missing"]) == 1
