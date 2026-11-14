from productcart import catalog, matcher, planner, render
from productcart.models import Offer, Profile
from productcart.sources import demo, feed


def offer(name, price, old, chain="x", **kw):
    return catalog.enrich(Offer(chain=chain, product_id=name, name=name, price=price, old_price=old, **kw))


def test_parse_pack():
    assert catalog.parse_pack("Молоко 2,5% 900 г") == (900.0, "g")
    assert catalog.parse_pack("Вода 6х0,5л") == (3000.0, "ml")
    assert catalog.parse_pack("Кава зернова 1 кг") == (1000.0, "g")
    assert catalog.parse_pack("Яйця С1 10 шт") == (10.0, "pcs")
    assert catalog.parse_pack("Банани") == (None, None)


def test_tags_and_exceptions():
    assert {"dairy", "lactose"} <= catalog.infer_tags("Молоко 2,5% 900 г")
    assert "lactose" not in catalog.infer_tags("Молоко безлактозне 1,5%")
    assert "dairy" not in catalog.infer_tags("Напій вівсяний 1 л")
    assert "gluten" in catalog.infer_tags("Макарони спагеті 500 г")
    assert "gluten" not in catalog.infer_tags("Макарони без глютену кукурудзяні")
    assert {"meat", "pork"} <= catalog.infer_tags("Свинина ошийок")
    assert "meat" not in catalog.infer_tags("Курага сушена")
    assert "meat" in catalog.infer_tags("Філе куряче охолоджене")
    assert "meat" not in catalog.infer_tags("Яйця курячі С1 10 шт")
    assert "alcohol" not in catalog.infer_tags("Пиво безалкогольне 0,5 л")


def test_unit_price_and_discount():
    o = offer("Молоко 900 г", 36.0, 45.0)
    assert o.discount_pct == 20.0 and o.unit_price() == (40.0, "кг")


def test_diet_and_dislike_filters():
    p = Profile(diets=["no_lactose", "vegetarian"], dislikes=["alcohol"], min_discount=5)
    keep = offer("Напій вівсяний 1 л", 60, 80)
    for name in ("Молоко 2,5% 900 г", "Філе куряче 1 кг", "Скумбрія 1 кг", "Пиво світле 0,5 л"):
        assert matcher.check(offer(name, 50, 70), p) is not None, name
    assert matcher.check(keep, p) is None


def test_regulars_brand_and_threshold():
    p = Profile(regulars=["кава зернова"], brands_like=["Lavazza"], brands_avoid=["Нелюбе"], min_discount=10)
    hit = matcher.score(offer("Кава зернова Lavazza 1 кг", 450, 600), p)
    assert hit.regular == "кава зернова" and any("Lavazza" in r for r in hit.reasons)
    assert matcher.check(offer("Кава мелена Нелюбе 250 г", 100, 150), p) is not None
    assert matcher.check(offer("Цукор 1 кг", 38, 40), p) is not None  # знижка 5% < 10%


def test_planner_covers_regulars_diversity_budget():
    p = Profile(regulars=["молоко", "яйця"], budget_week=150, min_discount=5)
    offers = [offer("Молоко 2,5% 900 г", 35, 42, chain="a"), offer("Молоко 3,2% 870 г", 32, 42, chain="b"),
              offer("Яйця С1 10 шт", 53, 65, chain="a"), offer("Кава 1 кг", 449, 619, chain="b"),
              offer("Банани 1 кг", 55, 70, chain="b")]
    m, _ = matcher.rank(offers, p)
    sl = planner.build(m, p)
    names = [x.offer.name for x in sl.items]
    assert sum(n.startswith("Молоко") for n in names) == 1          # одне молоко, не два
    assert "Яйця С1 10 шт" in names and "Кава 1 кг" not in names    # кава не влізла в бюджет
    assert sl.total <= 150 and sl.regulars_missing == [] and sl.skipped_budget >= 1
    assert "Разом" in render.text(sl)


def test_demo_and_feed(tmp_path):
    assert len(demo.load()) >= 20
    csv_path = tmp_path / "varus.csv"
    csv_path.write_text("chain,product_id,name,price,old_price,valid_to\n"
                        "varus,1,Гречка ядриця 1 кг,44,9,57,9,2026-11-20\n", encoding="utf-8")
    # кома в ціні без лапок ламає CSV — фід має бути з крапкою або в лапках
    csv_path.write_text('chain,product_id,name,price,old_price,valid_to\nvarus,1,Гречка ядриця 1 кг,"44,9",57.9,2026-11-20\n', encoding="utf-8")
    [o] = feed.load(csv_path)
    assert o.price == 44.9 and o.valid_to == "2026-11-20" and "grains" in o.tags and o.source == "feed:varus"


def test_regular_matching_by_name_words():
    p = Profile(regulars=["молоко", "кава зернова", "яйця"], min_discount=0)
    yes = ["Молоко 2,5% 900 г", "Кава зернова Lavazza 1 кг", "Яйця курячі С1 10 шт"]
    no = [("Йогурт грецький 2% 300 г", "Молочні продукти"), ("Молочний шоколад 90 г", None), ("Кава мелена 250 г", None)]
    for n in yes:
        assert matcher.score(offer(n, 10, 12), p).regular, n
    for n, cat in no:
        assert not matcher.score(offer(n, 10, 12, category=cat), p).regular, n
