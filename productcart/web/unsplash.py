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
TRY_KEY = "unsplash_try"
UTM = "utm_source=koshyk_z_aktsiy&utm_medium=referral"
BATCH = 20            # демо-ключ: 50 запитів/год, на страву йде 2 (пошук + download)
RETRY = timedelta(hours=1)

# Англійською Unsplash шукає значно краще.
QUERIES = {
    "thighs": "roasted chicken thighs potatoes",
    "plov": "plov pilaf rice chicken",
    "kotlety": "chicken cutlets buckwheat",
    "chicken-mush": "chicken mushrooms creamy sauce rice",
    "liver": "buckwheat liver mushrooms",
    "flotski": "pasta with minced meat",
    "casserole": "potato minced meat casserole",
    "borscht": "borscht soup",
    "hake": "baked fish potatoes carrots",
    "puree": "mashed potatoes sausages",
    "syrnyky": "syrniki cottage cheese pancakes",
    "omelette": "omelette peppers cheese",
    "spaghetti": "spaghetti tomato sauce cheese",
    "buckwheat-mush": "buckwheat with mushrooms",
    "rice-veg": "fried rice vegetables egg",
    "bean-soup": "bean vegetable soup",
    "lentils": "lentils with vegetables",
    "cabbage": "braised cabbage mushrooms",
    "ragu": "vegetable stew",
    "golubtsi": "cabbage rolls tomato sauce",
    "deruny": "potato pancakes sour cream",
    "lazy-varenyky": "lazy dumplings cottage cheese",
    "chicken-soup": "chicken noodle soup",
    "pork-stew": "pork stew potatoes",
    "chicken-pasta": "creamy chicken mushroom pasta",
    "fish-cream": "fish fillet sour cream rice",
    "solyanka": "braised cabbage sausages",
    "meatballs": "meatballs tomato sauce",
    "zrazy": "potato croquettes mushrooms",
    "zucchini-stew": "stewed zucchini vegetables",
    "chicken-zucchini": "chicken zucchini rice",
    "pancakes": "small fluffy pancakes sour cream",
    "liver-puree": "liver onions mashed potatoes",
    "grechanyky": "buckwheat patties tomato sauce",
    "chili": "chili con carne beans",
    "mushroom-soup": "mushroom cream soup",
    "eggs-sausage": "fried eggs sausages vegetables",
}

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


def pick(results: list[dict]) -> dict | None:
    for r in results:
        if r.get("urls", {}).get("raw") and not r.get("premium") and not r.get("plus"):
            return r
    return None


def fetch_one(client: httpx.Client, query: str) -> dict | None:
    r = client.get(f"{API}/search/photos", params={"query": query, "per_page": 10, "orientation": "landscape",
                                                   "content_filter": "high"})
    r.raise_for_status()
    p = pick(r.json().get("results") or [])
    if not p:
        return None
    dl = p.get("links", {}).get("download_location")
    if dl:   # так вимагають правила API: позначити, що фото використали
        try:
            client.get(dl)
        except httpx.HTTPError:
            pass
    user = p.get("user") or {}
    return {"raw": p["urls"]["raw"], "pid": p.get("id"), "by": user.get("name") or user.get("username") or "Unsplash",
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
    try:
        for d in todo:
            try:
                p = fetch_one(client, QUERIES[d])
            except httpx.HTTPError as e:
                log.warning("Unsplash: %s — %s", d, e)
                break      # ліміт або мережа — спробуємо пізніше
            if p:
                found[d] = p
    finally:
        if own:
            client.close()
    if found:
        data = load(conn)
        data.update(found)
        db.set_setting(conn, KEY, json.dumps(data, ensure_ascii=False))
        log.info("Unsplash: додано фото для %d страв", len(found))
    return len(found)


def ensure(conn, missing: list[str]) -> None:
    """Раз на годину у фоні дошукує фото для страв, яких ще немає. Сторінку не затримує."""
    if not access_key() or not [d for d in missing if d in QUERIES]:
        return
    if not _lock.acquire(blocking=False):
        return
    now = datetime.now(timezone.utc)
    last = db.get_setting(conn, TRY_KEY)
    if last and datetime.fromisoformat(last) > now - RETRY:
        _lock.release()
        return
    db.set_setting(conn, TRY_KEY, now.isoformat())

    def run():
        try:
            sync(conn, missing)
        except Exception:   # фото — не критично
            log.exception("Unsplash: не вдалося")
        finally:
            _lock.release()
    threading.Thread(target=run, daemon=True).start()
