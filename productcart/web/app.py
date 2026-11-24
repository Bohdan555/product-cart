"""Сайт: реєстрація → профіль → підключення «Сільпо» → готовий список з акцій.

Запуск: APP_SECRET=... BASE_URL=http://127.0.0.1:8000 uvicorn productcart.web.app:app
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import time
import logging
import contextlib
import copy
import json
import re
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import quote, unquote, urlencode, urlparse
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import matcher, menu, planner
from ..catalog import TAG_LABELS, enrich, first_word
from ..models import DIET_LABELS, DIETS, Offer, Profile
from ..render import CHAIN_NAMES, uah
from ..sources import demo, silpo, silpo_cart
from starlette.background import BackgroundTask, BackgroundTasks

from . import analytics, db, fridge, icons, logs, posthog, recipe_import, security, silpo_identity, unsplash, webpush
from .analytics import verify_click
from .google_oauth import GoogleAuth
from .telegram import Telegram
from . import telegram as tgm
from .silpo_oauth import SilpoGateway, expires_soon, pkce_pair

HERE = Path(__file__).parent
log = logging.getLogger("productcart.web")
COOKIE = "pc_session"
GOOGLE_COOKIE = "pc_google"  # state і PKCE verifier незавершеного входу через Google, зашифровані
CART_COOKIE = "pc_cart_n"  # скільки товарів у кошику «Сільпо» (лише для значка)
GUEST_COOKIE = "pc_guest"  # налаштування меню гостя (без реєстрації), зашифровані
# Кнопка «←» на внутрішніх сторінках: куди повертатись (вкладки й сторінки зі своєю «←» тут не перелічені)
BACK = {"/fridge": "/", "/pantry": "/", "/menu": "/", "/buy": "/", "/cook": "/", "/profile": "/me", "/notify": "/me",
        "/silpo": "/me", "/privacy": "/me", "/photos": "/me", "/admin/stats": "/me", "/login": "/", "/register": "/"}
cur_kit: contextvars.ContextVar = contextvars.ContextVar("cur_kit", default=None)   # прилади з поточного запиту
warm_soon: contextvars.ContextVar = contextvars.ContextVar("warm_soon", default=None)  # магазин, який варто оновити
cur_ip: contextvars.ContextVar = contextvars.ContextVar("cur_ip", default=None)     # (lat, lon) за мережею, з заголовків Vercel
MEALS = {"dinner": "Обід / вечеря", "breakfast": "Сніданок", "salad": "Салат"}
GUEST_KEYS = ("household", "dinners", "max_minutes", "menu", "menu_skip", "pantry_buy", "menu_week", "menu_prev",
              "dish_likes", "menu_days", "menu_budget", "likes", "dislikes", "cooking", "kitchen", "taste_done")
# Конструктор меню: що можна любити / виключити (теги інгредієнтів)
SETUP_TASTES = [("meat", "🍗", "М'ясо й птиця"), ("fish", "🐟", "Риба й морепродукти"), ("vegetables", "🥦", "Овочі"),
                ("grains", "🌾", "Крупи й гарніри"), ("dairy", "🧀", "Молочне"), ("eggs", "🍳", "Яйця"),
                ("pork", "🐖", "Свинина")]
SETUP_HIDE = {"meat", "fish", "pork"}       # від чого можна відмовитись зовсім (не їмо)
# Анкета смаків: страви-«зразки» за основним продуктом (по одній на кожен, щоб вибір щось казав про смак)
TASTE_PROTEINS = ["курка", "риба", "фарш", "свинина", "овочі", "яйця", "сир", "гриби", "бобові", "яловичина",
                  "печінка", "індичка"]


def taste_known(p: Profile) -> bool:
    """Чи вже знаємо смаки: пройшов анкету, пропустив її або вже щось позначав раніше."""
    return bool(p.taste_done or p.likes or p.dislikes or p.dish_likes)




def day_labels(p: Profile) -> list[str]:
    """Підписи днів меню: вибрані в конструкторі (Пн, Ср, Пт) або підряд з понеділка."""
    days = sorted(set(d for d in p.menu_days if 0 <= d <= 6))
    return [menu.DAYS[d] for d in days] if days and len(days) == p.dinners else menu.DAYS
OFFERS_TTL = timedelta(hours=6)
FRIDGE_PER_DAY = 15            # фото на людину за добу: кожне розпізнавання коштує грошей
ERROR_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
              "<title>Помилка — Кошик з акцій</title><body style='font:16px/1.5 system-ui;padding:32px;max-width:520px'>"
              "<h1>Щось пішло не так</h1><p>Ми вже бачимо цю помилку в логах. Спробуйте оновити сторінку.</p>"
              "<p><a href='/'>На головну</a></p><p style='color:#888'>Код запиту: {rid}</p>")
STALE_TTL = timedelta(days=1)   # старіший кеш не показуємо навіть тимчасово (акції швидко розкуповують)
offers_stale = contextvars.ContextVar("offers_stale", default=False)  # показали акції з застарілого кешу
MAX_NEARBY = 4  # щонайбільше магазинів поруч в одному списку
LIKE_TAGS = ["vegetables", "fruits", "coffee", "tea", "meat", "fish", "dairy", "eggs", "grains", "sweets"]
# Плитки смаків групами: схожі поруч
TASTE_GROUPS = [("Овочі й фрукти", ["vegetables", "fruits"]), ("Кава й чай", ["coffee", "tea"]),
                ("М'ясо й риба", ["meat", "pork", "fish"]), ("Молочне й яйця", ["dairy", "eggs"]),
                ("Крупи й солодке", ["grains", "sweets"]), ("Інше", ["alcohol", "household"])]
HIDE_TAGS = ["alcohol", "sweets", "household", "pork", "fish", "meat"]


def create_app(db_path: str | None = None, app_secret: str | None = None, base_url: str | None = None, push_sender=None,
               gateway: SilpoGateway | None = None, secure_cookies: bool | None = None,
               google: GoogleAuth | None = None, telegram: Telegram | None = None, vision=None) -> FastAPI:
    logs.setup()
    app_secret = app_secret or os.environ.get("APP_SECRET", "")
    vercel_url = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")  # Vercel задає сам, без https://
    base_url = (base_url or os.environ.get("BASE_URL")
                or (f"https://{vercel_url}" if vercel_url else "http://127.0.0.1:8000")).rstrip("/")
    conn = db.connect(db_path)
    box = security.TokenBox(app_secret)
    gw = gateway or SilpoGateway(conn, redirect_uri=f"{base_url}/auth/silpo/callback")
    secure = base_url.startswith("https://") if secure_cookies is None else secure_cookies
    google = google or GoogleAuth(redirect_uri=f"{base_url}/auth/google/callback")
    tg = telegram or Telegram()
    vapid = webpush.Vapid.from_secret(app_secret, base_url)     # ключ push-сповіщень — з APP_SECRET, без нового секрету
    push_send = push_sender or webpush.send                    # у тестах — підробка
    vision = vision or fridge.recognize     # фото холодильника → продукти (у тестах — підробка)

    app = FastAPI(title="Кошик з акцій", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def timing(request: Request, call_next):
        """Кожен запит: id (X-Request-ID), Server-Timing і один рядок логу «req» з підсумком.
        Непередбачена помилка — у лог з трасою, людині — сторінка з кодом запиту."""
        path = request.url.path
        ctx = logs.new_request(request.headers)
        ctx_token = logs.request_ctx.set(ctx)
        stats = [0, 0.0]
        token = db.db_stats.set(stats)
        purpose = (request.headers.get("x-sec-purpose") or request.headers.get("sec-purpose") or "").lower()
        analytics.prefetching.set("prefetch" in purpose)
        try:
            cur_ip.set((float(request.headers["x-vercel-ip-latitude"]), float(request.headers["x-vercel-ip-longitude"])))
        except (KeyError, ValueError):
            cur_ip.set(None)
        warm_soon.set(None)
        posthog.start(request.cookies, path)
        t = time.perf_counter()
        try:
            resp = await call_next(request)
        except Exception:
            log.exception("Помилка: %s %s", request.method, path)
            resp = HTMLResponse(ERROR_PAGE.format(rid=ctx["rid"]), status_code=500)
        finally:
            db.db_stats.reset(token)
        total = (time.perf_counter() - t) * 1000
        resp.headers["Server-Timing"] = (f'app;dur={total:.0f}, db;dur={stats[1] * 1000:.0f};desc="{stats[0]} queries", '
                                         f'region;desc="{os.environ.get("VERCEL_REGION", "local")}"')
        resp.headers["X-Request-ID"] = ctx["rid"]
        if (ph := posthog.take()):          # події запиту — у PostHog після відповіді
            prev = resp.background

            async def after():
                if prev:
                    await prev()
                await posthog.send(ph)
            resp.background = BackgroundTask(after)
        if not path.startswith("/static/"):
            st = resp.status_code
            level = (logging.ERROR if st >= 500 else logging.WARNING if total > 800 or st == 429
                     else logging.INFO)
            logs.event(log, "req", level, method=request.method, path=path, st=st, ms=round(total),
                       db=stats[0], db_ms=round(stats[1] * 1000), silpo=ctx["silpo_n"] or None,
                       silpo_ms=round(ctx["silpo_ms"]) if ctx["silpo_n"] else None,
                       prefetch=analytics.prefetching.get() or None, slow=total > 800 or None,
                       region=os.environ.get("VERCEL_REGION"))
        logs.request_ctx.reset(ctx_token)
        return resp

    @app.middleware("http")
    async def cache_static(request: Request, call_next):
        # статика (фото страв, стилі) кешується браузером і CDN Vercel, а не віддається функцією щоразу
        resp = await call_next(request)
        if request.url.path.startswith(("/static/fonts/", "/static/vendor/")) and resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"   # версія в назві файлу
        elif request.url.path.startswith("/static/") and resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=2592000, stale-while-revalidate=604800"
        return resp
    tpl = Jinja2Templates(directory=HERE / "templates")
    css_v = hashlib.sha1((HERE / "static" / "style.css").read_bytes()).hexdigest()[:10]
    js_v = hashlib.sha1((HERE / "static" / "ui.js").read_bytes()).hexdigest()[:10]
    icon_v = hashlib.sha1(b"".join(f.read_bytes() for f in sorted((HERE / "static" / "icons").iterdir()))).hexdigest()[:8]
    tpl.env.globals.update(css_v=css_v, js_v=js_v, icon_v=icon_v)

    # ---------- PWA: маніфест, service worker, іконки ----------
    sw_src = (HERE / "static" / "sw.js").read_text(encoding="utf-8")
    sw_body = (sw_src.replace("__CSS_V__", css_v).replace("__JS_V__", js_v)
               .replace("__ICON_V__", icon_v)
               .replace("__VERSION__", hashlib.sha1((sw_src + css_v + js_v + icon_v).encode()).hexdigest()[:10]))
    manifest = {
        "name": "Кошик з акцій", "short_name": "Кошик", "lang": "uk", "dir": "ltr",
        "description": "Що приготувати з того, що є, і кошик з акцій «Сільпо» поруч",
        "start_url": "/?source=pwa", "scope": "/", "id": "/", "display": "standalone",
        "background_color": "#12271f", "theme_color": "#0e0e0f", "orientation": "portrait",
        "categories": ["food", "shopping", "lifestyle"],
        "icons": [{"src": f"/static/icons/icon-192.png?v={icon_v}", "sizes": "192x192", "type": "image/png"},
                  {"src": f"/static/icons/icon-512.png?v={icon_v}", "sizes": "512x512", "type": "image/png"},
                  {"src": f"/static/icons/icon.svg?v={icon_v}", "sizes": "any", "type": "image/svg+xml"},
                  {"src": f"/static/icons/maskable-512.png?v={icon_v}", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}],
        "shortcuts": [{"name": "Що готуємо", "url": "/today", "icons": [{"src": f"/static/icons/icon-192.png?v={icon_v}", "sizes": "192x192"}]},
                      {"name": "Список покупок", "url": "/buy", "icons": [{"src": f"/static/icons/icon-192.png?v={icon_v}", "sizes": "192x192"}]},
                      {"name": "Акції", "url": "/list", "icons": [{"src": f"/static/icons/icon-192.png?v={icon_v}", "sizes": "192x192"}]}],
    }

    @app.get("/sw.js")
    def service_worker():
        return Response(sw_body, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest")
    def web_manifest():
        return JSONResponse(manifest, media_type="application/manifest+json",
                            headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/offline", response_class=HTMLResponse)
    def offline(request: Request):
        return render(request, "offline.html", None)

    for _path, _file in (("/favicon.ico", "favicon-64.png"), ("/apple-touch-icon.png", "apple-touch-icon.png"),
                         ("/apple-touch-icon-precomposed.png", "apple-touch-icon.png")):
        def _icon(_file=_file):
            return Response((HERE / "static" / "icons" / _file).read_bytes(), media_type="image/png",
                            headers={"Cache-Control": "public, max-age=604800"})
        app.add_api_route(_path, _icon, methods=["GET"], include_in_schema=False)
    tpl.env.globals.update(uah=uah, chain_name=lambda k: CHAIN_NAMES.get(k.split(" · ")[0], k.split(" · ")[0]),
                           DIET_LABELS=DIET_LABELS, TAG_LABELS=TAG_LABELS)
    app.state.conn, app.state.gw = conn, gw
    analytics.init(conn)
    analytics.ph_secret[:] = [app_secret]
    admin_emails = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
    tpl.env.globals.update(watch_word=watch_word, ic=icons.ic)
    tpl.env.globals.update(kcal=menu.kcal_per_portion)
    tpl.env.globals.update(click_url=lambda o: analytics.click_url(app_secret, o), visual=offer_visual, plural=plural,
                           name_visual=name_visual)

    # ---------- допоміжне ----------
    def user_of(request: Request):
        u = security.session_user(conn, request.cookies.get(COOKIE))
        if u:
            logs.set_user(u["id"])
        return u

    def demo_photos(offers) -> None:
        """Демо-товари без фото: стокове фото того самого виду продукту з Unsplash (квадрат для картки)."""
        if not any(o.source == "demo" and not o.image for o in offers):
            return
        menu_photos()                                   # заодно дошукає відсутні у фоні
        data = unsplash.load(conn)
        for o in offers:
            p = data.get("p:" + o.product_id) if o.source == "demo" and not o.image else None
            if p and unsplash.usable("p:" + o.product_id, p):
                o.image = unsplash.img_url(p["raw"], 360, 360)

    def menu_photos() -> dict[str, str]:
        """id страви → адреса фото: свої (Wikimedia) або з Unsplash. Відсутні дошукуються у фоні."""
        photos = local_photos()
        for d, p in unsplash.load(conn).items():
            if unsplash.usable(d, p):
                photos.setdefault(d, unsplash.img_url(p["raw"]))
        missing = [k for k in PRODUCT_PHOTO_KEYS if k not in photos] + [r.id for r in menu.RECIPES if r.id not in photos]   # товари першими: їх мало, а безуспішні страви не мають їх блокувати
        if missing and (todo := unsplash.claim(conn, missing)):
            photo_job.set(todo)     # render() допише пошук фоновою задачею після відповіді
        return photos

    def render(request: Request, name: str, user=None, status: int = 200, **ctx):
        is_admin = bool(user) and user["email"].lower() in admin_emails
        n = request.cookies.get(CART_COOKIE, "")
        todo, bg = photo_job.get(), BackgroundTasks()
        if todo:
            photo_job.set(None)
            bg.add_task(unsplash.run, conn, todo)
        if (st := warm_soon.get()):         # показали акції іншого/старого магазину — оновлюємо найближчий після відповіді
            warm_soon.set(None)
            bg.add_task(warm_store, st)
        bg = bg if bg.tasks else None
        ctx.setdefault("back_url", BACK.get(request.url.path))
        ph = {"key": posthog.key(), "host": posthog.HOST, "assets": posthog.ASSETS,
              "id": posthog.user_alias(app_secret, user["id"]) if user else None} if posthog.key() else None
        return tpl.TemplateResponse(request, name, dict(ctx, user=user, is_admin=is_admin, google_on=google.enabled, ph=ph,
                                                        cart_n=int(n) if n.isdigit() else None), status_code=status,
                                    background=bg)

    def go(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    NEXT_COOKIE = "after_login"

    def back_after_login(request: Request, default: str):
        """Куди повернути після входу (лише внутрішня адреса з cookie, яку ставить захищена сторінка)."""
        nxt = request.cookies.get(NEXT_COOKIE) or ""
        return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else default

    # Сторінки входу й підключення: з них не повертаємо (інакше людина ходитиме по колу)
    NO_RETURN = ("/register", "/login", "/silpo", "/start", "/logout", "/auth/")

    def origin_of(request: Request) -> str | None:
        """Внутрішня сторінка, з якої людина прийшла (Referer того самого сайту), крім сторінок входу."""
        ref = urlparse(request.headers.get("referer") or "")
        if ref.netloc and ref.netloc != request.url.netloc:
            return None
        path = ref.path or ""
        if not path.startswith("/") or path.startswith("//") or "\\" in path:
            return None
        if path in NO_RETURN or path.startswith("/auth/"):
            return None
        return path + (f"?{ref.query}" if ref.query else "")

    def remember_origin(request: Request, resp):
        """Запам'ятати, куди повернути після входу й підключення «Сільпо» (на 30 хв)."""
        if (o := origin_of(request)):
            resp.set_cookie(NEXT_COOKIE, o, max_age=1800, httponly=True, samesite="lax", secure=secure)
        return resp

    def need_user(request: Request, csrf: str | None = None):
        u = user_of(request)
        if u is None:
            return None, go("/login")
        if csrf is not None and not secrets.compare_digest(csrf, u["csrf"]):
            return None, HTMLResponse("Сторінка застаріла. Оновіть її й спробуйте ще раз.", status_code=400)
        return u, None

    def load_profile(user_id: int) -> tuple[Profile, bool]:
        r = conn.execute("SELECT data, health_consent FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        if not r:
            return Profile(name="Мій профіль"), False
        return Profile.from_dict(json.loads(r["data"])), bool(r["health_consent"])

    def save_profile(user_id: int, p: Profile, consent: bool) -> None:
        conn.execute("""INSERT INTO profiles (user_id, data, health_consent, updated_at) VALUES (?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, health_consent=excluded.health_consent,
                        updated_at=excluded.updated_at""",
                     (user_id, json.dumps(p.to_dict(), ensure_ascii=False), int(consent), security.iso(security.now())))

    def guest_profile(request: Request) -> Profile:
        p = Profile(name="Гість", household=2)
        blob = request.cookies.get(GUEST_COOKIE)
        raw = box.open(blob) if blob else None
        if raw:
            try:
                d = json.loads(raw)
                p = Profile.from_dict({**p.to_dict(), **{k: d[k] for k in GUEST_KEYS if k in d}})
            except (ValueError, TypeError, KeyError):
                pass
        return p

    def keep_guest(resp, p: Profile):
        p.dish_dislikes = p.dish_dislikes[:60]      # гість — у cookie (≤ 4 КБ): пам'ятаємо останні 60 «не моє»
        data = json.dumps({k: getattr(p, k) for k in GUEST_KEYS}, ensure_ascii=False)
        resp.set_cookie(GUEST_COOKIE, box.seal(data), max_age=30 * 86400, httponly=True, samesite="lax", secure=secure)
        return resp

    cart_counts: dict[int, tuple[float, int]] = {}  # user_id → (коли, скільки) — кеш для значка

    def remember_cart(resp, n: int, user_id: int | None = None):
        """Кількість товарів у кошику «Сільпо» для значка в шапці — оновлюється, коли ми читаємо кошик."""
        if user_id is not None:
            cart_counts[user_id] = (time.monotonic(), max(0, int(n)))
        resp.set_cookie(CART_COOKIE, str(max(0, int(n))), max_age=7 * 86400, httponly=True, samesite="lax", secure=secure)
        return resp

    def dict_store(o: Offer, st: dict) -> Offer:
        o.store_id, o.store_name = o.store_id or st["id"], o.store_name or st.get("title")
        return o

    def silpo_link(user_id: int):
        return conn.execute("SELECT * FROM silpo_links WHERE user_id=?", (user_id,)).fetchone()

    def silpo_tokens(user_id: int) -> dict | None:
        """Токени користувача; оновлює, якщо спливають. None — треба увійти в «Сільпо» знову."""
        link = silpo_link(user_id)
        if not link:
            return None
        raw = box.open(link["tokens_enc"])
        if not raw:
            return None
        tokens = json.loads(raw)
        if expires_soon(tokens):
            if not tokens.get("refresh_token"):
                return None
            try:
                tokens = gw.refresh(tokens)
            except Exception as e:
                logs.event(log, "silpo.refresh_failed", logging.WARNING, err=f"{type(e).__name__}: {str(e)[:200]}")
                return None
            conn.execute("UPDATE silpo_links SET tokens_enc=? WHERE user_id=?", (box.seal(json.dumps(tokens)), user_id))
        return tokens

    parsed_offers: dict[str, tuple[str, list[Offer]]] = {}  # у межах процесу: store_id → (fetched_at, готові акції)

    def cached_offers(store_id: str, max_age: timedelta = OFFERS_TTL) -> list[Offer] | None:
        # спершу лише дата (дешево); великий JSON читаємо й розбираємо, тільки якщо його ще немає в пам'яті
        r = conn.execute("SELECT fetched_at FROM offers_cache WHERE chain='silpo' AND store_id=?", (store_id,)).fetchone()
        if not r or r["fetched_at"] < security.iso(security.now() - max_age):
            return None
        hit = parsed_offers.get(store_id)
        if not hit or hit[0] != r["fetched_at"]:
            row = conn.execute("SELECT data FROM offers_cache WHERE chain='silpo' AND store_id=?", (store_id,)).fetchone()
            if not row:
                return None
            # розбір і класифікація (enrich) — найдорожче на сторінці; робимо раз на оновлення кешу магазину
            hit = parsed_offers[store_id] = (r["fetched_at"], [offer_from_dict(d) for d in json.loads(row["data"])])
        return [copy.copy(o) for o in hit[1]]     # копії: далі їм дописують магазин

    @contextlib.asynccontextmanager
    async def lazy_silpo(access_token: str):
        """Дає get_call(): з'єднання з «Сільпо» відкривається при першому виклику, а не одразу."""
        async with contextlib.AsyncExitStack() as stack:
            opened: list = []

            async def get_call():
                if not opened:
                    opened.append(await stack.enter_async_context(gw.session(access_token)))
                return opened[0]

            yield get_call

    branches_mem: list[dict] = []  # список магазинів у пам'яті процесу

    async def silpo_branches(get_call) -> list[dict]:
        """Список магазинів «Сільпо» однаковий для всіх — тримаємо в кеші на добу.
        get_call() відкриває з'єднання з «Сільпо» лише тоді, коли кеш застарів."""
        fresh_after = security.iso(security.now() - timedelta(hours=24))
        if branches_mem and branches_mem[0]["at"] >= fresh_after:
            return branches_mem[0]["items"]
        raw = db.get_setting(conn, "silpo_branches")
        if raw:
            data = json.loads(raw)
            if data.get("at", "") >= fresh_after:
                branches_mem[:] = [data]
                return data["items"]
        items = await silpo.list_branches(await get_call())
        db.set_setting(conn, "silpo_branches", json.dumps({"at": security.iso(security.now()), "items": items}, ensure_ascii=False))
        return items

    def save_offers_cache(store_id: str, offers: list[Offer]) -> None:
        conn.execute("""INSERT INTO offers_cache (chain, store_id, fetched_at, data) VALUES ('silpo', ?, ?, ?)
                        ON CONFLICT(chain, store_id) DO UPDATE SET fetched_at=excluded.fetched_at, data=excluded.data""",
                     (store_id, security.iso(security.now()), json.dumps([o.to_dict() for o in offers], ensure_ascii=False)))

    async def silpo_list_input(user_id: int, p: Profile, force: bool = False, allow_stale: bool = True):
        """Магазини для списку та їхні акції.
        Повертає (stores, offers, notice): stores — магазини поруч (з km) або вибраний вручну.
        allow_stale: якщо кеш магазину старший за 6 год, але молодший за тиждень — показуємо його одразу
        (offers_stale = True), а свіжі акції сторінка дотягує окремим запитом /offers/refresh."""
        offers_stale.set(False)
        link = silpo_link(user_id)
        if not link:
            return [], [], None
        tokens = silpo_tokens(user_id)
        if not tokens:
            return [], [], "Вхід у «Сільпо» застарів. Підключіть «Сільпо» ще раз."
        stores, notice = [], None
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                if p.lat is not None and p.lon is not None:
                    near = silpo.nearest(await silpo_branches(get_call), p.lat, p.lon, n=50)
                    stores = [b for b in near if b["km"] <= p.radius_km][:MAX_NEARBY]
                    if not stores and near:
                        stores = near[:1]
                        notice = (f"У радіусі {p.radius_km:g} км магазинів «Сільпо» немає. "
                                  f"Беремо найближчий — {near[0]['title']} ({near[0]['km']} км).")
                elif link["store_id"]:
                    stores = [{"id": link["store_id"], "title": link["store_title"]}]
                if not stores:
                    return [], [], None
                offers: list[Offer] = []
                todo = []
                stale = False
                for st in stores:
                    hit = None if force else cached_offers(st["id"])
                    if hit is None and not force and allow_stale:
                        hit = cached_offers(st["id"], STALE_TTL)
                        stale = stale or hit is not None
                    if hit is not None:
                        offers += [dict_store(o, st) for o in hit]
                    else:
                        todo.append(st)
                call = await get_call()
                if not db.get_setting(conn, "silpo_identity_probe"):      # один раз на сайт
                    db.set_setting(conn, "silpo_identity_probe", security.iso(security.now()))
                    await silpo_identity.probe(call, tokens) if todo else None
                sem = asyncio.Semaphore(4)

                async def one(st):
                    async with sem:
                        return st, await silpo.fetch_offers(call, st)

                for res in await asyncio.gather(*(one(st) for st in todo), return_exceptions=True):
                    if isinstance(res, Exception):
                        log.warning("Сільпо: магазин не віддав акції: %s", str(res)[:500])
                        notice = notice or "Частина магазинів «Сільпо» зараз не відповіла — показуємо акції решти. Спробуйте оновити пізніше."
                        continue
                    st, got = res
                    save_offers_cache(st["id"], got)
                    offers += got
        except Exception as e:
            e = root_error(e)
            log.warning("Сільпо: список акцій: %s: %s", type(e).__name__, str(e)[:1000])
            return stores, [], f"«Сільпо» не відповіло: {str(e)[:120]}"
        offers_stale.set(stale)
        return stores, offers, notice

    @app.post("/offers/refresh")
    async def offers_refresh(request: Request, csrf: str = Form("")):
        """Дотягує свіжі акції для застарілого кешу. Сторінка показала старі одразу й викликає це у фоні."""
        u, bad = need_user(request, csrf)
        if bad:
            return JSONResponse({"ok": False}, status_code=400)
        p, _ = load_profile(u["id"])
        _, offers, notice = await silpo_list_input(u["id"], p, allow_stale=False)
        return JSONResponse({"ok": bool(offers) and not notice, "notice": notice})

    # ---------- сторінки ----------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        """Головна «Що готуємо?»: разово на сьогодні або план на тиждень."""
        u = user_of(request)
        analytics.track(conn, u["id"] if u else None, "home_viewed", guest=not u)
        p = load_profile(u["id"])[0] if u else guest_profile(request)
        # вітрина: лише кеш акцій магазину (без запитів до «Сільпо»), інакше демо
        link = silpo_link(u["id"]) if u else None
        offers = cached_offers(link["store_id"], STALE_TTL) if link and link["store_id"] else None
        # «Гарячі акції» — лише з власного підключеного «Сільпо» (службове підключення — тільки для цін рецептів)
        is_demo = not offers
        deals = [] if is_demo else sorted((o for o in offers if o.discount_pct), key=lambda o: -(o.discount_pct or 0))[:8]
        meal = default_meal()
        ip = ip_place(request)
        near_ip = near_stores(ip[0], ip[1], n=1) if ip else []
        soon = menu.expiring(p.fridge_since, p.fridge, within=1, until=p.fridge_until) if u else []
        soon_url = (f"/today/dishes?{today_qs(p.fridge, 60, p.household or 2)}" if soon else None)
        return render(request, "home.html", u, cooking=cooking_items(p)[:3], link=link, is_demo=is_demo, deals=deals,
                      soon=[(k, menu.INGREDIENTS[k][0], menu.PANTRY_WORDS.get(k, ("", "🥫"))[1],
                             ("" if p.fridge_until.get(k) else "≈ ") + menu.when_label(d))     # ≈ — приблизно, за видом продукту
                            for k, d in soon], soon_url=soon_url, p_fridge_n=len(p.fridge) + len(p.fridge_other),
                      near=near_ip[0] if near_ip else None,
                      meal=meal, people=p.household or 2)

    @app.get("/me", response_class=HTMLResponse)
    def me_page(request: Request):
        """Профіль: статистика, кухня (сім'я, час, смаки, свої страви), підключення («Сільпо», Telegram)."""
        u = user_of(request)
        p = load_profile(u["id"])[0] if u else guest_profile(request)
        mine = menu.user_recipes(p)
        days = day_labels(p)[:p.dinners]
        ctx = dict(p=p, mine=mine, days=days,
                   link=silpo_link(u["id"]) if u else None, chat=tg_chat(u["id"]) if u else None,
                   push_n=len(push_subs(u["id"])) if u else 0,
                   likes=[TAG_LABELS.get(t, t) for t in p.likes][:4], hides=[TAG_LABELS.get(t, t) for t in p.dislikes][:3],
                   fridge_n=len(p.fridge) + len(p.fridge_other), cook_n=len(cooking_items(p)))
        return render(request, "me.html", u, **ctx)

    @app.post("/client-log")
    async def client_log(request: Request):
        """Короткі звіти з браузера (напр. чому не спрацював мікрофон) — лише в лог, без персональних даних."""
        form = await request.form()
        ua = request.headers.get("user-agent", "")
        browser = next((n for k, n in (("SamsungBrowser", "samsung"), ("Telegram", "telegram"), ("Instagram", "instagram"),
                                         ("FBAN", "facebook"), ("Firefox", "firefox"), ("EdgA", "edge"), ("OPR", "opera"),
                                         ("CriOS", "chrome-ios"), ("Chrome", "chrome"), ("Safari", "safari")) if k in ua), "other")
        logs.event(log, "client." + str(form.get("event", ""))[:20], logging.WARNING, error=str(form.get("error", ""))[:60],
                   extra=str(form.get("extra", ""))[:60], browser=browser, android="Android" in ua, ios="iPhone" in ua)
        return Response(status_code=204)

    @app.get("/week")
    def week_start(request: Request):
        """План на тиждень починається з «що є вдома» (крок ①); далі меню (②) і покупки (③).
        Смаки питаємо, коли вперше складаємо меню (конструктор /menu/setup)."""
        return go("/fridge")

    @app.get("/register", response_class=HTMLResponse)
    def register_form(request: Request):
        return remember_origin(request, render(request, "auth.html", mode="register"))

    @app.post("/register")
    def register(request: Request, email: str = Form(...), password: str = Form(...), agree: str = Form("")):
        email = email.strip().lower()
        err = None
        if "@" not in email or len(email) > 200:
            err = "Вкажіть правильну адресу email."
        elif len(password) < 8:
            err = "Пароль має бути щонайменше 8 символів."
        elif not agree:
            err = "Щоб продовжити, погодьтеся з умовами й політикою приватності."
        elif conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            err = "Цей email уже зареєстровано. Увійдіть."
        if err:
            return render(request, "auth.html", mode="register", error=err, email=email, status=400)
        uid = new_user(request, email, security.hash_password(password), "password")
        return signed_in(uid, after_signup(uid))

    def new_user(request: Request, email: str, pw_hash: str, method: str) -> int:
        uid = conn.execute("INSERT INTO users (email, pw_hash, created_at) VALUES (?, ?, ?) RETURNING id",
                           (email, pw_hash, security.iso(security.now()))).fetchone()["id"]
        analytics.track(conn, uid, "signup", method=method)
        if request.cookies.get(GUEST_COOKIE):  # налаштування меню, зроблені до реєстрації, переходять у профіль
            g = guest_profile(request)
            save_profile(uid, Profile(name="Мій профіль", **{k: getattr(g, k) for k in GUEST_KEYS}), False)
        return uid

    def after_signup(uid: int) -> str:
        """Новий акаунт: спершу смаки (якщо гостем ще не розповів), потім «Сільпо»."""
        return "/start"

    def signed_in(uid: int, url: str):
        token, _ = security.new_session(conn, uid)
        resp = go(url)
        resp.delete_cookie(GUEST_COOKIE)
        resp.set_cookie(COOKIE, token, max_age=security.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure)
        return resp

    # ---------- вхід через Google ----------
    @app.get("/auth/google")
    def google_start(request: Request):
        if not google.enabled:
            return go("/login")
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        resp = go(google.authorize_url(state, challenge))
        resp.set_cookie(GOOGLE_COOKIE, box.seal(json.dumps({"state": state, "verifier": verifier})),
                        max_age=600, httponly=True, samesite="lax", secure=secure)
        return resp

    @app.get("/auth/google/callback", response_class=HTMLResponse)
    def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        def fail(msg: str):
            logs.event(log, "auth.google_failed", logging.WARNING, reason=msg[:120], google_error=error or None)
            resp = render(request, "auth.html", mode="login", error=msg, status=400)
            resp.delete_cookie(GOOGLE_COOKIE)
            return resp

        blob = request.cookies.get(GOOGLE_COOKIE)
        raw = box.open(blob) if blob else None
        pending = json.loads(raw) if raw else {}
        if error:
            return fail("Вхід через Google скасовано." if error == "access_denied" else f"Google повернув помилку: {error[:80]}")
        if not code or not pending or not secrets.compare_digest(state, pending.get("state", "")):
            return fail("Вхід через Google застарів. Спробуйте ще раз.")
        try:
            info = google.exchange(code, pending["verifier"])
        except Exception as e:
            log.warning("Google: обмін коду не вдався: %s", str(e)[:300])
            return fail("Google не відповів. Спробуйте ще раз або увійдіть з паролем.")
        email = str(info.get("email") or "").strip().lower()
        if not email or info.get("email_verified") not in (True, "true"):
            return fail("Google не підтвердив вашу адресу email. Увійдіть з паролем.")
        r = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if r:
            analytics.track(conn, r["id"], "login", method="google")
            resp = signed_in(r["id"], back_after_login(request, "/list"))
            resp.delete_cookie(NEXT_COOKIE)
        else:
            uid = new_user(request, email, "google", "google")
            resp = signed_in(uid, after_signup(uid))
        resp.delete_cookie(GOOGLE_COOKIE)
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return remember_origin(request, render(request, "auth.html", mode="login"))

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...)):
        r = conn.execute("SELECT id, pw_hash FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        if r and r["pw_hash"] == "google":
            return render(request, "auth.html", mode="login", email=email, status=400,
                          error="Цей профіль створено через Google. Натисніть «Увійти через Google».")
        if not r or not security.verify_password(password, r["pw_hash"]):
            logs.event(log, "auth.login_failed", logging.WARNING, method="password")
            return render(request, "auth.html", mode="login", error="Неправильний email або пароль.", email=email, status=400)
        token, _ = security.new_session(conn, r["id"])
        analytics.track(conn, r["id"], "login")
        resp = go(back_after_login(request, "/list"))
        resp.delete_cookie(NEXT_COOKIE)
        resp.set_cookie(COOKIE, token, max_age=security.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure)
        return resp

    @app.post("/logout")
    def logout(request: Request, csrf: str = Form("")):
        u, bad = need_user(request, csrf)
        if bad:
            return bad
        security.drop_session(conn, u["token"])
        resp = go("/")
        resp.delete_cookie(COOKIE)
        resp.delete_cookie(CART_COOKIE)
        return resp

    # ---------- профіль ----------
    @app.get("/profile", response_class=HTMLResponse)
    def profile_form(request: Request, welcome: int = 0):
        u, bad = need_user(request)
        if bad:
            return bad
        p, consent = load_profile(u["id"])
        return profile_page(request, u, p, consent, welcome=welcome)

    def profile_page(request: Request, u, p: Profile, consent: bool, status: int = 200, **extra):
        photos = menu_photos()
        dishes = sorted(menu.RECIPES, key=lambda r: (r.id not in photos, r.minutes))   # спершу ті, що з фото
        return render(request, "profile.html", u, p=p, consent=consent, like_tags=LIKE_TAGS, hide_tags=HIDE_TAGS, taste_groups=TASTE_GROUPS,
                      diets=list(DIETS), silpo=silpo_link(u["id"]), status=status, photos=photos,
                      tastes=SETUP_TASTES, hideable=SETUP_HIDE,
                      dishes=[{"id": r.id, "title": r.title, "emoji": r.emoji, "minutes": r.minutes} for r in dishes[:12]],
                      **extra)

    @app.get("/profile/silpo-hints")
    async def profile_silpo_hints(request: Request):
        """«Заповнити за мене»: склад сім'ї, обмеження й регулярні покупки з «Сільпо» — як пропозиція в анкету."""
        u, bad = need_user(request)
        if bad:
            return JSONResponse({"error": "Увійдіть, щоб заповнити профіль."}, status_code=401)
        tokens = silpo_tokens(u["id"]) if silpo_link(u["id"]) else None
        if not tokens:
            return JSONResponse({"error": "Спершу підключіть «Сільпо».", "connect": "/silpo"}, status_code=409)
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                call = await get_call()
                hints = await silpo.profile_hints(call)
                buys = await silpo.purchase_hints(call)
        except Exception as e:
            e = root_error(e)
            log.warning("Сільпо: підказки профілю: %s: %s", type(e).__name__, str(e)[:800])
            return JSONResponse({"error": f"«Сільпо» не відповіло: {str(e)[:120]}"}, status_code=502)
        analytics.track(conn, u["id"], "profile_silpo_fill", orders=buys["orders"], regulars=len(buys["regulars"]),
                        diets=len(hints["diets"]), household=bool(hints["household"]))
        return JSONResponse({"household": hints["household"], "diets": hints["diets"], "regulars": buys["regulars"],
                             "brands": buys["brands"], "orders": buys["orders"]})

    @app.post("/profile/preview")
    async def profile_preview(request: Request):
        """Живий підсумок анкети: скільки акцій підходить і яке вийде меню. Лише кеш акцій або демо — без запитів до «Сільпо»."""
        form = await request.form()
        u, bad = need_user(request, str(form.get("csrf", "")))
        if bad:
            return JSONResponse({}, status_code=400)
        prev, _ = load_profile(u["id"])
        p, diets, consent = profile_from_form(form, prev)
        p.diets = diets   # для прев'ю згода не потрібна: нічого не зберігаємо
        offers, demo_used = [], False
        link = silpo_link(u["id"])
        if link:
            ids = [link["store_id"]] if link["store_id"] else []
            if not ids and prev.lat is not None and branches_mem:
                ids = [b["id"] for b in silpo.nearest(branches_mem[0]["items"], prev.lat, prev.lon, n=50)
                       if b["km"] <= p.radius_km][:MAX_NEARBY]
            for sid in ids:
                offers += cached_offers(sid) or []
        if not offers:
            offers, demo_used = demo.load(), True
        offers = planner.merge_nearby(offers)
        matches, _ = matcher.rank(offers, p)
        sl = planner.build(matches, p)
        m = menu.plan(offers, p, dinners=p.dinners, max_minutes=p.max_minutes)
        return JSONResponse({"offers": len(offers), "matched": len(matches), "list": len(sl.items),
                             "list_saving": round(sl.saving), "per_portion": round(m.per_portion) if m.dishes else None,
                             "menu_saving": round(m.saving), "dishes": len(m.dishes), "demo": demo_used})

    @app.post("/profile")
    async def profile_save(request: Request):
        form = await request.form()
        u, bad = need_user(request, form.get("csrf", ""))
        if bad:
            return bad

        prev, _ = load_profile(u["id"])
        p, diets, consent = profile_from_form(form, prev)
        if diets and not consent:
            return profile_page(request, u, p, False, status=400, picked_diets=diets,
                                error="Обмеження в харчуванні — це дані про здоров'я. Щоб ми їх врахували, позначте згоду.")
        conn.execute("""INSERT INTO profiles (user_id, data, health_consent, updated_at) VALUES (?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, health_consent=excluded.health_consent,
                        updated_at=excluded.updated_at""",
                     (u["id"], json.dumps(p.to_dict(), ensure_ascii=False), int(consent), security.iso(security.now())))
        analytics.track(conn, u["id"], "profile_saved", diets=len(p.diets), regulars=len(p.regulars), budget=bool(p.budget_week))
        if form.get("welcome"):             # перше заповнення — далі підключити «Сільпо» або до акцій
            return go("/silpo" if not silpo_link(u["id"]) else "/list")
        return go("/me?saved=1")

    # ---------- «Сільпо» ----------
    @app.get("/silpo", response_class=HTMLResponse)
    async def silpo_page(request: Request, city: str = "", q: str = "", error: str = "",
                         lat: float | None = None, lon: float | None = None):
        u, bad = need_user(request)
        if bad:
            return bad
        link = silpo_link(u["id"])
        stores, err, near_mode, ip_city = [], error or None, None, None
        p, _ = load_profile(u["id"])
        if link and lat is None and not (city or q) and not link["store_id"] and not error:
            # Магазин ще не обрано: пропонуємо найближчі за приблизною локацією з IP (Vercel додає ці заголовки),
            # а якщо її немає — магазини міста з профілю. Координати ніде не зберігаємо.
            try:
                lat = float(request.headers["x-vercel-ip-latitude"])
                lon = float(request.headers["x-vercel-ip-longitude"])
                near_mode = "ip"
                ip_city = unquote(request.headers.get("x-vercel-ip-city", "")) or None
            except (KeyError, ValueError):
                city = p.city or ""
        elif lat is not None and lon is not None:
            near_mode = "gps"
        if link and (near_mode or city or q):
            tokens = silpo_tokens(u["id"])
            if not tokens:
                err = "Вхід у «Сільпо» застарів. Підключіть ще раз."
            else:
                try:
                    async with lazy_silpo(tokens["access_token"]) as get_call:
                        if near_mode:
                            stores = silpo.nearest(await silpo_branches(get_call), lat, lon, n=6)
                        else:
                            stores = (await silpo.list_branches(await get_call(), city or None, q or None))[:40]
                except Exception as e:
                    err = f"«Сільпо» не відповіло: {str(e)[:120]}"
                if not stores and not err:
                    err = "Магазинів не знайдено. Перевірте місто або частину адреси."
        return remember_origin(request, render(request, "silpo.html", u, link=link, stores=stores, city=city or (p.city or ""), q=q, error=err,
                      near_mode=near_mode, ip_city=ip_city))

    @app.post("/silpo/connect")
    def silpo_connect(request: Request, csrf: str = Form("")):
        u, bad = need_user(request, csrf)
        if bad:
            return bad
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        conn.execute("DELETE FROM oauth_pending WHERE user_id=? OR created_at < ?",
                     (u["id"], security.iso(security.now() - timedelta(hours=1))))
        conn.execute("INSERT INTO oauth_pending (state, user_id, verifier, created_at) VALUES (?, ?, ?, ?)",
                     (state, u["id"], verifier, security.iso(security.now())))
        try:
            return remember_origin(request, go(gw.authorize_url(state, challenge)))
        except Exception as e:
            return go(f"/silpo?error=Не вдалося відкрити вхід «Сільпо»: {str(e)[:100]}")

    @app.post("/admin/silpo/connect")
    def admin_silpo_connect(request: Request, csrf: str = Form("")):
        """Службове підключення «Сільпо» (лише ціни інгредієнтів для рецептів) — з адмінки."""
        u, bad = admin_only(request)
        if bad:
            return bad
        if not secrets.compare_digest(csrf, u["csrf"]):
            return HTMLResponse("Сторінка застаріла. Оновіть її й спробуйте ще раз.", status_code=400)
        verifier, challenge = pkce_pair()
        state = "d." + secrets.token_urlsafe(24)
        conn.execute("DELETE FROM oauth_pending WHERE user_id=? OR created_at < ?",
                     (u["id"], security.iso(security.now() - timedelta(hours=1))))
        conn.execute("INSERT INTO oauth_pending (state, user_id, verifier, created_at) VALUES (?, ?, ?, ?)",
                     (state, u["id"], verifier, security.iso(security.now())))
        try:
            return go(gw.authorize_url(state, challenge))
        except Exception as e:
            return go(f"/admin/stats?silpo_error=Не вдалося відкрити вхід «Сільпо»: {str(e)[:100]}#silpo")

    @app.post("/admin/silpo/disconnect")
    def admin_silpo_disconnect(request: Request, csrf: str = Form("")):
        u, bad = admin_only(request)
        if bad:
            return bad
        if not secrets.compare_digest(csrf, u["csrf"]):
            return HTMLResponse("Сторінка застаріла. Оновіть її й спробуйте ще раз.", status_code=400)
        db.set_setting(conn, "silpo_donor", "")
        return go("/admin/stats#silpo")

    def donor_status() -> dict:
        d = donor_info()
        rows = conn.execute("SELECT fetched_at FROM offers_cache WHERE chain='silpo'").fetchall()
        fresh = security.iso(security.now() - OFFERS_TTL)
        try:
            demand = len(json.loads(db.get_setting(conn, "warm_demand") or "{}"))
        except ValueError:
            demand = 0
        return {"on": bool(d), "at": (d or {}).get("at", "")[:16].replace("T", " "), "dead": bool(d and d.get("dead")),
                "stores": len(rows), "fresh": sum(1 for r in rows if r["fetched_at"] >= fresh), "demand": demand,
                "last": max((r["fetched_at"] for r in rows), default="")[:16].replace("T", " ")}

    @app.get("/auth/silpo/callback")
    async def silpo_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
        u, bad = need_user(request)
        if bad:
            return bad
        pending = conn.execute("SELECT * FROM oauth_pending WHERE state=? AND user_id=?", (state, u["id"])).fetchone()
        conn.execute("DELETE FROM oauth_pending WHERE state=?", (state,))
        if error or not code:
            logs.event(log, "silpo.connect_failed", logging.WARNING, reason="cancelled", error=error or None)
            if state.startswith("d."):
                return go("/admin/stats?silpo_error=Вхід у «Сільпо» скасовано#silpo")
            return go(f"/silpo?error=Вхід у «Сільпо» скасовано: {error_description or error or 'немає коду'}")
        if not pending:
            logs.event(log, "silpo.connect_failed", logging.WARNING, reason="stale_state")
            return go("/silpo?error=Сесія входу застаріла. Натисніть «Підключити Сільпо» ще раз.")
        donor = state.startswith("d.") and u["email"].lower() in admin_emails
        try:
            tokens = gw.exchange(code, pending["verifier"])
        except Exception as e:
            log.warning("Сільпо: обмін коду на токен: %s: %s", type(e).__name__, str(e)[:300])
            return go(f"/admin/stats?silpo_error={str(e)[:150]}#silpo" if donor else f"/silpo?error={str(e)[:150]}")
        if donor:       # службове підключення: окремо від особистого «Сільпо» адміна
            db.set_setting(conn, "silpo_donor", box.seal(json.dumps({"tokens": tokens, "at": security.iso(security.now())})))
            logs.event(log, "silpo.donor_connected", logging.INFO)
            return go("/admin/stats#silpo")
        conn.execute("""INSERT INTO silpo_links (user_id, tokens_enc, connected_at) VALUES (?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET tokens_enc=excluded.tokens_enc, connected_at=excluded.connected_at""",
                     (u["id"], box.seal(json.dumps(tokens)), security.iso(security.now())))
        analytics.track(conn, u["id"], "silpo_connected")
        store = await auto_setup(request, u["id"], tokens)
        back = request.cookies.get(NEXT_COOKIE)
        if back and store:                 # повертаємо туди, звідки людина пішла підключати «Сільпо»
            resp = go(back_after_login(request, "/menu?ready=1"))
            resp.delete_cookie(NEXT_COOKIE)
            return resp
        return go("/menu?ready=1" if store else "/silpo")

    async def auto_setup(request: Request, user_id: int, tokens: dict) -> str | None:
        """Одразу після підключення «Сільпо»: найближчий магазин за приблизним місцем (з мережі) і анкета
        з «Сільпо» — лише порожні поля (обмеження в харчуванні не чіпаємо: на них потрібна згода).
        Повертає назву вибраного магазину або None, якщо магазин треба вибрати вручну."""
        p, consent = load_profile(user_id)
        link = silpo_link(user_id)
        title = link["store_title"] if link and link["store_id"] else None
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                if not title:
                    try:
                        lat = float(request.headers["x-vercel-ip-latitude"])
                        lon = float(request.headers["x-vercel-ip-longitude"])
                    except (KeyError, ValueError):
                        lat = lon = None
                    if lat is not None:
                        near = silpo.nearest(await silpo_branches(get_call), lat, lon, n=1)
                        if near and near[0]["km"] <= 30:
                            conn.execute("UPDATE silpo_links SET store_id=?, store_title=? WHERE user_id=?",
                                         (near[0]["id"], near[0]["title"][:120], user_id))
                            title = near[0]["title"]
                            analytics.track(conn, user_id, "store_selected", chain="silpo", store_id=near[0]["id"], auto=True)
                call = await get_call()
                changed = False
                if p.household <= 1:
                    hints = await silpo.profile_hints(call)
                    if hints.get("household"):
                        p.household, changed = hints["household"], True
                if not p.regulars:
                    buys = await silpo.purchase_hints(call)
                    if buys["regulars"]:
                        p.regulars, changed = buys["regulars"], True
                    if buys["brands"] and not p.brands_like:
                        p.brands_like, changed = buys["brands"], True
                if changed:
                    save_profile(user_id, p, consent)
        except Exception as e:
            log.warning("Сільпо: автоналаштування: %s", str(root_error(e))[:500])
        return title

    # ---------- смаки в конструкторі меню ----------
    def taste_dishes(p: Profile) -> list:
        """12 страв-зразків з фото, по одній на основний продукт; уже вподобані — першими."""
        photos = menu_photos()
        bad = p.excluded_tags()
        pool = [r for r in menu.RECIPES if "dinner" in r.meals and r.id not in p.dish_dislikes and not (menu.recipe_tags(r) & bad)]
        out = [r for r in pool if r.id in p.dish_likes][:12]
        for prot in TASTE_PROTEINS:
            if len(out) >= 12:
                break
            if any(r.protein == prot for r in out):
                continue
            pick = sorted((r for r in pool if r.protein == prot),
                          key=lambda r: (r.id not in photos, "класика" not in r.likes, r.minutes))
            if pick:
                out.append(pick[0])
        for r in sorted(pool, key=lambda r: (r.id not in photos, "класика" not in r.likes)):   # добираємо до 12
            if len(out) >= 12:
                break
            if r not in out:
                out.append(r)
        return [(r, photos.get(r.id), " ".join(sorted(menu.recipe_tags(r)))) for r in out]

    @app.get("/taste")
    def taste_page():
        """Старе посилання: смаки тепер — кроки конструктора меню."""
        return go("/profile")

    @app.get("/start", response_class=HTMLResponse)
    def start_page(request: Request):
        u, bad = need_user(request)
        if bad:
            return bad
        if silpo_link(u["id"]):
            return go("/menu")
        return render(request, "start.html", u)

    @app.post("/silpo/store")
    def silpo_store(request: Request, csrf: str = Form(""), store_id: str = Form(...), store_title: str = Form("")):
        u, bad = need_user(request, csrf)
        if bad:
            return bad
        conn.execute("UPDATE silpo_links SET store_id=?, store_title=? WHERE user_id=?", (store_id, store_title[:120], u["id"]))
        analytics.track(conn, u["id"], "store_selected", chain="silpo", store_id=store_id)
        resp = go(back_after_login(request, "/list"))     # магазин обрано — туди, звідки йшли підключати
        resp.delete_cookie(NEXT_COOKIE)
        return resp

    @app.post("/silpo/nearest")
    def silpo_nearest(request: Request, csrf: str = Form(""), lat: float = Form(...), lon: float = Form(...)):
        """Відкрили застосунок — перемикаємо «мій магазин» на найближчий до телефона (не далі 50 км)."""
        u, bad = need_user(request, csrf)
        if bad:
            return JSONResponse({"error": "csrf"}, status_code=400)
        link = silpo_link(u["id"])
        if not link or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return JSONResponse({"changed": False})
        near = near_stores(round(lat, 3), round(lon, 3), n=1)
        if not near:
            return JSONResponse({"changed": False})
        st = near[0]
        changed = st["id"] != link["store_id"]
        if changed:
            conn.execute("UPDATE silpo_links SET store_id=?, store_title=? WHERE user_id=?", (st["id"], st["title"][:120], u["id"]))
            analytics.track(conn, u["id"], "store_selected", chain="silpo", store_id=st["id"], auto=True)
        # «Акції» беруть магазини навколо збереженого місця (якщо людина колись дозволила його зберігати) — оновлюємо й його
        p, health = load_profile(u["id"])
        if p.lat is not None and silpo.distance_km(p.lat, p.lon, lat, lon) > 0.5:
            p.lat, p.lon, p.location_at = round(lat, 3), round(lon, 3), security.iso(security.now())
            save_profile(u["id"], p, health)
            changed = True
        return JSONResponse({"changed": changed, "store": st})

    @app.post("/silpo/disconnect")
    def silpo_disconnect(request: Request, csrf: str = Form("")):
        u, bad = need_user(request, csrf)
        if bad:
            return bad
        conn.execute("DELETE FROM silpo_links WHERE user_id=?", (u["id"],))
        return go("/silpo")

    # ---------- спільні ціни «Сільпо» (для гостей і тих, хто ще не підключив «Сільпо») ----------
    # «Сільпо» віддає ціни лише під входом. Кеш акцій магазину спільний; застарілий кеш популярних магазинів
    # оновлюємо через акаунт «донора» — першого адміністратора з підключеним «Сільпо».
    def donor_info() -> dict | None:
        """Службове підключення «Сільпо» з адмінки: {tokens, at, by}. Лише для цін інгредієнтів рецептів."""
        raw = db.get_setting(conn, "silpo_donor")
        data = json.loads(box.open(raw) or "null") if raw else None
        return data if isinstance(data, dict) and data.get("tokens") else None

    def donor_tokens() -> dict | None:
        d = donor_info()
        if not d:
            return None
        tokens = d["tokens"]
        if expires_soon(tokens):
            if not tokens.get("refresh_token"):
                return None
            try:
                tokens = gw.refresh(tokens)
            except Exception as e:
                logs.event(log, "silpo.donor_refresh_failed", logging.WARNING, err=f"{type(e).__name__}: {str(e)[:200]}")
                d["dead"] = security.iso(security.now())
                db.set_setting(conn, "silpo_donor", box.seal(json.dumps(d)))
                return None
            d["tokens"] = tokens
            d.pop("dead", None)
            db.set_setting(conn, "silpo_donor", box.seal(json.dumps(d)))
        return tokens

    def want_store(store_id: str) -> None:
        """Запам'ятовуємо, які магазини потрібні людям, — щоденне оновлення бере саме їх."""
        try:
            d = json.loads(db.get_setting(conn, "warm_demand") or "{}")
        except ValueError:
            d = {}
        now = security.iso(security.now())
        if d.get(store_id, "") >= security.iso(security.now() - timedelta(hours=6)):
            return
        d[store_id] = now
        old = security.iso(security.now() - timedelta(days=7))
        db.set_setting(conn, "warm_demand", json.dumps({k: v for k, v in d.items() if v >= old}))

    def shared_offers(p: Profile | None):
        """(магазин, акції, чи треба оновити) з кешу: найближчий з кешем ≤ доби, інакше найсвіжіший будь-який.
        Місце — з профілю, інакше за мережею. None — кешу немає зовсім."""
        names = {b["id"]: b for b in branches_cached()}
        place = (p.lat, p.lon) if p and p.lat is not None and p.lon is not None else cur_ip.get()
        near = near_stores(place[0], place[1], n=5) if place else []
        want = near[0] if near else None
        for st in near:
            hit = cached_offers(st["id"], STALE_TTL)
            if hit:
                fresh = cached_offers(st["id"]) is not None
                return dict(st, shared=True), hit, (want if (st is not want or not fresh) else None)
        r = conn.execute("SELECT store_id FROM offers_cache WHERE chain='silpo' AND fetched_at >= ? "
                         "ORDER BY fetched_at DESC LIMIT 1", (security.iso(security.now() - STALE_TTL),)).fetchone()
        if r and (hit := cached_offers(r["store_id"], STALE_TTL)):
            b = names.get(r["store_id"], {})
            st = {"id": r["store_id"], "title": b.get("title") or "Сільпо", "address": b.get("address") or b.get("title"),
                  "shared": True, "far": bool(place)}
            return st, hit, want
        return None, None, want

    async def warm_store(st: dict, timeout: float = 25) -> list[Offer] | None:
        """Свіжі акції магазину через акаунт донора; не частіше раз на 10 хв на магазин."""
        tokens = donor_tokens()
        if not tokens:
            return None
        key = f"warm_at:{st['id']}"
        if (db.get_setting(conn, key) or "") >= security.iso(security.now() - timedelta(minutes=10)):
            return None
        db.set_setting(conn, key, security.iso(security.now()))
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                got = await asyncio.wait_for(silpo.fetch_offers(await get_call(), st), timeout)
        except Exception as e:
            e = root_error(e)
            logs.event(log, "silpo.warm_failed", logging.WARNING, store=st["id"], err=f"{type(e).__name__}: {str(e)[:200]}")
            return None
        save_offers_cache(st["id"], got)
        return got

    async def guest_offers(p: Profile | None):
        """Акції для того, хто не підключив «Сільпо»: спільний кеш, за потреби — свіжі через донора, інакше демо.
        Повертає (stores, offers, is_demo)."""
        st, offers, want = shared_offers(p)
        if want:
            want_store(want["id"])
        if offers is None and want:        # кешу немає взагалі — один раз чекаємо на «Сільпо»
            got = await warm_store(want)
            if got:
                st, offers, want = dict(want, shared=True), got, None
        if offers is None:
            return [], demo.load(), True
        if want:
            warm_soon.set(want)
        return [st], [dict_store(o, st) for o in offers], False

    async def current_offers(user_id: int, p: Profile, refresh: bool):
        """Акції магазинів поруч (або демо, якщо «Сільпо» не підключено).
        Те, чого (за свіжою перевіркою) немає в магазині кошика людини, не показуємо."""
        link = silpo_link(user_id)
        stores, offers, notice = await silpo_list_input(user_id, p, force=refresh) if link else ([], [], None)
        is_demo = False
        if not offers and not notice:
            stores, offers, is_demo = await guest_offers(p)
        merged = planner.merge_nearby(offers)
        if offers and not is_demo and not stores[0].get("shared"):
            merged = drop_out_of_stock(user_id, link, merged)
        return link, stores, merged, notice, is_demo

    # ---------- наявність акцій у «моєму» магазині (де кошик «Сільпо») ----------
    STOCK_TTL = 2 * 3600
    cart_branch_mem: dict[int, tuple[float, str]] = {}            # uid → (коли, магазин кошика)
    stock_mem: dict[tuple[str, str], tuple[float, bool]] = {}     # (магазин, товар) → (коли, є в наявності)
    ctx_mem: dict[str, tuple[float, dict]] = {}                   # магазин → (коли, контекст пошуку зі слотом)

    def note_cart_branch(uid: int, cart) -> None:
        if cart is not None and getattr(cart, "branch_id", None):
            cart_branch_mem[uid] = (time.monotonic(), str(cart.branch_id))

    def home_branch(uid: int, link) -> str | None:
        """Магазин, де людина купує: магазин її кошика «Сільпо» (за останні 6 год), інакше вибраний вручну."""
        hit = cart_branch_mem.get(uid)
        if hit and time.monotonic() - hit[0] < 6 * 3600:
            return hit[1]
        return link["store_id"] if link and link["store_id"] else None

    def known_out(branch: str, pid: str) -> bool:
        hit = stock_mem.get((branch, pid))
        return bool(hit and not hit[1] and time.monotonic() - hit[0] < STOCK_TTL)

    def drop_out_of_stock(uid: int, link, offers: list[Offer]) -> list[Offer]:
        branch = home_branch(uid, link)
        return [o for o in offers if not known_out(branch, o.product_id)] if branch else offers

    @app.post("/offers/stock")
    async def offers_stock(request: Request):
        """Перевіряє, чи є показані акції в магазині кошика. → {"out": [productId…], "store": назва}.
        Немає в наявності, або пошук магазину інші товари знаходить, а цього — ні: вважаємо, що немає."""
        u = user_of(request)
        if u is None or not secrets.compare_digest(request.headers.get("x-csrf", ""), u["csrf"]):
            return JSONResponse({"out": []}, status_code=400)
        try:
            items = [(str(x["pid"])[:64], str(x.get("name") or "")[:200], bool(x.get("w")))
                     for x in ((await request.json()).get("items") or [])[:60] if x.get("pid")]
        except Exception:
            return JSONResponse({"out": []}, status_code=400)
        link = silpo_link(u["id"])
        tokens = silpo_tokens(u["id"]) if link else None
        if not tokens or not items:
            return {"out": []}
        now = time.monotonic()
        branch = home_branch(u["id"], link)
        fresh = lambda pid: (h := stock_mem.get((branch, pid))) and now - h[0] < STOCK_TTL
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                if branch is None:
                    cart = await silpo_cart.current_cart(await get_call())
                    note_cart_branch(u["id"], cart)
                    branch = home_branch(u["id"], link)
                    if branch is None:
                        return {"out": []}
                todo = [(pid, name, w) for pid, name, w in items if name and not fresh(pid)]
                if todo:
                    call = await get_call()
                    hit = ctx_mem.get(branch)
                    if not hit or now - hit[0] > 1800:
                        hit = ctx_mem[branch] = (now, await silpo.branch_context(call, branch))
                    found = await silpo_cart._search(call, dict(hit[1]), [name for _, name, _ in todo])
                    for pid, name, w in todo:
                        got = found.get(name.lower(), [])
                        same = next((x for x in got if str(silpo_cart._first(x, "id", "productId")) == pid), None)
                        if same is not None:
                            stock_mem[(branch, pid)] = (now, silpo_cart._stock_ok(same, 0.3 if w else 1))
                        elif got:
                            stock_mem[(branch, pid)] = (now, False)    # магазин інше знаходить, а цього немає
        except Exception as e:
            log.warning("Сільпо: перевірка наявності акцій: %s", str(root_error(e))[:300])
            return {"out": []}
        out = [pid for pid, _, _ in items if known_out(branch, pid)]
        logs.event(log, "offers.stock", checked=len(items), out=len(out))
        title = link["store_title"] if link and link["store_id"] == branch else None
        return {"out": out, "store": title}

    # ---------- список ----------
    PAGE = 20
    SORTS = {
        "rank": None,
        "disc": lambda o: -(o.discount_pct or 0),
        "save": lambda o: -((o.old_price or o.price) - o.price),
        "price": lambda o: o.price,
        "unit": lambda o: (u[0] if (u := o.unit_price()) and u[1] in ("кг", "л") else 1e9),
    }

    def feed_page(items: list, sort: str, cat: str, page: int) -> tuple[list, list, bool]:
        """Стрічка акцій: [(offer, причини)] → категорії (з усіх), сторінка після фільтра й сортування, чи є ще."""
        cats: list[str] = []
        for o, _ in items:
            c = offer_visual(o)["cat"]
            if c not in cats:
                cats.append(c)
        cats.sort(key=lambda c: CAT_ORDER.index(c) if c in CAT_ORDER else len(CAT_ORDER))
        if cat:
            items = [x for x in items if offer_visual(x[0])["cat"] == cat]
        key = SORTS.get(sort)
        if key:
            items = sorted(items, key=lambda x: key(x[0]))   # стабільне: за рівності лишається порядок «для вас»
        start = (max(1, page) - 1) * PAGE
        return cats, items[start:start + PAGE], len(items) > start + PAGE

    @app.get("/list", response_class=HTMLResponse)
    async def shopping_list(request: Request, refresh: int = 0, sort: str = "rank", cat: str = "", page: int = 1,
                            partial: int = 0):
        """Акції нескінченною стрічкою: перша сторінка — уся сторінка, далі (partial=1) лише картки."""
        sort = sort if sort in SORTS else "rank"
        u = user_of(request)
        if u is None or not silpo_link(u["id"]):    # акції — лише з власним підключеним «Сільпо»
            analytics.track(conn, u["id"] if u else None, "list_locked", guest=u is None)
            if partial:
                return HTMLResponse("")
            return render(request, "list_locked.html", u, guest=u is None)
        p, _ = load_profile(u["id"])
        link, stores, offers, notice, is_demo = await current_offers(u["id"], p, bool(refresh))
        if stores and stores[0].get("shared"):     # свій магазин ще не вибрано — службові ціни сюди не беремо
            stores, offers = [], []
        demo_photos(offers)
        matches, rejected = matcher.rank(offers, p)
        items = [(m.offer, m.reasons) for m in matches]
        ctx = dict(p=p, sl=planner.build(matches, p) if page == 1 and not partial else None, rejected=rejected[:40],
                   stale=offers_stale.get(), is_demo=False, link=link, notice=notice, stores=stores,
                   need_location=bool(link and p.lat is None and not link["store_id"]), guest=False)
        cats, chunk, more = feed_page(items, sort, cat, page)
        q = {"sort": sort, "cat": cat}
        next_url = f"/list?{urlencode(dict(q, page=page + 1, partial=1))}" if more else None
        if partial:
            return render(request, "_cards.html", u, items=chunk, next_url=next_url, **ctx)
        analytics.track(conn, u["id"], "list_refreshed" if refresh else "list_viewed", chain="silpo",
                        store_id=",".join(st["id"] for st in ctx["stores"])[:200] or None,
                        source="silpo", stores=len(ctx["stores"]), offers=len(items),
                        items=len(items), error=bool(ctx["notice"]), located=p.lat is not None)
        return render(request, "list.html", u, items=chunk, next_url=next_url, cats=cats, sort=sort, cat=cat,
                      total=len(items) if not cat else None, offers_total=len(items), **ctx)

    # ---------- меню ----------
    # Меню відкрите й без реєстрації: гість бачить демо-акції, його налаштування живуть у зашифрованій cookie.
    async def menu_offers(u, p: Profile, refresh: bool = False):
        if u:
            return await current_offers(u["id"], p, refresh)
        stores, offers, is_demo = await guest_offers(p)
        return None, stores, planner.merge_nearby(offers), None, is_demo

    def menu_done(u, p: Profile, health: bool, url: str):
        if u:
            save_profile(u["id"], p, health)
            return go(url)
        return keep_guest(go(url), p)

    def roll_week(p: Profile) -> bool:
        """Новий тиждень — нове меню, страви минулого тижня відсуваємо. True, якщо тиждень змінився."""
        week = "%d-W%02d" % date.today().isocalendar()[:2]
        if p.menu_week == week:
            return False
        if p.menu:
            p.menu_prev = p.menu
        p.menu, p.menu_skip, p.menu_week = [], [], week
        return True

    def prune_fridge(p: Profile) -> bool:
        """Прибирає з холодильника те, що зіпсувалося понад 2 дні тому. True, якщо щось прибрали."""
        old = date.today() - timedelta(days=2)
        gone = [k for k in p.fridge if (d := menu.use_by(k, p.fridge_since, p.fridge_until)[0]) and d < old]
        for k in gone:
            p.fridge.remove(k)
            p.fridge_since.pop(k, None)
            p.fridge_until.pop(k, None)
        return bool(gone)

    def week_menu(p: Profile, offers: list[Offer]):
        """Меню цього тижня (змінює p.menu*). Повертає (меню, чи змінився профіль)."""
        new_week = roll_week(p)
        if p.fridge and prune_fridge(p):        # зіпсоване давно — вже не «є вдома»
            new_week = True
        m = menu.plan(offers, p, dinners=p.dinners, max_minutes=p.max_minutes, budget=p.menu_budget,
                      keep=p.menu, pantry_buy=p.pantry_buy)
        changed = m.ids != p.menu or new_week
        p.menu = m.ids
        return m, changed

    @app.get("/buy", response_class=HTMLResponse)
    async def buy_page(request: Request, refresh: int = 0):
        """Крок ③ «Що купити»: список до меню цього тижня й кошик «Сільпо»."""
        return await menu_page(request, refresh, view="buy")

    @app.get("/menu", response_class=HTMLResponse)
    async def menu_page(request: Request, refresh: int = 0, view: str = "eat"):
        u = user_of(request)
        p, health = load_profile(u["id"]) if u else (guest_profile(request), False)
        link, stores, offers, notice, is_demo = await menu_offers(u, p, bool(refresh))
        m, changed = week_menu(p, offers)
        if changed and u:
            save_profile(u["id"], p, health)
        analytics.track(conn, u["id"] if u else None, "menu_viewed", source="demo" if is_demo else "silpo",
                        stores=len(stores), dishes=len(m.dishes), promo_lines=len(m.promo_lines), total=m.total,
                        saving=m.saving, guest=not u)
        resp = render(request, "menu.html", u, p=p, m=m, is_demo=is_demo, link=link, notice=notice, stores=stores,
                      days=day_labels(p), fmt_qty=menu.fmt_qty, ingredients=menu.INGREDIENTS, guest=not u,
                      stale=bool(u) and offers_stale.get(), ready=bool(request.query_params.get("ready")),
                      photos=menu_photos(), swapped=request.query_params.get("swapped"),
                      fresh=bool(request.query_params.get("fresh")),
                      view="buy" if view == "buy" else "eat", day_names=menu.DAYS,
                      sel_days=p.menu_days or list(range(p.dinners)))
        return keep_guest(resp, p) if (changed and not u) else resp

    async def menu_form(request: Request):
        """(form, user | None, profile, health, bad). Гість не має CSRF-токена: його захищає SameSite=Lax cookie."""
        form = await request.form()
        u = user_of(request)
        if u is None:
            return form, None, guest_profile(request), False, None
        if not secrets.compare_digest(str(form.get("csrf", "")), u["csrf"]):
            return form, u, None, False, HTMLResponse("Сторінка застаріла. Оновіть її й спробуйте ще раз.", status_code=400)
        p, health = load_profile(u["id"])
        return form, u, p, health, None

    @app.post("/menu/settings")
    async def menu_settings(request: Request):
        form, u, p, health, bad = await menu_form(request)
        if bad:
            return bad
        try:
            if form.get("people"):
                p.household = max(1, min(12, int(form["people"])))
            if form.get("dinners"):
                p.dinners = max(1, min(7, int(form["dinners"])))
                p.menu, p.menu_skip, p.menu_days = [], [], []
            if form.get("max_minutes"):
                p.max_minutes = max(15, min(180, int(form["max_minutes"])))
                p.menu, p.menu_skip = [], []
            if "days" in form:
                days = sorted({int(x) for x in str(form["days"]).split(",") if x.strip().isdigit() and int(x) <= 6})
                if days:
                    p.menu_days, p.dinners = days, len(days)
                    p.menu, p.menu_skip = [], []
            if "budget" in form:
                b = int(float(form["budget"] or 0))
                p.menu_budget = float(max(0, min(20000, b))) or None
                p.menu, p.menu_skip = [], []
        except ValueError:
            return HTMLResponse("Неправильні налаштування меню.", status_code=400)
        analytics.track(conn, u["id"] if u else None, "menu_settings", dinners=p.dinners, max_minutes=p.max_minutes,
                        people=p.household, guest=not u)
        return menu_done(u, p, health, "/menu")

    # ---------- конструктор меню: по одному питанню, меню складається наживо ----------
    def setup_apply(p: Profile, form) -> Profile:
        """Відповіді конструктора → профіль (без збереження)."""
        def num(key, lo, hi, default):
            try:
                return max(lo, min(hi, int(float(form.get(key) or default))))
            except ValueError:
                return default
        p.household = num("people", 1, 12, p.household)
        days = sorted({int(x) for x in str(form.get("days", "")).split(",") if x.strip().isdigit() and int(x) <= 6})
        if days:
            p.menu_days, p.dinners = days, len(days)
        p.max_minutes = num("minutes", 15, 180, p.max_minutes)
        budget = num("budget", 0, 20000, 0)
        p.menu_budget = float(budget) if budget else None
        if "likes" in form or "hide" in form:     # смаки тепер в анкеті (/taste); старі форми ще можуть їх надіслати
            tags = {t for t, _, _ in SETUP_TASTES}
            likes = [t for t in str(form.get("likes", "")).split(",") if t in tags]
            hide = [t for t in str(form.get("hide", "")).split(",") if t in SETUP_HIDE]
            p.likes = [t for t in p.likes if t not in tags] + likes
            p.dislikes = [t for t in p.dislikes if t not in SETUP_HIDE] + hide
        if "shown" in form:                       # «Улюблені страви»: позначені з показаних зразків
            shown = set(str(form.get("shown", "")).split(","))
            picked = [x for x in str(form.get("dishes", "")).split(",") if x in menu.RECIPE_BY_ID]
            p.dish_likes = (picked + [x for x in p.dish_likes if x not in shown and x not in picked])[:30]
            p.dish_dislikes = [x for x in p.dish_dislikes if x not in picked]
        return p

    def setup_offers(u) -> tuple[list[Offer], bool]:
        """Акції для живого прев'ю: лише кеш магазину (без запитів до «Сільпо») або демо."""
        offers: list[Offer] = []
        if u:
            link = silpo_link(u["id"])
            if link and link["store_id"]:
                offers = cached_offers(link["store_id"], STALE_TTL) or []
        return (planner.merge_nearby(offers), False) if offers else (planner.merge_nearby(demo.load()), True)

    @app.get("/menu/setup")
    def menu_setup_moved():
        """Конструктор замінено чипами на сторінці меню — старі посилання ведуть туди."""
        return go("/menu")

    @app.get("/menu/setup-old", response_class=HTMLResponse)
    def menu_setup_page(request: Request):
        u = user_of(request)
        p = load_profile(u["id"])[0] if u else guest_profile(request)
        days = p.menu_days if len(p.menu_days) == p.dinners else list(range(p.dinners))
        try:
            step = max(0, min(5, int(request.query_params.get("step", 0))))
        except ValueError:
            step = 0
        return render(request, "setup.html", u, p=p, days=days, day_names=menu.DAYS, tastes=SETUP_TASTES,
                      hideable=SETUP_HIDE, dishes=taste_dishes(p), step=step,
                      first=bool(request.query_params.get("first")), guest=not u)

    @app.post("/menu/preview")
    async def menu_preview(request: Request):
        """Живий підсумок у конструкторі: страви й ціна за поточними відповідями. Нічого не зберігає."""
        form = await request.form()
        u = user_of(request)
        p = load_profile(u["id"])[0] if u else guest_profile(request)
        p = setup_apply(p, form)
        offers, is_demo = setup_offers(u)
        p.menu, p.menu_skip = [], []
        m = menu.plan(offers, p, dinners=p.dinners, max_minutes=p.max_minutes, budget=p.menu_budget, keep=[])
        photos = menu_photos()
        labels = day_labels(p)
        return JSONResponse({
            "dishes": [{"id": d.id, "title": d.title, "emoji": d.emoji, "photo": photos.get(d.id), "day": labels[i],
                        "price": round(d.per_portion), "save": round(d.saving)} for i, d in enumerate(m.dishes)],
            "per_portion": round(m.per_portion), "total": round(m.total), "saving": round(m.saving),
            "pool": len([r for r in menu.RECIPES + menu.user_recipes(p) if menu.allowed(r, p, p.max_minutes)]),
            "demo": is_demo})

    @app.post("/menu/setup")
    async def menu_setup_save(request: Request):
        form, u, p, health, bad = await menu_form(request)
        if bad:
            return bad
        p = setup_apply(p, form)
        p.menu, p.menu_skip = [], []
        p.taste_done = True
        analytics.track(conn, u["id"] if u else None, "menu_setup", people=p.household, dinners=p.dinners,
                        max_minutes=p.max_minutes, budget=p.menu_budget, likes=len(p.likes), guest=not u)
        return menu_done(u, p, health, "/menu?fresh=1")

    @app.post("/menu/swap")
    async def menu_swap(request: Request):
        form, u, p, health, bad = await menu_form(request)
        if bad:
            return bad
        try:
            i = int(form.get("i", -1))
        except ValueError:
            return HTMLResponse("Неправильна страва.", status_code=400)
        _, _, offers, _, _ = await menu_offers(u, p)
        current = p.menu or menu.plan(offers, p, dinners=p.dinners, max_minutes=p.max_minutes).ids
        new = menu.swap(offers, p, current, i, max_minutes=p.max_minutes, skip=p.menu_skip)
        if new is None:
            return go("/menu#d%d" % max(i, 0))
        # пам'ятаємо всі замінені й показані за тиждень (гостю — менше: усе живе в cookie)
        seen = [current[i], new[i]] + [x for x in p.menu_skip if x not in (current[i], new[i])]
        p.menu_skip = seen[:200 if u else 40]
        p.menu = new
        analytics.track(conn, u["id"] if u else None, "menu_swap", dish_from=current[i], dish_to=new[i], guest=not u)
        return menu_done(u, p, health, f"/menu?swapped={i}#d{i}")

    # ---------- оцінки рецептів: 👍 / 👎 (дизлайки бачить лише адмінка) ----------
    VOTER_COOKIE = "vid"

    def apply_vote(p: Profile, rid: str, vote: int) -> None:
        """Голос → профіль: 👍 частіше в підборі, 👎 більше не пропонуємо, 0 — нейтрально."""
        p.dish_likes = [x for x in p.dish_likes if x != rid]
        p.dish_dislikes = [x for x in p.dish_dislikes if x != rid]
        if vote > 0:
            p.dish_likes = ([rid] + p.dish_likes)[:30]
        elif vote < 0:
            p.dish_dislikes = ([rid] + p.dish_dislikes)[:400]     # свайпи «не моє» — пам'ятаємо надовго

    def record_vote(voter: str, rid: str, vote: int) -> None:
        conn.execute("DELETE FROM recipe_votes WHERE voter=? AND recipe_id=?", (voter, rid))
        if vote:
            conn.execute("INSERT INTO recipe_votes (voter, recipe_id, vote, at) VALUES (?, ?, ?, ?)",
                         (voter, rid, 1 if vote > 0 else -1, security.iso(security.now())))

    def likes_of(rid: str) -> int:
        r = conn.execute("SELECT COUNT(*) AS n FROM recipe_votes WHERE recipe_id=? AND vote=1", (rid,)).fetchone()
        return int(r["n"] or 0)

    def vote_stats() -> list[dict]:
        rows = conn.execute("SELECT recipe_id, SUM(CASE WHEN vote=1 THEN 1 ELSE 0 END) AS up, "
                            "SUM(CASE WHEN vote=-1 THEN 1 ELSE 0 END) AS down, MAX(at) AS last "
                            "FROM recipe_votes GROUP BY recipe_id").fetchall()
        out = [{"id": r["recipe_id"], "title": (menu.RECIPE_BY_ID[r["recipe_id"]].title if r["recipe_id"] in menu.RECIPE_BY_ID else r["recipe_id"]),
                "up": int(r["up"] or 0), "down": int(r["down"] or 0), "last": (r["last"] or "")[:10]} for r in rows]
        for x in out:
            x["score"] = x["up"] - x["down"]
        return sorted(out, key=lambda x: (-(x["up"] + x["down"]), -x["score"]))

    tpl.env.globals.update(likes_of=likes_of, recipe_ids=set(menu.RECIPE_BY_ID))

    @app.post("/recipe/vote")
    async def recipe_vote(request: Request):
        """👍 / 👎 рецепту (повторне натискання знімає голос). JSON, без перезавантаження."""
        form, u, p, health, bad = await menu_form(request)
        if bad:
            return JSONResponse({"error": "Оновіть сторінку"}, status_code=400)
        rid = str(form.get("id", ""))
        if rid not in menu.RECIPE_BY_ID:
            return JSONResponse({"error": "Невідома страва"}, status_code=400)
        vote = {"up": 1, "down": -1}.get(str(form.get("vote", "")), 0)
        vid = request.cookies.get(VOTER_COOKIE) or ""
        new_vid = None
        if not u and not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", vid):
            vid = new_vid = secrets.token_urlsafe(16)
        voter = f"u:{u['id']}" if u else f"g:{vid}"
        apply_vote(p, rid, vote)
        record_vote(voter, rid, vote)
        analytics.track(conn, u["id"] if u else None, "dish_vote", dish=rid, vote=vote, guest=not u)
        resp = JSONResponse({"vote": {1: "up", -1: "down"}.get(vote, ""), "likes": likes_of(rid)})
        if new_vid:
            resp.set_cookie(VOTER_COOKIE, new_vid, max_age=365 * 86400, httponly=True, samesite="lax", secure=secure)
        if u:
            save_profile(u["id"], p, health)
            return resp
        return keep_guest(resp, p)

    @app.post("/menu/like")
    async def menu_like(request: Request):
        """♡ страві: вподобані частіше потрапляють у меню. Відповідь — JSON, сторінка не перезавантажується."""
        form, u, p, health, bad = await menu_form(request)
        if bad:
            return JSONResponse({"error": "Оновіть сторінку"}, status_code=400)
        rid = str(form.get("id", ""))
        if rid not in menu.RECIPE_BY_ID and rid not in {r.id for r in menu.user_recipes(p)}:
            return JSONResponse({"error": "Невідома страва"}, status_code=400)
        liked = rid not in p.dish_likes
        p.dish_likes = ([rid] + [x for x in p.dish_likes if x != rid])[:30] if liked else [x for x in p.dish_likes if x != rid]
        if liked:
            p.dish_dislikes = [x for x in p.dish_dislikes if x != rid]
        analytics.track(conn, u["id"] if u else None, "dish_like" if liked else "dish_unlike", dish=rid, guest=not u)
        if u and rid in menu.RECIPE_BY_ID:
            record_vote(f"u:{u['id']}", rid, 1 if liked else 0)
        resp = JSONResponse({"liked": liked, "count": len(p.dish_likes)})
        if u:
            save_profile(u["id"], p, health)
            return resp
        return keep_guest(resp, p)

    @app.post("/menu/pantry")
    async def menu_pantry(request: Request):
        form, u, p, health, bad = await menu_form(request)
        if bad:
            return bad
        key = str(form.get("key", ""))
        if key not in menu.PANTRY:
            return HTMLResponse("Невідомий продукт.", status_code=400)
        p.pantry_buy = [k for k in p.pantry_buy if k != key] if key in p.pantry_buy else p.pantry_buy + [key]
        return menu_done(u, p, health, "/buy")

    @app.post("/menu/have")
    async def menu_have(request: Request):
        """«Немає» біля продукту з «Є вдома» (op=remove) і «Повернути» (op=add) — без перезавантаження сторінки."""
        form, u, p, health, bad = await menu_form(request)
        if bad:
            return bad
        key = str(form.get("key", ""))
        if key not in menu.INGREDIENTS:
            return HTMLResponse("Невідомий продукт.", status_code=400)
        if form.get("op") == "add":
            if key not in p.fridge:
                p.fridge = sorted(set(p.fridge) | {key})
                p.fridge_since[key] = date.today().isoformat()
                p.fridge_at = security.iso(security.now())
        else:
            p.fridge = [k for k in p.fridge if k != key]
            p.fridge_since.pop(key, None)
            p.fridge_until.pop(key, None)
            if not p.fridge and not p.fridge_other:
                p.fridge_at = None
        if u:
            analytics.track(conn, u["id"], "fridge_removed" if form.get("op") != "add" else "fridge_restored", key=key)
        return menu_done(u, p, health, "/buy")

    # ---------- перегляд кошика «Сільпо» ----------
    @app.get("/silpo/cart", response_class=HTMLResponse)
    async def silpo_cart_view(request: Request, error: str = "", changed: str = "", cleared: int = 0):
        u = user_of(request)
        if u is None:
            return render(request, "silpo_cart.html", None, cart=None, connect="register")
        link = silpo_link(u["id"])
        tokens = silpo_tokens(u["id"]) if link else None
        if not tokens:
            return render(request, "silpo_cart.html", u, cart=None, connect="silpo" if not link else "again")
        cart, err = None, None
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                cart = await silpo_cart.current_cart(await get_call())
                if silpo_cart.slot_expired(cart):     # старе вікно → «Сільпо» показує все як «немає в наявності»
                    cart = await silpo_cart.ensure_fresh_slot(await get_call(), cart,
                                                              {b["id"]: b for b in await silpo_branches(get_call)})
            note_cart_branch(u["id"], cart)
        except Exception as e:
            e = root_error(e)
            log.warning("Сільпо: перегляд кошика: %s: %s", type(e).__name__, str(e)[:800])
            err = f"«Сільпо» не відповіло: {str(e)[:150]}"
        analytics.track(conn, u["id"], "silpo_cart_viewed", items=cart.count if cart else 0)
        if cart and (cart.checkout or cart.checkout_mobile):
            logs.event(log, "silpo.checkout_link", web=link_shape(cart.checkout), mobile=link_shape(cart.checkout_mobile),
                       inapp=in_app_browser(request) or None)
        resp = render(request, "silpo_cart.html", u, cart=cart, error=err, notice=error[:300] or None, changed=changed,
                      slot_old=silpo_cart.slot_expired(cart),
                      store=link["store_title"], inapp=in_app_browser(request),
                      cleared=bool(cleared))
        return remember_cart(resp, cart.count if cart else 0, u["id"]) if not err else resp

    @app.post("/silpo/cart/change")
    async def silpo_cart_change(request: Request):
        form = await request.form()
        u, bad = need_user(request, str(form.get("csrf", "")))
        if bad:
            return bad
        tokens = silpo_tokens(u["id"]) if silpo_link(u["id"]) else None
        if not tokens:
            return go("/silpo")
        line_id, action = str(form.get("line", "")), str(form.get("action", ""))
        try:
            qty = float(str(form.get("quantity", "0")).replace(",", "."))
        except ValueError:
            return HTMLResponse("Неправильна кількість.", status_code=400)
        if action not in ("set", "remove") or not line_id or not (0 <= qty <= 50):
            return HTMLResponse("Неправильна дія з кошиком.", status_code=400)
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                call = await get_call()
                cart = await silpo_cart.current_cart(call)
                if cart is None:
                    return go("/silpo/cart")
                if action == "remove" or qty == 0:
                    await silpo_cart.remove_line(call, cart, line_id)
                else:
                    await silpo_cart.set_quantity(call, cart, line_id, round(qty, 3))
        except Exception as e:
            e = root_error(e)
            log.warning("Сільпо: зміна кошика: %s: %s", type(e).__name__, str(e)[:800])
            msg = str(e) if isinstance(e, silpo_cart.CartError) else f"«Сільпо» не прийняло зміну: {str(e)[:150]}"
            return go("/silpo/cart?error=" + quote(msg))
        analytics.track(conn, u["id"], "silpo_cart_changed", action=action)
        cart_counts.pop(u["id"], None)
        return go(f"/silpo/cart?changed={quote(line_id)}#l-{quote(line_id)}")

    @app.post("/silpo/cart/add")
    async def silpo_cart_add(request: Request):
        """«+ У кошик» з картки акції: кладемо одну упаковку (вагове — 0,5 кг) у кошик «Сільпо». Відповідь — JSON."""
        form = await request.form()
        u = user_of(request)
        if u is None:
            return JSONResponse({"error": "Увійдіть", "go": "/register"}, status_code=401)
        if not secrets.compare_digest(str(form.get("csrf", "")), u["csrf"]):
            return JSONResponse({"error": "Оновіть сторінку"}, status_code=400)
        link = silpo_link(u["id"])
        tokens = silpo_tokens(u["id"]) if link else None
        if not tokens:
            return JSONResponse({"error": "Підключіть «Сільпо»", "go": "/silpo"}, status_code=409)
        pid, name = str(form.get("pid", "")).strip(), str(form.get("name", ""))[:200]
        weighted = form.get("weighted") == "1"
        if not pid or len(pid) > 64:
            return JSONResponse({"error": "Невідомий товар"}, status_code=400)
        want = silpo_cart.Want(pid, name or "товар", 500 if weighted else 1, "g" if weighted else "pcs",
                               product_id=pid, packs=1, weighted=weighted)
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                branch = None
                if link["store_id"]:
                    known = {b["id"]: b for b in await silpo_branches(get_call)}
                    branch = known.get(link["store_id"]) or {"id": link["store_id"], "title": link["store_title"]}
                res = await silpo_cart.build(await get_call(), [want], branch,
                                             branches={b["id"]: b for b in await silpo_branches(get_call)})
        except Exception as e:
            err = root_error(e)
            log.warning("Сільпо: + у кошик: %s: %s", type(err).__name__, str(err)[:500])
            msg = str(err) if isinstance(err, silpo_cart.CartError) else "«Сільпо» не відповіло"
            return JSONResponse({"error": msg[:200]}, status_code=502)
        if not res["placed"]:
            if res.get("branch_id"):
                stock_mem[(str(res["branch_id"]), pid)] = (time.monotonic(), False)
            return JSONResponse({"error": "Немає й схожого"}, status_code=409)
        swap = res["replaced"][0][1] if res["replaced"] else None
        if res.get("branch_id"):
            cart_branch_mem[u["id"]] = (time.monotonic(), str(res["branch_id"]))
            if swap or not res["placed"]:          # акційного в магазині кошика немає — більше не показуємо
                stock_mem[(str(res["branch_id"]), pid)] = (time.monotonic(), False)
        analytics.track(conn, u["id"], "deal_added", chain="silpo", product_id=pid, replaced=bool(swap))
        return remember_cart(JSONResponse({"ok": True, "count": res["count"], "replaced": swap}), res["count"], u["id"])

    @app.post("/silpo/cart/clear")
    async def silpo_cart_clear(request: Request, csrf: str = Form("")):
        u, bad = need_user(request, csrf)
        if bad:
            return bad
        tokens = silpo_tokens(u["id"]) if silpo_link(u["id"]) else None
        if not tokens:
            return go("/silpo")
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                call = await get_call()
                cart = await silpo_cart.current_cart(call)
                if cart is not None and cart.count:
                    await silpo_cart.clear(call, cart)
        except Exception as e:
            e = root_error(e)
            log.warning("Сільпо: очищення кошика: %s: %s", type(e).__name__, str(e)[:800])
            return go("/silpo/cart?error=" + quote(f"«Сільпо» не очистило кошик: {str(e)[:150]}"))
        analytics.track(conn, u["id"], "silpo_cart_cleared")
        return remember_cart(go("/silpo/cart?cleared=1"), 0, u["id"])

    @app.get("/silpo/cart/count")
    async def silpo_cart_count(request: Request):
        """Для значка в шапці: скільки товарів у кошику «Сільпо». Кеш на хвилину, щоб не смикати «Сільпо» щоразу."""
        u = user_of(request)
        if not u:
            return JSONResponse({"n": None})
        hit = cart_counts.get(u["id"])
        if hit and time.monotonic() - hit[0] < 60:
            return remember_cart(JSONResponse({"n": hit[1]}), hit[1], u["id"])
        tokens = silpo_tokens(u["id"]) if silpo_link(u["id"]) else None
        if not tokens:
            return JSONResponse({"n": None})
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                n = await silpo_cart.count(await get_call())
        except Exception as e:
            log.warning("Сільпо: кількість у кошику: %s", str(root_error(e))[:300])
            return JSONResponse({"n": None})
        return remember_cart(JSONResponse({"n": n}), n, u["id"])

    # ---------- кошик «Сільпо» з меню ----------
    @app.post("/menu/silpo-cart", response_class=HTMLResponse)
    async def menu_silpo_cart(request: Request):
        form = await request.form()
        u, bad = need_user(request, str(form.get("csrf", "")))
        if bad:
            return bad
        p, _ = load_profile(u["id"])
        link = silpo_link(u["id"])
        tokens = silpo_tokens(u["id"]) if link else None
        if not tokens:
            return go("/silpo")
        _, stores, offers, notice, is_demo = await current_offers(u["id"], p, False)
        if is_demo:
            return go("/silpo")
        m = menu.plan(offers, p, dinners=p.dinners, max_minutes=p.max_minutes, budget=p.menu_budget,
                      keep=p.menu, pantry_buy=p.pantry_buy)
        p_save, health_save = load_profile(u["id"])
        remember_cooking(p_save, m.ids, m.people, "week")
        save_profile(u["id"], p_save, health_save)
        bought = {k for k in str(form.get("bought", "")).split(",") if k}
        wants = silpo_cart.wants_from_menu(m.cart, bought)
        wants += [silpo_cart.Want("x:" + x, x, 1, "pcs") for x in m.extras if "x:" + x not in bought]   # з моїх страв
        ctx = dict(bought=",".join(sorted(bought)), wanted=len(wants))
        if not wants:
            return render(request, "cart.html", u, error="Усе зі списку вже позначено як куплене — додавати нічого.", **ctx)
        return await put_in_silpo_cart(request, u, form, tokens, stores, wants, ctx, "silpo_cart_built", m.saving)

    async def put_in_silpo_cart(request: Request, u, form, tokens: dict, stores: list, wants: list, ctx: dict,
                                event: str, saving: float, **extra):
        """Кладе товари в кошик «Сільпо»: спершу попереджає про наявний кошик, потім додає. Спільне для меню й «Сьогодні»."""
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                call = await get_call()
                if not form.get("confirm"):
                    cart = await silpo_cart.current_cart(call)
                    if cart and cart.count:
                        sample = [n for n in cart.names.values() if n][:5]
                        return render(request, "cart.html", u, confirm=True, had=cart.count, sample=sample, **ctx)
                branch = None
                if stores:   # магазин для самовивозу, якщо кошика ще немає: найближчий, з координатами
                    branch = dict(stores[0])
                    if branch.get("lat") is None:
                        known = {b["id"]: b for b in await silpo_branches(get_call)}
                        branch = {**known.get(branch["id"], {}), **{k: v for k, v in branch.items() if v is not None}}
                res = await silpo_cart.build(call, wants, branch,
                                             branches={b["id"]: b for b in await silpo_branches(get_call)})
        except Exception as e:
            err = root_error(e)
            if isinstance(err, silpo_cart.CartError):
                return render(request, "cart.html", u, error=str(err), **ctx)
            log.warning("Сільпо: кошик не зібрався: %s: %s", type(err).__name__, str(err)[:1500])
            return render(request, "cart.html", u, status=502, **ctx,
                          error=f"«Сільпо» відповіло помилкою: {str(err)[:200]}. Спробуйте ще раз за хвилину.")
        if res["placed"]:
            n_pl = len(res["placed"])
            await asyncio.to_thread(push_user, u["id"], "🛒 Кошик у «Сільпо» зібрано",
                                    f"{n_pl} {plural(n_pl, 'товар', 'товари', 'товарів')} уже в кошику. Оплатіть — рецепти збережено.",
                                    "/silpo/cart", "cart")
        chat = tg_chat(u["id"])
        if chat and res["placed"]:
            await asyncio.to_thread(tg.send, chat["chat_id"],
                                    f"🛒 Кошик у «Сільпо» зібрано: {len(res['placed'])} "
                                    f"{plural(len(res['placed']), 'товар', 'товари', 'товарів')}.\n"
                                    "Після оплати повертайтесь — рецепти вже збережено 🍳",
                                    [("Мої рецепти", f"{base_url}/cook?bought=1")])
        analytics.track(conn, u["id"], event, placed=len(res["placed"]), missing=len(res["missing"]),
                        had=res["had"], total=res["total"], checkout=bool(res["checkout"]), saving=round(saving, 2), **extra)
        return remember_cart(render(request, "cart.html", u, res=res, **ctx), res["count"], u["id"])

    @app.get("/photos", response_class=HTMLResponse)
    def photo_credits(request: Request):
        menu_photos()
        return render(request, "photos.html", user_of(request), credits=menu_credits(),
                      status_note=f"{db.get_setting(conn, unsplash.ERR_KEY)} · спроба {db.get_setting(conn, unsplash.TRY_KEY)} · ключ {'є' if unsplash.access_key() else 'немає'}",
                      unsplash={d: dict(p, by_url=unsplash.with_utm(p["by_url"]), img=unsplash.img_url(p["raw"], 192, 144))
                                for d, p in unsplash.load(conn).items() if d not in menu_credits() and unsplash.usable(d, p)},
                      unsplash_home=unsplash.with_utm("https://unsplash.com"),
                      titles={**{r.id: r.title for r in menu.RECIPES}, **{"p:" + o.product_id: o.name for o in demo.load()}})

    def branches_cached() -> list[dict]:
        """Магазини «Сільпо» з кешу (будь-якої давності — адреси змінюються рідко). Без запитів до «Сільпо»."""
        if branches_mem:
            return branches_mem[0]["items"]
        raw = db.get_setting(conn, "silpo_branches")
        try:
            return json.loads(raw)["items"] if raw else []
        except (ValueError, KeyError):
            return []

    def ip_place(request: Request):
        """Приблизне місце за мережею (заголовки Vercel): (lat, lon, місто) або None."""
        try:
            return (float(request.headers["x-vercel-ip-latitude"]), float(request.headers["x-vercel-ip-longitude"]),
                    unquote(request.headers.get("x-vercel-ip-city", "")) or None)
        except (KeyError, ValueError):
            return None

    def near_stores(lat: float, lon: float, n: int = 3) -> list[dict]:
        # далі 50 км — це не «поруч» (закордон, VPN): нічого не показуємо
        return [{"id": b["id"], "title": b["title"], "address": b.get("address") or b["title"], "km": b["km"]}
                for b in silpo.nearest(branches_cached(), lat, lon, n=n) if b["km"] <= NEAR_MAX_KM]

    @app.get("/near")
    def near(request: Request, lat: float | None = None, lon: float | None = None):
        """Найближчі магазини «Сільпо» до точки (з телефона) або за мережею. Координати не зберігаємо."""
        src, city = "gps", None
        if lat is None or lon is None:
            ip = ip_place(request)
            if not ip:
                return JSONResponse({"stores": []}, headers={"Cache-Control": "no-store"})
            lat, lon, city = ip
            src = "ip"
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return JSONResponse({"error": "bad coords"}, status_code=400)
        lat, lon = round(lat, 3), round(lon, 3)
        stores = near_stores(lat, lon)
        u = user_of(request)
        analytics.track(conn, u["id"] if u else None, "near_lookup", src=src, found=len(stores), guest=not u)
        return JSONResponse({"src": src, "city": city, "lat": lat, "lon": lon, "stores": stores},
                            headers={"Cache-Control": "no-store"})

    @app.post("/location")
    def location_save(request: Request, csrf: str = Form(""), lat: float = Form(...), lon: float = Form(...),
                      consent: str = Form("")):
        u, bad = need_user(request, csrf)
        if bad:
            return bad
        if not consent:
            return go("/list")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return HTMLResponse("Неправильні координати.", status_code=400)
        p, health = load_profile(u["id"])
        # ~110 м точності: достатньо, щоб знайти магазини поруч, і не зберігаємо точну адресу
        p.lat, p.lon, p.location_at = round(lat, 3), round(lon, 3), security.iso(security.now())
        save_profile(u["id"], p, health)
        analytics.track(conn, u["id"], "location_set")
        return go("/list")

    @app.post("/location/clear")
    def location_clear(request: Request, csrf: str = Form("")):
        u, bad = need_user(request, csrf)
        if bad:
            return bad
        p, health = load_profile(u["id"])
        p.lat = p.lon = p.location_at = None
        save_profile(u["id"], p, health)
        return go("/list")

    @app.get("/go")
    def go_offer(request: Request):
        """Перехід на товар мережі: рахуємо клік і перенаправляємо на підписану адресу."""
        q = verify_click(app_secret, dict(request.query_params))
        if not q:
            return HTMLResponse("Посилання пошкоджене.", status_code=400)
        u = user_of(request)
        analytics.track(conn, u["id"] if u else None, "offer_click", chain=q["c"], store_id=q["s"] or None,
                        product_id=q["p"], category=q["k"] or None, discount=float(q["d"] or 0))
        return RedirectResponse(q["u"], status_code=302)

    def mask_email(e: str) -> str:
        name, _, dom = e.partition("@")
        return (name[:1] + "•••" + (name[-1:] if len(name) > 2 else "")) + "@" + dom

    def admin_users(limit: int = 300) -> list[dict]:
        """Користувачі для адмінки. Без даних про здоров'я (дієти) і без токенів."""
        since = security.iso(security.now() - timedelta(days=30))
        act = {r["user_id"]: (r["last"], int(r["n"] or 0)) for r in conn.execute(
            "SELECT user_id, MAX(ts) AS last, SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS n FROM events "
            "WHERE user_id IS NOT NULL GROUP BY user_id", (since,)).fetchall()}
        silpo_ = {r["user_id"]: r["store_title"] or "підключено" for r in conn.execute("SELECT user_id, store_title FROM silpo_links").fetchall()}
        push = {r["user_id"]: int(r["n"]) for r in conn.execute("SELECT user_id, COUNT(*) AS n FROM push_subs GROUP BY user_id").fetchall()}
        tg = {r["user_id"] for r in conn.execute("SELECT user_id FROM tg_links").fetchall()}
        votes = {r["voter"]: int(r["n"]) for r in conn.execute("SELECT voter, COUNT(*) AS n FROM recipe_votes GROUP BY voter").fetchall()}
        out = []
        for r in conn.execute("SELECT u.id, u.email, u.pw_hash, u.created_at, p.data FROM users u LEFT JOIN profiles p ON p.user_id = u.id "
                              "ORDER BY u.id DESC LIMIT ?", (limit,)).fetchall():
            try:
                d = json.loads(r["data"]) if r["data"] else {}
            except ValueError:
                d = {}
            last, n30 = act.get(r["id"], (None, 0))
            out.append({"id": r["id"], "email": r["email"], "masked": mask_email(r["email"]),
                        "via": "Google" if r["pw_hash"] == "google" else "Email", "since": (r["created_at"] or "")[:10],
                        "last": (last or "")[:10], "n30": n30, "silpo": silpo_.get(r["id"], ""),
                        "notify": ", ".join(x for x in (f"push×{push[r['id']]}" if r["id"] in push else "", "Telegram" if r["id"] in tg else "") if x),
                        "people": d.get("household") or "", "menu": len(d.get("menu") or []), "cooking": len(d.get("cooking") or []),
                        "likes": len(d.get("dish_likes") or []), "votes": votes.get(f"u:{r['id']}", 0),
                        "fridge": len(d.get("fridge") or []), "my_dishes": len(d.get("my_dishes") or [])})
        return out

    def admin_only(request: Request):
        """(user, None) для адміна; інакше (None, відповідь): вхід або відмова."""
        u = user_of(request)
        if u is None:
            resp = go("/login?admin=1")
            resp.set_cookie(NEXT_COOKIE, request.url.path, max_age=900, httponly=True, samesite="lax", secure=secure)
            return None, resp
        if u["email"].lower() not in admin_emails:
            logs.event(log, "admin.denied", logging.WARNING)
            return None, render(request, "auth.html", mode="login", status=403,
                                error="Адмінка доступна лише власнику сайту. Увійдіть під акаунтом адміністратора.")
        return u, None

    @app.get("/admin/users.csv")
    def admin_users_csv(request: Request):
        u, bad = admin_only(request)
        if bad:
            return bad
        import csv
        import io
        buf = io.StringIO()
        rows = admin_users(limit=100000)
        cols = ["id", "email", "via", "since", "last", "n30", "silpo", "notify", "people", "menu", "cooking", "likes", "votes", "fridge", "my_dishes"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        logs.event(log, "admin.export_users", rows=len(rows))
        return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=users.csv", "Cache-Control": "no-store"})

    @app.get("/admin/stats", response_class=HTMLResponse)
    def admin_stats(request: Request, days: int = 30):
        u, bad = admin_only(request)
        if bad:
            return bad
        return render(request, "stats.html", u, st=analytics.stats(conn, days=max(7, min(90, days))), votes=vote_stats(),
                      users=admin_users(), feats=analytics.features(conn), donor=donor_status(), silpo_error=request.query_params.get("silpo_error", "")[:200])

    # ---------- «Готую»: рецепти страв, які людина обрала (сьогодні або на тиждень) ----------
    COOK_TTL = timedelta(days=14)

    def remember_cooking(p: Profile, ids: list[str], n: int, src: str) -> None:
        """Додає рецепти нагору списку «Готую» (без повторів), старші за 2 тижні забуваємо."""
        now = security.iso(security.now())
        old = security.iso(security.now() - COOK_TTL)
        keep = [c for c in p.cooking if c.get("id") not in ids and c.get("at", "") >= old]
        p.cooking = ([{"id": i, "n": n, "at": now, "src": src} for i in ids] + keep)[:21]

    def cooking_items(p: Profile) -> list[dict]:
        """[{c, recipe, dish}] — для сторінки «Готую»; рецепти, яких уже немає, пропускаємо."""
        mine = {r.id: r for r in menu.user_recipes(p)}
        out = []
        for c in p.cooking:
            r = menu.RECIPE_BY_ID.get(c.get("id")) or mine.get(c.get("id"))
            if r:
                out.append({"c": c, "recipe": r, "dish": menu.Dish(r, max(1, int(c.get("n") or 2)))})
        return out

    def save_any(request: Request, u, p: Profile, health: bool, resp):
        """Зберегти профіль: у базу для користувача або в cookie для гостя."""
        if u:
            save_profile(u["id"], p, health)
            return resp
        return keep_guest(resp, p)

    @app.get("/cook", response_class=HTMLResponse)
    def cook_page(request: Request):
        u = user_of(request)
        p = load_profile(u["id"])[0] if u else guest_profile(request)
        return render(request, "cook.html", u, items=cooking_items(p), photos=menu_photos(),
                      bought=bool(request.query_params.get("bought")))

    @app.get("/cook/{rid}", response_class=HTMLResponse)
    def cook_recipe(request: Request, rid: str):
        u = user_of(request)
        p = load_profile(u["id"])[0] if u else guest_profile(request)
        it = next((x for x in cooking_items(p) if x["recipe"].id == rid), None)
        if it is None:          # рецепт, якого немає у списку, теж показуємо — на 2 людей
            r = menu.RECIPE_BY_ID.get(rid) or {x.id: x for x in menu.user_recipes(p)}.get(rid)
            if not r:
                return go("/cook")
            it = {"c": {"id": rid, "n": p.household or 2}, "recipe": r, "dish": menu.Dish(r, p.household or 2)}
        return render(request, "cook_recipe.html", u, it=it, p=p, photos=menu_photos(), ingredients=menu.INGREDIENTS,
                      fmt_qty=menu.fmt_qty, saved=any(c.get("id") == rid for c in p.cooking),
                      src=next((d.get("src") for d in p.my_dishes if d.get("id") == rid and d.get("src")), None))

    @app.post("/cook/done")
    async def cook_done(request: Request):
        """«Приготовано»: прибираємо зі списку й рахуємо в «Приготовано» профілю."""
        form, u, p, health, bad = await menu_form(request)
        if bad:
            return bad
        rid = str(form.get("id", ""))
        p.cooking = [c for c in p.cooking if c.get("id") != rid]
        r = menu.RECIPE_BY_ID.get(rid)
        if r and p.fridge:       # приготували — швидкопсувне з цього рецепта вже не чекає в холодильнику
            used = {k for k in r.ingredients if k in menu.SHELF_DAYS}
            p.fridge = [k for k in p.fridge if k not in used]
            p.fridge_since = {k: v for k, v in p.fridge_since.items() if k not in used}
            p.fridge_until = {k: v for k, v in p.fridge_until.items() if k not in used}
        analytics.track(conn, u["id"] if u else None, "cooked", dish=rid, guest=not u)
        return save_any(request, u, p, health, go("/cook?done=1"))

    # ---------- «Що приготувати сьогодні»: продукти → час і люди → 3 страви → рецепт ----------
    def today_args(request: Request) -> tuple[list[str], int, int]:
        q = request.query_params
        have = [k for k in str(q.get("have", "")).split(",") if k in menu.INGREDIENTS]
        try:
            t = max(15, min(180, int(q.get("t") or 30)))
            n = max(1, min(12, int(q.get("n") or 2)))
        except ValueError:
            t, n = 30, 2
        k = q.get("k")       # кухонні прилади: є в запиті — беремо (навіть порожнє = «без обмежень»)
        cur_kit.set(None if k is None else [x for x in dict.fromkeys(str(k).split(",")) if x in menu.KITCHEN])
        return have, t, n

    def default_meal() -> str:
        """Зранку (5:00–10:59 за Києвом) спершу показуємо сніданки, решту дня — обід і вечерю."""
        try:
            from zoneinfo import ZoneInfo
            h = datetime.now(ZoneInfo("Europe/Kyiv")).hour
        except Exception:
            h = (datetime.now(timezone.utc).hour + 2) % 24
        return "breakfast" if 5 <= h < 11 else "dinner"

    def today_qs(have, t, n, need=()) -> str:
        q = {"have": ",".join(have), "t": t, "n": n}
        if need:
            q["need"] = ",".join(need)
        if cur_kit.get() is not None:
            q["k"] = ",".join(cur_kit.get())
        return urlencode(q)

    def today_need(src) -> list[str]:
        """«Немає» біля «мабуть, є»: ці запаси докуповуємо (з query або з форми)."""
        return [k for k in str(src.get("need", "")).split(",") if k in menu.ASSUMED]

    async def today_offers(request: Request, u, n):
        p, health = load_profile(u["id"]) if u else (guest_profile(request), False)
        kit = cur_kit.get()
        if kit is not None and kit != p.kitchen:
            p.kitchen = kit
            if u:                       # запам'ятовуємо прилади — діють і на меню тижня
                save_profile(u["id"], p, health)
        p.household = n
        link, stores, offers, notice, is_demo = await menu_offers(u, p)
        return p, link, stores, is_demo, offers

    @app.get("/today", response_class=HTMLResponse)
    def today_page(request: Request):
        u = user_of(request)
        have, t, n = today_args(request)
        if not request.query_params.get("have") and u:      # що вже є вдома з фото холодильника
            p = load_profile(u["id"])[0]
            have = [k for k in p.fridge if k in menu.PANTRY_WORDS]
            n = p.household or n
        words = {k: [v[0], v[1], list(v[2])] for k, v in menu.PANTRY_WORDS.items()}
        kit = cur_kit.get()
        if kit is None:
            kit = (load_profile(u["id"])[0] if u else guest_profile(request)).kitchen
        return render(request, "today.html", u, words=words, have=have, t=t, n=n, kit=kit, kitchen=menu.KITCHEN)

    @app.get("/today/dishes", response_class=HTMLResponse)
    async def today_dishes(request: Request):
        u = user_of(request)
        have, t, n = today_args(request)
        p, link, stores, is_demo, offers = await today_offers(request, u, n)
        meal = request.query_params.get("meal")
        if meal not in MEALS:
            meal = default_meal()
        soon = [k for k, _ in menu.expiring(p.fridge_since, p.fridge, within=2, until=p.fridge_until)] if u else []
        opts = menu.today(offers, p, have, max_minutes=t, n=30, meal=meal, soon=soon)   # 3 видно, решта — для «↻ Інша»
        analytics.track(conn, u["id"] if u else None, "today_dishes", have=len(have), minutes=t, people=n,
                        options=len(opts), guest=not u, meal=meal)
        return render(request, "today_dishes.html", u, opts=opts, qs=today_qs(have, t, n), have=have, t=t, n=n,
                      meal=meal, meals=MEALS, kit=p.kitchen, cook_hint=menu.cook_hint,
                      is_demo=is_demo, store=(stores[0].get("address") or stores[0]["title"]) if stores else None,
                      shared=bool(stores and stores[0].get("shared")), photos=menu_photos())

    @app.get("/today/dish/{rid}", response_class=HTMLResponse)
    async def today_dish(request: Request, rid: str):
        u = user_of(request)
        have, t, n = today_args(request)
        need = today_need(request.query_params)
        p, link, stores, is_demo, offers = await today_offers(request, u, n)
        opts = [o for o in menu.today(offers, p, have, max_minutes=180, n=999, need=need, meal=None) if o.dish.id == rid]
        if not opts:
            return go(f"/today/dishes?{today_qs(have, t, n)}")
        # посилання «Немає» / «Є» для запасів, що «мабуть, є»: перемикаємо продукт у need
        toggle = {k: f"/today/dish/{rid}?" + today_qs(have, t, n, [x for x in need if x != k] if k in need else need + [k])
                  for k in opts[0].dish.recipe.ingredients if k in menu.ASSUMED}
        return render(request, "today_dish.html", u, o=opts[0], p=p, qs=today_qs(have, t, n), is_demo=is_demo,
                      link=link, photos=menu_photos(), ingredients=menu.INGREDIENTS, fmt_qty=menu.fmt_qty,
                      t=t, n=n, have=have, need=need, toggle=toggle,
                      meal=request.query_params.get("meal") if request.query_params.get("meal") in MEALS else None)

    @app.post("/today/cooked", response_class=HTMLResponse)
    async def today_cooked(request: Request):
        form = await request.form()
        u = user_of(request)
        if u and not secrets.compare_digest(str(form.get("csrf", "")), u["csrf"]):
            return HTMLResponse("Сторінка застаріла. Оновіть її й спробуйте ще раз.", status_code=400)
        rid = str(form.get("id", ""))
        p, health = load_profile(u["id"]) if u else (guest_profile(request), False)
        title = next((r.title for r in menu.RECIPES + menu.user_recipes(p) if r.id == rid), "")
        try:
            n = max(1, min(12, int(form.get("n") or p.household or 2)))
        except ValueError:
            n = 2
        if title:
            remember_cooking(p, [rid], n, "today")
        analytics.track(conn, u["id"] if u else None, "cook_started", dish=rid, guest=not u)
        return save_any(request, u, p, health, render(request, "today_done.html", u, title=title, rid=rid))

    @app.post("/today/cart")
    async def today_cart(request: Request):
        """Докупити бракуюче для обраної страви — у кошик «Сільпо»."""
        form = await request.form()
        u, bad = need_user(request, str(form.get("csrf", "")))
        if bad:
            return bad
        rid = str(form.get("id", ""))
        have = [k for k in str(form.get("have", "")).split(",") if k in menu.INGREDIENTS]
        try:
            n = max(1, min(12, int(form.get("n") or 2)))
        except ValueError:
            n = 2
        tokens = silpo_tokens(u["id"]) if silpo_link(u["id"]) else None
        if not tokens:
            return go("/silpo")
        p, _ = load_profile(u["id"])
        p.household = n
        _, stores, offers, notice, is_demo = await current_offers(u["id"], p, False)
        if is_demo:
            return go("/silpo")
        need = today_need(form)
        opts = [o for o in menu.today(offers, p, have, max_minutes=180, n=999, need=need, meal=None) if o.dish.id == rid]
        if not opts or not opts[0].lines:
            return go("/today")
        o = opts[0]
        p2, health2 = load_profile(u["id"])
        remember_cooking(p2, [rid], n, "today")
        save_profile(u["id"], p2, health2)
        wants = silpo_cart.wants_from_menu(o.lines)
        ctx = dict(bought="", wanted=len(wants), cart_action="/today/cart",
                   cart_hidden={"id": rid, "have": ",".join(have), "n": n, "need": ",".join(need)})
        return await put_in_silpo_cart(request, u, form, tokens, stores, wants, ctx, "today_cart", o.buy_saving, dish=rid)

    # ---------- що в холодильнику: фото → продукти вдома → меню доїдає їх ----------
    def fridge_scans_today(uid: int) -> int:
        since = security.iso(security.now() - timedelta(days=1))
        r = conn.execute("SELECT COUNT(*) AS n FROM events WHERE user_id=? AND event='fridge_scan' AND ts >= ?",
                         (uid, since)).fetchone()
        return r["n"] if r else 0

    @app.get("/fridge", response_class=HTMLResponse)
    def fridge_page(request: Request):
        u = user_of(request)
        p = load_profile(u["id"])[0] if u else None
        choices = sorted(((k, v[0]) for k, v in menu.INGREDIENTS.items()), key=lambda x: x[1])
        words = {k: [v[0], v[1], list(v[2])] for k, v in menu.PANTRY_WORDS.items()}
        tp = p or guest_profile(request)
        return render(request, "fridge.html", u, p=p, choices=choices, names=dict(choices), words=words,
                      next_url="/menu",
                      terms=[fridge_term(p, k) for k in p.fridge] if p else [])

    def fridge_term(p: Profile, k: str) -> dict:
        """Рядок «Термін придатності»: продукт, дата «вжити до» (вручну або орієнтовно), скільки лишилось."""
        d, manual = menu.use_by(k, p.fridge_since, p.fridge_until)
        left = (d - date.today()).days if d else None
        return {"key": k, "name": menu.INGREDIENTS[k][0], "emoji": menu.PANTRY_WORDS.get(k, ("", "🥫"))[1],
                "date": d.isoformat() if d else "", "manual": manual, "left": left,
                "label": ("" if d is None else ("прострочено" if left < 0 else menu.when_label(left) if left <= 1
                          else f"до {d.day:02d}.{d.month:02d}"))}

    # ---------- «Холодильник»: окрема сторінка — що є вдома й до коли ----------
    def pantry_groups(p: Profile) -> list[tuple[str, str, list[dict]]]:
        rows = sorted((fridge_term(p, k) for k in p.fridge),
                      key=lambda x: (x["left"] is None, x["left"] if x["left"] is not None else 0, x["name"]))
        g = [("soon", "Використати скоро", [x for x in rows if x["left"] is not None and x["left"] <= 1]),
             ("week", "Цього тижня", [x for x in rows if x["left"] is not None and 1 < x["left"] <= 7]),
             ("long", "Довго", [x for x in rows if x["left"] is None or x["left"] > 7])]
        return [x for x in g if x[2]]

    @app.get("/pantry", response_class=HTMLResponse)
    def pantry(request: Request):
        u, bad = need_user(request)
        if bad:
            return bad
        p, health = load_profile(u["id"])
        if prune_fridge(p):
            save_profile(u["id"], p, health)
        words = {k: [v[0], v[1], list(v[2])] for k, v in menu.PANTRY_WORDS.items()}
        return render(request, "pantry.html", u, p=p, groups=pantry_groups(p), words=words,
                      cook_url=f"/today/dishes?{today_qs(p.fridge, 60, p.household or 2)}" if p.fridge else None)

    @app.post("/pantry/add")
    async def pantry_add(request: Request):
        """Додати продукти (з поля, голосу чи фото): ключі з довідника й решту — «інше»."""
        form = await request.form()
        u, bad = need_user(request, str(form.get("csrf", "")))
        if bad:
            return JSONResponse({"error": "Сторінка застаріла. Оновіть її."}, status_code=400)
        p, health = load_profile(u["id"])
        today_s = date.today().isoformat()
        bought = fridge.receipt_date(str(form.get("since", "")))   # дата покупки з чека — точніший відлік терміну
        for k in form.getlist("key"):
            if k in menu.INGREDIENTS and k not in p.fridge:
                p.fridge.append(k)
                p.fridge_since[k] = bought or today_s
            elif k in p.fridge and bought and bought > p.fridge_since.get(k, ""):
                p.fridge_since[k] = bought          # купили свіже — відлік з нової покупки
                p.fridge_until.pop(k, None)
        for x in form.getlist("other"):
            x = str(x).strip()[:60]
            if x and x.lower() not in {o.lower() for o in p.fridge_other} and len(p.fridge_other) < 40:
                p.fridge_other.append(x)
        p.fridge = sorted(set(p.fridge))
        p.fridge_at = security.iso(security.now())
        p.menu, p.menu_skip = [], []            # меню тижня перебудуємо під нове
        save_profile(u["id"], p, health)
        analytics.track(conn, u["id"], "pantry_add", have=len(p.fridge))
        return JSONResponse({"ok": True, "n": len(p.fridge) + len(p.fridge_other)})

    @app.post("/pantry/remove")
    async def pantry_remove(request: Request):
        form = await request.form()
        u, bad = need_user(request, str(form.get("csrf", "")))
        if bad:
            return JSONResponse({"error": "Сторінка застаріла. Оновіть її."}, status_code=400)
        p, health = load_profile(u["id"])
        key, other = str(form.get("key", "")), str(form.get("other", ""))
        if key:
            p.fridge = [k for k in p.fridge if k != key]
            p.fridge_since.pop(key, None)
            p.fridge_until.pop(key, None)
        if other:
            p.fridge_other = [x for x in p.fridge_other if x != other]
        if not p.fridge and not p.fridge_other:
            p.fridge_at = None
        p.menu, p.menu_skip = [], []
        save_profile(u["id"], p, health)
        analytics.track(conn, u["id"], "fridge_removed", key=key or "other")
        return JSONResponse({"ok": True})

    @app.post("/fridge/expiry")
    async def fridge_expiry(request: Request):
        """Термін з упаковки: дата або «сьогодні / завтра / +3 дні»; порожньо — знову орієнтовно."""
        form = await request.form()
        u, bad = need_user(request, str(form.get("csrf", "")))
        if bad:
            return JSONResponse({"error": "Сторінка застаріла. Оновіть її."}, status_code=400)
        p, health = load_profile(u["id"])
        key, val = str(form.get("key", "")), str(form.get("date", "")).strip()
        if key not in p.fridge:
            return JSONResponse({"error": "Цього продукту вже немає в списку."}, status_code=400)
        if val:
            try:
                d = date.fromisoformat(val[:10])
            except ValueError:
                return JSONResponse({"error": "Неправильна дата."}, status_code=400)
            if not (date.today() - timedelta(days=30) <= d <= date.today() + timedelta(days=730)):
                return JSONResponse({"error": "Дата надто далека."}, status_code=400)
            p.fridge_until[key] = d.isoformat()
        else:
            p.fridge_until.pop(key, None)
        save_profile(u["id"], p, health)
        analytics.track(conn, u["id"], "fridge_expiry_set", manual=bool(val))
        return JSONResponse(fridge_term(p, key))

    @app.get("/fridge/badge")
    def fridge_badge(request: Request):
        """Число на іконці встановленої апки: скільки продуктів зіпсується за ≤ 2 дні (0 — прибрати значок)."""
        u = user_of(request)
        if u is None:
            return JSONResponse({"n": 0}, headers={"Cache-Control": "no-store"})
        p, _ = load_profile(u["id"])
        n = len(menu.expiring(p.fridge_since, p.fridge, within=2, until=p.fridge_until)) if p.tg_expiry and p.fridge else 0
        return JSONResponse({"n": n}, headers={"Cache-Control": "no-store"})

    @app.post("/fridge/scan")
    async def fridge_scan(request: Request):
        """Фото → список продуктів (JSON). Фото не зберігаємо."""
        form = await request.form()
        u, bad = need_user(request, str(form.get("csrf", "")))
        if bad:
            return JSONResponse({"error": "Увійдіть, щоб розпізнавати фото"}, status_code=401)
        photo = form.get("photo")
        if photo is None or not hasattr(photo, "read"):
            return JSONResponse({"error": "Додайте фото"}, status_code=400)
        data = await photo.read()
        media = (getattr(photo, "content_type", "") or "").split(";")[0]
        if media not in fridge.MEDIA or not data or len(data) > fridge.MAX_BYTES:
            return JSONResponse({"error": "Потрібне фото JPG, PNG або WebP до 8 МБ"}, status_code=400)
        if fridge_scans_today(u["id"]) >= FRIDGE_PER_DAY:
            return JSONResponse({"error": "На сьогодні досить фото — спробуйте завтра"}, status_code=429)
        kind = "receipt" if form.get("kind") == "receipt" else "fridge"
        analytics.track(conn, u["id"], "receipt_scan" if kind == "receipt" else "fridge_scan", size_kb=len(data) // 1024)
        try:
            oidc = request.headers.get("x-vercel-oidc-token")
            items = await asyncio.to_thread(vision, data, media, oidc, kind) if kind == "receipt" else \
                await asyncio.to_thread(vision, data, media, oidc)
        except fridge.NotConfigured:
            log.warning("Холодильник: розпізнавання не налаштоване (немає ключа)")
            return JSONResponse({"error": "Розпізнавання фото ще налаштовується. Додайте продукти вручну нижче."}, status_code=503)
        except Exception as e:
            log.warning("Холодильник: не вдалося розпізнати: %s: %s", type(e).__name__, str(e)[:300])
            if any(x in str(e) for x in ("customer_verification_required", "no_providers_available", "Free tier",
                                         "insufficient", "credit")):
                return JSONResponse({"error": "Розпізнавання фото тимчасово недоступне. Додайте продукти вручну нижче."},
                                    status_code=503)
            return JSONResponse({"error": "Не вдалося розпізнати фото. Спробуйте ще раз або додайте продукти вручну."}, status_code=502)
        for it in items:
            it["emoji"] = name_visual(it["name"])["emoji"]
            it["label"] = menu.INGREDIENTS[it["ingredient"]][0] if it["ingredient"] != "other" else None
        return JSONResponse({"items": items})

    @app.post("/fridge/save")
    async def fridge_save(request: Request):
        form = await request.form()
        u, bad = need_user(request, str(form.get("csrf", "")))
        if bad:
            return bad
        p, health = load_profile(u["id"])
        p.fridge = sorted({k for k in form.getlist("have") if k in menu.INGREDIENTS})
        today_s = date.today().isoformat()      # нове вдома — з сьогодні; те, що вже було, зберігає свою дату
        p.fridge_since = {k: p.fridge_since.get(k, today_s) for k in p.fridge}
        p.fridge_until = {k: v for k, v in p.fridge_until.items() if k in p.fridge}
        p.fridge_other = [str(x).strip()[:60] for x in form.getlist("other") if str(x).strip()][:40]
        p.fridge_at = security.iso(security.now()) if (p.fridge or p.fridge_other) else None
        p.menu, p.menu_skip = [], []        # меню перебудуємо під те, що є вдома
        save_profile(u["id"], p, health)
        analytics.track(conn, u["id"], "fridge_saved", have=len(p.fridge), other=len(p.fridge_other))
        return go("/menu?fresh=1" if p.fridge else "/menu")

    @app.post("/fridge/remove")
    async def fridge_remove(request: Request):
        """«Немає» біля продукту з холодильника в меню: прибираємо його — і він знову йде в кошик."""
        form = await request.form()
        u, bad = need_user(request, str(form.get("csrf", "")))
        if bad:
            return bad
        p, health = load_profile(u["id"])
        key = str(form.get("key", ""))
        p.fridge = [k for k in p.fridge if k != key]
        p.fridge_since.pop(key, None)
        p.fridge_until.pop(key, None)
        if not p.fridge and not p.fridge_other:
            p.fridge_at = None
        save_profile(u["id"], p, health)
        analytics.track(conn, u["id"], "fridge_removed", key=key)
        return go("/buy")

    # ---------- мої страви ----------
    def ing_choices() -> list[tuple[str, str, str]]:
        """(ключ, назва, одиниця для поля) — довідник інгредієнтів, з якими рахуємо ціни й акції."""
        unit = {"g": "г", "ml": "мл", "pcs": "шт"}
        return sorted(((k, v[0], unit[v[1]]) for k, v in menu.INGREDIENTS.items()), key=lambda x: x[1])

    @app.get("/dishes", response_class=HTMLResponse)
    def dishes_page(request: Request, edit: str = "", saved: str = ""):
        u = user_of(request)
        if u is None:
            return render(request, "dishes.html", None, mine=[], cur=None, choices=ing_choices(), emojis=menu.MY_EMOJI)
        p, _ = load_profile(u["id"])
        mine = menu.user_recipes(p)
        cur = next((d for d in p.my_dishes if d.get("id") == edit), None)
        return render(request, "dishes.html", u, mine=mine, cur=cur, choices=ing_choices(), emojis=menu.MY_EMOJI,
                      saved=saved, in_menu=set(p.menu))

    @app.post("/dishes")
    async def dishes_save(request: Request):
        form = await request.form()
        u, bad = need_user(request, form.get("csrf", ""))
        if bad:
            return bad
        p, health = load_profile(u["id"])
        serv = max(1, min(12, int(float(form.get("servings") or 4))))
        ing: dict[str, float] = {}
        for k, q in zip(form.getlist("ing"), form.getlist("qty")):
            try:
                qv = float(str(q).replace(",", "."))
            except ValueError:
                continue
            if k in menu.INGREDIENTS and qv > 0:
                ing[k] = round(ing.get(k, 0) + qv * 4 / serv, 1)     # зберігаємо на 4 порції, як усі рецепти
        extra = [x.strip()[:40] for x in str(form.get("extra", "")).replace(";", ",").replace("\n", ",").split(",") if x.strip()][:15]
        did = str(form.get("id") or "")
        if not did.startswith(menu.MY_PREFIX):
            did = menu.MY_PREFIX + secrets.token_hex(4)
        d = {"id": did, "title": str(form.get("title", "")).strip()[:80], "emoji": str(form.get("emoji", "")),
             "minutes": max(5, min(240, int(float(form.get("minutes") or 45)))), "ing": ing, "extra": extra,
             "steps": str(form.get("steps", ""))[:2000],
             "src": str(form.get("src", ""))[:300] if str(form.get("src", "")).startswith(("http://", "https://")) else ""}
        if not menu.user_recipe(d):
            cur = dict(d, servings=serv)
            return render(request, "dishes.html", u, mine=menu.user_recipes(p), cur=cur, choices=ing_choices(),
                          emojis=menu.MY_EMOJI, in_menu=set(p.menu), status=400,
                          error="Додайте назву і хоча б один інгредієнт.")
        d["servings"] = serv
        p.my_dishes = [x for x in p.my_dishes if x.get("id") != did][:29] + [d]
        roll_week(p)
        if form.get("to_menu") and did not in p.menu:      # одразу в меню цього тижня — замість останньої страви
            p.menu = ([did] + p.menu)[:max(1, p.dinners)]
        save_profile(u["id"], p, health)
        analytics.track(conn, u["id"], "my_dish_saved", ingredients=len(ing), extras=len(extra))
        return go(f"/dishes?saved={did}")

    @app.post("/dishes/import", response_class=HTMLResponse)
    async def dishes_import(request: Request):
        """Посилання на рецепт → заповнена форма «моєї страви» (людина перевіряє й зберігає)."""
        form = await request.form()
        u, bad = need_user(request, str(form.get("csrf", "")))
        if bad:
            return bad
        p, _ = load_profile(u["id"])
        url = str(form.get("url", ""))[:500]
        ctx = dict(mine=menu.user_recipes(p), choices=ing_choices(), emojis=menu.MY_EMOJI, in_menu=set(p.menu), imp_url=url)
        try:
            draft = await asyncio.to_thread(recipe_import.import_recipe, url, request.headers.get("x-vercel-oidc-token"))
        except recipe_import.ImportError_ as e:
            analytics.track(conn, u["id"], "recipe_import", ok=False)
            return render(request, "dishes.html", u, cur=None, imp_error=str(e), status=422, **ctx)
        except Exception as e:
            log.warning("Рецепт: імпорт не вдався: %s: %s", type(e).__name__, str(e)[:300])
            analytics.track(conn, u["id"], "recipe_import", ok=False)
            return render(request, "dishes.html", u, cur=None, status=502, **ctx,
                          imp_error="Не вдалося розібрати сторінку. Спробуйте інше посилання або заповніть вручну.")
        analytics.track(conn, u["id"], "recipe_import", ok=True, ingredients=len(draft["ing"]), extras=len(draft["extra"]))
        if draft["emoji"] not in menu.MY_EMOJI:
            draft["emoji"] = menu.MY_EMOJI[0]
        return render(request, "dishes.html", u, cur=draft, imported=draft["host"], **ctx)

    @app.post("/dishes/delete")
    async def dishes_delete(request: Request):
        form = await request.form()
        u, bad = need_user(request, form.get("csrf", ""))
        if bad:
            return bad
        p, health = load_profile(u["id"])
        did = str(form.get("id", ""))
        p.my_dishes = [x for x in p.my_dishes if x.get("id") != did]
        p.menu = [x for x in p.menu if x != did]
        save_profile(u["id"], p, health)
        return go("/dishes")

    # ---------- сповіщення в Telegram ----------
    # ---------- Web Push (PWA) ----------
    def push_subs(user_id: int) -> list:
        return conn.execute("SELECT endpoint, p256dh, auth FROM push_subs WHERE user_id=?", (user_id,)).fetchall()

    def push_user(user_id: int, title: str, body: str, url: str = "/", tag: str | None = None,
                  badge: int | None = None) -> int:
        """Надсилає на всі пристрої людини. Повертає, на скільки дійшло. Відписані — прибираємо.
        badge — число на іконці встановленої апки (скільки продуктів скоро зіпсується)."""
        ok = 0
        msg = {"title": title, "body": body[:240], "url": url, "tag": tag}
        if badge is not None:
            msg["badge"] = badge
        for r in push_subs(user_id):
            try:
                if push_send(vapid, dict(r), msg):
                    ok += 1
            except webpush.Gone:
                conn.execute("DELETE FROM push_subs WHERE endpoint=?", (r["endpoint"],))
            except Exception as e:
                log.warning("Push: не надіслали: %s", str(e)[:200])
        return ok

    @app.get("/push/key")
    def push_key():
        return {"key": vapid.public_key}

    @app.post("/push/subscribe")
    async def push_subscribe(request: Request):
        u = user_of(request)
        if u is None:
            return JSONResponse({"ok": False, "go": "/register"}, status_code=401)
        if not secrets.compare_digest(request.headers.get("x-csrf", ""), u["csrf"]):
            return JSONResponse({"ok": False}, status_code=400)
        try:
            d = await request.json()
            endpoint, keys = str(d["endpoint"]), d.get("keys") or {}
            p256dh, auth = str(keys["p256dh"]), str(keys["auth"])
            assert len(webpush.unb64u(p256dh)) == 65 and len(webpush.unb64u(auth)) == 16
        except Exception:
            return JSONResponse({"ok": False, "error": "неправильна підписка"}, status_code=400)
        if not webpush.allowed_endpoint(endpoint):
            return JSONResponse({"ok": False, "error": "невідомий push-сервіс"}, status_code=400)
        conn.execute("""INSERT INTO push_subs (endpoint, user_id, p256dh, auth, created_at) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth""",
                     (endpoint, u["id"], p256dh, auth, security.iso(security.now())))
        p, health = load_profile(u["id"])
        if not (p.tg_deals or p.tg_weekly):          # щойно ввімкнули — нехай сповіщення одразу щось означають
            p.tg_deals = p.tg_weekly = True
            save_profile(u["id"], p, health)
        analytics.track(conn, u["id"], "push_subscribed", host=(webpush.urlsplit(endpoint).hostname or "")[:60],
                        pwa=request.headers.get("x-pwa") == "1")
        sent = await asyncio.to_thread(push_user, u["id"], "Сповіщення ввімкнено ✓",
                                       "Повідомимо про знижки на ваші товари й нове меню щопонеділка.", "/notify", "hello")
        return {"ok": True, "sent": sent}

    @app.post("/push/unsubscribe")
    async def push_unsubscribe(request: Request):
        u = user_of(request)
        if u is None or not secrets.compare_digest(request.headers.get("x-csrf", ""), u["csrf"]):
            return JSONResponse({"ok": False}, status_code=400)
        try:
            endpoint = str((await request.json())["endpoint"])
        except Exception:
            return JSONResponse({"ok": False}, status_code=400)
        conn.execute("DELETE FROM push_subs WHERE endpoint=? AND user_id=?", (endpoint, u["id"]))
        analytics.track(conn, u["id"], "push_unsubscribed")
        return {"ok": True}

    def tg_chat(user_id: int):
        return conn.execute("SELECT chat_id, linked_at FROM tg_links WHERE user_id=?", (user_id,)).fetchone()

    def pct(x) -> int:
        try:
            return max(0, min(90, int(float(x or 0))))
        except ValueError:
            return 0

    def add_watch(p: Profile, word: str, min_pct: int | None = None) -> str | None:
        word = " ".join(word.lower().split())[:40]
        if len(word) < 2:
            return None
        if word not in p.watch:
            p.watch = (p.watch + [word])[-30:]
            p.watch_min[word] = p.watch_default if min_pct is None else min_pct
        elif min_pct is not None:
            p.watch_min[word] = min_pct
        p.watch_min = {w: v for w, v in p.watch_min.items() if w in p.watch and v}
        return word

    @app.get("/notify", response_class=HTMLResponse)
    def notify_page(request: Request, saved: int = 0):
        u, bad = need_user(request)
        if bad:
            return bad
        p, _ = load_profile(u["id"])
        return render(request, "notify.html", u, p=p, chat=tg_chat(u["id"]), tg_on=tg.enabled, bot=tg.bot,
                      push_n=len(push_subs(u["id"])),
                      start_url=f"https://t.me/{tg.bot}?start={tgm.link_code(app_secret, u['id'])}",
                      silpo=silpo_link(u["id"]), saved=saved)

    @app.post("/notify")
    async def notify_save(request: Request):
        form = await request.form()
        u, bad = need_user(request, form.get("csrf", ""))
        if bad:
            return bad
        p, health = load_profile(u["id"])
        if form.get("action") == "unlink":
            conn.execute("DELETE FROM tg_links WHERE user_id=?", (u["id"],))
            return go("/notify")
        p.tg_deals, p.tg_weekly = bool(form.get("tg_deals")), bool(form.get("tg_weekly"))
        p.tg_expiry = bool(form.get("tg_expiry"))
        p.watch_default = pct(form.get("watch_default"))
        p.watch, p.watch_min = [], {}
        for w, m in zip(form.getlist("w"), form.getlist("wmin")):
            add_watch(p, str(w), pct(m))
        save_profile(u["id"], p, health)
        return go("/notify?saved=1")

    @app.post("/notify/watch")
    async def notify_watch(request: Request):
        """«🔔 Стежити» з картки акції: додає слово до списку (AJAX)."""
        form = await request.form()
        u, bad = need_user(request, form.get("csrf", ""))
        if bad:
            return JSONResponse({"ok": False}, status_code=400)
        p, health = load_profile(u["id"])
        word = add_watch(p, str(form.get("word", "")))
        if not word:
            return JSONResponse({"ok": False}, status_code=400)
        save_profile(u["id"], p, health)
        return JSONResponse({"ok": True, "word": word, "min": p.watch_min.get(word, 0), "linked": bool(tg_chat(u["id"]))})

    def tg_reply(chat: str, text: str, path: str | None = None, label: str = "Відкрити сайт"):
        tg.send(chat, text, [(label, f"{base_url}{path}")] if path else None)

    @app.post("/tg/webhook")
    async def tg_webhook(request: Request):
        """Повідомлення боту. Telegram підписує запити секретом, який ми задали в setWebhook."""
        got = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not tg.enabled or not hmac.compare_digest(got, tgm.webhook_secret(app_secret)):
            return JSONResponse({"ok": False}, status_code=403)
        msg = (await request.json()).get("message") or {}
        chat = msg.get("chat") or {}
        cid, text = str(chat.get("id") or ""), (msg.get("text") or "").strip()
        if not cid or chat.get("type") != "private":
            return {"ok": True}
        if text.startswith("/start"):
            uid = tgm.check_code(app_secret, text[6:].strip())
            if uid and conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone():
                conn.execute("DELETE FROM tg_links WHERE chat_id=? OR user_id=?", (cid, uid))
                conn.execute("INSERT INTO tg_links (user_id, chat_id, linked_at) VALUES (?, ?, ?)",
                             (uid, cid, security.iso(security.now())))
                analytics.track(conn, uid, "tg_linked")
                await asyncio.to_thread(tg_reply, cid, "✅ <b>Готово!</b> Писатиму про акції, за якими ви стежите, "
                                        "великі знижки на те, що ви купуєте, і щопонеділка — меню на тиждень.\n\n"
                                        "Вимкнути: /stop", "/notify", "Налаштувати")
            elif conn.execute("SELECT 1 FROM tg_links WHERE chat_id=?", (cid,)).fetchone():
                await asyncio.to_thread(tg_reply, cid, "Ви вже підключені 👍 Налаштування — на сайті.", "/notify", "Налаштувати")
            else:
                await asyncio.to_thread(tg_reply, cid, "Привіт! Щоб отримувати сповіщення, відкрийте «Сповіщення» "
                                        "на сайті й натисніть «Підключити Telegram».", "/notify")
        elif text.startswith("/stop"):
            conn.execute("DELETE FROM tg_links WHERE chat_id=?", (cid,))
            await asyncio.to_thread(tg_reply, cid, "Сповіщення вимкнено. Повернутись — кнопка на сайті.", "/notify")
        else:
            await asyncio.to_thread(tg_reply, cid, "Я надсилаю сповіщення про акції. Налаштування — на сайті.", "/notify",
                                    "Налаштувати")
        return {"ok": True}

    @app.get("/tg/setup")
    def tg_setup():
        """Під'єднує бота до сайту (setWebhook). Безпечно викликати повторно."""
        if not tg.enabled:
            return JSONResponse({"ok": False, "why": "немає TELEGRAM_BOT_TOKEN"}, status_code=503)
        try:
            tg.set_webhook(f"{base_url}/tg/webhook", tgm.webhook_secret(app_secret))
        except Exception as e:
            return JSONResponse({"ok": False, "why": str(e)[:200]}, status_code=502)
        return {"ok": True, "bot": tg.bot}

    async def notify_user(uid: int, chat: str | None, menu_day: bool, st: dict) -> None:
        """Акції й меню тижня — у Telegram (якщо прив'язаний) і push на пристрої (якщо ввімкнені)."""
        has_push = bool(push_subs(uid))

        async def deliver(tg_text: str, tg_btn: tuple[str, str], title: str, body: str, url: str, tag: str,
                          telegram: bool = True, badge: int | None = None) -> bool:
            ok = False
            if telegram and chat and tg.enabled:
                ok = await asyncio.to_thread(tg.send, chat, tg_text, [tg_btn]) or ok
            if has_push:
                ok = bool(await asyncio.to_thread(push_user, uid, title, body, url, tag, badge)) or ok
            return ok

        p, health = load_profile(uid)
        # один зведений push на всі продукти, яким лишилось ≤ 2 днів (сьогодні / завтра / за 2 дні)
        soon = menu.expiring(p.fridge_since, p.fridge, within=2, until=p.fridge_until) if p.tg_expiry and p.fridge and has_push else []
        xk = "x:" + date.today().isoformat()
        if soon and not conn.execute("SELECT 1 FROM tg_sent WHERE user_id=? AND key=?", (uid, xk)).fetchone():
            names = ", ".join(f"{menu.INGREDIENTS[k][0].lower()} — {'' if p.fridge_until.get(k) else '≈ '}{menu.when_label(d)}"
                              for k, d in soon)
            approx = any(not p.fridge_until.get(k) for k, _ in soon)
            url = f"/today/dishes?{today_qs(p.fridge, 60, p.household or 2)}"
            title = f"⏳ Скоро зіпсується: {len(soon)} {plural(len(soon), 'продукт', 'продукти', 'продуктів')}"
            if approx:
                names += " (≈ приблизно)"
            if await deliver(f"⏳ Варто використати: {names}.\n" + ("≈ — приблизний термін за видом продукту, точна дата — на упаковці.\n" if approx else "")
                             + "Підібрали страви з того, що є вдома.",
                             ("Що приготувати", base_url + url), title, names, url, "expiry",
                             telegram=False, badge=len(soon)):      # лише push на телефон + число на іконці
                conn.execute("INSERT INTO tg_sent (user_id, key, sent_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                             (uid, xk, security.iso(security.now())))
                st["expiry"] = st.get("expiry", 0) + 1
        if not silpo_link(uid) or not (p.tg_deals or (p.tg_weekly and menu_day)):
            return
        _, offers, _ = await silpo_list_input(uid, p, allow_stale=False)
        if not offers:
            return
        offers = planner.merge_nearby(offers)
        sent = {r["key"] for r in conn.execute("SELECT key FROM tg_sent WHERE user_id=?", (uid,)).fetchall()}
        now = security.iso(security.now())

        def mark(keys):
            for k in keys:
                conn.execute("INSERT INTO tg_sent (user_id, key, sent_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                             (uid, k, now))

        if p.tg_deals:
            items = tgm.pick_deals(offers, p, sent)
            if items and await deliver(tgm.deals_text(items), ("Усі акції", f"{base_url}/list?sort=disc"),
                                       "🔔 Цікаві акції у «Сільпо»", tgm.deals_short(items), "/list?sort=disc", "deals"):
                mark(tgm.offer_key(o) for o, _ in items)
                st["deals"] += 1
        wk = "w:%d-W%02d" % date.today().isocalendar()[:2]
        if p.tg_weekly and menu_day and wk not in sent:
            m, changed = week_menu(p, offers)
            if changed:
                save_profile(uid, p, health)
            if m.dishes and await deliver(tgm.menu_text(m, day_labels(p)), ("Відкрити меню", f"{base_url}/menu"),
                                          "🍽 Меню на тиждень готове",
                                          ", ".join(d.title for d in m.dishes[:3]) + (" і ще" if len(m.dishes) > 3 else ""),
                                          "/menu", "menu"):
                mark([wk])
                st["menus"] += 1

    @app.get("/healthz")
    def healthz():
        """Для зовнішнього «будильника» (напр. cron-job.org раз на 5 хв): будить функцію й базу, щоб перше відкриття було швидким."""
        conn.execute("SELECT 1").fetchone()
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.get("/cron/warm")
    async def cron_warm(request: Request):
        """Раз на день (Vercel Cron): свіжі акції магазинів, які люди відкривали за тиждень, — через донора."""
        secret = os.environ.get("CRON_SECRET")
        allowed = bool(secret) and secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {secret}")
        if not allowed:
            return JSONResponse({"ok": False}, status_code=403)
        try:
            d = json.loads(db.get_setting(conn, "warm_demand") or "{}")
        except ValueError:
            d = {}
        names = {b["id"]: b for b in branches_cached()}
        todo = [names[k] for k, _ in sorted(d.items(), key=lambda kv: kv[1], reverse=True)
                if k in names and cached_offers(k) is None][:8]
        sem = asyncio.Semaphore(3)

        async def one(b):
            async with sem:
                return await warm_store({"id": b["id"], "title": b["title"], "address": b.get("address")}, timeout=20)

        res = await asyncio.wait_for(asyncio.gather(*(one(b) for b in todo), return_exceptions=True), 55)
        return {"ok": True, "stores": len(todo), "warmed": sum(1 for r in res if isinstance(r, list) and r)}

    @app.get("/cron/notify")
    async def cron_notify(request: Request):
        """Раз на день (Vercel Cron): цікаві акції, а щопонеділка — меню на тиждень."""
        secret = os.environ.get("CRON_SECRET")
        allowed = bool(secret) and secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {secret}")
        if not allowed:
            return JSONResponse({"ok": False}, status_code=403)
        last = db.get_setting(conn, "tg_cron_at")
        if last and last > security.iso(security.now() - timedelta(hours=20)):
            return {"ok": True, "skipped": "вже надсилали сьогодні"}
        db.set_setting(conn, "tg_cron_at", security.iso(security.now()))
        conn.execute("DELETE FROM tg_sent WHERE sent_at < ?", (security.iso(security.now() - timedelta(days=30)),))
        st = {"users": 0, "deals": 0, "menus": 0, "errors": 0}
        menu_day = tgm.is_menu_day()
        chats = {r["user_id"]: r["chat_id"] for r in conn.execute("SELECT user_id, chat_id FROM tg_links").fetchall()} \
            if tg.enabled else {}
        uids = set(chats) | {r["user_id"] for r in conn.execute("SELECT DISTINCT user_id FROM push_subs").fetchall()}
        for uid in sorted(uids):
            st["users"] += 1
            try:
                await notify_user(uid, chats.get(uid), menu_day, st)
            except Exception as e:
                st["errors"] += 1
                log.warning("Telegram: розсилка користувачу: %s", str(root_error(e))[:300])
        logs.event(log, "tg.cron", logging.WARNING if st["errors"] else logging.INFO, menu_day=menu_day, **st)
        return st

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return render(request, "privacy.html", user_of(request))

    return app


# Іконка й колір плитки для товару без фото (за тегом), і підпис категорії для фільтра
VISUALS = [("coffee", "☕", 28, "Кава"), ("tea", "🍵", 95, "Чай"), ("fish", "🐟", 200, "Риба й морепродукти"), ("meat", "🍗", 12, "М'ясо"),
           ("dairy", "🥛", 210, "Молочне"), ("eggs", "🥚", 45, "Яйця"), ("vegetables", "🥦", 120, "Овочі"),
           ("fruits", "🍎", 350, "Фрукти"), ("grains", "🌾", 40, "Крупи"), ("sweets", "🍫", 330, "Солодке"),
           ("alcohol", "🍷", 345, "Напої"), ("household", "🧴", 260, "Побут"), ("gluten", "🍞", 32, "Хліб і макарони")]


# Спершу за назвою товару (точніше), потім за тегом. (основи слів, емодзі, категорія для фільтра)
NAME_VISUALS = [
    (("засіб", "засоб", "мило", "порошок", "гель для", "серветк", "папір туалет", "таблетки для", "капсули для",
      "освіжувач", "кондиціонер для", "ополіскувач", "відбілювач", "плямовивід", "для прання", "для посуд",
      "шампунь", "дезодорант", "зубна паст", "крем для", "бальзам для", "губк", "пакети для", "очищувач"), "🧴", "Побут"),
    (("паштет", "печінк", "ковбас", "сосиск"), "🥫", "М'ясо"),
    (("пиво",), "🍺", "Напої"), (("вино", "коньяк", "горілк"), "🍷", "Напої"),
    (("сік", "нектар"), "🧃", "Напої"), (("вода",), "💧", "Напої"),
    (("напій",), "🥤", "Напої"),
    (("кава",), "☕", "Кава"), (("чай",), "🍵", "Чай"),
    (("печиво", "крекер", "вафл"), "🍪", "Солодке"), (("шоколад",), "🍫", "Солодке"),
    (("цукерк",), "🍬", "Солодке"), (("морозив",), "🍦", "Солодке"),
    (("кокос",), "🥥", "Бакалія"), (("бекон",), "🥓", "М'ясо"),
    (("тофу",), "🧊", "Бакалія"), (("кіноа", "кус-кус", "кускус"), "🌾", "Крупи"), (("шпинат",), "🥬", "Овочі"),
    (("овоч", "суміш"), "🥦", "Овочі"), (("оливк", "маслин"), "🫒", "Бакалія"),
    (("горіх",), "🌰", "Бакалія"), (("тортиль", "тісто"), "🫓", "Хліб і макарони"),
    (("кисломол", "творог"), "🥛", "Молочне"), (("сир ", "сир,", "гауда", "моцарел", "бринз"), "🧀", "Молочне"),
    (("молоко", "кефір", "йогурт", "ряжанк", "сметан", "вершк"), "🥛", "Молочне"), (("масло вершк",), "🧈", "Молочне"),
    (("яйц",), "🥚", "Яйця"),
    (("макарон", "спагеті", "локшин", "фузил", "пене "), "🍝", "Хліб і макарони"),
    (("свинин", "ялович", "телятин", "фарш", "ошийок", "ребр"), "🥩", "М'ясо"),
    (("ковбас", "сосиск", "сардельк", "шинк"), "🌭", "М'ясо"),
    ((" кур", "курят", "курк", "курч", "індич", "гомілк", "стегн"), "🍗", "М'ясо"),
    (("кревет", "мідії", "мідій", "кальмар", "восьмин", "краб", "морепрод", "устриц", "ікра"), "🦐", "Риба й морепродукти"),
    (("скумбр", "лосос", "хек", "минта", "тріск", "оселед", "риб", "форел", "сьомг", "тунц", "тунец", "дорадо", "сибас"), "🐟", "Риба й морепродукти"),
    (("хліб", "батон", "багет", "лаваш"), "🍞", "Хліб і макарони"),
    (("рис",), "🍚", "Крупи"), (("гречк", "крупа", "пшон", "вівсян пластів", "булгур"), "🌾", "Крупи"),
    (("борошно",), "🌾", "Бакалія"), (("олія",), "🫒", "Бакалія"), (("цукор",), "🧂", "Бакалія"),
    (("огірк",), "🥒", "Овочі"), (("помідор", "томат"), "🍅", "Овочі"), (("картопл",), "🥔", "Овочі"),
    (("морква",), "🥕", "Овочі"), (("цибул",), "🧅", "Овочі"), (("капуст",), "🥬", "Овочі"),
    (("перець",), "🫑", "Овочі"), (("печериц", "гриб"), "🍄", "Овочі"),
    (("кабач", "цукін"), "🥒", "Овочі"), (("квасол", "сочевиц", "нут"), "🫘", "Крупи"),
    (("банан",), "🍌", "Фрукти"), (("яблук",), "🍎", "Фрукти"), (("апельсин", "мандарин"), "🍊", "Фрукти"),
    (("лимон",), "🍋", "Фрукти"), (("виноград",), "🍇", "Фрукти"), (("груш",), "🍐", "Фрукти"),
]
CAT_HUE = {label: hue for _, _, hue, label in VISUALS} | {"Бакалія": 40}


CAT_PASTEL = {"Молочне": "p-sky", "Яйця": "p-citrus", "М'ясо": "p-tomato", "Риба й морепродукти": "p-sky", "Овочі": "p-basil",
              "Фрукти": "p-citrus", "Крупи": "p-citrus", "Хліб і макарони": "p-citrus", "Бакалія": "p-citrus",
              "Кава": "p-plum", "Чай": "p-basil", "Солодке": "p-berry", "Напої": "p-plum", "Побут": "p-sky"}


# Порядок кнопок-категорій: схожі поруч (овочі з фруктами, кава з чаєм, м'ясо з рибою…)
CAT_ORDER = ["Овочі", "Фрукти", "Кава", "Чай", "М'ясо", "Риба й морепродукти", "Молочне", "Яйця", "Крупи", "Хліб і макарони",
             "Бакалія", "Солодке", "Напої", "Побут"]


FRESH_CATS = ("Фрукти", "Овочі")   # лише якщо назва ними починається («Яблука», а не «Паштет з яблуком»)


def _visual_hit(low: str, first: str, stems, label: str) -> bool:
    if label in FRESH_CATS:
        return any(first.startswith(st.strip()) for st in stems)
    return any(st in low for st in stems)


def name_visual(name: str) -> dict:
    """Емодзі, категорія й пастельний колір за назвою продукту (для рядків списку й плиток акцій)."""
    low, first = f" {(name or '').lower()} ", first_word(name)
    for stems, emoji, label in NAME_VISUALS:
        if _visual_hit(low, first, stems, label):
            return {"emoji": emoji, "cat": label, "pastel": CAT_PASTEL.get(label, "p-basil")}
    return {"emoji": "🛒", "cat": "Інше", "pastel": "p-basil"}


def offer_visual(o: Offer) -> dict:
    name, first = f" {(o.name or '').lower()} ", first_word(o.name)
    for stems, emoji, label in NAME_VISUALS:
        if _visual_hit(name, first, stems, label):
            return {"emoji": emoji, "hue": CAT_HUE.get(label, 150), "cat": label, "pastel": CAT_PASTEL.get(label, "p-basil")}
    for tag, emoji, hue, label in VISUALS:
        if tag in o.tags:
            return {"emoji": emoji, "hue": hue, "cat": label, "pastel": CAT_PASTEL.get(label, "p-basil")}
    cat = o.category or "Інше"
    return {"emoji": "🛒", "hue": 150, "cat": cat, "pastel": CAT_PASTEL.get(cat, "p-basil")}


STATIC_DIR = Path(__file__).parent / "static"


def profile_from_form(form, prev: Profile) -> tuple[Profile, list[str], bool]:
    """Анкета профілю → Profile. Повертає також вибрані обмеження й чи є згода (без згоди обмеження не зберігаємо)."""
    def lst(key):
        return [x.strip() for x in str(form.get(key, "")).replace(";", ",").replace("\n", ",").split(",") if x.strip()]

    def num(key, default, cast=float):
        try:
            return cast(form.get(key)) if form.get(key) not in (None, "") else default
        except (TypeError, ValueError):
            return default

    recipe_ids = {r.id for r in menu.RECIPES}
    diets = [d for d in form.getlist("diets") if d in DIETS]
    consent = bool(form.get("health_consent"))
    # Оновлюємо лише те, що є у формі (коротка анкета не має скидати сім'ю, смаки, меню тощо).
    # «_f» — які групи редагує форма (порожні чекбокси інакше не відрізнити від «поля немає»).
    marked = set(form.getlist("_f"))
    has = lambda key: key in form or key in marked
    p = Profile.from_dict(prev.to_dict())
    p.lat, p.lon, p.location_at = prev.lat, prev.lon, prev.location_at
    if has("name"):
        p.name = str(form.get("name") or "Мій профіль")[:60]
    if has("household"):
        p.household = max(1, min(12, num("household", 1, int)))
    if has("diets") or has("health_consent"):
        p.diets = diets if consent else []
    if has("likes") or has("dislikes"):
        p.likes = [t for t in form.getlist("likes") if t in TAG_LABELS]
        p.dislikes = [t for t in form.getlist("dislikes") if t in TAG_LABELS]
    for key, cap in (("regulars", 30), ("brands_like", 20), ("brands_avoid", 20)):
        if has(key):
            setattr(p, key, lst(key)[:cap])
    if has("city"):
        p.city = str(form.get("city") or "")[:60] or None
    if has("budget_week"):
        p.budget_week = num("budget_week", None) or None
    if has("min_discount"):
        p.min_discount = max(0.0, min(80.0, num("min_discount", 10.0)))
    if has("max_items"):
        p.max_items = max(5, min(40, num("max_items", 20, int)))
    if has("radius_km"):
        p.radius_km = max(0.3, min(5.0, num("radius_km", 1.0)))
    if has("dish_likes"):
        p.dish_likes = [x for x in lst("dish_likes") if x in recipe_ids][:30]
    if has("dish_dislikes"):
        p.dish_dislikes = [x for x in lst("dish_dislikes") if x in recipe_ids][:400]
    if not has("diets") and not has("health_consent"):
        diets, consent = list(prev.diets), bool(prev.diets)
    if p.dish_dislikes != prev.dish_dislikes:   # нелюбі страви не мають лишитись у збереженому меню
        p.menu = [x for x in p.menu if x not in p.dish_dislikes]
    return p, diets, consent


def in_app_browser(request) -> str | None:
    """Вбудований браузер месенджера (там людина не увійшла на silpo.ua): назва або None."""
    ua = request.headers.get("user-agent", "")
    for key, name in (("Telegram", "Telegram"), ("Instagram", "Instagram"), ("FBAN", "Facebook"), ("FBAV", "Facebook"),
                      ("Viber", "Viber"), ("TikTok", "TikTok")):
        if key in ua:
            return name
    return None


def link_shape(url: str | None) -> str | None:
    """Адреса без ідентифікаторів: https://silpo.ua/cart/<id>?token=… → https://silpo.ua/cart/#?token=…"""
    if not url:
        return None
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
    except ValueError:
        return "?"
    path = re.sub(r"[0-9a-fA-F-]{6,}|\d+", "#", u.path)
    params = ",".join(sorted({kv.split("=")[0] for kv in u.query.split("&") if kv}))
    return f"{u.scheme}://{u.netloc}{path}" + (f"?{params}" if params else "")


def watch_word(name: str) -> str:
    """Що стежити для товару: «Кава зернова Lavazza 1кг» → «кава зернова»."""
    return silpo.generic_name(name) or " ".join(name.lower().split()[:2])


def root_error(e: BaseException) -> BaseException:
    """anyio загортає помилки всередині з'єднання MCP у групу — дістаємо першу справжню."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


@lru_cache(maxsize=1)
def menu_credits() -> dict:
    """Автори фото страв (ліцензії CC вимагають їх зазначати) — показуємо на сторінці /photos."""
    path = STATIC_DIR / "dishes" / "credits.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


NEAR_MAX_KM = 50
PRODUCT_PHOTO_KEYS = ["p:" + k for k in unsplash.PRODUCT_QUERIES]
photo_job: contextvars.ContextVar[list | None] = contextvars.ContextVar("photo_job", default=None)

def local_photos() -> dict[str, str]:
    return {d: f"/static/dishes/{d}.jpg" for d in menu_credits()}


def offer_from_dict(d: dict) -> Offer:
    known = {k: v for k, v in d.items() if k in Offer.__dataclass_fields__}
    known["tags"] = set()        # теги з назви перераховуємо: кеш міг зберегти їх за старими правилами
    return enrich(Offer(**known))


app = create_app() if os.environ.get("APP_SECRET") else None
