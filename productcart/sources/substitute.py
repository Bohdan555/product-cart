"""Заміна товару, якого немає в наявності: наскільки кандидат схожий на оригінал (0…1). Без LLM — лише правила.

Що важить (у порядку ваги):
- той самий вид продукту («сир твердий», «молоко», «філе куряче») — головне слово назви оригіналу є в кандидаті;
- спільні ознаки (Гауда, ультрапастеризоване, охолоджене, гречана) — без бренду й чисел;
- жирність / відсоток поруч (2,5% ↔ 3,2% — добре, 2,5% ↔ 15% — погано);
- розмір упаковки поруч і та сама «ваговість».
Кандидат, що змінює форму продукту (корм, соус, чипси, паштет… яких не було в оригіналі), відкидаємо зовсім.
"""
from __future__ import annotations

import re

from ..catalog import parse_pack

# Слова, що роблять інший продукт: «корм з куркою» — не курка, «соус сирний» — не сир.
FORM_WORDS = (
    "корм", "соус", "паштет", "чипс", "сухарик", "снек", "напій", "напо", "сироп", "морозив", "десерт", "пюре",
    "дитяч", "суміш", "приправ", "смак", "ароматиз", "цукерк", "батончик", "печиво", "мармелад", "вафл", "крекер",
    "шампунь", "засіб", "мило", "крем", "гель", "консерв", "мариноване", "маринован", "копчен", "в'ялен",
    "сушен", "заморож", "с/м", "пельмен", "вареник", "салат", "спред", "плавлен", "згущ", "йогурт", "сирок",
    "глазур", "начинк", "ласощ", "для котів", "для собак", "для тварин",
)
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_PCT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_QUOTED = re.compile(r"[«\"“][^»\"”]*[»\"”]")
_UNITS = {"г", "кг", "мл", "л", "шт", "уп", "гр", "x", "×"}
_STOP = {"з", "із", "зі", "та", "і", "й", "в", "у", "для", "на", "без", "по", "ст", "тм", "пет", "пл", "ж/б", "с/б"}
MIN_SCORE = 0.45       # нижче — краще сказати «немає», ніж покласти щось чуже


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[^\wʼ'/-]+", (name or "").lower().replace("’", "'")) if w]


def stem(w: str) -> str:
    return w[:5] if len(w) > 5 else w


def descriptors(name: str) -> list[str]:
    """Значущі слова без бренду, чисел і одиниць: «Сир Пирятин Гауда твердий 45% 300г» → [сир, гауда, твердий].
    Бренд — слово з великої літери не на початку або в лапках; але відомі сорти (Гауда, Моцарела) лишаємо,
    бо вони пишуться з малої в інших назвах — порівнюємо за основою, тож регістр не заважає."""
    raw = _QUOTED.sub(" ", name or "")
    out = []
    for i, w in enumerate(re.split(r"[^\wʼ'/-]+", raw.replace("’", "'"))):
        if not w or any(ch.isdigit() for ch in w):
            continue
        lw = w.lower()
        if lw in _UNITS or lw in _STOP or len(lw) < 3:
            continue
        if i > 0 and w[:1].isupper() and not w.isupper() and lw not in _KNOWN_CAPS:
            continue          # бренд: Пирятин, Яготинське, Біло…
        out.append(lw)
    return out


# Сорти й види, які часто пишуть з великої літери, але це не бренд
_KNOWN_CAPS = {"гауда", "едам", "моцарела", "пармезан", "чеддер", "маасдам", "фета", "брі", "камамбер", "рікота",
               "маскарпоне", "моцарелла", "пармеджано", "реджано", "дор", "блю", "голландський", "російський",
               "королівська", "тигрова", "атлантичний", "норвезький"}


def head(name: str) -> list[str]:
    """Вид продукту — перші 1–2 значущі слова: «Сир твердий Гауда» → [сир, твердий]; «Креветка королівська» → [креветка, королівська]."""
    d = descriptors(name)
    if not d:
        return []
    if len(d) > 1 and (d[0] in _TWO_WORD or d[1].endswith(("ий", "а", "е", "і", "ї"))):
        return d[:2]
    return d[:1]


_TWO_WORD = {"сир", "філе", "крупа", "масло", "олія", "ковбаса", "йогурт", "кава", "чай", "сік", "вода", "фарш",
             "печериці", "гриби", "борошно", "молоко", "сметана", "кефір", "хліб", "сосиски", "оливки", "томати"}


_ADJ_END = ("ий", "ій", "ова", "ева", "ове", "еве", "яче", "аче", "ине", "не", "ні", "ві", "ові", "ська", "цька",
            "ське", "цьке", "ка", "ена", "ене", "ані", "яні", "ані", "ові", "ячі", "ачі")


def is_adj(w: str) -> bool:
    """Грубо: прикметник (куряче, твердий, королівська), а не сам продукт (сир, філе, креветка)."""
    return len(w) > 4 and w.endswith(_ADJ_END) and w not in _NOUNS


_NOUNS = {"креветка", "ковбаса", "сосиска", "котлета", "булка", "сметана", "гречка", "картопля", "морква", "цибуля",
          "капуста", "оливка", "ягоди", "печериці", "вершки", "макарони", "спагеті", "локшина", "сардельки", "тефтелі",
          "яйця", "кабачки", "огірки", "томати", "банани", "яблука", "лимони", "чипси", "сосиски", "оливки", "маслини"}


# Різновиди, що міняють продукт усередині виду: зелена цибуля ≠ ріпчаста, кисломолочний сир ≠ твердий
TYPE_MARKERS = ("кисломолоч", "зелен", "солон", "варен", "гостр", "чилі", "ліверн", "кров'ян", "знежир", "безлактоз",
                "без цукру", "дієтич", "фарширов", "консервован")


def _markers(name: str) -> set[str]:
    low = (name or "").lower()
    return {m for m in TYPE_MARKERS if m in low}


def _pct(name: str) -> float | None:
    m = _PCT.findall(name or "")
    return float(m[-1].replace(",", ".")) if m else None


def _forms(name: str) -> set[str]:
    low = (name or "").lower()
    return {f for f in FORM_WORDS if f in low}


def changes_form(original: str, candidate: str) -> bool:
    return bool(_forms(candidate) - _forms(original))


def _has(stems: set[str], w: str) -> bool:
    s = stem(w)
    return any(x == s or x.startswith(s) or s.startswith(x) for x in stems)


def relevance(original: str, candidate: str, *, orig_weighted: bool | None = None, cand_weighted: bool | None = None) -> float:
    """0…1: наскільки candidate годиться замість original. 0 — точно ні."""
    if not candidate:
        return 0.0
    if _forms(candidate) - _forms(original):
        return 0.0
    cd = descriptors(candidate) or _words(candidate)
    cstems = {stem(w) for w in cd}
    h = head(original) or _words(original)[:1]
    if not h:
        return 0.0
    head_hit = sum(_has(cstems, w) for w in h) / len(h)
    noun = next((w for w in h if not is_adj(w)), h[0])
    if not _has(cstems, noun):
        return 0.0                                   # інший вид продукту: «стегно куряче» замість «куряче філе»
    od = [w for w in descriptors(original) if w not in h]
    desc = sum(_has(cstems, w) for w in od) / len(od) if od else 1.0
    extra = [w for w in cd if not _has({stem(x) for x in descriptors(original)}, w)]
    noise = min(1.0, len(extra) / 4)                 # багато чужих ознак — гірше
    p1, p2 = _pct(original), _pct(candidate)
    fat = 1.0 if p1 is None or p2 is None else max(0.0, 1 - abs(p1 - p2) / max(3.0, p1 * 0.6))
    q1, u1 = parse_pack(original)
    q2, u2 = parse_pack(candidate)
    if q1 and q2 and u1 == u2:
        pack = min(q1, q2) / max(q1, q2)
    else:
        pack = 0.6
    same_w = 1.0 if orig_weighted is None or cand_weighted is None or orig_weighted == cand_weighted else 0.5
    m1, m2 = _markers(original), _markers(candidate)
    kind = (0.6 if m2 - m1 else 1.0) * (0.85 if m1 - m2 else 1.0)   # «сир кисломолочний» ≠ «сир твердий»
    score = kind * (0.40 * head_hit + 0.25 * desc + 0.10 * (1 - noise) + 0.12 * fat + 0.08 * pack + 0.05 * same_w)
    return round(score, 3)


def queries(original: str, generic: str | None = None, alts: tuple = ()) -> list[str]:
    """Черга запитів для заміни, від найточнішого: вид + ознаки без бренду → вид продукту → загальна назва → інші."""
    d = descriptors(original)
    h = head(original)
    out = []
    if len(d) > len(h):
        out.append(" ".join(d[:4]).capitalize())
    if h:
        out.append(" ".join(h).capitalize())
    out += [generic or "", *alts]
    seen, res = set(), []
    for q in out:
        k = q.strip().lower()
        if k and k not in seen:
            seen.add(k)
            res.append(q.strip())
    return res
