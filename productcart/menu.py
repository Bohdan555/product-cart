"""Меню вечерь на тиждень з акцій магазинів поруч.

Рецепти описані через «інгредієнти» з коренями слів, за якими їх знаходимо серед акційних товарів.
Підбір страв, кількість упаковок, вартість і економію рахує код (без LLM).
Інгредієнти, яких немає в акціях, рахуємо за орієнтовною ціною й позначаємо це в результаті.
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta
import re
from dataclasses import dataclass, field, replace

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
    "zucchini": ("Кабачок", "g", 1000, 45, True, {"vegetables"}, [["кабач"], ["цукін"]], ["ікра", "чипс", "консерв"]),
    "kefir": ("Кефір", "ml", 900, 50, True, {"dairy", "lactose"}, [["кефір"]], ["безлактоз"]),
    "oil": ("Олія соняшникова", "ml", 850, 85, False, set(), [["олія"]], ["оливк", "кунжут", "кокос", "лляна"]),
    # --- додано разом із новими стравами ---
    "turkey": ("Філе індички", "g", 500, 330, True, {"meat"}, [["індич"], ["індик"]], ["копч", "ковбас", "сосиск", "шинк"]),
    "beef": ("Яловичина", "g", 1000, 380, True, {"meat"}, [["яловичин"]], ["копч", "тушонк", "консерв", "фарш"]),
    "ham": ("Шинка", "g", 300, 320, True, {"meat", "pork"}, [["шинк"]], []),
    "tuna": ("Тунець консервований", "g", 185, 420, False, {"fish"}, [["тунець"], ["тунц"]], []),
    "cream": ("Вершки", "ml", 200, 220, True, {"dairy", "lactose"}, [["вершки"]], ["масло", "сир", "морозив", "спред", "суміш"]),
    "brynza": ("Бринза / фета", "g", 250, 300, True, {"dairy"}, [["бринз"], ["фета"], ["сир", "розсол"]], []),
    "broccoli": ("Броколі", "g", 400, 160, True, {"vegetables"}, [["броколі"]], ["суміш"]),
    "cauliflower": ("Цвітна капуста", "g", 800, 90, True, {"vegetables"}, [["капуст", "цвітн"]], ["суміш"]),
    "tomato": ("Помідори", "g", 1000, 90, True, {"vegetables"}, [["помідор"], ["томат", "рожев"], ["томат", "черрі"], ["томат", "слив"]],
               ["сушен", "в'ялен", "соку", "консерв", "паст", "соус", "сік"]),
    "cucumber": ("Огірки", "g", 1000, 80, True, {"vegetables"}, [["огір"]], ["солон", "марин", "консерв"]),
    "eggplant": ("Баклажан", "g", 1000, 80, True, {"vegetables"}, [["баклажан"]], ["ікра", "консерв"]),
    "pumpkin": ("Гарбуз", "g", 1500, 30, False, {"vegetables"}, [["гарбуз"]], ["насін", "олія", "сік", "пюре"]),
    "garlic": ("Часник", "g", 100, 200, False, {"vegetables"}, [["часник"]], ["сушен", "мелен", "соус", "чипс", "гранул"]),
    "greens": ("Зелень (кріп, петрушка)", "g", 50, 400, True, {"vegetables"}, [["кріп"], ["петрушк"], ["зелень"]], ["сушен"]),
    "corn": ("Кукурудза консервована", "g", 340, 110, False, {"vegetables"}, [["кукурудз", "консерв"], ["кукурудз", "цукров"]],
             ["пластів", "борошн", "палич", "попкорн", "круп"]),
    "peas": ("Горошок консервований", "g", 400, 90, False, {"vegetables"}, [["горош", "зелен"]], ["перець"]),
    "apples": ("Яблука", "g", 1000, 40, False, {"fruits"}, [["яблук"]], ["сік", "чипс", "пюре", "сушен", "пиріг", "оцет"]),
    "lavash": ("Лаваш", "g", 250, 120, True, {"grains", "gluten"}, [["лаваш"]], []),
    "bread": ("Хліб", "g", 500, 60, True, {"grains", "gluten"}, [["хліб"], ["батон"]], ["хлібц", "сухар"]),
    "oats": ("Вівсяні пластівці", "g", 500, 70, False, {"grains", "gluten"}, [["вівсян", "пластів"]], ["печиво", "батончик"]),
    "bulgur": ("Булгур", "g", 500, 90, False, {"grains", "gluten"}, [["булгур"]], []),
    "millet": ("Пшоно", "g", 1000, 50, False, {"grains"}, [["пшон"]], []),
    "chickpeas": ("Нут", "g", 500, 90, False, {"grains"}, [["нут"]], ["хумус", "борошн"]),
    "pelmeni": ("Пельмені", "g", 800, 190, False, {"meat", "gluten"}, [["пельмен"]], []),
    "varenyky": ("Вареники з картоплею", "g", 800, 130, False, {"grains", "gluten"}, [["варени", "картопл"]], ["ліниві"]),
    "mayo": ("Майонез", "g", 300, 150, False, {"eggs"}, [["майонез"]], []),
    "soy_sauce": ("Соєвий соус", "ml", 250, 200, False, set(), [["соус", "соєв"]], []),
    # --- сніданки й салати ---
    "banana": ("Банани", "g", 1000, 70, True, {"fruits"}, [["банан"]], ["чипс", "сушен", "пюре", "йогурт", "сік"]),
    "berries": ("Ягоди заморожені", "g", 400, 250, False, {"fruits"}, [["ягід", "замор"], ["ягоди", "замор"], ["полуниц"], ["лохин"], ["малин"], ["чорниц"]],
                ["варення", "джем", "йогурт", "сік", "чай", "цукерк", "сушен"]),
    "avocado": ("Авокадо", "g", 400, 300, True, {"fruits"}, [["авокадо"]], ["олія", "соус"]),
    "yogurt": ("Йогурт натуральний", "g", 400, 160, True, {"dairy", "lactose"}, [["йогурт"]], ["питн", "фрукт", "полуниц", "вишн", "персик", "печиво"]),
    "semolina": ("Манна крупа", "g", 800, 45, False, {"grains", "gluten"}, [["манн"]], []),
    "crab_sticks": ("Крабові палички", "g", 200, 250, True, {"fish"}, [["краб"]], ["чипс", "смак"]),
    "radish": ("Редиска", "g", 300, 120, True, {"vegetables"}, [["редис"]], []),
    "lettuce": ("Листя салату", "g", 150, 400, True, {"vegetables"}, [["салат", "лист"], ["айсберг"], ["руккол"], ["салат", "ромен"], ["шпинат"]],
                ["готов", "заправ", "крабов", "олів"]),
}
PANTRY = {"oil", "rice", "buckwheat", "pasta", "flour", "lentils", "tomato_paste", "garlic", "soy_sauce", "bulgur",
          "millet", "oats", "chickpeas"}

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
    extras: tuple = ()          # власна страва: інгредієнти поза довідником («паприка», «лаваш») — без ціни
    mine: bool = False          # страва, яку записав сам користувач
    meals: frozenset = frozenset({"dinner"})   # коли їдять: dinner (обід/вечеря), breakfast, salad


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
    # --- більше різноманіття ---
    _r("golubtsi", "Ліниві голубці", 60, {"класика"}, "фарш", "🥬",
       {"minced": 500, "rice": 150, "cabbage": 400, "onion": 100, "carrot": 100, "tomato_paste": 50, "sour_cream": 150},
       "Відваріть рис до напівготовності, дрібно нашаткуйте капусту й ошпарте її окропом.",
       "Змішайте фарш, рис, капусту й половину цибулі; посоліть і сформуйте довгасті котлети.",
       "Обсмажте решту цибулі з морквою, додайте томатну пасту, сметану й склянку води.",
       "Викладіть голубці в соус і тушкуйте під кришкою 35 хвилин."),
    _r("deruny", "Деруни зі сметаною", 40, {"класика", "вегетаріанське"}, "овочі", "🥔",
       {"potato": 1000, "eggs": 2, "onion": 100, "flour": 60, "sour_cream": 200, "oil": 80},
       "Натріть картоплю й цибулю на дрібній тертці, злийте зайвий сік.",
       "Додайте яйця, борошно й сіль, перемішайте.",
       "Смажте ложкою на розігрітій олії по 3 хвилини з кожного боку.",
       "Подавайте гарячими зі сметаною."),
    _r("lazy-varenyky", "Ліниві вареники", 30, {"вегетаріанське", "дітям", "швидко"}, "сир", "🥟",
       {"cottage": 500, "eggs": 1, "flour": 120, "sour_cream": 150, "butter": 30},
       "Розімніть сир виделкою з яйцем і дрібкою солі.",
       "Додайте борошно, замісіть м'яке тісто, скачайте ковбаску й наріжте шматочками.",
       "Варіть у підсоленому окропі 3–4 хвилини після спливання.",
       "Подавайте з маслом і сметаною."),
    _r("chicken-soup", "Курячий суп з локшиною", 50, {"класика", "курка", "дітям"}, "курка", "🍜",
       {"chicken_thigh": 600, "potato": 400, "carrot": 150, "onion": 100, "pasta": 150},
       "Залийте курку 2,5 л холодної води, доведіть до кипіння й зніміть піну; варіть 25 хвилин.",
       "Додайте нарізану картоплю, моркву й цибулю, варіть ще 10 хвилин.",
       "Всипте локшину й варіть до готовності, 6–8 хвилин. Посоліть."),
    _r("pork-stew", "Свинина, тушкована з картоплею", 75, {"класика"}, "свинина", "🍖",
       {"pork": 600, "potato": 800, "carrot": 150, "onion": 150, "tomato_paste": 40, "oil": 30},
       "Наріжте свинину шматочками й обсмажте до рум'яності.",
       "Додайте цибулю й моркву, потім томатну пасту.",
       "Додайте картоплю, залийте водою наполовину й тушкуйте під кришкою 45 хвилин."),
    _r("chicken-pasta", "Паста з куркою та печерицями у вершковому соусі", 30, {"курка", "швидко", "дітям"}, "курка", "🍝",
       {"chicken_fillet": 500, "pasta": 400, "milk": 300, "cheese": 100, "mushrooms": 200, "butter": 20},
       "Відваріть пасту до стану аль денте.",
       "Обсмажте нарізане філе й печериці на маслі.",
       "Влийте молоко, додайте тертий сир і прогрійте до загустіння.",
       "Змішайте з пастою."),
    _r("fish-cream", "Риба в сметані з рисом", 35, {"риба"}, "риба", "🐟",
       {"fish": 700, "sour_cream": 200, "onion": 100, "rice": 300},
       "Поставте варитися рис.",
       "Викладіть рибу у форму, зверху цибулю кільцями, посоліть.",
       "Залийте сметаною й запікайте 20–25 хвилин при 190 °C."),
    _r("solyanka", "Тушкована капуста з сосисками", 35, {"дітям", "швидко"}, "сосиски", "🌭",
       {"sausages": 400, "cabbage": 600, "onion": 100, "carrot": 100, "tomato_paste": 50, "oil": 30},
       "Обсмажте цибулю з морквою, додайте нашатковану капусту.",
       "Тушкуйте 15 хвилин, додайте томатну пасту.",
       "Додайте нарізані сосиски й тушкуйте ще 10 хвилин."),
    _r("meatballs", "Тефтелі в томатному соусі з гречкою", 50, {"дітям", "класика"}, "фарш", "🍲",
       {"minced": 500, "rice": 80, "onion": 100, "tomatoes_can": 400, "buckwheat": 300, "eggs": 1},
       "Змішайте фарш, сирий рис, яйце й половину цибулі; сформуйте кульки.",
       "Обсмажте решту цибулі, додайте томати й склянку води.",
       "Покладіть тефтелі в соус і тушкуйте 30 хвилин; тим часом зваріть гречку."),
    _r("zrazy", "Картопляні зрази з грибами", 50, {"вегетаріанське"}, "гриби", "🥔",
       {"potato": 1000, "mushrooms": 300, "onion": 100, "eggs": 1, "flour": 80, "oil": 60},
       "Зваріть картоплю й зробіть пюре, додайте яйце та 2 ложки борошна.",
       "Обсмажте печериці з цибулею для начинки.",
       "Сформуйте коржики з начинкою, обваляйте в борошні й обсмажте до рум'яності."),
    _r("zucchini-stew", "Кабачки, тушковані з овочами", 35, {"вегетаріанське", "веганське", "швидко"}, "овочі", "🥒",
       {"zucchini": 800, "pepper": 200, "carrot": 150, "onion": 100, "tomatoes_can": 400, "oil": 30},
       "Наріжте овочі кубиками.",
       "Обсмажте цибулю з морквою, додайте кабачки й перець.",
       "Додайте томати й тушкуйте 15–20 хвилин."),
    _r("chicken-zucchini", "Курка з кабачками і рисом", 35, {"курка", "швидко"}, "курка", "🍗",
       {"chicken_fillet": 600, "zucchini": 500, "rice": 300, "onion": 100, "oil": 30},
       "Поставте варитися рис.",
       "Обсмажте шматочки філе з цибулею 7 хвилин.",
       "Додайте кабачки й тушкуйте ще 10 хвилин; посоліть."),
    _r("pancakes", "Оладки на кефірі зі сметаною", 30, {"вегетаріанське", "дітям", "швидко"}, "сир", "🥞",
       {"kefir": 500, "eggs": 2, "flour": 300, "oil": 60, "sour_cream": 150},
       "Змішайте кефір, яйця, щіпку солі й соди.",
       "Додайте борошно до густоти сметани.",
       "Смажте ложкою на олії по 2 хвилини з кожного боку; подавайте зі сметаною."),
    _r("liver-puree", "Печінка по-домашньому з пюре", 45, {"класика"}, "печінка", "🍽️",
       {"liver": 500, "onion": 200, "sour_cream": 150, "potato": 900, "milk": 150, "butter": 30},
       "Зваріть картоплю й розімніть з молоком і маслом.",
       "Обсмажте цибулю, додайте печінку й смажте 5 хвилин.",
       "Додайте сметану й тушкуйте ще 5 хвилин."),
    _r("grechanyky", "Гречаники в томатному соусі", 50, {"класика", "дітям"}, "фарш", "🍘",
       {"minced": 400, "buckwheat": 200, "onion": 100, "eggs": 1, "oil": 50, "tomatoes_can": 400},
       "Зваріть гречку й остудіть.",
       "Змішайте з фаршем, яйцем і цибулею; сформуйте котлети й обсмажте.",
       "Залийте томатами й тушкуйте 15 хвилин."),
    _r("chili", "Квасоля з фаршем і перцем", 35, {"швидко"}, "фарш", "🌶️",
       {"beans": 800, "minced": 400, "tomatoes_can": 400, "onion": 100, "pepper": 200, "oil": 20},
       "Обсмажте фарш із цибулею до розсипчастості.",
       "Додайте перець і томати, тушкуйте 10 хвилин.",
       "Додайте квасолю й прогрійте ще 5 хвилин."),
    _r("mushroom-soup", "Крем-суп з печериць", 35, {"вегетаріанське"}, "гриби", "🍄",
       {"mushrooms": 500, "potato": 400, "onion": 100, "milk": 400, "butter": 30},
       "Обсмажте цибулю й печериці на маслі.",
       "Додайте картоплю, залийте водою й варіть 15 хвилин.",
       "Влийте молоко, збийте блендером до кремової текстури, посоліть."),
    _r("eggs-sausage", "Яєчня з сосисками й овочами", 15, {"швидко", "дітям"}, "яйця", "🍳",
       {"eggs": 8, "sausages": 300, "pepper": 150, "tomatoes_can": 200, "oil": 20},
       "Обсмажте нарізані сосиски й перець 5 хвилин.",
       "Додайте томати, прогрійте.",
       "Вбийте яйця й смажте під кришкою 4–5 хвилин."),
]

from .recipes_more import more as _more  # noqa: E402 — ще страви, окремим файлом
RECIPES += _more(_r)
from .recipes_bs import ALSO_BREAKFAST, ALSO_SALAD, breakfasts, salads  # noqa: E402 — сніданки й салати
RECIPES = [replace(r, meals=r.meals | ({"breakfast"} if r.id in ALSO_BREAKFAST else set())
                   | ({"salad"} if r.id in ALSO_SALAD else set())) for r in RECIPES]
RECIPES += [replace(r, meals=frozenset({"breakfast"})) for r in breakfasts(_r)]
RECIPES += [replace(r, meals=frozenset({"salad"})) for r in salads(_r)]
RECIPE_BY_ID = {r.id: r for r in RECIPES}

# ---------- власні страви користувача ----------
MY_PREFIX = "my-"
_PROTEIN = [("chicken_fillet", "курка"), ("chicken_thigh", "курка"), ("minced", "фарш"), ("pork", "свинина"),
            ("liver", "печінка"), ("fish", "риба"), ("sausages", "сосиски"), ("eggs", "яйця"), ("cottage", "сир"),
            ("beans", "бобові"), ("lentils", "бобові"), ("mushrooms", "гриби"), ("turkey", "індичка"),
            ("beef", "яловичина"), ("tuna", "риба"), ("ham", "шинка"), ("chickpeas", "бобові"), ("pelmeni", "фарш"),
            ("brynza", "сир")]
MY_EMOJI = ["🍲", "🥘", "🍝", "🍛", "🥗", "🍗", "🐟", "🥩", "🥞", "🍳", "🥔", "🫕"]


def user_recipe(d: dict) -> Recipe | None:
    """Словник з профілю → Recipe. Невідомі інгредієнти відкидаємо; без жодного відомого — не страва."""
    ings = {k: float(q) for k, q in (d.get("ing") or {}).items() if k in INGREDIENTS and float(q or 0) > 0}
    title = str(d.get("title") or "").strip()[:80]
    if not title or not str(d.get("id", "")).startswith(MY_PREFIX) or not (ings or d.get("extra")):
        return None
    protein = next((p for k, p in _PROTEIN if k in ings), "овочі")
    steps = tuple(x.strip() for x in str(d.get("steps") or "").split("\n") if x.strip())[:12]
    return Recipe(d["id"], title, int(d.get("minutes") or 45), frozenset({"моє"}), protein,
                  d.get("emoji") if d.get("emoji") in MY_EMOJI else "🍲", ings, steps,
                  tuple(str(x)[:40] for x in (d.get("extra") or [])[:15]), True)


def user_recipes(profile: Profile) -> list[Recipe]:
    return [r for r in (user_recipe(d) for d in (getattr(profile, "my_dishes", None) or [])) if r]


DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^\wʼ'-]+", (name or "").lower().replace("’", "'")) if t]


# Інший продукт, хоч і зі словом інгредієнта в назві: «паштет з вершковим маслом» — не масло,
# «оливки фаршировані» — не фарш, «чипси зі смаком сиру» — не сир.
OTHER_PRODUCT = ("паштет", "соус", "чипс", "снек", "сухарик", "фарширов", "зі смаком", "смак ", "ароматиз", "корм",
                 "цукерк", "батончик", "печиво", "сироп", "для котів", "для собак")


def matches_ingredient(name: str, key: str) -> bool:
    _, title, _, _, _, _, variants, excludes = INGREDIENTS[key]
    low = (name or "").lower()
    if any(e in low for e in excludes):
        return False
    if any(w in low and w not in title.lower() for w in OTHER_PRODUCT):
        return False
    toks = _tokens(name)
    return any(all(any(t.startswith(stem) for t in toks) for stem in var) for var in variants)


def recipe_tags(rec: Recipe) -> set[str]:
    out: set[str] = set()
    for k in rec.ingredients:
        out |= INGREDIENTS[k][5]
    return out


_BASES = ("rice", "buckwheat", "pasta", "potato")


def _base(r: "Recipe") -> str | None:
    return next((b for b in _BASES if b in r.ingredients), None)


def _is_soup(r: "Recipe") -> bool:
    return any(w in r.title.lower() for w in ("суп", "борщ"))


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


_UNIT_LABEL = {"g": "кг", "ml": "л", "pcs": "шт"}
_source_cache: dict[int, dict[str, "Source"]] = {}


def source_map(offers: list[Offer]) -> dict[str, Source]:
    """Для кожного інгредієнта — найдешевший за кг/л/шт акційний товар; інакше орієнтовна ціна.
    Назву кожної акції розбираємо один раз і перевіряємо лише акції, де трапляється початок потрібного слова;
    результат для того самого набору акцій тримаємо в пам'яті (меню й «Замінити» рахують його двічі поспіль)."""
    sig = hash(tuple((o.product_id, o.store_id, o.price, o.name) for o in offers))
    hit = _source_cache.get(sig)
    if hit is not None:
        return {k: Source(k, v.offer) for k, v in hit.items()}
    toks = [_tokens(o.name) for o in offers]
    lows = [(o.name or "").lower() for o in offers]
    by_prefix: dict[str, set[int]] = {}
    for i, ts in enumerate(toks):
        for t in ts:
            by_prefix.setdefault(t[:3], set()).add(i)
    best: dict[str, Source] = {}
    for key, ing in INGREDIENTS.items():
        unit, variants, excludes = ing[1], ing[6], ing[7]
        idx: set[int] = set()
        for var in variants:
            idx |= by_prefix.get(var[0][:3], set())
        cands = []
        for i in idx:
            if any(e in lows[i] for e in excludes):
                continue
            ts = toks[i]
            if not any(all(any(t.startswith(stem) for t in ts) for stem in var) for var in variants):
                continue
            o = offers[i]
            up = o.unit_price()
            # одиниці мають збігатися: г ↔ кг, мл ↔ л, шт ↔ шт
            if not up or _UNIT_LABEL[unit] != up[1]:
                continue
            cands.append((up[0], i, o))
        cands.sort(key=lambda x: (x[0], x[1]))
        best[key] = Source(key, cands[0][2] if cands else None)
    if len(_source_cache) > 16:
        _source_cache.clear()
    _source_cache[sig] = best
    return {k: Source(k, v.offer) for k, v in best.items()}


