"""Сайт: реєстрація → профіль → підключення «Сільпо» → готовий список з акцій.

Запуск: APP_SECRET=... BASE_URL=http://127.0.0.1:8000 uvicorn productcart.web.app:app
"""
from __future__ import annotations

import asyncio
import hashlib
import time
import logging
import contextlib
import json
import os
import secrets
from datetime import timedelta
from functools import lru_cache
from urllib.parse import quote, unquote
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import matcher, menu, planner
from ..catalog import TAG_LABELS, enrich
from ..models import DIET_LABELS, DIETS, Offer, Profile
from ..render import CHAIN_NAMES, uah
from ..sources import demo, silpo, silpo_cart
from . import analytics, db, security
from .analytics import verify_click
from .google_oauth import GoogleAuth
from .silpo_oauth import SilpoGateway, expires_soon, pkce_pair

HERE = Path(__file__).parent
log = logging.getLogger("productcart.web")
COOKIE = "pc_session"
GOOGLE_COOKIE = "pc_google"  # state і PKCE verifier незавершеного входу через Google, зашифровані
CART_COOKIE = "pc_cart_n"  # скільки товарів у кошику «Сільпо» (лише для значка)
GUEST_COOKIE = "pc_guest"  # налаштування меню гостя (без реєстрації), зашифровані
GUEST_KEYS = ("household", "dinners", "max_minutes", "menu", "menu_skip", "pantry_buy")
OFFERS_TTL = timedelta(hours=6)
MAX_NEARBY = 4  # щонайбільше магазинів поруч в одному списку
LIKE_TAGS = ["dairy", "meat", "fish", "eggs", "vegetables", "fruits", "grains", "coffee", "tea", "sweets"]
HIDE_TAGS = ["alcohol", "sweets", "household", "pork", "fish", "meat"]


