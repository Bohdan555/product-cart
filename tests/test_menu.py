from productcart import catalog, menu, planner
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


def test_weekly_menu_changes_and_more_recipes():
    import datetime
    from unittest import mock
    from productcart import menu, planner
    from productcart.models import Profile
    from productcart.sources import demo
    assert len(menu.RECIPES) >= 35
    offers = planner.merge_nearby(demo.load())
    p = Profile(household=3)
    weeks = []
    for wk in range(4):
        with mock.patch("productcart.menu.date") as md:
            md.today.return_value = datetime.date(2026, 11, 16) + datetime.timedelta(weeks=wk)
            m = menu.plan(offers, p, dinners=5, max_minutes=60)
        weeks.append(set(m.ids))
        p.menu_prev = m.ids
    for a, b in zip(weeks, weeks[1:]):
        assert len(a & b) <= 2                     # тиждень у тиждень — щонайменше 3 нові страви
    assert len(set().union(*weeks)) >= 14


def test_source_map_cache_is_safe():
    from productcart import menu, planner
    from productcart.sources import demo
    offers = planner.merge_nearby(demo.load())
    a, b = menu.source_map(offers), menu.source_map(offers)
    assert a is not b and {k: v.offer for k, v in a.items()} == {k: v.offer for k, v in b.items()}
    a["eggs"].offer = None                         # зміна однієї копії не псує кеш
    assert menu.source_map(offers)["eggs"].offer is not None or b["eggs"].offer is None


def test_pantry_words_cover_dictionary_and_synonyms():
    from productcart import menu
    assert set(menu.PANTRY_WORDS) == set(menu.INGREDIENTS)
    assert menu.word_to_key("Курятина") == "chicken_fillet" and menu.word_to_key("картошка") == "potato"
    assert menu.word_to_key("помідори") == "tomato" and menu.word_to_key("томати в соку") == "tomatoes_can" and menu.word_to_key("творог") == "cottage"
    assert menu.word_to_key("сир") == "cheese" and menu.word_to_key("авокадо") == "avocado" and menu.word_to_key("кумкват") is None


def test_other_products_with_ingredient_word_do_not_match():
    from productcart.menu import matches_ingredient as m
    assert not m("Паштет «Онисс» «Одеський» печінковий з вершковим маслом 240г", "butter")
    assert not m("Оливки «Повна Чаша»® зелені фаршировані лососем 290г", "minced")
    assert m("Масло вершкове Селянське 82% 200г", "butter") and m("Фарш свино-яловичий 500г", "minced")


def test_recipe_catalog_is_consistent():
    from productcart.menu import recipe_tags
    from productcart.web import unsplash
    ids = [r.id for r in menu.RECIPES]
    assert len(ids) >= 100 and len(set(ids)) == len(ids)
    for r in menu.RECIPES:
        assert set(r.ingredients) <= set(menu.INGREDIENTS), r.id
        assert r.steps and r.id in unsplash.QUERIES, r.id
        if "вегетаріанське" in r.likes:
            assert not recipe_tags(r) & {"meat", "fish"}, r.id
        if "веганське" in r.likes:
            assert not recipe_tags(r) & {"meat", "fish", "dairy", "eggs"}, r.id


def test_swap_goes_through_whole_pool_before_repeating():
    offs = planner.merge_nearby(demo.load())
    p = Profile(name="x")
    cur = menu.plan(offs, p, dinners=5, max_minutes=p.max_minutes).ids
    skip, seen = [], []
    for _ in range(60):
        new = menu.swap(offs, p, cur, 0, max_minutes=p.max_minutes, skip=skip)
        skip = [cur[0], new[0]] + [x for x in skip if x not in (cur[0], new[0])]
        cur = new
        seen.append(cur[0])
    assert len(set(seen)) == 60                                       # 60 замін — жодного повтору
    prots = [next(r for r in menu.RECIPES if r.id == x).protein for x in seen[:10]]
    assert all(a != b for a, b in zip(prots, prots[1:]))              # щоразу інший основний продукт


def test_today_more_options():
    offs = planner.merge_nearby(demo.load())
    a = menu.today(offs, Profile(name="x"), ["eggs"], 40, n=3)
    b = menu.today(offs, Profile(name="x"), ["eggs"], 40, n=9)
    assert len(b) == 9 and [o.dish.recipe.id for o in b[:3]] == [o.dish.recipe.id for o in a]
    assert len({o.dish.recipe.id for o in b}) == 9


def test_breakfasts_and_salads_kept_out_of_dinner_week():
    offs = planner.merge_nearby(demo.load())
    p = Profile(name="x")
    assert sum("breakfast" in r.meals for r in menu.RECIPES) >= 30 and sum("salad" in r.meals for r in menu.RECIPES) >= 20
    m = menu.plan(offs, p, dinners=7)
    assert all("dinner" in d.recipe.meals for d in m.dishes)
    b = menu.today(offs, p, ["eggs", "milk"], 30, n=6, meal="breakfast")
    s = menu.today(offs, p, ["cucumber", "tomato"], 30, n=6, meal="salad")
    assert len(b) == 6 and all("breakfast" in o.dish.recipe.meals for o in b)
    assert len(s) == 6 and all("salad" in o.dish.recipe.meals for o in s)
    assert any(o.dish.recipe.id.startswith("s-") for o in s)
