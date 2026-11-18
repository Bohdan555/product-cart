"""Рецепт за посиланням: сторінка → назва, порції, час, інгредієнти (у наш довідник) і кроки.

1) Більшість кулінарних сайтів мають розмітку schema.org/Recipe (JSON-LD) — читаємо її кодом, без ШІ.
2) Якщо розмітки немає — текст сторінки розбирає Claude (той самий ключ, що й для фото холодильника).
Зіставлення з довідником і кількості рахує код. Результат — чернетка: людина перевіряє й зберігає сама.
"""
from __future__ import annotations

import html as htmllib
import ipaddress
import json
import logging
import re
from urllib.parse import urlsplit

import httpx

from .. import menu
from . import logs

log = logging.getLogger("productcart.recipe")

MAX_BYTES = 2_500_000
UA = "Mozilla/5.0 (compatible; KoshykRecipeImport/1.0; +https://my-product-cart-ua.vercel.app)"


class ImportError_(ValueError):
    """Зрозуміла людині причина, чому рецепт не розібрався."""


# ---------- безпечне завантаження сторінки ----------

def safe_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url if url and "." in url else url
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host or "." not in host:
        raise ImportError_("Це не схоже на посилання. Скопіюйте адресу сторінки з рецептом.")
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ImportError_("Це посилання не відкривається.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None               # звичайне доменне ім'я
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved):
        raise ImportError_("Це посилання не відкривається.")
    return url


def _resolves_public(host: str) -> bool:
    """Доменне ім'я не веде на внутрішню адресу (захист від підміни через DNS)."""
    import socket
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True             # не розв'язалось — httpx сам поверне помилку
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def fetch(url: str) -> tuple[str, str]:
    """(html, кінцева адреса). Перенаправлення — вручну, кожен крок перевіряємо."""
    try:
        with httpx.Client(timeout=12, follow_redirects=False,
                          headers={"User-Agent": UA, "Accept-Language": "uk,ru;q=0.8,en;q=0.6"}) as c:
            for _ in range(6):
                url = safe_url(url)
                if not _resolves_public(urlsplit(url).hostname or ""):
                    raise ImportError_("Це посилання не відкривається.")
                with c.stream("GET", url) as r:
                    if r.is_redirect and r.headers.get("location"):
                        url = str(r.url.join(r.headers["location"]))
                        continue
                    if r.status_code >= 400:
                        raise ImportError_(f"Сайт відповів помилкою {r.status_code}. Спробуйте інше посилання.")
                    buf = b""
                    for chunk in r.iter_bytes():
                        buf += chunk
                        if len(buf) > MAX_BYTES:
                            break
                    return buf.decode(r.encoding or "utf-8", errors="replace"), str(r.url)
            raise ImportError_("Забагато перенаправлень. Спробуйте пряме посилання на рецепт.")
    except httpx.HTTPError as e:
        raise ImportError_("Не вдалося відкрити сторінку. Перевірте посилання або спробуйте пізніше.") from e


# ---------- schema.org/Recipe ----------

def _iter_ld(html: str):
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except json.JSONDecodeError:
                continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                yield x
                for k in ("@graph", "mainEntity", "itemListElement"):
                    if k in x:
                        stack.append(x[k])


def _is_recipe(x: dict) -> bool:
    t = x.get("@type")
    return t == "Recipe" or (isinstance(t, list) and "Recipe" in t)


def _text(x) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", str(x or ""))).strip()


def _steps(ins) -> list[str]:
    out: list[str] = []
    if isinstance(ins, str):
        out = [s.strip() for s in re.split(r"\n+|(?<=[.!])\s+(?=\d+\.\s)", _text(ins)) if s.strip()]
    elif isinstance(ins, list):
        for s in ins:
            if isinstance(s, str):
                out.append(_text(s))
            elif isinstance(s, dict):
                if s.get("itemListElement"):
                    out += _steps(s["itemListElement"])
                else:
                    out.append(_text(s.get("text") or s.get("name")))
    return [re.sub(r"\s+", " ", s) for s in out if s][:15]


def _minutes(iso) -> int | None:
    m = re.match(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?", str(iso or ""))
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _servings(y) -> int | None:
    if isinstance(y, list):
        y = next((v for v in y if v), None)
    m = re.search(r"\d+", str(y or ""))
    return max(1, min(12, int(m.group()))) if m else None


def from_jsonld(html: str) -> dict | None:
    for x in _iter_ld(html):
        if not _is_recipe(x):
            continue
        ings = [_text(i) for i in (x.get("recipeIngredient") or x.get("ingredients") or []) if _text(i)]
        if not ings:
            continue
        mins = _minutes(x.get("totalTime")) or ((_minutes(x.get("prepTime")) or 0) + (_minutes(x.get("cookTime")) or 0)) or None
        return {"title": _text(x.get("name"))[:80], "servings": _servings(x.get("recipeYield")), "minutes": mins,
                "ingredients": ings[:40], "steps": _steps(x.get("recipeInstructions"))}
    return None


# ---------- без розмітки: текст сторінки розбирає Claude ----------

def page_text(html: str, limit: int = 14000) -> str:
    html = re.sub(r"<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    text = _text(re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", html, flags=re.I))
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text))
    return ((_text(title.group(1)) + "\n") if title else "") + text[:limit]


LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "title": {"type": "string"},
        "servings": {"type": "integer"},
        "minutes": {"type": "integer"},
        "ingredients": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["found", "title", "servings", "minutes", "ingredients", "steps"],
    "additionalProperties": False,
}


def from_llm(text: str, oidc_token: str | None = None) -> dict | None:
    from . import fridge
    client, models = fridge.make_client(oidc_token)
    resp = client.messages.create(
        model=models[0], max_tokens=4000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": LLM_SCHEMA}},
        system=("Витягни з тексту веб-сторінки один кулінарний рецепт українською: назву, кількість порцій (якщо не вказано — 4), "
                "час приготування в хвилинах (якщо не вказано — оціни), список інгредієнтів рядками як на сторінці "
                "(«500 г курячого філе»), кроки приготування коротко. Якщо рецепта на сторінці немає — found=false."),
        messages=[{"role": "user", "content": text}],
    )
    if resp.stop_reason == "refusal":
        return None
    out = json.loads(next((b.text for b in resp.content if b.type == "text"), "{}") or "{}")
    logs.event(log, "recipe.llm", found=out.get("found"), in_tokens=getattr(resp.usage, "input_tokens", None),
               out_tokens=getattr(resp.usage, "output_tokens", None))
    return out if out.get("found") and out.get("ingredients") else None


# ---------- рядок інгредієнта → наш довідник і кількість ----------

_UNIT = [
    (r"кг|кілогр\w*", ("g", 1000)), (r"г|гр|грам\w*", ("g", 1)), (r"мл|мілілітр\w*", ("ml", 1)), (r"л|літр\w*", ("ml", 1000)),
    (r"ст\.?\s*л\.?|стол\w*\s+лож\w*|ст\.\s*ложк\w*", ("spoon", 15)), (r"ч\.?\s*л\.?|чайн\w*\s+лож\w*", ("spoon", 5)),
    (r"склян\w*|чашк\w*", ("cup", 200)), (r"шт\.?|штук\w*", ("pcs", 1)),
    (r"зубч\w*|пуч\w*|гілоч\w*|щіпк\w*|дрібк\w*|листк\w*", ("misc", 1)),
]
_SPICE = ("чорн", "мелен", "духмян", "горош", "чилі", "червоний мелений", "за смаком")
_PIECE_G = {"potato": 150, "onion": 100, "carrot": 100, "pepper": 150, "zucchini": 300, "beet": 200, "cabbage": 1500,
            "chicken_thigh": 150, "chicken_fillet": 250, "sausages": 50, "tomatoes_can": 100, "tomato": 120, "cucumber": 150,
            "eggplant": 300, "apples": 180, "pumpkin": 1500, "broccoli": 400, "cauliflower": 800, "garlic": 40,
            "turkey": 300, "lavash": 90, "banana": 120, "avocado": 200, "radish": 20}
_DEFAULT = {"g": 200, "ml": 100, "pcs": 2}
_STOP = {"свіжий", "свіжа", "свіже", "свіжі", "великий", "велика", "середня", "середній", "дрібна", "дрібний", "нарізаний",
         "подрібнений", "смак", "за", "на", "для", "або", "і", "та", "й", "з", "із", "у", "в", "по", "трохи", "кілька"}


def _num(s: str) -> float | None:
    s = s.replace(",", ".").replace("½", ".5").replace("¼", ".25").replace("¾", ".75")
    try:
        if "/" in s:
            a, b = s.split("/", 1)
            return float(a) / float(b)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def _stem(w: str) -> str:
    return w[:-2] if len(w) > 4 else (w[:-1] if len(w) > 3 else w)


def _word_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    sa, sb = _stem(a), _stem(b)
    return sa == sb or (len(sb) >= 4 and a.startswith(sb)) or (len(sa) >= 4 and b.startswith(sa))


def key_of(text: str) -> str | None:
    """«курячого філе» → chicken_fillet; «твердого сиру» → cheese. Довші збіги (2–3 слова) — першими."""
    words = [w for w in re.findall(r"[a-zа-яіїєґ'’]+", text.lower()) if w not in _STOP]
    best = None
    for i in range(len(words)):
        for n in (3, 2, 1):
            if i + n > len(words):
                continue
            chunk = words[i:i + n]
            for k, (_, _, syn) in menu.PANTRY_WORDS.items():
                for s in syn:
                    sw = s.split()
                    if len(sw) == n and all(_word_eq(chunk[j], sw[j]) for j in range(n)):
                        if best is None or n > best[1]:
                            best = (k, n)
            if best and best[1] == n:
                break
        if best and best[1] >= 2:
            break
    return best[0] if best else None