@dataclass
class Dish:
    recipe: Recipe
    people: int
    used: float = 0.0                  # вартість того, що реально піде в страву, ₴
    saving: float = 0.0                # скільки з цього зекономили акції, ₴
    why: list[str] = field(default_factory=list)
    reuse: list[str] = field(default_factory=list)
    promo_keys: list[str] = field(default_factory=list)
    home: list[str] = field(default_factory=list)      # інгредієнти, які вже є вдома (з холодильника)

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
    have: bool = False                 # уже є вдома (з фото холодильника) — не купуємо

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
        return [l for l in self.lines if l.pantry and not l.have]

    @property
    def home_lines(self) -> list[Line]:
        """Що для меню вже є вдома (з фото холодильника)."""
        return [l for l in self.lines if l.have]

    @property
    def ids(self) -> list[str]:
        return [d.id for d in self.dishes]

    @property
    def extras(self) -> list[str]:
        """Інгредієнти власних страв поза довідником — без ціни, окремим блоком."""
        out: list[str] = []
        for d in self.dishes:
            out += [x for x in d.recipe.extras if x.lower() not in {y.lower() for y in out}]
        return out


# ---------- калорійність ----------
# ккал на 100 г / 100 мл; для штучного (яйця) — на 1 шт. Довідкові середні значення (сирі продукти, крупи — сухі).
KCAL = {
    "chicken_fillet": 110, "chicken_thigh": 185, "minced": 260, "pork": 250, "liver": 140, "fish": 80, "sausages": 260,
    "eggs": 70, "milk": 52, "sour_cream": 160, "cottage": 120, "cheese": 360, "butter": 740, "potato": 77, "carrot": 35,
    "onion": 41, "cabbage": 27, "beet": 43, "mushrooms": 22, "pepper": 27, "tomatoes_can": 25, "tomato_paste": 100,
    "rice": 345, "buckwheat": 330, "pasta": 350, "flour": 340, "lentils": 320, "beans": 100, "zucchini": 24, "kefir": 50,
    "oil": 900, "turkey": 115, "beef": 190, "ham": 260, "tuna": 140, "cream": 120, "brynza": 260, "broccoli": 34,
    "cauliflower": 25, "tomato": 20, "cucumber": 15, "eggplant": 24, "pumpkin": 26, "garlic": 150, "greens": 40, "corn": 80,
    "peas": 55, "apples": 50, "lavash": 275, "bread": 250, "oats": 360, "bulgur": 340, "millet": 350, "chickpeas": 360,
    "pelmeni": 270, "varenyky": 160, "mayo": 620, "soy_sauce": 55, "banana": 90, "berries": 45, "avocado": 160, "yogurt": 70,
    "semolina": 330, "crab_sticks": 90, "radish": 16, "lettuce": 15,
}
# Що реально з'їдається: стегна — без кісток (~70%), олія для смаження — приблизно половина лишається на сковорідці
KCAL_YIELD = {"chicken_thigh": 0.7, "oil": 0.6}
_kcal_cache: dict[str, int | None] = {}


