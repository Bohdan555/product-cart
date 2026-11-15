from productcart import catalog, menu
from productcart.models import Offer, Profile
from productcart.sources import demo


def offer(name, price, old, **kw):
    return catalog.enrich(Offer(chain="silpo", product_id=name, name=name, price=price, old_price=old, **kw))


def test_ingredient_matching():
    assert menu.matches_ingredient("Філе куряче охолоджене 1 кг", "chicken_fillet")
    assert not menu.matches_ingredient("Філе куряче копчене 300 г", "chicken_fillet")
    assert menu.matches_ingredient("Гречка ядриця 1 кг", "buckwheat")
    assert menu.matches_ingredient("Молоко 2,5% 900 г", "milk")
    assert not menu.matches_ingredient("Молоко безлактозне 1,5% 900 г", "milk")
    assert not menu.matches_ingredient("Шоколад молочний 90 г", "milk")
    assert menu.matches_ingredient("Яйця курячі С1 10 шт", "eggs")
    assert not menu.matches_ingredient("Рисові хлібці 100 г", "rice")
    assert menu.matches_ingredient("Олія соняшникова рафінована 850 мл", "oil")
    assert not menu.matches_ingredient("Олія оливкова 500 мл", "oil")


def test_promo_source_and_whole_packs():
    offers = [offer("Гречка ядриця 1 кг", 40, 60), offer("Гречка ядриця 800 г", 40, 50)]
    src = menu.source_map(offers)
    assert src["buckwheat"].offer.name == "Гречка ядриця 1 кг"      # дешевше за кг
    assert src["rice"].offer is None and src["rice"].unit_price == 72  # орієнтовна ціна
    packs, pack, cost, regular, left = menu._cost("buckwheat", 1500, src["buckwheat"])
    assert (packs, cost, regular, left) == (2, 80, 120, 500)


def test_plan_prefers_promo_and_respects_diets():
    offers = [offer("Філе куряче охолоджене 1 кг", 150, 230), offer("Гречка ядриця 1 кг", 40, 60),
              offer("Яйця курячі С1 10 шт", 45, 60)]
    m = menu.plan(offers, Profile(household=2, likes=["meat"]), dinners=3)
    assert len(m.dishes) == 3 and "Курячі котлети з гречкою" in [d.title for d in m.dishes]
    assert any("м'ясо" in w for d in m.dishes for w in d.why)
    assert m.saving > 0 and m.promo_lines and m.other_lines and m.total == round(sum(l.cost for l in m.cart), 2)
    veg = menu.plan(offers, Profile(diets=["vegetarian"]), dinners=5)
    assert veg.dishes and not any(menu.recipe_tags(d.recipe) & {"meat", "fish"} for d in veg.dishes)
    vegan = menu.plan(offers, Profile(diets=["vegan"]), dinners=7)
    assert all(d.protein in {"бобові", "гриби", "овочі"} for d in vegan.dishes)


def test_time_budget_and_diversity():
    offers = demo.load()
    fast = menu.plan(offers, Profile(), dinners=5, max_minutes=30)
    assert fast.dishes and all(d.minutes <= 30 for d in fast.dishes)
    full = menu.plan(offers, Profile(household=4), dinners=7)
    assert len({d.title for d in full.dishes}) == 7
    assert max(sum(d.protein == x for d in full.dishes) for x in {d.protein for d in full.dishes}) <= 3
    cheap = menu.plan(offers, Profile(household=4), dinners=7, budget=full.total * 0.8)
    assert cheap.total < full.total


def test_fmt_qty():
    assert menu.fmt_qty(1500, "g") == "1,5 кг" and menu.fmt_qty(262.5, "g") == "260 г" and menu.fmt_qty(8.25, "pcs") == "9 шт"


def test_honest_totals_pantry_and_swap():
    offers = demo.load()
    p = Profile(household=1)
    m = menu.plan(offers, p, dinners=3)
    pantry = [l for l in m.lines if l.pantry]
    assert pantry and all(not l.buy for l in pantry)                     # запаси не рахуємо в «до каси»
    assert m.total == round(sum(l.cost for l in m.lines if not l.pantry), 2)
    assert m.per_portion < m.total / 3                                   # ціна порції — за з'їденим, не за упаковками
    assert all(d.recipe.steps for d in m.dishes)
    k = pantry[0].key
    m2 = menu.plan(offers, p, dinners=3, keep=m.ids, pantry_buy=[k])
    assert m2.ids == m.ids and m2.total > m.total                        # «закінчилось» → у кошик
    new = menu.swap(offers, p, m.ids, 1)
    assert new and new[0] == m.ids[0] and new[2] == m.ids[2] and new[1] not in m.ids
    again = menu.swap(offers, p, new, 1, skip=[m.ids[1]])
    assert again[1] not in (m.ids[1], new[1])
    assert menu.plan(offers, p, dinners=3, keep=new).ids == new          # збережене меню лишається
    assert menu.plan(offers, p, dinners=3, max_minutes=20, keep=new).ids != new  # але не якщо вже не підходить
