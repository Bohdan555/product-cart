"""Сайт: реєстрація → профіль → підключення «Сільпо» → готовий список з акцій.

Запуск: APP_SECRET=... BASE_URL=http://127.0.0.1:8000 uvicorn productcart.web.app:app
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import timedelta
from urllib.parse import unquote
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import matcher, planner
from ..catalog import TAG_LABELS, enrich
from ..models import DIET_LABELS, DIETS, Offer, Profile
from ..render import CHAIN_NAMES, uah
from ..sources import demo, silpo
from . import analytics, db, security
from .analytics import verify_click
from .silpo_oauth import SilpoGateway, expires_soon, pkce_pair

HERE = Path(__file__).parent
COOKIE = "pc_session"
OFFERS_TTL = timedelta(hours=6)
LIKE_TAGS = ["dairy", "meat", "fish", "eggs", "vegetables", "fruits", "grains", "coffee", "tea", "sweets"]
HIDE_TAGS = ["alcohol", "sweets", "household", "pork", "fish", "meat"]


def create_app(db_path: str | None = None, app_secret: str | None = None, base_url: str | None = None,
               gateway: SilpoGateway | None = None, secure_cookies: bool | None = None) -> FastAPI:
    app_secret = app_secret or os.environ.get("APP_SECRET", "")
    vercel_url = os.environ.get("VERCEL_PROJECT_PRODUCTION_URL")  # Vercel задає сам, без https://
    base_url = (base_url or os.environ.get("BASE_URL")
                or (f"https://{vercel_url}" if vercel_url else "http://127.0.0.1:8000")).rstrip("/")
    conn = db.connect(db_path)
    box = security.TokenBox(app_secret)
    gw = gateway or SilpoGateway(conn, redirect_uri=f"{base_url}/auth/silpo/callback")
    secure = base_url.startswith("https://") if secure_cookies is None else secure_cookies

    app = FastAPI(title="Кошик з акцій", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.globals.update(uah=uah, chain_name=lambda k: CHAIN_NAMES.get(k.split(" · ")[0], k.split(" · ")[0]),
                           DIET_LABELS=DIET_LABELS, TAG_LABELS=TAG_LABELS)
    app.state.conn, app.state.gw = conn, gw
    analytics.init(conn)
    admin_emails = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
    tpl.env.globals.update(click_url=lambda o: analytics.click_url(app_secret, o))

    # ---------- допоміжне ----------
    def user_of(request: Request):
        return security.session_user(conn, request.cookies.get(COOKIE))

    def render(request: Request, name: str, user=None, status: int = 200, **ctx):
        is_admin = bool(user) and user["email"].lower() in admin_emails
        return tpl.TemplateResponse(request, name, dict(ctx, user=user, is_admin=is_admin), status_code=status)

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

    def cached_offers(store_id: str) -> list[Offer] | None:
        r = conn.execute("SELECT fetched_at, data FROM offers_cache WHERE chain='silpo' AND store_id=?", (store_id,)).fetchone()
        if not r or r["fetched_at"] < security.iso(security.now() - OFFERS_TTL):
            return None
        return [offer_from_dict(d) for d in json.loads(r["data"])]

    async def silpo_offers(user_id: int, force: bool = False) -> tuple[list[Offer], str | None]:
        link = silpo_link(user_id)
        if not link or not link["store_id"]:
            return [], "Оберіть свій магазин «Сільпо»."
        if not force and (hit := cached_offers(link["store_id"])) is not None:
            return hit, None
        tokens = silpo_tokens(user_id)
        if not tokens:
            return [], "Вхід у «Сільпо» застарів. Підключіть «Сільпо» ще раз."
        try:
            async with gw.session(tokens["access_token"]) as call:
                offers = await silpo.fetch_offers(call, {"id": link["store_id"], "title": link["store_title"]})
        except Exception as e:
            return [], f"«Сільпо» не відповіло: {str(e)[:120]}"
        conn.execute("""INSERT INTO offers_cache (chain, store_id, fetched_at, data) VALUES ('silpo', ?, ?, ?)
                        ON CONFLICT(chain, store_id) DO UPDATE SET fetched_at=excluded.fetched_at, data=excluded.data""",
                     (link["store_id"], security.iso(security.now()), json.dumps([o.to_dict() for o in offers], ensure_ascii=False)))
        return offers, None

    # ---------- сторінки ----------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        u = user_of(request)
        if u:
            return go("/list")
        return render(request, "index.html")

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
        uid = conn.execute("INSERT INTO users (email, pw_hash, created_at) VALUES (?, ?, ?) RETURNING id",
                           (email, security.hash_password(password), security.iso(security.now()))).fetchone()["id"]
        token, _ = security.new_session(conn, uid)
        analytics.track(conn, uid, "signup")
        resp = go("/profile?welcome=1")
        resp.set_cookie(COOKIE, token, max_age=security.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure)
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return render(request, "auth.html", mode="login")

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...)):
        r = conn.execute("SELECT id, pw_hash FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
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
        return resp

    # ---------- профіль ----------
    @app.get("/profile", response_class=HTMLResponse)
    def profile_form(request: Request, welcome: int = 0):
        u, bad = need_user(request)
        if bad:
            return bad
        p, consent = load_profile(u["id"])
        return render(request, "profile.html", u, p=p, consent=consent, welcome=welcome, like_tags=LIKE_TAGS,
                      hide_tags=HIDE_TAGS, diets=list(DIETS), silpo=silpo_link(u["id"]))

    @app.post("/profile")
    async def profile_save(request: Request):
        form = await request.form()
        u, bad = need_user(request, form.get("csrf", ""))
        if bad:
            return bad

        def lst(key):
            return [x.strip() for x in str(form.get(key, "")).replace(";", ",").replace("\n", ",").split(",") if x.strip()]

        diets = [d for d in form.getlist("diets") if d in DIETS]
        consent = bool(form.get("health_consent"))
        p = Profile(
            name=str(form.get("name") or "Мій профіль")[:60],
            household=max(1, min(12, int(form.get("household") or 1))),
            diets=diets if consent else [],
            likes=[t for t in form.getlist("likes") if t in TAG_LABELS],
            dislikes=[t for t in form.getlist("dislikes") if t in TAG_LABELS],
            regulars=lst("regulars")[:30],
            brands_like=lst("brands_like")[:20],
            brands_avoid=lst("brands_avoid")[:20],
            city=str(form.get("city") or "")[:60] or None,
            budget_week=float(form.get("budget_week")) if form.get("budget_week") else None,
            min_discount=max(0.0, min(80.0, float(form.get("min_discount") or 10))),
            max_items=max(5, min(40, int(form.get("max_items") or 20))),
        )
        if diets and not consent:
            return render(request, "profile.html", u, p=p, consent=False, like_tags=LIKE_TAGS, hide_tags=HIDE_TAGS,
                          diets=list(DIETS), silpo=silpo_link(u["id"]), status=400, picked_diets=diets,
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
                    async with gw.session(tokens["access_token"]) as call:
                        if near_mode:
                            stores = silpo.nearest(await silpo.list_branches(call), lat, lon, n=6)
                        else:
                            stores = (await silpo.list_branches(call, city or None, q or None))[:40]
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

    # ---------- список ----------
    @app.get("/list", response_class=HTMLResponse)
    async def shopping_list(request: Request, refresh: int = 0):
        u, bad = need_user(request)
        if bad:
            return bad
        p, _ = load_profile(u["id"])
        link = silpo_link(u["id"])
        notice = None
        if link and link["store_id"]:
            offers, notice = await silpo_offers(u["id"], force=bool(refresh))
            is_demo = False
        else:
            offers, is_demo = demo.load(), True
        matches, rejected = matcher.rank(offers, p)
        sl = planner.build(matches, p)
        analytics.track(conn, u["id"], "list_refreshed" if refresh else "list_viewed",
                        chain=None if is_demo else "silpo", store_id=None if is_demo else link["store_id"],
                        source="demo" if is_demo else "silpo", offers=len(offers), items=len(sl.items), error=bool(notice))
        return render(request, "list.html", u, p=p, sl=sl, offers_total=len(offers), rejected=rejected[:40],
                      is_demo=is_demo, link=link, notice=notice, need_store=bool(link and not link["store_id"]))

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


def offer_from_dict(d: dict) -> Offer:
    known = {k: v for k, v in d.items() if k in Offer.__dataclass_fields__}
    known["tags"] = set(known.get("tags") or [])
    return enrich(Offer(**known))


app = create_app() if os.environ.get("APP_SECRET") else None
