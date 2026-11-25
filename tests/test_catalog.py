

def test_fruits_only_when_name_starts_with_fruit():
    from productcart.catalog import infer_tags
    from productcart.web.app import name_visual
    assert infer_tags("Яблука Голден 1кг") == {"fruits"}
    assert "fruits" not in infer_tags("Паштет печінковий з яблуком 250г")
    assert infer_tags("Таблетки для посудомийної машини Лимон 50шт") == {"household"}
    assert name_visual("Освіжувач повітря Яблуко")["cat"] == "Побут"
    assert name_visual("Паштет з яблуком")["cat"] == "М'ясо"
    assert name_visual("Мандарини")["cat"] == "Фрукти"


def test_is_food_hides_household_dishes_and_gadgets():
    from productcart.catalog import is_food
    from productcart.models import Offer

    def o(name, cat=None):
        return Offer(chain="s", store_id="1", product_id="x", name=name, price=10, category=cat)
    for n in ("Ополіскувач для посуду 400 мл", "Термочашка 350 мл", "Сковорода 24 см", "Батарейки AA", "Корм для котів 85 г",
              "Губка кухонна", "Мило рідке"):
        assert not is_food(o(n)), n
    assert not is_food(o("Набір", "Побутова хімія"))
    for n in ("Чай чорний у пакетиках 25 шт", "Салат Олів'є в контейнері 300 г", "Кетчуп 450 г", "Ніжний сир кисломолочний",
              "Філе куряче охолоджене", "Шоколад молочний"):
        assert is_food(o(n)), n