def kcal_per_portion(rec: "Recipe") -> int | None:
    """Орієнтовна калорійність однієї порції (рецепт — на 4 порції). Олію для смаження рахуємо всю — тож це верхня межа.
    None — якщо для якогось інгредієнта немає даних."""
    if not rec.mine and rec.id in _kcal_cache:
        return _kcal_cache[rec.id]
    total = 0.0
    for k, q in rec.ingredients.items():
        if k not in KCAL or k not in INGREDIENTS:
            total = None
            break
        total += (q * KCAL[k] if INGREDIENTS[k][1] == "pcs" else q / 100 * KCAL[k]) * KCAL_YIELD.get(k, 1.0)
    val = None if total is None else int(round(total / 4 / 10) * 10)
    if not rec.mine:
        _kcal_cache[rec.id] = val
    return val


# ---------- кухонні прилади ----------
KITCHEN = {"stove": "Плита", "oven": "Духовка", "multicooker": "Мультиварка", "airfryer": "Аерогриль",
           "microwave": "Мікрохвильовка", "blender": "Блендер"}      # назва; іконка — icons.ic(ключ)
# Що страві треба зробити → якими приладами це можна: смажити — плита чи мультиварка; варити й гріти — ще й мікрохвильовка;
# запікати — духовка чи аерогриль; збити — блендер
CAN_DO = {"fry": ("stove", "multicooker"), "boil": ("stove", "multicooker", "microwave"),
          "oven": ("oven", "airfryer"), "blender": ("blender",)}
