"""Фото страв з Unsplash (офіційний API, ключ UNSPLASH_ACCESS_KEY лише на сервері).

Правила Unsplash: картинки показуємо прямо з images.unsplash.com (hotlink), вказуємо автора
з посиланням на Unsplash і «смикаємо» download_location, коли фото обрано.
Знайдені фото зберігаємо в settings, тож до API йдемо лише по ті страви, яких ще немає.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

import httpx

from . import db

log = logging.getLogger("productcart.unsplash")

API = "https://api.unsplash.com"
KEY = "unsplash_photos"
TRY_KEY = "unsplash_try_at"
ERR_KEY = "unsplash_err"
UTM = "utm_source=koshyk_z_aktsiy&utm_medium=referral"
BATCH = 20            # демо-ключ: 50 запитів/год, на страву йде 2 (пошук + download)
RETRY = timedelta(minutes=15)

# Демо-товари (вигадані, мережі demo-*): стокове фото того самого виду продукту. Ключ — "p:" + product_id.
PRODUCT_QUERIES = {
    "a1": "milk bottle", "a2": "milk carton", "a3": "greek yogurt bowl", "a4": "gouda cheese", "a5": "eggs carton",
    "a6": "raw chicken breast", "a7": "rye bread loaf", "a8": "coffee beans", "a9": "bananas bunch", "a10": "beer bottle",
    "b1": "glass of milk", "b2": "buckwheat groats", "b3": "dry spaghetti pasta", "b4": "corn pasta", "b5": "smoked mackerel",
    "b6": "raw pork neck", "b7": "yellow apples", "b8": "dark chocolate bar", "b9": "ground coffee", "b10": "dish sponge soap bubbles",
    "c1": "oat milk", "c2": "cottage cheese", "c3": "oatmeal cookies", "c4": "long grain rice", "c5": "salmon fillet",
    "c6": "cucumbers", "c7": "sunflower oil bottle", "c8": "black tea bags",
}

# Англійською Unsplash шукає значно краще.
QUERIES = {
    "thighs": "roasted chicken thighs potatoes",
    "plov": "plov pilaf rice chicken",
    "kotlety": "chicken cutlets buckwheat",
    "chicken-mush": "chicken mushrooms creamy sauce rice",
    "liver": "fried chicken liver onions",
    "flotski": "pasta with minced meat",
    "casserole": "shepherds pie casserole",
    "borscht": "borscht soup",
    "hake": "baked fish potatoes carrots",
    "puree": "mashed potatoes sausages",
    "syrnyky": "syrniki cottage cheese pancakes",
    "omelette": "omelette peppers cheese",
    "spaghetti": "spaghetti tomato sauce cheese",
    "buckwheat-mush": "buckwheat with mushrooms",
    "rice-veg": "fried rice vegetables egg",
    "bean-soup": "bean vegetable soup",
    "lentils": "lentil stew bowl",
    "cabbage": "braised cabbage mushrooms",
    "ragu": "vegetable stew",
    "golubtsi": "cabbage rolls tomato sauce",
    "deruny": "potato pancakes sour cream",
    "lazy-varenyky": "lazy dumplings cottage cheese",
    "chicken-soup": "chicken noodle soup",
    "pork-stew": "pork stew potatoes",
    "chicken-pasta": "creamy chicken mushroom pasta",
    "fish-cream": "baked fish fillet rice plate",
    "solyanka": "braised cabbage sausages",
    "meatballs": "meatballs tomato sauce",
    "zrazy": "potato croquettes mushrooms",
    "zucchini-stew": "zucchini ratatouille",
    "chicken-zucchini": "chicken zucchini rice",
    "pancakes": "small fluffy pancakes sour cream",
    "liver-puree": "liver onions mashed potatoes",
    "grechanyky": "buckwheat patties tomato sauce",
    "chili": "chili con carne beans",
    "mushroom-soup": "mushroom cream soup",
    "eggs-sausage": "fried eggs sausages vegetables",
}
QUERIES.update({"p:" + k: v for k, v in PRODUCT_QUERIES.items()})

from ..recipes_more import PHOTO_QUERIES as _MORE  # noqa: E402
QUERIES.update(_MORE)
from ..recipes_extra import PHOTO_QUERIES as _EXTRA  # noqa: E402
QUERIES.update(_EXTRA)
from ..recipes_ua import PHOTO_QUERIES as _UA  # noqa: E402
QUERIES.update(_UA)
from ..tastes import PHOTO_QUERIES as _TASTES  # noqa: E402 — фото карток свайп-анкети смаків
QUERIES.update(_TASTES)
from ..recipes_bs import PHOTO_QUERIES as _BS  # noqa: E402
QUERIES.update(_BS)

# Невдалі перші підбори: перешукати (нові записи зберігають свій запит у "q").
REDO = {"liver", "lentils", "casserole", "fish-cream", "zucchini-stew"}

_lock = threading.Lock()
_mem: dict = {}


def access_key() -> str:
    return os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()


def img_url(raw: str, w: int = 720, h: int = 540) -> str:
    sep = "&" if "?" in raw else "?"
    return f"{raw}{sep}w={w}&h={h}&fit=crop&crop=entropy&auto=format&q=70"


def with_utm(url: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}{UTM}"


def load(conn) -> dict:
    """{dish_id: {raw, by, by_url, page, pid}} — з пам'яті процесу, інакше з бази."""
    if not _mem:
        raw = db.get_setting(conn, KEY)
        _mem.update(json.loads(raw) if raw else {})
    return _mem


