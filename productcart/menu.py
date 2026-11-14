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

# (назва, хвилин, теги вподобань, основний продукт, емодзі, інгредієнти на 4 порції)
RECIPES: list[tuple] = [
    ("Курячі стегна з картоплею в духовці", 60, {"курка", "класика", "дітям"}, "курка", "🍗",
     {"chicken_thigh": 1000, "potato": 1000, "carrot": 150, "oil": 40}),
    ("Плов з куркою", 70, {"курка", "класика"}, "курка", "🍛",
     {"chicken_thigh": 1000, "rice": 450, "carrot": 300, "onion": 200, "oil": 80}),
    ("Курячі котлети з гречкою", 45, {"курка", "дітям"}, "курка", "🍽️",
     {"chicken_fillet": 700, "eggs": 1, "onion": 100, "buckwheat": 400, "oil": 40}),
    ("Куряче філе з печерицями в сметані та рис", 35, {"курка", "швидко"}, "курка", "🍲",
     {"chicken_fillet": 600, "mushrooms": 400, "sour_cream": 200, "onion": 100, "rice": 300}),
    ("Гречка з печінкою та грибами", 40, {"класика"}, "печінка", "🥘",
     {"buckwheat": 400, "liver": 500, "mushrooms": 400, "onion": 200, "sour_cream": 150}),
    ("Макарони по-флотськи", 30, {"дітям", "швидко"}, "фарш", "🍝",
     {"pasta": 800, "minced": 500, "onion": 200, "tomato_paste": 70, "oil": 30}),
    ("Запіканка з фаршем і картоплею", 60, {"класика", "дітям"}, "фарш", "🥧",
     {"minced": 500, "potato": 1000, "onion": 150, "cheese": 100, "eggs": 2, "sour_cream": 150}),
    ("Борщ зі свининою", 120, {"класика"}, "свинина", "🥣",
     {"pork": 1000, "beet": 600, "cabbage": 700, "potato": 700, "carrot": 200, "onion": 150, "tomato_paste": 70, "sour_cream": 150}),
    ("Хек запечений з овочами", 40, {"риба"}, "риба", "🐟",
     {"fish": 800, "carrot": 200, "onion": 150, "potato": 800, "oil": 30}),
    ("Пюре з сосисками", 30, {"дітям", "швидко"}, "сосиски", "🌭",
     {"potato": 1200, "milk": 200, "butter": 40, "sausages": 600}),
    ("Сирники зі сметаною", 30, {"вегетаріанське", "дітям", "швидко"}, "сир", "🥞",
     {"cottage": 800, "eggs": 2, "flour": 120, "sour_cream": 150, "oil": 50}),
    ("Омлет з перцем і сиром", 15, {"вегетаріанське", "швидко", "дітям"}, "яйця", "🍳",
     {"eggs": 8, "pepper": 250, "cheese": 100, "oil": 20}),
    ("Макарони з сиром і томатами", 20, {"вегетаріанське", "швидко", "дітям"}, "сир", "🧀",
     {"pasta": 800, "cheese": 100, "tomatoes_can": 400, "onion": 100, "oil": 20}),
    ("Рис з овочами та яйцем", 25, {"вегетаріанське", "швидко"}, "яйця", "🍚",
     {"rice": 400, "eggs": 4, "pepper": 250, "carrot": 150, "onion": 100, "oil": 30}),
    ("Овочевий суп з квасолею", 40, {"вегетаріанське", "веганське", "швидко"}, "бобові", "🥕",
     {"beans": 800, "potato": 500, "carrot": 200, "onion": 100, "tomatoes_can": 400}),
    ("Сочевиця з овочами", 40, {"вегетаріанське", "веганське"}, "бобові", "🫘",
     {"lentils": 400, "carrot": 200, "onion": 150, "tomato_paste": 70, "oil": 30}),
    ("Тушкована капуста з печерицями", 40, {"вегетаріанське", "веганське", "класика"}, "гриби", "🍄",
     {"cabbage": 1000, "mushrooms": 400, "carrot": 200, "onion": 100, "tomato_paste": 70, "oil": 40}),
    ("Овочеве рагу", 45, {"вегетаріанське", "веганське"}, "овочі", "🥦",
     {"potato": 600, "carrot": 200, "onion": 150, "pepper": 250, "cabbage": 500, "tomatoes_can": 400, "oil": 40}),
]

DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MEATY = {"курка", "печінка", "фарш", "свинина", "риба", "сосиски"}


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^\wʼ'-]+", (name or "").lower().replace("’", "'")) if t]


def matches_ingredient(name: str, key: str) -> bool:
    _, _, _, _, _, _, variants, excludes = INGREDIENTS[key]
    low = (name or "").lower()
    if any(e in low for e in excludes):
        return False
    toks = _tokens(name)
    return any(all(any(t.startswith(stem) for t in toks) for stem in var) for var in variants)


def recipe_tags(rec) -> set[str]:
    out: set[str] = set()
    for k in rec[5]:
        out |= INGREDIENTS[k][5]
    return out


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
            if not up:
                continue
            # одиниці мають збігатися: г ↔ кг, мл ↔ л, шт ↔ шт
            if {"g": "кг", "ml": "л", "pcs": "шт"}[unit] != up[1]:
                continue
            cands.append((up[0], o))
        cands.sort(key=lambda x: x[0])
        best[key] = Source(key, cands[0][1] if cands else None)
    return best


@dataclass
class Dish:
    title: str
    minutes: int
    emoji: str
    protein: str
    ingredients: dict[str, float]
    why: list[str] = field(default_factory=list)
    promo_keys: list[str] = field(default_factory=list)


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

    @property
    def is_promo(self) -> bool:
        return self.source.offer is not None


@dataclass
class Menu:
    dishes: list[Dish]
    lines: list[Line]
    people: int

    @property
    def total(self) -> float:
        return round(sum(l.cost for l in self.lines), 2)

    @property
    def regular(self) -> float:
        return round(sum(l.regular for l in self.lines), 2)

    @property
    def saving(self) -> float:
        return round(self.regular - self.total, 2)

    @property
    def per_portion(self) -> float:
        n = self.people * len(self.dishes)
        return round(self.total / n, 2) if n else 0.0

    @property
    def promo_lines(self) -> list[Line]:
        return [l for l in self.lines if l.is_promo]

    @property
    def other_lines(self) -> list[Line]:
        return [l for l in self.lines if not l.is_promo]


def allowed(rec, profile: Profile, max_minutes: int) -> bool:
    title, minutes, likes, protein, _, ingr = rec
    if minutes > max_minutes:
        return False
    excluded = profile.excluded_tags()
    if recipe_tags(rec) & excluded:
        return False
    return True


def _cost(key: str, need: float, src: Source) -> tuple[int, float, float, float, float]:
    """(упаковок, обсяг упаковки, вартість, вартість без акції, залишок) для потреби need у г/мл/шт."""
    unit = INGREDIENTS[key][1]
    per = 1 if unit == "pcs" else 1000
    o = src.offer
    if o and o.weighted:  # вагове: купуємо скільки треба
        qty = need
        return 1, qty, round(qty / per * src.unit_price, 2), round(qty / per * src.regular_unit_price, 2), 0.0
    pack = (o.pack_qty if o and o.pack_qty else INGREDIENTS[key][2])
    packs = max(1, math.ceil(need / pack - 1e-9))
    price_pack = (o.price if o else pack / per * src.unit_price)
    regular_pack = (o.old_price or o.price) if o else price_pack
    return packs, pack, round(packs * price_pack, 2), round(packs * regular_pack, 2), packs * pack - need


def basket(dishes: list[Dish], sources: dict[str, Source], people: int) -> list[Line]:
    need: dict[str, float] = {}
    for d in dishes:
        for k, q in d.ingredients.items():
            need[k] = need.get(k, 0) + q * people / 4
    lines = []
    for k, q in need.items():
        packs, pack, cost, regular, left = _cost(k, q, sources[k])
        lines.append(Line(k, INGREDIENTS[k][0], q, INGREDIENTS[k][1], packs, pack, cost, regular, sources[k], left))
    lines.sort(key=lambda l: (not l.is_promo, l.name))
    return lines