_NEEDS = {
    "oven": re.compile(r"духовц|запік|запеч|°\s*c|°с", re.I),
    "blender": re.compile(r"блендер", re.I),
    "fry": re.compile(r"смаж|тушк|сковор|підрум|пасеру|обжар|\bспеч|\bпечіть|гарячою олією|на маслі", re.I),
    "boil": re.compile(r"вар|кип|прогрій", re.I),
}
_needs_cache: dict[str, frozenset] = {}


def recipe_needs(rec: Recipe) -> frozenset:
    """Прилади, без яких страву не приготувати — за кроками рецепта (салат чи йогурт — нічого)."""
    hit = _needs_cache.get(rec.id) if not rec.mine else None
    if hit is not None:
        return hit
    text = " ".join(rec.steps)
    need = frozenset(k for k, rx in _NEEDS.items() if rx.search(text))
    if not rec.mine:
        _needs_cache[rec.id] = need
    return need


def can_cook(rec: Recipe, kitchen) -> bool:
    if not kitchen:
        return True
    have = set(kitchen)
    return all(any(t in have for t in CAN_DO[n]) for n in recipe_needs(rec))


def cook_hint(rec: Recipe, kitchen) -> str | None:
    """«у мультиварці» / «в аерогрилі», якщо готуватимемо замінником, бо основного приладу немає."""
    if not kitchen:
        return None
    have, need = set(kitchen), recipe_needs(rec)
    if "oven" in need and "oven" not in have and "airfryer" in have:
        return "в аерогрилі"
    if need & {"fry", "boil"} and "stove" not in have:
        if "multicooker" in have:
            return "у мультиварці"
        if "microwave" in have and "fry" not in need:
            return "у мікрохвильовці"
    return None


