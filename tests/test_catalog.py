

def test_fruits_only_when_name_starts_with_fruit():
    from productcart.catalog import infer_tags
    from productcart.web.app import name_visual
    assert infer_tags("Яблука Голден 1кг") == {"fruits"}
    assert "fruits" not in infer_tags("Паштет печінковий з яблуком 250г")
    assert infer_tags("Таблетки для посудомийної машини Лимон 50шт") == {"household"}
    assert name_visual("Освіжувач повітря Яблуко")["cat"] == "Побут"
    assert name_visual("Паштет з яблуком")["cat"] == "М'ясо"
    assert name_visual("Мандарини")["cat"] == "Фрукти"
