"""Меню вечерь на тиждень з акцій магазинів поруч.

Рецепти описані через «інгредієнти» з коренями слів, за якими їх знаходимо серед акційних товарів.
Підбір страв, кількість упаковок, вартість і економію рахує код (без LLM).
Інгредієнти, яких немає в акціях, рахуємо за орієнтовною ціною й позначаємо це в результаті.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .catalog import TAG_LABELS
from .models import DIETS, Offer, Profile

# ключ: (назва, одиниця g|ml|pcs, стандартна упаковка, орієнтовна ціна за кг|л|шт, швидкопсувне,
#        теги, варіанти назви — кожен список коренів має весь знайтися в назві товару, корені-винятки)
INGREDIENTS: dict[str, tuple] = {
    "chicken_fillet": ("Філе куряче", "g", 1000, 220, True, {"meat"}, [["філе", "кур"], ["філе", "курч"]], ["копч", "варен"]),
    "chicken_thigh": ("Стегно куряче", "g", 1000, 160, True, {"meat"}, [["стегн", "кур"], ["стегн", "курч"], ["гомілк", "кур"]], ["копч"]),
    "minced": ("Фарш", "g", 500, 210, True, {"meat", "pork"}, [["фарш"]], ["риб"]),
    "pork": ("Свинина", "g", 1000, 230, True, {"meat", "pork"}, [["свинин"]], ["копч", "сало", "паштет"]),
    "liver": ("Печінка куряча", "g", 500, 130, True, {"meat"}, [["печінк"]], ["паштет"]),
    "fish": ("Філе риби (хек, минтай)", "g", 800, 280, True, {"fish"}, [["хек"], ["минта"], ["тріск"], ["філе", "риб"]], ["копч", "солон", "консерв"]),
    "sausages": ("Сосиски", "g", 400, 250, True, {"meat", "pork"}, [["сосиск"]], []),
    "eggs": ("Яйця", "pcs", 10, 6.5, True, {"eggs"}, [["яйц"]], ["шоколад", "перепел"]),
    "milk": ("Молоко", "ml", 900, 48, True, {"dairy", "lactose"}, [["молоко"]], ["шоколад", "згущ", "кокос", "вівсян", "мигдал", "рослин", "сухе", "безлактоз"]),
    "sour_cream": ("Сметана", "g", 300, 140, True, {"dairy", "lactose"}, [["сметан"]], []),
    "cottage": ("Сир кисломолочний", "g", 400, 180, True, {"dairy", "lactose"}, [["сир", "кисломол"], ["творог"]], ["сирок", "глазур"]),
    "cheese": ("Сир твердий", "g", 200, 450, True, {"dairy"}, [["сир", "тверд"], ["гауда"], ["сир", "голланд"], ["моцарел"]], ["плавлен"]),
    "butter": ("Масло вершкове", "g", 200, 420, True, {"dairy", "lactose"}, [["масло", "вершк"]], ["спред"]),
    "potato": ("Картопля", "g", 2000, 25, False, {"vegetables"}, [["картопл"]], ["чипс", "пюре", "фрі", "пластів"]),
    "carrot": ("Морква", "g", 1000, 22, False, {"vegetables"}, [["морк"]], ["корейськ", "сік"]),
    "onion": ("Цибуля", "g", 1000, 22, False, {"vegetables"}, [["цибул"]], ["зелен", "смажен"]),
    "cabbage": ("Капуста білокачанна", "g", 1500, 20, False, {"vegetables"}, [["капуст"]], ["квашен", "морськ", "броколі", "цвітн", "пекін"]),
    "beet": ("Буряк", "g", 1000, 20, False, {"vegetables"}, [["буряк"]], ["сік"]),
    "mushrooms": ("Печериці", "g", 400, 150, True, {"vegetables"}, [["печериц"], ["гриби", "свіж"]], ["мариног", "консерв"]),
    "pepper": ("Перець солодкий", "g", 500, 140, True, {"vegetables"}, [["перець", "солодк"], ["перець", "болгар"]], ["мелен", "горош"]),
    "tomatoes_can": ("Томати в соку", "g", 400, 120, False, {"vegetables"}, [["томат", "соку"], ["томат", "власн"], ["томат", "консерв"]], []),
    "tomato_paste": ("Томатна паста", "g", 140, 190, False, {"vegetables"}, [["томатн", "паст"]], []),
    "rice": ("Рис", "g", 900, 72, False, {"grains"}, [["рис"]], ["рисов", "хлібц", "пудинг"]),
    "buckwheat": ("Гречка", "g", 1000, 55, False, {"grains"}, [["гречк"], ["греча", "круп"]], ["хлібц", "борошн"]),
    "pasta": ("Макарони", "g", 800, 55, False, {"grains", "gluten"}, [["макарон"], ["спагет"], ["ріжк"], ["пене"], ["фузил"]], ["безглютен", "без глютен", "кукурудз", "рисов"]),
    "flour": ("Борошно пшеничне", "g", 1000, 32, False, {"grains", "gluten"}, [["борошн"]], ["рисов", "кукурудз", "гречан"]),
    "lentils": ("Сочевиця", "g", 500, 90, False, {"grains"}, [["сочевиц"]], []),
    "beans": ("Квасоля консервована", "g", 400, 100, False, {"grains"}, [["квасол"]], ["стручк"]),
    "oil": ("Олія соняшникова", "ml", 850, 85, False, set(), [["олія"]], ["оливк", "кунжут", "кокос", "лляна"]),
}
PANTRY = {"oil", "rice", "buckwheat", "pasta", "flour", "lentils", "tomato_paste"}

@dataclass(frozen=True, eq=False)
class Recipe:
    id: str
    title: str
    minutes: int
    likes: frozenset
    protein: str
    emoji: str
    ingredients: dict           # ключ інгредієнта → кількість на 4 порції (г | мл | шт)
    steps: tuple = ()


def _r(id, title, minutes, likes, protein, emoji, ingredients, *steps):
    return Recipe(id, title, minutes, frozenset(likes), protein, emoji, ingredients, steps)


RECIPES: list[Recipe] = [
    _r("thighs", "Курячі стегна з картоплею в духовці", 60, {"курка", "класика", "дітям"}, "курка", "🍗",
       {"chicken_thigh": 1000, "potato": 1000, "carrot": 150, "oil": 40},
       "Наріжте картоплю часточками, моркву кружальцями.",
       "Натріть стегна сіллю й спеціями, перемішайте овочі з олією.",
       "Викладіть усе у форму й запікайте 50 хвилин при 200 °C."),
    _r("plov", "Плов з куркою", 70, {"курка", "класика"}, "курка", "🍛",
       {"chicken_thigh": 1000, "rice": 450, "carrot": 300, "onion": 200, "oil": 80},
       "Обсмажте шматочки курки в казані до скоринки.",
       "Додайте цибулю й моркву соломкою, смажте 10 хвилин.",
       "Засипте промитий рис, залийте гарячою водою на 1,5 см вище.",
       "Тушкуйте під кришкою на малому вогні 30 хвилин."),
    _r("kotlety", "Курячі котлети з гречкою", 45, {"курка", "дітям"}, "курка", "🍽️",
       {"chicken_fillet": 700, "eggs": 1, "onion": 100, "buckwheat": 400, "oil": 40},
       "Відваріть гречку: на склянку крупи дві склянки води, 20 хвилин під кришкою.",
       "Подрібніть філе й цибулю, додайте яйце, посоліть і поперчіть.",
       "Сформуйте котлети й обсмажте по 4–5 хвилин з кожного боку.",
       "Накрийте кришкою й доведіть до готовності ще 8 хвилин."),
    _r("chicken-mush", "Курка з печерицями в сметані та рис", 35, {"курка", "швидко"}, "курка", "🍲",
       {"chicken_fillet": 600, "mushrooms": 400, "sour_cream": 200, "onion": 100, "rice": 300},
       "Поставте варитися рис.",
       "Наріжте філе шматочками й обсмажте 6–7 хвилин, перекладіть у тарілку.",
       "На тій самій сковороді підрум'яньте цибулю й печериці.",
       "Поверніть курку, додайте сметану й тушкуйте 5 хвилин."),
    _r("liver", "Гречка з печінкою та грибами", 40, {"класика"}, "печінка", "🥘",
       {"buckwheat": 400, "liver": 500, "mushrooms": 400, "onion": 200, "sour_cream": 150},
       "Відваріть гречку.",
       "Обсмажте цибулю й гриби 8 хвилин.",
       "Додайте печінку шматочками, смажте 5 хвилин, влийте сметану й прогрійте.",
       "Подавайте з гречкою."),
    _r("flotski", "Макарони по-флотськи", 30, {"дітям", "швидко"}, "фарш", "🍝",
       {"pasta": 800, "minced": 500, "onion": 200, "tomato_paste": 70, "oil": 30},
       "Відваріть макарони.",
       "Обсмажте цибулю, додайте фарш і смажте 10 хвилин, розбиваючи грудки.",
       "Додайте томатну пасту й трохи води, тушкуйте 5 хвилин.",
       "Змішайте з макаронами."),
    _r("casserole", "Запіканка з фаршем і картоплею", 60, {"класика", "дітям"}, "фарш", "🥧",
       {"minced": 500, "potato": 1000, "onion": 150, "cheese": 100, "eggs": 2, "sour_cream": 150},
       "Зваріть картоплю й розімніть з яйцями на пюре.",
       "Обсмажте фарш з цибулею.",
       "Викладіть шарами: половина пюре, фарш, решта пюре.",
       "Змастіть сметаною, посипте сиром і запікайте 25 хвилин при 190 °C."),
    _r("borscht", "Борщ зі свининою", 120, {"класика"}, "свинина", "🥣",
       {"pork": 1000, "beet": 600, "cabbage": 700, "potato": 700, "carrot": 200, "onion": 150, "tomato_paste": 70, "sour_cream": 150},
       "Залийте свинину 3 л води й варіть годину, знімаючи піну.",
       "Додайте картоплю й капусту, варіть 15 хвилин.",
       "Обсмажте цибулю, моркву й буряк з томатною пастою 10 хвилин.",
       "Додайте засмажку в каструлю, варіть 10 хвилин і дайте настоятися.",
       "Подавайте зі сметаною."),
    _r("hake", "Хек з картоплею та морквою в духовці", 40, {"риба"}, "риба", "🐟",
       {"fish": 800, "carrot": 200, "onion": 150, "potato": 800, "oil": 30},
       "Наріжте картоплю часточками, моркву кружальцями, цибулю півкільцями.",
       "Перемішайте овочі з олією й сіллю та викладіть у форму.",
       "Запікайте 20 хвилин при 200 °C.",
       "Покладіть зверху рибу й запікайте ще 15 хвилин."),
    _r("puree", "Пюре з сосисками", 30, {"дітям", "швидко"}, "сосиски", "🌭",
       {"potato": 1200, "milk": 200, "butter": 40, "sausages": 600},
       "Зваріть картоплю в підсоленій воді, 20 хвилин.",
       "Розімніть з гарячим молоком і маслом.",
       "Відваріть або обсмажте сосиски."),
    _r("syrnyky", "Сирники зі сметаною", 30, {"вегетаріанське", "дітям", "швидко"}, "сир", "🥞",
       {"cottage": 800, "eggs": 2, "flour": 120, "sour_cream": 150, "oil": 50},
       "Розімніть сир виделкою з яйцями й дрібкою солі.",
       "Додайте борошно, щоб маса не липла до рук.",
       "Сформуйте сирники й обваляйте в борошні.",
       "Смажте на середньому вогні по 3–4 хвилини з кожного боку."),
    _r("omelette", "Омлет з перцем і сиром", 15, {"вегетаріанське", "швидко", "дітям"}, "яйця", "🍳",
       {"eggs": 8, "pepper": 250, "cheese": 100, "oil": 20},
       "Наріжте перець смужками й обсмажте 3 хвилини.",
       "Збийте яйця з дрібкою солі й вилийте на перець.",
       "Посипте тертим сиром і накрийте кришкою на 5 хвилин."),
    _r("spaghetti", "Спагеті з томатами й сиром", 20, {"вегетаріанське", "швидко", "дітям"}, "сир", "🧀",
       {"pasta": 800, "cheese": 100, "tomatoes_can": 400, "onion": 100, "oil": 20},
       "Відваріть спагеті за інструкцією на пачці.",
       "Обсмажте цибулю, додайте томати й тушкуйте 8 хвилин.",
       "Змішайте спагеті з соусом і посипте сиром."),
    _r("buckwheat-mush", "Гречка з печерицями", 30, {"вегетаріанське", "швидко"}, "гриби", "🍄",
       {"buckwheat": 300, "mushrooms": 400, "onion": 150, "sour_cream": 150, "oil": 30},
       "Відваріть гречку.",
       "Обсмажте цибулю, додайте печериці й готуйте 10 хвилин.",
       "Змішайте з гречкою й подавайте зі сметаною."),
    _r("rice-veg", "Рис з овочами та яйцем", 25, {"вегетаріанське", "швидко"}, "яйця", "🍚",
       {"rice": 400, "eggs": 4, "pepper": 250, "carrot": 150, "onion": 100, "oil": 30},
       "Відваріть рис і дайте йому охолонути.",
       "Обсмажте цибулю, моркву й перець 5 хвилин.",
       "Додайте рис, зробіть заглибину й вбийте яйця.",
       "Перемішайте, коли яйця схопляться."),
    _r("bean-soup", "Суп з квасолею та овочами", 40, {"вегетаріанське", "веганське", "швидко"}, "бобові", "🥕",
       {"beans": 800, "potato": 500, "carrot": 200, "onion": 100, "tomatoes_can": 400},
       "Наріжте картоплю, моркву й цибулю кубиками.",
       "Залийте 1,5 л води й варіть 15 хвилин.",
       "Додайте квасолю й томати, варіть ще 10 хвилин."),
    _r("lentils", "Сочевиця з овочами", 40, {"вегетаріанське", "веганське"}, "бобові", "🫘",
       {"lentils": 400, "carrot": 200, "onion": 150, "tomato_paste": 70, "oil": 30},
       "Обсмажте цибулю й моркву 5 хвилин.",
       "Додайте промиту сочевицю, томатну пасту й 1 л води.",
       "Тушкуйте 25 хвилин, поки сочевиця не стане м'якою."),
    _r("cabbage", "Тушкована капуста з печерицями", 40, {"вегетаріанське", "веганське", "класика"}, "гриби", "🥬",
       {"cabbage": 1000, "mushrooms": 400, "carrot": 200, "onion": 100, "tomato_paste": 70, "oil": 40},
       "Нашаткуйте капусту, натріть моркву.",
       "Обсмажте цибулю й печериці 5 хвилин.",
       "Додайте капусту, моркву й томатну пасту, тушкуйте під кришкою 25 хвилин."),
    _r("ragu", "Овочеве рагу", 45, {"вегетаріанське", "веганське"}, "овочі", "🥦",
       {"potato": 600, "carrot": 200, "onion": 150, "pepper": 250, "cabbage": 500, "tomatoes_can": 400, "oil": 40},
       "Наріжте всі овочі кубиками.",
       "Обсмажте цибулю й моркву, додайте картоплю й капусту.",
       "Додайте перець і томати, тушкуйте під кришкою 25 хвилин."),
]
RECIPE_BY_ID = {r.id: r for r in RECIPES}

DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^\wʼ'-]+", (name or "").lower().replace("’", "'")) if t]


def matches_ingredient(name: str, key: str) -> bool:
    _, _, _, _, _, _, variants, excludes = INGREDIENTS[key]
    low = (name or "").lower()
    if any(e in low for e in excludes):
        return False
    toks = _tokens(name)
    return any(all(any(t.startswith(stem) for t in toks) for stem in var) for var in variants)


def recipe_tags(rec: Recipe) -> set[str]:
    out: set[str] = set()
    for k in rec.ingredients:
        out |= INGREDIENTS[k][5]
    return out


def _per(key: str) -> int:
    return 1 if INGREDIENTS[key][1] == "pcs" else 1000


@dataclass
class Source:
    """Звідки беремо інгредієнт: акційний товар магазину або орієнтовна ціна."""
    key: str
    offer: Offer | None = None

    @property
    def unit_price(self) -> float:   # ₴ за кг | л | шт
        if self.offer:
            up = self.offer.unit_price()
            if up:
                return up[0]
        return float(INGREDIENTS[self.key][3])

    @property
    def regular_unit_price(self) -> float:
        if self.offer and self.offer.old_price:
            up = self.offer.unit_price()
            if up:
                return up[0] * self.offer.old_price / self.offer.price
        return self.unit_price


def source_map(offers: list[Offer]) -> dict[str, Source]:
    """Для кожного інгредієнта — найдешевший за кг/л/шт акційний товар; інакше орієнтовна ціна."""
    best: dict[str, Source] = {}
    for key in INGREDIENTS:
        unit = INGREDIENTS[key][1]
        cands = []
        for o in offers:
            if not matches_ingredient(o.name, key):
                continue
            up = o.unit_price()
            # одиниці мають збігатися: г ↔ кг, мл ↔ л, шт ↔ шт
            if not up or {"g": "кг", "ml": "л", "pcs": "шт"}[unit] != up[1]:
                continue
            cands.append((up[0], o))
        cands.sort(key=lambda x: x[0])
        best[key] = Source(key, cands[0][1] if cands else None)
    return best


@dataclass
class Dish:
    recipe: Recipe
    people: int
    used: float = 0.0                  # вартість того, що реально піде в страву, ₴
    saving: float = 0.0                # скільки з цього зекономили акції, ₴
    why: list[str] = field(default_factory=list)
    reuse: list[str] = field(default_factory=list)
    promo_keys: list[str] = field(default_factory=list)

    # сумісність зі старим кодом і шаблонами
    title = property(lambda self: self.recipe.title)
    minutes = property(lambda self: self.recipe.minutes)
    emoji = property(lambda self: self.recipe.emoji)
    protein = property(lambda self: self.recipe.protein)
    id = property(lambda self: self.recipe.id)

    @property
    def ingredients(self) -> dict[str, float]:
        return {k: q * self.people / 4 for k, q in self.recipe.ingredients.items()}

    @property
    def per_portion(self) -> float:
        return round(self.used / self.people, 2)


@dataclass
class Line:
    key: str
    name: str
    need: float
    unit: str
    packs: int
    pack: float
    cost: float
    regular: float
    source: Source
    left: float
    days: list[str] = field(default_factory=list)
    pantry: bool = False
    buy: bool = True                   # для запасів: людина сказала, що вдома закінчилось

    @property
    def is_promo(self) -> bool:
        return self.source.offer is not None


@dataclass
class Menu:
    dishes: list[Dish]
    lines: list[Line]
    people: int

    @property
    def cart(self) -> list[Line]:
        return [l for l in self.lines if l.buy]

    @property
    def total(self) -> float:          # до каси
        return round(sum(l.cost for l in self.cart), 2)

    @property
    def regular(self) -> float:
        return round(sum(l.regular for l in self.cart), 2)

    @property
    def saving(self) -> float:
        return round(self.regular - self.total, 2)

    @property
    def used_total(self) -> float:     # вартість з'їденого, без залишків упаковок
        return round(sum(d.used for d in self.dishes), 2)

    @property
    def per_portion(self) -> float:
        n = self.people * len(self.dishes)
        return round(self.used_total / n, 2) if n else 0.0

    @property
    def per_dinner(self) -> float:
        return round(self.used_total / len(self.dishes), 2) if self.dishes else 0.0

    @property
    def promo_lines(self) -> list[Line]:
        return [l for l in self.cart if l.is_promo]

    @property
    def other_lines(self) -> list[Line]:
        return [l for l in self.cart if not l.is_promo]

    @property
    def pantry_lines(self) -> list[Line]:
        return [l for l in self.lines if l.pantry]

    @property
    def ids(self) -> list[str]:
        return [d.id for d in self.dishes]


def allowed(rec: Recipe, profile: Profile, max_minutes: int) -> bool:
    return rec.minutes <= max_minutes and not (recipe_tags(rec) & profile.excluded_tags())


def _cost(key: str, need: float, src: Source) -> tuple[int, float, float, float, float]:
    """(упаковок, обсяг упаковки, вартість, вартість без акції, залишок) для потреби need у г/мл/шт."""
    per = _per(key)
    o = src.offer
    if o and o.weighted:  # вагове: купуємо скільки треба
        return 1, need, round(need / per * src.unit_price, 2), round(need / per * src.regular_unit_price, 2), 0.0
    pack = (o.pack_qty if o and o.pack_qty else INGREDIENTS[key][2])
    packs = max(1, math.ceil(need / pack - 1e-9))
    price_pack = (o.price if o else pack / per * src.unit_price)
    regular_pack = (o.old_price or o.price) if o else price_pack
    return packs, pack, round(packs * price_pack, 2), round(packs * regular_pack, 2), packs * pack - need


def basket(dishes: list[Dish], sources: dict[str, Source], people: int, pantry_buy=()) -> list[Line]:
    need: dict[str, float] = {}
    days: dict[str, list[str]] = {}
    for i, d in enumerate(dishes):
        for k, q in d.recipe.ingredients.items():
            need[k] = need.get(k, 0) + q * people / 4
            days.setdefault(k, []).append(DAYS[i % 7])
    lines = []
    for k, q in need.items():
        packs, pack, cost, regular, left = _cost(k, q, sources[k])
        pantry = k in PANTRY
        lines.append(Line(k, INGREDIENTS[k][0], q, INGREDIENTS[k][1], packs, pack, cost, regular, sources[k], left,
                          days[k], pantry, (not pantry) or k in pantry_buy))
    lines.sort(key=lambda l: (not l.is_promo, l.name))
    return lines


class _Ctx:
    """Усе, що потрібно для оцінки страв: джерела цін, вподобання, пул дозволених рецептів."""

    def __init__(self, offers: list[Offer], profile: Profile, max_minutes: int):
        self.sources = source_map(offers)
        self.likes = set(profile.likes)
        self.regular_stems = [w[:-1] if len(w) > 4 else w for r in profile.regulars for w in _tokens(r)]
        self.pool = [r for r in RECIPES if allowed(r, profile, max_minutes)]
        self.people = max(1, profile.household)

    def score(self, r: Recipe, others: list[Recipe]) -> float:
        cost = saving = 0.0
        for k, q in r.ingredients.items():
            src = self.sources[k]
            cost += q / _per(k) * src.regular_unit_price
            saving += q / _per(k) * (src.regular_unit_price - src.unit_price)
        n_promo = sum(1 for k in r.ingredients if self.sources[k].offer)
        fresh = [k for k in r.ingredients if INGREDIENTS[k][4]]
        shared = [k for k in fresh if any(k in o.ingredients for o in others)]
        overlap = len(shared) / len(fresh) if fresh else 0
        pref = len(recipe_tags(r) & self.likes) + 0.5 * len(self._regs(r))
        same = sum(1 for o in others if o.protein == r.protein)
        penalty = 2.0 if same >= 2 else (0.6 if same else 0)
        return 2.0 * pref + 6.0 * (saving / cost if cost else 0) + 0.4 * n_promo + 1.5 * overlap - penalty

    def _regs(self, r: Recipe) -> list[str]:
        return [k for k in r.ingredients if any(st in INGREDIENTS[k][0].lower() for st in self.regular_stems)]

    def dish(self, r: Recipe, earlier: list[Recipe]) -> Dish:
        d = Dish(r, self.people)
        for k, q in d.ingredients.items():
            src = self.sources[k]
            d.used += q / _per(k) * src.unit_price
            d.saving += q / _per(k) * (src.regular_unit_price - src.unit_price)
        d.used, d.saving = round(d.used, 2), round(d.saving, 2)
        d.promo_keys = [k for k in r.ingredients if self.sources[k].offer]
        liked = sorted(recipe_tags(r) & self.likes)
        if liked:
            d.why.append("Вам подобається: " + ", ".join(TAG_LABELS.get(t, t).lower() for t in liked))
        regs = self._regs(r)
        if regs:
            d.why.append("Є з вашого щотижневого: " + ", ".join(INGREDIENTS[k][0].lower() for k in regs))
        d.reuse = [INGREDIENTS[k][0].lower() for k in r.ingredients
                   if INGREDIENTS[k][4] and any(k in e.ingredients for e in earlier)]
        return d

    def menu(self, recipes: list[Recipe], pantry_buy=()) -> Menu:
        dishes = [self.dish(r, recipes[:i]) for i, r in enumerate(recipes)]
        return Menu(dishes, basket(dishes, self.sources, self.people, pantry_buy), self.people)

    def greedy(self, n: int, fixed: list[Recipe] | None = None) -> list[Recipe]:
        chosen = list(fixed or [])
        while len(chosen) < n:
            cands = [r for r in self.pool if r not in chosen]
            if not cands:
                break
            chosen.append(max(cands, key=lambda r: self.score(r, chosen)))
        return chosen


def plan(offers: list[Offer], profile: Profile, dinners: int = 5, max_minutes: int = 60,
         budget: float | None = None, keep: list[str] | None = None, pantry_buy=()) -> Menu:
    """Меню на `dinners` вечерь. Жадібний підбір: вподобання + частка вартості страви в акції +
    доїдання відкритих швидкопсувних упаковок − повтор основного продукту; далі підгонка під бюджет.
    `keep` — збережене меню людини (після замін): його страви лишаємо, якщо вони ще підходять."""
    ctx = _Ctx(offers, profile, max_minutes)
    kept = [RECIPE_BY_ID[i] for i in (keep or []) if i in RECIPE_BY_ID and RECIPE_BY_ID[i] in ctx.pool][:dinners]
    if keep and len(kept) == dinners and len(set(kept)) == dinners:
        chosen = kept
    else:
        chosen = ctx.greedy(dinners)
        if budget:
            chosen = _fit_budget(chosen, ctx, budget)
    return ctx.menu(chosen, pantry_buy)


def swap(offers: list[Offer], profile: Profile, current: list[str], i: int, max_minutes: int = 60,
         skip=()) -> list[str] | None:
    """Замінює i-ту страву на найкращу іншу з пулу (без уже вибраних і нещодавно відхилених).
    Повертає новий список id або None, якщо замінити нема на що."""
    ctx = _Ctx(offers, profile, max_minutes)
    recipes = [RECIPE_BY_ID[x] for x in current if x in RECIPE_BY_ID]
    if not 0 <= i < len(recipes):
        return None
    others = recipes[:i] + recipes[i + 1:]
    cands = [r for r in ctx.pool if r not in recipes and r.id not in skip]
    if not cands:
        cands = [r for r in ctx.pool if r not in recipes]
    if not cands:
        return None
    new = max(cands, key=lambda r: ctx.score(r, others))
    return [r.id for r in recipes[:i] + [new] + recipes[i + 1:]]


def _fit_budget(chosen: list[Recipe], ctx: _Ctx, budget: float) -> list[Recipe]:
    def total(rs):
        return ctx.menu(rs).total
    for _ in range(10):
        cur = total(chosen)
        if cur <= budget:
            return chosen
        best = None
        for i in range(len(chosen)):
            for r in ctx.pool:
                if r in chosen:
                    continue
                trial = chosen[:i] + [r] + chosen[i + 1:]
                t = total(trial)
                if t < cur and (best is None or t < best[0]):
                    best = (t, trial)
        if not best:
            return chosen
        chosen = best[1]
    return chosen


assert all(set(r.ingredients) <= set(INGREDIENTS) for r in RECIPES)
assert len(RECIPE_BY_ID) == len(RECIPES)
assert set(DIETS)  # обмеження з профілю працюють через Profile.excluded_tags()


def unit_label(unit: str) -> str:
    return {"g": "г", "ml": "мл", "pcs": "шт"}[unit]


def fmt_qty(q: float, unit: str) -> str:
    """900 г → «900 г», 1500 г → «1,5 кг», 8.25 шт → «9 шт»."""
    if unit == "pcs":
        return f"{math.ceil(q - 1e-9)} шт"
    big = {"g": "кг", "ml": "л"}[unit]
    if q >= 1000:
        return f"{q / 1000:.2f}".rstrip("0").rstrip(".").replace(".", ",") + f" {big}"
    return f"{int(round(q / 10) * 10) or int(round(q))} {unit_label(unit)}"