def allowed(rec: Recipe, profile: Profile, max_minutes: int) -> bool:
    return (rec.minutes <= max_minutes and not (recipe_tags(rec) & profile.excluded_tags())
            and rec.id not in profile.dish_dislikes and can_cook(rec, getattr(profile, "kitchen", None)))


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


def basket(dishes: list[Dish], sources: dict[str, Source], people: int, pantry_buy=(), have=()) -> list[Line]:
    need: dict[str, float] = {}
    days: dict[str, list[str]] = {}
    for i, d in enumerate(dishes):
        for k, q in d.recipe.ingredients.items():
            need[k] = need.get(k, 0) + q * people / 4
            days.setdefault(k, []).append(DAYS[i % 7])
    lines = []
    for k, q in need.items():
        packs, pack, cost, regular, left = _cost(k, q, sources[k])
        pantry, home = k in PANTRY, k in have
        lines.append(Line(k, INGREDIENTS[k][0], q, INGREDIENTS[k][1], packs, pack, cost, regular, sources[k], left,
                          days[k], pantry, not home and ((not pantry) or k in pantry_buy), home))
    lines.sort(key=lambda l: (not l.is_promo, l.name))
    return lines


class _Ctx:
    """Усе, що потрібно для оцінки страв: джерела цін, вподобання, пул дозволених рецептів."""

    def __init__(self, offers: list[Offer], profile: Profile, max_minutes: int, meal: str | None = "dinner"):
        self.sources = source_map(offers)
        self.likes = set(profile.likes)
        self.dish_likes = set(profile.dish_likes)
        self.avoid = set(profile.menu_prev)   # страви минулого тижня — штраф, щоб меню мінялось
        self.week = date.today().isocalendar()[:2]
        self.regular_stems = [w[:-1] if len(w) > 4 else w for r in profile.regulars for w in _tokens(r)]
        self.have = set(getattr(profile, "fridge", None) or []) & set(INGREDIENTS)   # уже є вдома
        self.mine = user_recipes(profile)
        self.by_id = {**RECIPE_BY_ID, **{r.id: r for r in self.mine}}
        self.dish_likes |= {r.id for r in self.mine}       # свої страви — як улюблені
        # з анкети смаків: основні продукти вподобаних страв — схожі страви теж трохи вище
        self.like_proteins = {self.by_id[x].protein for x in self.dish_likes if x in self.by_id}
        self.pool = [r for r in RECIPES + self.mine if (meal is None or meal in r.meals) and allowed(r, profile, max_minutes)]
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
        pref = self.taste(r) + 0.5 * len(self._regs(r))
        same = sum(1 for o in others if o.protein == r.protein)
        penalty = 2.0 if same >= 2 else (0.6 if same else 0)
        base = _base(r)   # однаковий гарнір (рис, гречка, макарони, картопля) кілька днів поспіль набридає
        if base:
            penalty += 0.9 * sum(1 for o in others if _base(o) == base)
        if _is_soup(r):
            penalty += 1.2 * sum(1 for o in others if _is_soup(o))
        if r.id in self.avoid:
            penalty += 2.5
        home = sum(3.0 if INGREDIENTS[k][4] else 1.0 for k in r.ingredients if k in self.have)   # спершу доїдаємо своє
        return (2.0 * pref + 6.0 * (saving / cost if cost else 0) + 0.4 * n_promo + 1.5 * overlap + home - penalty
                + self.jitter(r))

    def taste(self, r: Recipe) -> float:
        """Наскільки страва під смаки з анкети: улюблені продукти, вподобана страва, схожа на вподобану."""
        return (len(recipe_tags(r) & self.likes) + (2.0 if r.id in self.dish_likes else 0)
                + (0.5 if r.protein in self.like_proteins and r.id not in self.dish_likes else 0))

    def jitter(self, r: Recipe) -> float:
        """Невелика «випадковість», що змінюється щотижня: за тих самих акцій меню не повторюється тиждень у тиждень."""
        h = hashlib.sha1(f"{r.id}:{self.week}".encode()).digest()
        return h[0] / 255 * 1.8

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
        home = [INGREDIENTS[k][0].lower() for k in r.ingredients if k in self.have]
        d.home = home
        if home:
            d.why.append("Є вдома: " + ", ".join(home))
        if r.mine:
            d.why.append("Ваша страва")
        elif r.id in self.dish_likes:
            d.why.append("Ви позначили: смачно")
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
        return Menu(dishes, basket(dishes, self.sources, self.people, pantry_buy, self.have), self.people)

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
    kept = [ctx.by_id[i] for i in (keep or []) if i in ctx.by_id and ctx.by_id[i] in ctx.pool][:dinners]
    if keep and len(kept) == dinners and len(set(kept)) == dinners:
        chosen = kept
    else:
        chosen = ctx.greedy(dinners, fixed=list(dict.fromkeys(kept)))   # що лишилось зі збереженого — доповнюємо
        if budget:
            chosen = _fit_budget(chosen, ctx, budget)
    return ctx.menu(chosen, pantry_buy)