def amount_of(line: str, key: str) -> float | None:
    """Кількість у одиницях довідника (г | мл | шт) для одного рядка; None — не вказано."""
    unit = menu.INGREDIENTS[key][1]
    m = re.search(r"(\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?|[½¼¾])\s*(" + "|".join(p for p, _ in _UNIT) + r")?(?![a-zа-яіїєґ])",
                  line.lower())
    if not m:
        return None
    q = _num(m.group(1).replace(" ", ""))
    if q is None:
        return None
    u = next((v for p, v in _UNIT if m.group(2) and re.fullmatch(p, m.group(2).strip())), None)
    if u is None:                               # «2 цибулини», «3 яйця»
        if unit == "pcs":
            return q
        return q * _PIECE_G[key] if key in _PIECE_G else None
    kind, mult = u
    if kind == "misc":
        return q * 5 if key == "garlic" and m.group(2).strip().startswith("зубч") else None   # зубчик часнику ≈ 5 г
    if unit == "pcs":
        return q if kind == "pcs" else None
    if kind == "pcs":
        return q * _PIECE_G[key] if key in _PIECE_G else None
    return q * mult                             # г/мл, ложки й склянки — як г або мл


def map_ingredients(lines: list[str], servings: int) -> tuple[dict[str, float], list[str]]:
    """→ ({ключ: кількість на 4 порції}, [решта як «інше»])."""
    ing: dict[str, float] = {}
    extras: list[str] = []
    for line in lines:
        k = key_of(line)
        low = line.lower()
        if k == "pepper" and any(w in low for w in _SPICE):     # чорний / мелений перець — спеція, не овоч
            k = None
        elif k and "за смаком" in low and amount_of(line, k) is None:
            k = None
        if k is None:
            name = re.sub(r"^[\d\s.,/½¼¾-]+", "", line).strip(" -–—:;,.")
            name = re.sub(r"^(" + "|".join(p for p, _ in _UNIT) + r")\s+", "", name, flags=re.I)
            name = re.sub(r"\s*(за смаком|до смаку)\s*$", "", name, flags=re.I).strip(" ,")
            if name and len(extras) < 15:
                extras.append(name[:40])
            continue
        q = amount_of(line, k)
        if q is None:
            q = _DEFAULT[menu.INGREDIENTS[k][1]] * servings / 4
        ing[k] = round(ing.get(k, 0) + q * 4 / max(1, servings), 1)
    return ing, extras


def emoji_for(ing: dict, title: str) -> str:
    t = title.lower()
    for words, e in ((("суп", "борщ", "юшк"), "🍲"), (("салат",), "🥗"), (("млинц", "оладк", "сирник", "панкейк"), "🥞"),
                     (("омлет", "яєчн"), "🍳"), (("паст", "спагет", "макарон"), "🍝"), (("плов", "рис"), "🍛")):
        if any(w in t for w in words):
            return e
    for k, e in (("fish", "🐟"), ("chicken_fillet", "🍗"), ("chicken_thigh", "🍗"), ("pork", "🥩"), ("minced", "🥩"),
                 ("potato", "🥔")):
        if k in ing:
            return e
    return "🍲"


def import_recipe(url: str, oidc_token: str | None = None) -> dict:
    """Посилання → чернетка «моєї страви» для форми: {title, emoji, minutes, servings, ing, extra, steps, src}."""
    url = safe_url(url)
    html, final = fetch(url)
    data, how = from_jsonld(html), "jsonld"
    if data is None:
        how = "llm"
        try:
            data = from_llm(page_text(html), oidc_token)
        except Exception as e:      # ключ не налаштовано чи модель недоступна — просто не вийшло
            log.warning("Рецепт: розбір ШІ не вдався: %s: %s", type(e).__name__, str(e)[:200])
            data = None
    if not data:
        raise ImportError_("На цій сторінці не знайшли рецепта. Спробуйте посилання саме на рецепт.")
    servings = data.get("servings") or 4
    ing, extras = map_ingredients(data.get("ingredients") or [], servings)
    host = urlsplit(final).hostname or ""
    logs.event(log, "recipe.import", how=how, host=host, ingredients=len(data.get("ingredients") or []),
               matched=len(ing), extras=len(extras))
    return {"title": (data.get("title") or "Мій рецепт")[:80], "emoji": emoji_for(ing, data.get("title") or ""),
            "minutes": max(5, min(240, int(data.get("minutes") or 45))), "servings": servings, "ing": ing,
            "extra": extras, "steps": "\n".join(data.get("steps") or [])[:2000], "src": final, "host": host}
