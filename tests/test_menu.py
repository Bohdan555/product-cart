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
    assert m.saving > 0 and m.promo_lines and m.other_lines and m.total == sum(l.cost for l in m.lines)
    veg = menu.plan(offers, Profile(diets=["vegetarian"]), dinners=5)
    assert veg.dishes and not any(menu.recipe_tags(next(r for r in menu.RECIPES if r[0] == d.title)) & {"meat", "fish"}
                                  for d in veg.dishes)
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