# ---------- словник «що є вдома»: коротка назва, емодзі й слова, якими люди це називають ----------
PANTRY_WORDS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "chicken_fillet": ("Куряче філе", "🍗", ("курка", "курятина", "куряче філе", "філе куряче", "грудка", "куряча грудка", "філе")),
    "chicken_thigh": ("Курячі стегна", "🍗", ("стегна", "стегно", "гомілки", "гомілка", "ніжки", "крильця", "окорочка")),
    "minced": ("Фарш", "🥩", ("фарш", "м'ясний фарш", "мʼясний фарш")),
    "pork": ("Свинина", "🥩", ("свинина", "ошийок", "ребра", "свиняче")),
    "liver": ("Печінка", "🥩", ("печінка", "печінь")),
    "fish": ("Риба", "🐟", ("риба", "хек", "минтай", "тріска", "філе риби")),
    "sausages": ("Сосиски", "🌭", ("сосиски", "сардельки", "ковбаски")),
    "eggs": ("Яйця", "🥚", ("яйця", "яйце", "яєчка", "яєць", "яєчко")),
    "milk": ("Молоко", "🥛", ("молоко",)),
    "kefir": ("Кефір", "🥛", ("кефір",)),
    "sour_cream": ("Сметана", "🥛", ("сметана",)),
    "cottage": ("Кисломолочний сир", "🧀", ("кисломолочний сир", "сир кисломолочний", "творог", "сир домашній")),
    "cheese": ("Твердий сир", "🧀", ("сир", "твердий сир", "сир твердий", "гауда", "моцарела", "пармезан")),
    "butter": ("Вершкове масло", "🧈", ("масло", "вершкове масло", "масло вершкове")),
    "potato": ("Картопля", "🥔", ("картопля", "картошка", "бульба")),
    "carrot": ("Морква", "🥕", ("морква", "морковка")),
    "onion": ("Цибуля", "🧅", ("цибуля", "лук")),
    "cabbage": ("Капуста", "🥬", ("капуста",)),
    "beet": ("Буряк", "🫜", ("буряк", "буряки")),
    "mushrooms": ("Печериці", "🍄", ("печериці", "гриби", "шампіньйони")),
    "pepper": ("Солодкий перець", "🫑", ("перець", "болгарський перець", "солодкий перець", "паприка свіжа")),
    "zucchini": ("Кабачок", "🥒", ("кабачок", "кабачки", "цукіні")),
    "tomatoes_can": ("Томати в соку", "🥫", ("томати в соку", "консервовані томати", "томати консервовані")),
    "tomato_paste": ("Томатна паста", "🥫", ("томатна паста", "паста томатна", "кетчуп")),
    "rice": ("Рис", "🍚", ("рис",)),
    "buckwheat": ("Гречка", "🌾", ("гречка", "гречана крупа")),
    "pasta": ("Макарони", "🍝", ("макарони", "спагеті", "паста", "вермішель", "локшина", "ріжки")),
    "lentils": ("Сочевиця", "🫘", ("сочевиця",)),
    "beans": ("Квасоля", "🫘", ("квасоля",)),
    "flour": ("Борошно", "🌾", ("борошно", "мука")),
    "oil": ("Олія", "🫒", ("олія", "соняшникова олія")),
    "turkey": ("Індичка", "🦃", ("індичка", "індик", "філе індички")),
    "beef": ("Яловичина", "🥩", ("яловичина", "телятина", "говядина")),
    "ham": ("Шинка", "🥓", ("шинка", "бекон")),
    "tuna": ("Тунець", "🐟", ("тунець", "консерва тунця")),
    "cream": ("Вершки", "🥛", ("вершки",)),
    "brynza": ("Бринза", "🧀", ("бринза", "фета", "бринза")),
    "broccoli": ("Броколі", "🥦", ("броколі",)),
    "cauliflower": ("Цвітна капуста", "🥦", ("цвітна капуста",)),
    "tomato": ("Помідори", "🍅", ("помідори", "помідор", "томати", "томат", "свіжі помідори", "черрі", "помідори черрі")),
    "cucumber": ("Огірки", "🥒", ("огірок", "огірки", "огірочки")),
    "eggplant": ("Баклажан", "🍆", ("баклажан", "баклажани", "сині")),
    "pumpkin": ("Гарбуз", "🎃", ("гарбуз",)),
    "garlic": ("Часник", "🧄", ("часник",)),
    "greens": ("Зелень", "🌿", ("зелень", "кріп", "петрушка", "кинза", "базилік")),
    "corn": ("Кукурудза", "🌽", ("кукурудза",)),
    "peas": ("Горошок", "🫛", ("горошок", "зелений горошок")),
    "apples": ("Яблука", "🍎", ("яблуко", "яблука")),
    "lavash": ("Лаваш", "🫓", ("лаваш", "тортилья")),
    "bread": ("Хліб", "🍞", ("хліб", "батон", "булка")),
    "oats": ("Вівсянка", "🥣", ("вівсянка", "вівсяні пластівці", "геркулес")),
    "bulgur": ("Булгур", "🌾", ("булгур",)),
    "millet": ("Пшоно", "🌾", ("пшоно", "пшонка")),
    "chickpeas": ("Нут", "🫘", ("нут",)),
    "pelmeni": ("Пельмені", "🥟", ("пельмені",)),
    "varenyky": ("Вареники", "🥟", ("вареники",)),
    "mayo": ("Майонез", "🫙", ("майонез",)),
    "soy_sauce": ("Соєвий соус", "🫙", ("соєвий соус", "соус соєвий")),
    "banana": ("Банани", "🍌", ("банан", "банани")),
    "berries": ("Ягоди", "🫐", ("ягоди", "заморожені ягоди", "полуниця", "лохина", "малина", "чорниця")),
    "avocado": ("Авокадо", "🥑", ("авокадо",)),
    "yogurt": ("Йогурт", "🥛", ("йогурт", "грецький йогурт")),
    "semolina": ("Манка", "🌾", ("манка", "манна крупа")),
    "crab_sticks": ("Крабові палички", "🦀", ("крабові палички", "крабові")),
    "radish": ("Редиска", "🌱", ("редиска", "редис")),
    "lettuce": ("Листя салату", "🥬", ("салат листовий", "листя салату", "айсберг", "руккола", "шпинат")),
}