def plan(offers: list[Offer], profile: Profile, dinners: int = 5, max_minutes: int = 60,
         budget: float | None = None) -> Menu:
    """Жадібний підбір: вподобання + частка вартості страви в акції + доїдання відкритих швидкопсувних упаковок
    − повтор основного продукту. Потім підгонка під бюджет заміною на дешевші підходящі страви."""
    sources = source_map(offers)
    likes = set(profile.likes)
    regular_stems = [w[:-1] if len(w) > 4 else w for r in profile.regulars for w in _tokens(r)]
    pool = [r for r in RECIPES if allowed(r, profile, max_minutes)]
    people = max(1, profile.household)
    chosen: list[Dish] = []
    opened: set[str] = set()
    proteins: dict[str, int] = {}
    for _ in range(dinners):
        best, best_score = None, -1e9
        for r in pool:
            if any(d.title == r[0] for d in chosen):
                continue
            title, minutes, rlikes, protein, emoji, ingr = r
            cost = sum(q / (1 if INGREDIENTS[k][1] == "pcs" else 1000) * sources[k].regular_unit_price for k, q in ingr.items())
            saving = sum(q / (1 if INGREDIENTS[k][1] == "pcs" else 1000) * (sources[k].regular_unit_price - sources[k].unit_price)
                         for k, q in ingr.items())
            promo_share = saving / cost if cost else 0
            n_promo = sum(1 for k in ingr if sources[k].offer)
            fresh = [k for k in ingr if INGREDIENTS[k][4]]
            reuse = [k for k in fresh if k in opened]
            overlap = len(reuse) / len(fresh) if fresh else 0
            liked = sorted(recipe_tags(r) & likes)
            regs = [k for k in ingr if any(st in INGREDIENTS[k][0].lower() for st in regular_stems)]
            pref = len(liked) + 0.5 * len(regs)
            penalty = 2.0 if proteins.get(protein, 0) >= 2 else (0.6 if proteins.get(protein) else 0)
            score = 2.0 * pref + 6.0 * promo_share + 0.4 * n_promo + 1.5 * overlap - penalty
            if score > best_score:
                why = []
                if liked:
                    why.append("вам подобається: " + ", ".join(TAG_LABELS.get(t, t).lower() for t in liked))
                if regs:
                    why.append("є з вашого щотижневого: " + ", ".join(INGREDIENTS[k][0].lower() for k in regs))
                if n_promo:
                    why.append(f"{n_promo} інгр. в акції")
                if reuse:
                    why.append("доїдаємо: " + ", ".join(INGREDIENTS[k][0].lower() for k in reuse))
                best, best_score = (r, why, [k for k in ingr if sources[k].offer]), score
        if not best:
            break
        r, why, promo_keys = best
        chosen.append(Dish(r[0], r[1], r[4], r[3], dict(r[5]), why, promo_keys))
        proteins[r[3]] = proteins.get(r[3], 0) + 1
        opened |= {k for k in r[5] if INGREDIENTS[k][4]}

    if budget:
        chosen = _fit_budget(chosen, pool, sources, people, budget)
    return Menu(chosen, basket(chosen, sources, people), people)


def _fit_budget(chosen, pool, sources, people, budget):
    for _ in range(10):
        total = sum(l.cost for l in basket(chosen, sources, people))
        if total <= budget:
            return chosen
        used = {d.title for d in chosen}
        best = None
        for i in range(len(chosen)):
            for r in pool:
                if r[0] in used:
                    continue
                cand = Dish(r[0], r[1], r[4], r[3], dict(r[5]), ["дешевша заміна під бюджет"],
                            [k for k in r[5] if sources[k].offer])
                trial = chosen[:i] + [cand] + chosen[i + 1:]
                t = sum(l.cost for l in basket(trial, sources, people))
                if t < total and (best is None or t < best[0]):
                    best = (t, trial)
        if not best:
            return chosen
        chosen = best[1]
    return chosen



def diet_ok_note(profile: Profile) -> str | None:
    if not profile.diets:
        return None
    return "Страви підібрано без продуктів, що не підходять за вашими обмеженнями (за назвою інгредієнта)."


assert all(set(r[5]) <= set(INGREDIENTS) for r in RECIPES)
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