def usable(d: str, p: dict) -> bool:
    """Чи підходить збережене фото: старі записи без "q" — якщо страви немає в REDO, нові — якщо запит не змінився."""
    return p.get("q") == QUERIES.get(d) if "q" in p else d not in REDO


def pick(results: list[dict], used: set = frozenset()) -> dict | None:
    for r in results:
        if r.get("id") in used:
            continue     # одне фото — одній страві
        if r.get("urls", {}).get("raw") and not r.get("premium") and not r.get("plus"):
            return r
    return None


def fetch_one(client: httpx.Client, query: str, used: set = frozenset()) -> dict | None:
    r = client.get(f"{API}/search/photos", params={"query": query, "per_page": 10, "orientation": "landscape",
                                                   "content_filter": "high"})
    r.raise_for_status()
    p = pick(r.json().get("results") or [], used)
    if not p:
        return None
    dl = p.get("links", {}).get("download_location")
    if dl:   # так вимагають правила API: позначити, що фото використали
        try:
            client.get(dl)
        except httpx.HTTPError:
            pass
    user = p.get("user") or {}
    return {"raw": p["urls"]["raw"], "pid": p.get("id"), "q": query, "by": user.get("name") or user.get("username") or "Unsplash",
            "by_url": (user.get("links") or {}).get("html") or "https://unsplash.com", "page": (p.get("links") or {}).get("html")}


def sync(conn, missing: list[str], client: httpx.Client | None = None) -> int:
    """Знаходить фото для страв без фото. Повертає, скільки додано."""
    key = access_key()
    todo = [d for d in missing if d in QUERIES][:BATCH]
    if not key or not todo:
        return 0
    own = client is None
    client = client or httpx.Client(timeout=10, headers={"Authorization": f"Client-ID {key}", "Accept-Version": "v1"})
    found = {}
    used = {p.get("pid") for d, p in load(conn).items() if usable(d, p)}
    try:
        for d in todo:
            try:
                p = fetch_one(client, QUERIES[d], used)
            except httpx.HTTPError as e:
                if not found:
                    raise      # жодного — хай буде видно причину
                log.warning("Unsplash: %s — %s", d, e)
                break      # ліміт або мережа — решту пізніше
            if p:
                found[d] = p
                used.add(p["pid"])
    finally:
        if own:
            client.close()
    if found:
        data = load(conn)
        data.update(found)
        db.set_setting(conn, KEY, json.dumps(data, ensure_ascii=False))
    return len(found)


def claim(conn, missing: list[str]) -> list[str]:
    """Чи пора дошукати фото: є ключ, є страви без фото і минуло RETRY від останньої спроби.
    Повертає список страв для фонового sync (порожній — нічого не робити)."""
    todo = [d for d in missing if d in QUERIES]
    if not access_key() or not todo or not _lock.acquire(blocking=False):
        return []
    try:
        now = datetime.now(timezone.utc)
        last = db.get_setting(conn, TRY_KEY)
        if last and datetime.fromisoformat(last) > now - RETRY:
            return []
        db.set_setting(conn, TRY_KEY, now.isoformat())
        return todo
    finally:
        _lock.release()


def run(conn, todo: list[str]) -> None:
    """Фонова задача після відповіді. Помилку пишемо в лог і в settings (unsplash_err)."""
    try:
        n = sync(conn, todo)
        db.set_setting(conn, ERR_KEY, f"ok: +{n}")
        log.info("Unsplash: додано фото для %d страв", n)
    except Exception as e:   # фото — не критично
        db.set_setting(conn, ERR_KEY, f"{type(e).__name__}: {e}"[:300])
        log.warning("Unsplash: помилка %s: %s", type(e).__name__, str(e)[:300])
