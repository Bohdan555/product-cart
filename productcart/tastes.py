"""Свайп-анкета смаків: продукти, які людина оцінює 😋 / 🥲 / «не знаю».

Кожна картка — це група інгредієнтів довідника (menu.INGREDIENTS). 🥲 = «не їм»: страв з цими продуктами
не пропонуємо; 😋 = «смакує»: такі страви вище в підборі. Порядок — від того, що найсильніше розрізняє людей
(м'ясо, риба, субпродукти), до гарнірів і «нових смаків». Фото — Unsplash за запитом в однаковому стилі.
"""
from __future__ import annotations

# (id, назва на картці, емодзі-запаска, ключі інгредієнтів, запит для фото)
CARDS: list[tuple[str, str, str, tuple[str, ...], str]] = [
    ("chicken", "Курка", "🍗", ("chicken_fillet", "chicken_thigh", "chicken_wings"), "roast chicken drumsticks golden crispy"),
    ("pork", "Свинина", "🥩", ("pork", "bacon"), "grilled pork chops juicy"),
    ("beef", "Яловичина", "🥩", ("beef",), "beef steak sliced medium rare"),
    ("minced", "Котлети й фарш", "🍔", ("minced",), "homemade meatballs tomato sauce"),
    ("liver", "Печінка", "🫀", ("liver",), "pan fried liver onions plate"),
    ("turkey", "Індичка", "🦃", ("turkey",), "roast turkey breast sliced plate herbs"),
    ("fish", "Біла риба", "🐟", ("fish", "mackerel", "herring"), "pan seared white fish fillet lemon"),
    ("salmon", "Червона риба", "🍣", ("salmon",), "salmon fillet dinner plate asparagus"),
    ("seafood", "Морепродукти", "🦐", ("shrimp", "crab_sticks"), "garlic butter shrimp pan"),
    ("sausages", "Сосиски й шинка", "🌭", ("sausages", "ham"), "grilled sausages plate"),
    ("eggs", "Яйця", "🍳", ("eggs",), "sunny side up eggs skillet"),
    ("cheese", "Сир твердий", "🧀", ("cheese", "brynza"), "cheese platter sliced cheese grapes"),
    ("cottage", "Кисломолочний сир", "🥞", ("cottage",), "cottage cheese bowl fresh berries honey"),
    ("mushrooms", "Гриби", "🍄", ("mushrooms",), "fried mushrooms garlic butter pan"),
    ("beans", "Бобові", "🫘", ("beans", "lentils", "chickpeas", "green_beans"), "chili beans bowl served"),
    ("buckwheat", "Гречка", "🌾", ("buckwheat",), "buckwheat with mushrooms bowl"),
    ("rice", "Рис", "🍚", ("rice",), "steamed rice bowl"),
    ("pasta", "Макарони й локшина", "🍝", ("pasta", "noodles"), "spaghetti tomato sauce basil"),
    ("potato", "Картопля", "🥔", ("potato",), "roasted potatoes rosemary crispy"),
    ("cabbage", "Капуста й броколі", "🥦", ("cabbage", "cauliflower", "broccoli"), "roasted broccoli cauliflower baking dish"),
    ("beet", "Буряк", "🥗", ("beet",), "roasted beetroot salad"),
    ("eggplant", "Баклажани й кабачки", "🍆", ("eggplant", "zucchini"), "grilled eggplant slices plate herbs"),
    ("pumpkin", "Гарбуз", "🎃", ("pumpkin",), "roasted butternut squash plate"),
    ("avocado", "Авокадо", "🥑", ("avocado",), "avocado toast"),
    ("grains", "Кіноа, кус-кус, булгур", "🥗", ("quinoa", "couscous", "bulgur"), "couscous salad plate vegetables bright"),
    ("tofu", "Тофу", "🧊", ("tofu",), "crispy tofu stir fry"),
]
BY_ID = {c[0]: c for c in CARDS}


def keys_of(card_ids) -> list[str]:
    """Картки → ключі інгредієнтів (без повторів, у порядку карток)."""
    out: list[str] = []
    for cid in card_ids:
        for k in BY_ID.get(cid, (None, None, None, ()))[3]:
            if k not in out:
                out.append(k)
    return out


def photo_key(cid: str) -> str:
    return "t:" + cid


PHOTO_QUERIES = {photo_key(c[0]): c[4] for c in CARDS}