def create_app(db_path: str | None = None, app_secret: str | None = None, base_url: str | None = None,
               gateway: SilpoGateway | None = None, secure_cookies: bool | None = None,
               google: GoogleAuth | None = None) -> FastAPI:
    app_secret = app_secret or os.environ.get("APP_SECRET", "")
    vercel_url = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")  # Vercel задає сам, без https://
    base_url = (base_url or os.environ.get("BASE_URL")
                or (f"https://{vercel_url}" if vercel_url else "http://127.0.0.1:8000")).rstrip("/")
    conn = db.connect(db_path)
    box = security.TokenBox(app_secret)
    gw = gateway or SilpoGateway(conn, redirect_uri=f"{base_url}/auth/silpo/callback")
    secure = base_url.startswith("https://") if secure_cookies is None else secure_cookies
    google = google or GoogleAuth(redirect_uri=f"{base_url}/auth/google/callback")

    app = FastAPI(title="Кошик з акцій", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def timing(request: Request, call_next):
        # Server-Timing: загальний час, база (кількість і час), регіон функції
        stats = [0, 0.0]
        token = db.db_stats.set(stats)
        t = time.perf_counter()
        try:
            resp = await call_next(request)
        finally:
            db.db_stats.reset(token)
        total = (time.perf_counter() - t) * 1000
        resp.headers["Server-Timing"] = (f'app;dur={total:.0f}, db;dur={stats[1] * 1000:.0f};desc="{stats[0]} queries", '
                                         f'region;desc="{os.environ.get("VERCEL_REGION", "local")}"')
        if total > 800 and not request.url.path.startswith("/static/"):
            log.warning("Повільно: %s %s — %.0f мс, база %d запитів / %.0f мс", request.method, request.url.path,
                        total, stats[0], stats[1] * 1000)
        return resp

    @app.middleware("http")
    async def cache_static(request: Request, call_next):
        # статика (фото страв, стилі) кешується браузером і CDN Vercel, а не віддається функцією щоразу
        resp = await call_next(request)
        if request.url.path.startswith("/static/") and resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=2592000, stale-while-revalidate=604800"
        return resp
    tpl = Jinja2Templates(directory=HERE / "templates")
    css_v = hashlib.sha1((HERE / "static" / "style.css").read_bytes()).hexdigest()[:10]
    tpl.env.globals.update(css_v=css_v)
    tpl.env.globals.update(uah=uah, chain_name=lambda k: CHAIN_NAMES.get(k.split(" · ")[0], k.split(" · ")[0]),
                           DIET_LABELS=DIET_LABELS, TAG_LABELS=TAG_LABELS)
    app.state.conn, app.state.gw = conn, gw
    analytics.init(conn)
    admin_emails = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
    tpl.env.globals.update(click_url=lambda o: analytics.click_url(app_secret, o), visual=offer_visual, plural=plural)

    # ---------- допоміжне ----------
    def user_of(request: Request):
        return security.session_user(conn, request.cookies.get(COOKIE))

    def render(request: Request, name: str, user=None, status: int = 200, **ctx):
        is_admin = bool(user) and user["email"].lower() in admin_emails
        n = request.cookies.get(CART_COOKIE, "")
        return tpl.TemplateResponse(request, name, dict(ctx, user=user, is_admin=is_admin, google_on=google.enabled,
                                                        cart_n=int(n) if n.isdigit() else None), status_code=status)

    def go(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

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
            except Exception:
                return None
            conn.execute("UPDATE silpo_links SET tokens_enc=? WHERE user_id=?", (box.seal(json.dumps(tokens)), user_id))
        return tokens

    parsed_offers: dict[str, tuple[str, list[dict]]] = {}  # у межах процесу: store_id → (fetched_at, акції)

    def cached_offers(store_id: str) -> list[Offer] | None:
        # спершу лише дата (дешево); великий JSON читаємо й розбираємо, тільки якщо його ще немає в пам'яті
        r = conn.execute("SELECT fetched_at FROM offers_cache WHERE chain='silpo' AND store_id=?", (store_id,)).fetchone()
        if not r or r["fetched_at"] < security.iso(security.now() - OFFERS_TTL):
            return None
        hit = parsed_offers.get(store_id)
        if not hit or hit[0] != r["fetched_at"]:
            row = conn.execute("SELECT data FROM offers_cache WHERE chain='silpo' AND store_id=?", (store_id,)).fetchone()
            if not row:
                return None
            hit = parsed_offers[store_id] = (r["fetched_at"], json.loads(row["data"]))
        return [offer_from_dict(d) for d in hit[1]]

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

    async def silpo_list_input(user_id: int, p: Profile, force: bool = False):
        """Магазини для списку та їхні акції.
        Повертає (stores, offers, notice): stores — магазини поруч (з km) або вибраний вручну."""
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
                for st in stores:
                    hit = None if force else cached_offers(st["id"])
                    if hit is not None:
                        offers += [dict_store(o, st) for o in hit]
                    else:
                        todo.append(st)
                call = await get_call() if todo else None
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
        return stores, offers, notice

    # ---------- сторінки ----------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        u = user_of(request)
        if u:
            return go("/list")
        offers = planner.merge_nearby(demo.load())
        analytics.track(conn, None, "landing_viewed")
        previews = [(n, menu.plan(offers, Profile(name="Гість", household=n), dinners=5, max_minutes=60)) for n in (1, 2, 4)]
        return render(request, "index.html", previews=previews, days=menu.DAYS, photos=menu_photos(),
                      recipes=len(menu.RECIPES))

    @app.get("/register", response_class=HTMLResponse)
    def register_form(request: Request):
        return render(request, "auth.html", mode="register")

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
        return signed_in(uid, "/profile?welcome=1")

    def new_user(request: Request, email: str, pw_hash: str, method: str) -> int:
        uid = conn.execute("INSERT INTO users (email, pw_hash, created_at) VALUES (?, ?, ?) RETURNING id",
                           (email, pw_hash, security.iso(security.now()))).fetchone()["id"]
        analytics.track(conn, uid, "signup", method=method)
        if request.cookies.get(GUEST_COOKIE):  # налаштування меню, зроблені до реєстрації, переходять у профіль
            g = guest_profile(request)
            save_profile(uid, Profile(name="Мій профіль", **{k: getattr(g, k) for k in GUEST_KEYS}), False)
        return uid

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
            resp = signed_in(r["id"], "/list")
        else:
            resp = signed_in(new_user(request, email, "google", "google"), "/profile?welcome=1")
        resp.delete_cookie(GOOGLE_COOKIE)
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "auth.html", mode="login")

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...)):
        r = conn.execute("SELECT id, pw_hash FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        if r and r["pw_hash"] == "google":
            return render(request, "auth.html", mode="login", email=email, status=400,
                          error="Цей профіль створено через Google. Натисніть «Увійти через Google».")
        if not r or not security.verify_password(password, r["pw_hash"]):
            return render(request, "auth.html", mode="login", error="Неправильний email або пароль.", email=email, status=400)
        token, _ = security.new_session(conn, r["id"])
        analytics.track(conn, r["id"], "login")
        resp = go("/list")
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
        return render(request, "profile.html", u, p=p, consent=consent, like_tags=LIKE_TAGS, hide_tags=HIDE_TAGS,
                      diets=list(DIETS), silpo=silpo_link(u["id"]), status=status, photos=photos,
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
        return go("/silpo" if not silpo_link(u["id"]) else "/list")

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
        return render(request, "silpo.html", u, link=link, stores=stores, city=city or (p.city or ""), q=q, error=err,
                      near_mode=near_mode, ip_city=ip_city)

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
            return go(gw.authorize_url(state, challenge))
        except Exception as e:
            return go(f"/silpo?error=Не вдалося відкрити вхід «Сільпо»: {str(e)[:100]}")

    @app.get("/auth/silpo/callback")
    def silpo_callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
        u, bad = need_user(request)
        if bad:
            return bad
        pending = conn.execute("SELECT * FROM oauth_pending WHERE state=? AND user_id=?", (state, u["id"])).fetchone()
        conn.execute("DELETE FROM oauth_pending WHERE state=?", (state,))
        if error or not code:
            return go(f"/silpo?error=Вхід у «Сільпо» скасовано: {error_description or error or 'немає коду'}")
        if not pending:
            return go("/silpo?error=Сесія входу застаріла. Натисніть «Підключити Сільпо» ще раз.")
        try:
            tokens = gw.exchange(code, pending["verifier"])
        except Exception as e:
            return go(f"/silpo?error={str(e)[:150]}")
        conn.execute("""INSERT INTO silpo_links (user_id, tokens_enc, connected_at) VALUES (?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET tokens_enc=excluded.tokens_enc, connected_at=excluded.connected_at""",
                     (u["id"], box.seal(json.dumps(tokens)), security.iso(security.now())))
        analytics.track(conn, u["id"], "silpo_connected")
        return go("/silpo")

    @app.post("/silpo/store")
    def silpo_store(request: Request, csrf: str = Form(""), store_id: str = Form(...), store_title: str = Form("")):
        u, bad = need_user(request, csrf)
        if bad:
            return bad
        conn.execute("UPDATE silpo_links SET store_id=?, store_title=? WHERE user_id=?", (store_id, store_title[:120], u["id"]))
        analytics.track(conn, u["id"], "store_selected", chain="silpo", store_id=store_id)
        return go("/list")

    @app.post("/silpo/disconnect")
    def silpo_disconnect(request: Request, csrf: str = Form("")):
        u, bad = need_user(request, csrf)
        if bad:
            return bad
        conn.execute("DELETE FROM silpo_links WHERE user_id=?", (u["id"],))
        return go("/silpo")

    async def current_offers(user_id: int, p: Profile, refresh: bool):
        """Акції магазинів поруч (або демо, якщо «Сільпо» не підключено)."""
        link = silpo_link(user_id)
        stores, offers, notice = await silpo_list_input(user_id, p, force=refresh) if link else ([], [], None)
        is_demo = not offers and not notice
        if is_demo:
            offers = demo.load()
        return link, stores, planner.merge_nearby(offers), notice, is_demo

    # ---------- список ----------
    @app.get("/list", response_class=HTMLResponse)
    async def shopping_list(request: Request, refresh: int = 0):
        u, bad = need_user(request)
        if bad:
            return bad
        p, _ = load_profile(u["id"])
        link, stores, offers, notice, is_demo = await current_offers(u["id"], p, bool(refresh))
        matches, rejected = matcher.rank(offers, p)
        sl = planner.build(matches, p)
        analytics.track(conn, u["id"], "list_refreshed" if refresh else "list_viewed",
                        chain=None if is_demo else "silpo", store_id=",".join(st["id"] for st in stores)[:200] or None,
                        source="demo" if is_demo else "silpo", stores=len(stores), offers=len(offers),
                        items=len(sl.items), error=bool(notice), located=p.lat is not None)
        return render(request, "list.html", u, p=p, sl=sl, offers_total=len(offers), rejected=rejected[:40],
                      is_demo=is_demo, link=link, notice=notice, stores=stores,
                      need_location=bool(link and p.lat is None and not link["store_id"]))

    # ---------- меню ----------
    # Меню відкрите й без реєстрації: гість бачить демо-акції, його налаштування живуть у зашифрованій cookie.
    async def menu_offers(u, p: Profile, refresh: bool = False):
        if u:
            return await current_offers(u["id"], p, refresh)
        return None, [], planner.merge_nearby(demo.load()), None, True

    def menu_done(u, p: Profile, health: bool, url: str):
        if u:
            save_profile(u["id"], p, health)
            return go(url)
        return keep_guest(go(url), p)

    @app.get("/menu", response_class=HTMLResponse)
    async def menu_page(request: Request, refresh: int = 0):
        u = user_of(request)
        p, health = load_profile(u["id"]) if u else (guest_profile(request), False)
        link, stores, offers, notice, is_demo = await menu_offers(u, p, bool(refresh))
        m = menu.plan(offers, p, dinners=p.dinners, max_minutes=p.max_minutes, budget=p.menu_budget,
                      keep=p.menu, pantry_buy=p.pantry_buy)
        changed = m.ids != p.menu
        p.menu = m.ids
        if changed and u:
            save_profile(u["id"], p, health)
        analytics.track(conn, u["id"] if u else None, "menu_viewed", source="demo" if is_demo else "silpo",
                        stores=len(stores), dishes=len(m.dishes), promo_lines=len(m.promo_lines), total=m.total,
                        saving=m.saving, guest=not u)
        resp = render(request, "menu.html", u, p=p, m=m, is_demo=is_demo, link=link, notice=notice, stores=stores,
                      days=menu.DAYS, fmt_qty=menu.fmt_qty, ingredients=menu.INGREDIENTS, guest=not u,
                      photos=menu_photos(), swapped=request.query_params.get("swapped"))
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
                p.menu, p.menu_skip = [], []
            if form.get("max_minutes"):
                p.max_minutes = max(15, min(180, int(form["max_minutes"])))
                p.menu, p.menu_skip = [], []
        except ValueError:
            return HTMLResponse("Неправильні налаштування меню.", status_code=400)
        analytics.track(conn, u["id"] if u else None, "menu_settings", dinners=p.dinners, max_minutes=p.max_minutes,
                        people=p.household, guest=not u)
        return menu_done(u, p, health, "/menu")

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
        p.menu_skip = ([current[i]] + [x for x in p.menu_skip if x != current[i]])[:6]
        p.menu = new
        analytics.track(conn, u["id"] if u else None, "menu_swap", dish_from=current[i], dish_to=new[i], guest=not u)
        return menu_done(u, p, health, f"/menu?swapped={i}#d{i}")

    @app.post("/menu/pantry")
    async def menu_pantry(request: Request):
        form, u, p, health, bad = await menu_form(request)
        if bad:
            return bad
        key = str(form.get("key", ""))
        if key not in menu.PANTRY:
            return HTMLResponse("Невідомий продукт.", status_code=400)
        p.pantry_buy = [k for k in p.pantry_buy if k != key] if key in p.pantry_buy else p.pantry_buy + [key]
        return menu_done(u, p, health, "/menu#list")

    # ---------- перегляд кошика «Сільпо» ----------
    @app.get("/silpo/cart", response_class=HTMLResponse)
    async def silpo_cart_view(request: Request, error: str = "", changed: str = "", cleared: int = 0):
        u, bad = need_user(request)
        if bad:
            return bad
        tokens = silpo_tokens(u["id"]) if silpo_link(u["id"]) else None
        if not tokens:
            return go("/silpo")
        cart, err = None, None
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                cart = await silpo_cart.current_cart(await get_call())
        except Exception as e:
            e = root_error(e)
            log.warning("Сільпо: перегляд кошика: %s: %s", type(e).__name__, str(e)[:800])
            err = f"«Сільпо» не відповіло: {str(e)[:150]}"
        analytics.track(conn, u["id"], "silpo_cart_viewed", items=cart.count if cart else 0)
        resp = render(request, "silpo_cart.html", u, cart=cart, error=err, notice=error[:300] or None, changed=changed,
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
        bought = {k for k in str(form.get("bought", "")).split(",") if k}
        wants = silpo_cart.wants_from_menu(m.cart, bought)
        ctx = dict(bought=",".join(sorted(bought)), wanted=len(wants))
        if not wants:
            return render(request, "cart.html", u, error="Усе зі списку вже позначено як куплене — додавати нічого.", **ctx)
        try:
            async with lazy_silpo(tokens["access_token"]) as get_call:
                call = await get_call()
                if not form.get("confirm"):
                    cart = await silpo_cart.current_cart(call)
                    if cart and cart.count:
                        sample = [n for n in cart.names.values() if n][:5]
                        return render(request, "cart.html", u, confirm=True, had=cart.count, sample=sample, **ctx)
                branch = None
                if stores:   # магазин для самовивозу, якщо кошика ще немає: найближчий із меню, з координатами
                    branch = dict(stores[0])
                    if branch.get("lat") is None:
                        known = {b["id"]: b for b in await silpo_branches(get_call)}
                        branch = {**known.get(branch["id"], {}), **{k: v for k, v in branch.items() if v is not None}}
                res = await silpo_cart.build(call, wants, branch)
        except Exception as e:
            err = root_error(e)
            if isinstance(err, silpo_cart.CartError):
                return render(request, "cart.html", u, error=str(err), **ctx)
            log.warning("Сільпо: кошик не зібрався: %s: %s", type(err).__name__, str(err)[:1500])
            return render(request, "cart.html", u, status=502, **ctx,
                          error=f"«Сільпо» відповіло помилкою: {str(err)[:200]}. Спробуйте ще раз за хвилину.")
        analytics.track(conn, u["id"], "silpo_cart_built", placed=len(res["placed"]), missing=len(res["missing"]),
                        had=res["had"], total=res["total"], checkout=bool(res["checkout"]))
        return remember_cart(render(request, "cart.html", u, res=res, **ctx), res["count"], u["id"])

    @app.get("/photos", response_class=HTMLResponse)
    def photo_credits(request: Request):
        return render(request, "photos.html", user_of(request), credits=menu_credits(),
                      titles={r.id: r.title for r in menu.RECIPES})

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

    @app.get("/admin/stats", response_class=HTMLResponse)
    def admin_stats(request: Request, days: int = 30):
        u, bad = need_user(request)
        if bad:
            return bad
        if u["email"].lower() not in admin_emails:
            return HTMLResponse("Сторінка доступна лише власнику сайту.", status_code=403)
        return render(request, "stats.html", u, st=analytics.stats(conn, days=max(7, min(90, days))))

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return render(request, "privacy.html", user_of(request))

    return app


# Іконка й колір плитки для товару без фото (за тегом), і підпис категорії для фільтра
VISUALS = [("coffee", "☕", 28, "Кава"), ("tea", "🍵", 95, "Чай"), ("fish", "🐟", 200, "Риба"), ("meat", "🍗", 12, "М'ясо"),
           ("dairy", "🥛", 210, "Молочне"), ("eggs", "🥚", 45, "Яйця"), ("vegetables", "🥦", 120, "Овочі"),
           ("fruits", "🍎", 350, "Фрукти"), ("grains", "🌾", 40, "Крупи"), ("sweets", "🍫", 330, "Солодке"),
           ("alcohol", "🍷", 345, "Напої"), ("household", "🧴", 260, "Побут"), ("gluten", "🍞", 32, "Хліб і макарони")]


def offer_visual(o: Offer) -> dict:
    for tag, emoji, hue, label in VISUALS:
        if tag in o.tags:
            return {"emoji": emoji, "hue": hue, "cat": label}
    return {"emoji": "🛒", "hue": 150, "cat": o.category or "Інше"}


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
    p = Profile(
        name=str(form.get("name") or "Мій профіль")[:60],
        household=max(1, min(12, num("household", 1, int))),
        diets=diets if consent else [],
        likes=[t for t in form.getlist("likes") if t in TAG_LABELS],
        dislikes=[t for t in form.getlist("dislikes") if t in TAG_LABELS],
        regulars=lst("regulars")[:30],
        brands_like=lst("brands_like")[:20],
        brands_avoid=lst("brands_avoid")[:20],
        city=str(form.get("city") or "")[:60] or None,
        budget_week=num("budget_week", None) or None,
        min_discount=max(0.0, min(80.0, num("min_discount", 10.0))),
        max_items=max(5, min(40, num("max_items", 20, int))),
        radius_km=max(0.3, min(5.0, num("radius_km", 1.0))),
        dish_likes=[x for x in lst("dish_likes") if x in recipe_ids][:30],
        dish_dislikes=[x for x in lst("dish_dislikes") if x in recipe_ids][:30],
    )
    p.lat, p.lon, p.location_at = prev.lat, prev.lon, prev.location_at
    for k in ("dinners", "max_minutes", "menu_budget", "menu", "menu_skip", "pantry_buy"):
        setattr(p, k, getattr(prev, k))
    if p.dish_dislikes != prev.dish_dislikes:   # нелюбі страви не мають лишитись у збереженому меню
        p.menu = [x for x in p.menu if x not in p.dish_dislikes]
    return p, diets, consent


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


def menu_photos() -> set[str]:
    return set(menu_credits())


def offer_from_dict(d: dict) -> Offer:
    known = {k: v for k, v in d.items() if k in Offer.__dataclass_fields__}
    known["tags"] = set(known.get("tags") or [])
    return enrich(Offer(**known))


app = create_app() if os.environ.get("APP_SECRET") else None