# Як шукати продукт у каталозі магазину: прості запити по черзі (перший — найточніший).
# Довгі назви з довідника на кшталт «Філе риби (хек, минтай)» каталог часто не знаходить.
SEARCH_TERMS: dict[str, tuple[str, ...]] = {
    "chicken_fillet": ("філе куряче", "філе курчати", "куряче філе"),
    "chicken_thigh": ("стегно куряче", "стегно курчати", "гомілка куряча"),
    "minced": ("фарш", "фарш свинячо-яловичий", "фарш курячий"),
    "pork": ("свинина", "ошийок свинячий", "свинина лопатка"),
    "liver": ("печінка куряча", "печінка"),
    "fish": ("хек", "минтай", "філе риби", "тріска"),
    "sausages": ("сосиски", "сардельки"),
    "eggs": ("яйця курячі", "яйця"),
    "milk": ("молоко 2,5%", "молоко"),
    "kefir": ("кефір",),
    "sour_cream": ("сметана 15%", "сметана"),
    "cottage": ("сир кисломолочний", "кисломолочний сир"),
    "cheese": ("сир твердий", "сир гауда", "сир"),
    "butter": ("масло вершкове", "масло солодковершкове"),
    "potato": ("картопля",),
    "carrot": ("морква",),
    "onion": ("цибуля ріпчаста", "цибуля"),
    "cabbage": ("капуста білокачанна", "капуста"),
    "beet": ("буряк",),
    "mushrooms": ("печериці", "гриби печериці"),
    "pepper": ("перець солодкий", "перець болгарський"),
    "zucchini": ("кабачок", "цукіні"),
    "tomatoes_can": ("томати в соку", "томати очищені", "томати консервовані"),
    "tomato_paste": ("томатна паста", "паста томатна"),
    "rice": ("рис", "рис довгозернистий"),
    "buckwheat": ("гречка", "крупа гречана"),
    "pasta": ("макарони", "спагеті", "макаронні вироби"),
    "lentils": ("сочевиця",),
    "beans": ("квасоля консервована", "квасоля"),
    "flour": ("борошно пшеничне", "борошно"),
    "oil": ("олія соняшникова", "олія"),
    "turkey": ("філе індички", "індичка"),
    "beef": ("яловичина", "яловичина лопатка"),
    "ham": ("шинка",),
    "tuna": ("тунець консервований", "тунець"),
    "cream": ("вершки 10%", "вершки"),
    "brynza": ("бринза", "сир фета"),
    "broccoli": ("броколі",),
    "cauliflower": ("капуста цвітна",),
    "tomato": ("помідори", "томати рожеві", "томати черрі"),
    "cucumber": ("огірки",),
    "eggplant": ("баклажан",),
    "pumpkin": ("гарбуз",),
    "garlic": ("часник",),
    "greens": ("кріп", "петрушка", "зелень"),
    "corn": ("кукурудза консервована", "кукурудза цукрова"),
    "peas": ("горошок зелений", "горошок консервований"),
    "apples": ("яблука",),
    "lavash": ("лаваш",),
    "bread": ("хліб", "батон"),
    "oats": ("пластівці вівсяні", "вівсянка"),
    "bulgur": ("булгур",),
    "millet": ("пшоно",),
    "chickpeas": ("нут",),
    "pelmeni": ("пельмені",),
    "varenyky": ("вареники з картоплею", "вареники"),
    "mayo": ("майонез",),
    "soy_sauce": ("соус соєвий", "соєвий соус"),
    "banana": ("банани",),
    "berries": ("ягоди заморожені", "суміш ягід заморожена", "полуниця"),
    "avocado": ("авокадо",),
    "yogurt": ("йогурт натуральний", "йогурт грецький"),
    "semolina": ("крупа манна", "манка"),
    "crab_sticks": ("крабові палички",),
    "radish": ("редиска", "редис"),
    "lettuce": ("салат листовий", "салат айсберг", "руккола", "шпинат"),
}


def search_terms(key: str) -> tuple[str, ...]:
    return SEARCH_TERMS.get(key) or ((INGREDIENTS[key][0],) if key in INGREDIENTS else ())


def word_to_key(text: str) -> str | None:
    """«курятина» → chicken_fillet, «картошка» → potato. Точний збіг слова, інакше — збіг початку."""
    t = " ".join((text or "").lower().replace("’", "'").replace("ʼ", "'").split())
    if len(t) < 2:
        return None
    for k, (_, _, words) in PANTRY_WORDS.items():
        if t in words:
            return k
    for k, (_, _, words) in PANTRY_WORDS.items():
        if any(w.startswith(t) or (len(t) > 4 and t.startswith(w)) for w in words):
            return k
    return None


# ---------- термін придатності: скільки зазвичай зберігається в холодильнику після покупки (днів) ----------
# Орієнтовні типові строки для відкритого/свіжого продукту; точну дату людина бачить на упаковці.
SHELF_DAYS = {
    "minced": 1, "liver": 1, "fish": 1, "chicken_fillet": 2, "chicken_thigh": 2, "turkey": 2, "pork": 3, "beef": 3,
    "mushrooms": 4, "greens": 4, "lettuce": 4, "avocado": 4, "bread": 4, "cottage": 4,
    "milk": 5, "kefir": 5, "cream": 5, "ham": 5, "sausages": 5, "crab_sticks": 5, "broccoli": 5, "tomato": 5,
    "cucumber": 5, "banana": 5, "lavash": 5,
    "sour_cream": 7, "yogurt": 7, "pepper": 7, "zucchini": 7, "eggplant": 7, "cauliflower": 7, "radish": 7,
    "brynza": 10, "cheese": 14, "eggs": 21, "butter": 21,
}


def expiring(since: dict[str, str], have, today: date | None = None, within: int = 1) -> list[tuple[str, int]]:
    """Продукти з холодильника, які варто використати за `within` днів: [(ключ, днів лишилось)], найтерміновіші першими.
    0 — сьогодні (або вже прострочено), 1 — завтра."""
    today = today or date.today()
    out = []
    for k in have:
        days, d0 = SHELF_DAYS.get(k), since.get(k)
        if not days or not d0:
            continue
        try:
            left = (date.fromisoformat(d0[:10]) + timedelta(days=days) - today).days
        except ValueError:
            continue
        if left <= within:
            out.append((k, max(0, left)))
    return sorted(out, key=lambda x: (x[1], INGREDIENTS[x[0]][0]))


def when_label(left: int) -> str:
    return "сьогодні" if left <= 0 else "завтра" if left == 1 else f"за {left} дн."


# ---------- «Що приготувати сьогодні»: одна страва з того, що є ----------
ASSUMED = {"oil", "flour", "tomato_paste", "garlic"}   # олія, борошно, томатна паста, часник — майже завжди є вдома


@dataclass
class Option:
    """Варіант на сьогодні: страва, що з неї вже є, що докупити і скільки це коштує."""
    dish: Dish
    have: list[str]
    lines: list[Line]                  # що докупити (з цінами й акціями)

    @property
    def buy_cost(self) -> float:
        return round(sum(l.cost for l in self.lines), 2)

    @property
    def buy_saving(self) -> float:
        return round(sum(l.regular - l.cost for l in self.lines), 2)


def today(offers: list[Offer], profile: Profile, have, max_minutes: int = 60, n: int = 3, need=(),
          meal: str | None = "dinner", soon=()) -> list[Option]:
    """Три страви, для яких найменше докуповувати: спершу кількість бракуючого, далі — скільки свого
    використовуємо, далі — ціна докупки. Вподобання й «не їм» з профілю діють як завжди.
    `need` — з «мабуть, є» (олія, борошно, томатна паста) людина сказала «немає»: їх докуповуємо."""
    have = set(have) & set(INGREDIENTS)
    ctx = _Ctx(offers, profile, max_minutes, meal)
    ctx.have = have
    home = have | (ASSUMED - set(need))
    opts = []
    for r in ctx.pool:
        d = ctx.dish(r, [])
        lines = [l for l in basket([d], ctx.sources, ctx.people, have=home) if l.buy or (l.pantry and l.key not in home)]
        for l in lines:
            l.buy = True
        opts.append(Option(d, [k for k in r.ingredients if k in have], lines))
    # що докупити мінус що вже є: страва, яка бере обидва ваші продукти й просить 3 нових, краща за ту,
    # що бере один і просить 2 (інакше з великим вибором страв те, що є вдома, губиться)
    # смаки з анкети важать як один продукт докупки: улюблену страву покажемо, навіть якщо треба докупити ще щось
    # `soon` — те, що скоро зіпсується: страва, яка його використовує, важить як на 1,5 продукту докупки менше
    soon = set(soon) & have
    opts.sort(key=lambda o: (len(o.lines) - len(o.have) - min(1.5, 0.5 * ctx.taste(o.dish.recipe))
                             - 1.5 * len(soon & set(o.dish.recipe.ingredients)), -len(o.have),
                             -ctx.score(o.dish.recipe, []) * 0.05 + o.buy_cost / 100))
    out, proteins = [], set()
    for o in opts:          # різноманітність: не три страви з курки
        if o.dish.recipe.protein in proteins and len(opts) > n * 2:
            continue
        out.append(o)
        proteins.add(o.dish.recipe.protein)
        if len(out) == n:
            break
    if len(out) < n:        # «ще варіанти»: різні основні продукти вичерпались — добираємо за рейтингом
        out += [o for o in opts if o not in out][:n - len(out)]
    # видимі три (медалі 1/2/3) — за тим, скільки докуповувати: менше покупок → вище місце
    out[:3] = sorted(out[:3], key=lambda o: (len(o.lines), -len(o.have), o.buy_cost))
    return out


def swap(offers: list[Offer], profile: Profile, current: list[str], i: int, max_minutes: int = 60,
         skip=()) -> list[str] | None:
    """Замінює i-ту страву на найкращу іншу з пулу (без уже вибраних і нещодавно відхилених).
    Повертає новий список id або None, якщо замінити нема на що."""
    ctx = _Ctx(offers, profile, max_minutes)
    recipes = [ctx.by_id[x] for x in current if x in ctx.by_id]
    if not 0 <= i < len(recipes):
        return None
    others = recipes[:i] + recipes[i + 1:]
    old = recipes[i]
    # Уже показані цього тижня (skip) не повторюємо, доки не переберемо весь вибір — тоді нове коло.
    cands = [r for r in ctx.pool if r not in recipes and r.id not in skip]
    if not cands:
        cands = [r for r in ctx.pool if r not in recipes]
    if not cands:
        return None
    # «Інша страва» — зазвичай хочеться чогось іншого: спершу з іншим основним продуктом, ніж у заміненої
    diff = [r for r in cands if r.protein != old.protein]
    new = max(diff or cands, key=lambda r: ctx.score(r, others))
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
