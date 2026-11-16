"""Повний шлях на сайті з підробленим «Сільпо»: реєстрація → профіль → підключення → магазин → список."""
import contextlib
import re
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi.testclient import TestClient

from productcart.web.app import create_app
from test_silpo import FakeMCP


class FakeGateway:
    def __init__(self):
        self.refreshed = 0

    def authorize_url(self, state, challenge):
        return f"https://mcp.silpo.test/authorize?state={state}&code_challenge={challenge}"

    def exchange(self, code, verifier):
        assert code == "good-code" and len(verifier) >= 43
        return {"access_token": "AT-secret-123", "refresh_token": "RT", "expires_in": 3600, "obtained_at": 10**10}

    def refresh(self, tokens):
        self.refreshed += 1
        return dict(tokens, access_token="AT-new")

    @contextlib.asynccontextmanager
    async def session(self, access_token):
        assert access_token.startswith("AT-")
        yield FakeMCP()


import os

PG_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG_URL else []))
def client(tmp_path, request):
    if request.param == "postgres":
        import psycopg
        with psycopg.connect(PG_URL, autocommit=True) as c:
            c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        target = PG_URL
    else:
        target = str(tmp_path / "web.db")
    app = create_app(db_path=target, app_secret="x" * 32, base_url="http://test", gateway=FakeGateway())
    with TestClient(app, follow_redirects=False) as c:
        yield c


def csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def register(c, email="a@b.ua"):
    r = c.post("/register", data={"email": email, "password": "password123", "agree": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/start"
    return csrf(c.get("/profile").text)


def test_register_validation_and_login(client):
    r = client.post("/register", data={"email": "bad", "password": "password123", "agree": "1"})
    assert r.status_code == 400 and "email" in r.text
    register(client)
    r = client.post("/register", data={"email": "a@b.ua", "password": "password123", "agree": "1"})
    assert "уже зареєстровано" in r.text
    client.cookies.clear()
    assert client.post("/login", data={"email": "a@b.ua", "password": "wrong-pass"}).status_code == 400
    assert client.post("/login", data={"email": "A@B.ua", "password": "password123"}).headers["location"] == "/list"


def test_pages_require_login(client):
    for path in ("/profile", "/silpo"):
        assert client.get(path).headers["location"] == "/login"


def test_health_consent_and_csrf(client):
    token = register(client)
    data = {"csrf": token, "name": "Я", "household": "2", "diets": "no_lactose", "regulars": "молоко, кава зернова"}
    r = client.post("/profile", data=data)
    assert r.status_code == 400 and "згоду" in r.text
    assert client.post("/profile", data=dict(data, csrf="forged")).status_code == 400
    r = client.post("/profile", data=dict(data, health_consent="1"))
    assert r.status_code == 303 and r.headers["location"] == "/silpo"
    page, rejected = client.get("/list").text.split("<details")
    assert "Демо-ціни" in page and "Молоко 2,5% 900 г" not in page and "безлактозне" in page
    assert "Молоко 2,5% 900 г — без лактози" in rejected


def test_silpo_connect_store_list_disconnect(client):
    token = register(client)
    client.post("/profile", data={"csrf": token, "regulars": "кава зернова", "min_discount": "10"})
    r = client.get("/auth/silpo/callback", params={"code": "good-code", "state": "unknown"})
    assert "застаріла" in unquote(r.headers["location"])
    r = client.post("/silpo/connect", data={"csrf": token})
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = client.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
    assert r.headers["location"] == "/silpo"

    # токени зберігаються лише зашифрованими
    row = client.app.state.conn.execute("SELECT tokens_enc FROM silpo_links").fetchone()
    assert "AT-secret-123" not in row["tokens_enc"]

    assert "Визначити моє місце" in client.get("/list").text
    near = client.get("/silpo", params={"lat": "50.45", "lon": "30.52"}).text
    assert "Найближчі до вас" in near and "км" in near
    ip = client.get("/silpo", headers={"x-vercel-ip-latitude": "50.45", "x-vercel-ip-longitude": "30.52",
                                       "x-vercel-ip-city": "Kyiv"}).text
    assert "приблизно за вашою мережею (Kyiv)" in ip
    page = client.get("/silpo", params={"city": "Київ"}).text
    assert "Київ, вул. Івана Франка, 1" in page
    r = client.post("/silpo/store", data={"csrf": token, "store_id": "b1", "store_title": "Київ, вул. Івана Франка, 1"})
    page = client.get(r.headers["location"]).text
    assert "Кава зернова Арабіка 1кг" in page and "демо" not in page and "ви купуєте «кава зернова»" in page

    client.post("/silpo/disconnect", data={"csrf": token})
    assert "Підключити «Сільпо»" in client.get("/silpo").text


@pytest.mark.parametrize("backend", ["sqlite"] + (["postgres"] if PG_URL else []))
def test_analytics_clicks_and_admin_stats(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("ADMIN_EMAILS", "owner@test.ua")
    target = str(tmp_path / "web.db")
    if backend == "postgres":
        import psycopg
        with psycopg.connect(PG_URL, autocommit=True) as pc:
            pc.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        target = PG_URL
    app = create_app(db_path=target, app_secret="y" * 32, base_url="http://test", gateway=FakeGateway())
    with TestClient(app, follow_redirects=False) as c:
        token = register(c, "user@test.ua")
        c.post("/profile", data={"csrf": token, "regulars": "кава зернова", "min_discount": "10"})
        r = c.post("/silpo/connect", data={"csrf": token})
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        c.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
        c.post("/silpo/store", data={"csrf": token, "store_id": "b1", "store_title": "Київ, Франка"})
        page = c.get("/list").text
        go = re.search(r'href="(/go\?[^"]+)"', page).group(1).replace("&amp;", "&")
        r = c.get(go)
        assert r.status_code == 302 and r.headers["location"].startswith("https://silpo.ua/product/")
        # підробка адреси переходу не працює (немає відкритого редіректу)
        assert c.get(go.replace("silpo.ua", "evil.example")).status_code == 400
        # звичайний користувач статистику не бачить
        assert c.get("/admin/stats").status_code == 403
        c.cookies.clear()
        register(c, "owner@test.ua")
        page = c.get("/admin/stats").text
        assert "Статистика" in page and "Перейшли на товар" in page and "user@test.ua" not in page
        st = __import__("productcart.web.analytics", fromlist=["stats"]).stats(app.state.conn)
        assert st["users_total"] == 2 and st["clicks_30"] == 1 and st["silpo_linked"] == 1
        assert [f["users"] for f in st["funnel"]] == [2, 1, 1, 1, 1, 1]



def test_nearby_stores_within_radius(client):
    token = register(client)
    client.post("/profile", data={"csrf": token, "regulars": "кава зернова", "min_discount": "10", "radius_km": "1"})
    r = client.post("/silpo/connect", data={"csrf": token})
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
    # без згоди місце не зберігаємо
    client.post("/location", data={"csrf": token, "lat": "50.4421", "lon": "30.5123"})
    assert "Визначити моє місце" in client.get("/list").text
    client.post("/location", data={"csrf": token, "lat": "50.4421", "lon": "30.5123", "consent": "1"})
    page = client.get("/list").text
    # у радіусі 1 км — b1 (Франка) і b4 (Хрещатик), b5 (Перемоги) далеко
    assert "вул. Івана Франка, 1" in page and "вул. Хрещатик, 2" in page and "Перемоги" not in page.split("<details")[0]
    assert "ще в 1 поруч" in page
    # профіль зберігає округлене місце, повторне збереження анкети його не стирає
    client.post("/profile", data={"csrf": token, "regulars": "кава зернова", "radius_km": "0.3"})
    assert "Визначити моє місце" not in client.get("/list").text  # анкета не стерла збережене місце
    client.post("/location", data={"csrf": token, "lat": "50.452", "lon": "30.530", "consent": "1"})  # ~1 км від найближчого
    page = client.get("/list").text
    assert "найближчий" in page  # у 300 м магазинів немає → беремо найближчий
    client.post("/location/clear", data={"csrf": token})
    assert "Визначити моє місце" in client.get("/list").text


def test_menu_page_and_settings(client):
    token = register(client)
    r = client.get("/menu")
    assert r.status_code == 200 and "Вечері цього тижня" in r.text and "Демо" in r.text and "Список покупок" in r.text
    assert r.text.count('<article class="day') == 5 and "Як готувати" in r.text
    assert client.post("/menu/settings", data={"csrf": "bad", "dinners": "3"}).status_code == 400
    assert client.post("/menu/settings", data={"csrf": token, "dinners": "3"}).status_code == 303
    assert client.post("/menu/settings", data={"csrf": token, "max_minutes": "30"}).status_code == 303
    assert client.post("/menu/settings", data={"csrf": token, "people": "2"}).status_code == 303
    html = client.get("/menu").text
    assert html.count('<article class="day') == 3 and 'value="30" aria-pressed="true"' in html and "<b>2</b>" in html
    titles = re.findall(r'<h3 class="dt">([^<]+)</h3>', html)
    r = client.post("/menu/swap", data={"csrf": token, "i": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/menu?swapped=1#d1"
    after = re.findall(r'<h3 class="dt">([^<]+)</h3>', client.get("/menu").text)
    assert after[0] == titles[0] and after[1] != titles[1] and after[2] == titles[2]
    # «закінчилось» додає запас у кошик і повертається назад
    key = re.search(r'name="key" value="([a-z_]+)"', client.get("/menu").text).group(1)
    client.post("/menu/pantry", data={"csrf": token, "key": key})
    assert "Докупити:" in client.get("/menu").text
    assert client.post("/menu/pantry", data={"csrf": token, "key": "evil"}).status_code == 400
    # збереження профілю не скидає меню
    client.post("/profile", data={"csrf": token, "household": "2", "min_discount": "10"})
    assert re.findall(r'<h3 class="dt">([^<]+)</h3>', client.get("/menu").text) == after
    assert client.get("/photos").status_code == 200


def test_landing_and_guest_menu(client):
    r = client.get("/")
    assert r.status_code == 200 and 'href="/today"' in r.text and 'href="/week"' in r.text
    assert "прототип" not in r.text and 'href="/me"' in r.text
    assert client.get("/week").headers["location"] == "/fridge"
    # гість відкриває меню без реєстрації, на демо-цінах
    r = client.get("/menu")
    assert r.status_code == 200 and "Демо-ціни" in r.text and "pc_guest" in client.cookies
    assert client.post("/menu/settings", data={"people": "4"}).headers["location"] == "/menu"
    assert client.post("/menu/settings", data={"dinners": "3"}).status_code == 303
    assert client.get("/menu").text.count('<article class="day') == 3
    first = re.search(r'id="rec0".*?<h3[^>]*>([^<]+)<', client.get("/menu").text, re.S)
    assert client.post("/menu/swap", data={"i": "0"}).headers["location"].startswith("/menu?swapped=0")
    after = re.search(r'id="rec0".*?<h3[^>]*>([^<]+)<', client.get("/menu").text, re.S)
    if first and after:
        assert first.group(1) != after.group(1)
    # підроблена cookie гостя не ламає сторінку
    client.cookies.set("pc_guest", "garbage")
    assert client.get("/menu").status_code == 200
    # після реєстрації налаштування гостя переходять у профіль
    client.cookies.clear()
    client.post("/menu/settings", data={"people": "5"})
    register(client)
    assert "pc_guest" not in client.cookies
    assert 'href="/register">Увійти' not in client.get("/menu").text
    assert 'value="5"' in client.get("/profile").text or "<b>5</b>" in client.get("/menu").text


def test_static_is_cacheable(client):
    r = client.get("/static/style.css")
    assert r.status_code == 200 and "s-maxage" in r.headers["cache-control"]
    assert "/static/style.css?v=" in client.get("/").text


class FakeGoogle:
    enabled = True

    def __init__(self, email="g@gmail.com", verified=True):
        self.email, self.verified = email, verified

    def authorize_url(self, state, challenge):
        return f"https://accounts.google.test/auth?state={state}&code_challenge={challenge}"

    def exchange(self, code, verifier):
        assert code == "g-code" and len(verifier) >= 43
        return {"email": self.email, "email_verified": self.verified, "sub": "123"}


def google_client(tmp_path, **kw):
    app = create_app(db_path=str(tmp_path / "g.db"), app_secret="x" * 32, base_url="http://test",
                     gateway=FakeGateway(), google=FakeGoogle(**kw))
    return TestClient(app, follow_redirects=False)


def google_login(c, code="g-code"):
    r = c.get("/auth/google")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return c.get(f"/auth/google/callback?code={code}&state={state}")


def test_google_signup_login_and_guards(tmp_path):
    with google_client(tmp_path) as c:
        assert "Увійти через Google" in c.get("/login").text
        c.post("/menu/settings", data={"people": "4"})           # гість налаштував меню
        r = google_login(c)
        assert r.status_code == 303 and r.headers["location"] == "/start"
        assert "<b>4</b>" in c.get("/menu").text                  # налаштування гостя збереглись
        c.cookies.clear()
        assert google_login(c).headers["location"] == "/list"      # повторний вхід — той самий профіль
        c.cookies.clear()
        r = c.post("/login", data={"email": "g@gmail.com", "password": "whatever1"})
        assert r.status_code == 400 and "через Google" in r.text   # паролем у Google-профіль не зайти
        # підроблений state
        c.get("/auth/google")
        assert c.get("/auth/google/callback?code=g-code&state=evil").status_code == 400
        assert c.get("/auth/google/callback?error=access_denied").status_code == 400


def test_google_links_existing_and_rejects_unverified(tmp_path):
    with google_client(tmp_path, email="a@b.ua") as c:
        register(c)
        c.cookies.clear()
        assert google_login(c).headers["location"] == "/list"
    with google_client(tmp_path / "x", email="n@b.ua", verified=False) as c:
        (tmp_path / "x").mkdir(exist_ok=True)
        assert google_login(c).status_code == 400


def test_google_hidden_without_keys(client):
    assert "Google" not in client.get("/login").text
    assert client.get("/auth/google").headers["location"] == "/login"


def test_menu_to_silpo_cart(tmp_path):
    from test_silpo_cart import CartMCP
    shared = CartMCP(existing={"other": 1})

    class Combined(FakeMCP):
        async def __call__(self, name, args):
            if "cart" in name or name == "silpo_find_products_batch":
                return await shared(name, args)
            return await super().__call__(name, args)

    class Gw(FakeGateway):
        @contextlib.asynccontextmanager
        async def session(self, access_token):
            yield Combined()

    app = create_app(db_path=str(tmp_path / "c.db"), app_secret="x" * 32, base_url="http://test", gateway=Gw())
    with TestClient(app, follow_redirects=False) as c:
        token = register(c)
        assert "Зробити кошик у «Сільпо»" in c.get("/menu").text               # без «Сільпо» — пропозиція підключити
        r = c.post("/silpo/connect", data={"csrf": token})
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        c.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
        c.post("/silpo/store", data={"csrf": token, "store_id": "b1", "store_title": "Франка"})
        assert "Зібрати кошик у «Сільпо»" in c.get("/menu").text
        r = c.post("/menu/silpo-cart", data={"csrf": token, "bought": ""})
        assert "вже 1 товар" in r.text and 'name="confirm"' in r.text   # попередження про наявний кошик
        assert shared.items == {"other": 1}                            # до підтвердження нічого не змінено
        r = c.post("/menu/silpo-cart", data={"csrf": token, "bought": "", "confirm": "1"})
        assert r.status_code == 200 and "До кошика й оплати" in r.text and "pc_cart_n" in c.cookies
        cart_page = c.get("/silpo/cart").text
        assert "Оплатити на сайті «Сільпо»" in cart_page and "https://silpo.ua/pay/c1" in cart_page and "hcart" in cart_page
        assert shared.items["other"] == 1 and len(shared.items) > 1
        assert c.post("/menu/silpo-cart", data={"csrf": "bad"}).status_code == 400


def test_silpo_cart_view(tmp_path):
    from test_silpo_cart import CartMCP
    shared = CartMCP(existing={"other": 1, "huge": 56.3})

    class Combined(FakeMCP):
        async def __call__(self, name, args):
            if "cart" in name:
                return await shared(name, args)
            return await super().__call__(name, args)

    class Gw(FakeGateway):
        @contextlib.asynccontextmanager
        async def session(self, access_token):
            yield Combined()

    app = create_app(db_path=str(tmp_path / "v.db"), app_secret="x" * 32, base_url="http://test", gateway=Gw())
    with TestClient(app, follow_redirects=False) as c:
        token = register(c)
        assert "Підключити «Сільпо»" in c.get("/silpo/cart").text          # без «Сільпо» — пропозиція підключити
        r = c.post("/silpo/connect", data={"csrf": token})
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        c.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
        page = c.get("/silpo/cart").text
        assert "товар other" in page and "У кошику 2 товари" in page and "https://silpo.ua/pay/c1" in page
        assert "Підозріло багато" in page                                 # 56,3 шт штучного — підсвічуємо
        assert "Мій кошик у «Сільпо»" in c.get("/silpo").text


def test_silpo_cart_edit(tmp_path):
    from test_silpo_cart import EditMCP
    shared = EditMCP(existing={"meat": 56.3, "eggs": 2})

    class Combined(FakeMCP):
        async def __call__(self, name, args):
            if "cart" in name:
                return await shared(name, args)
            return await super().__call__(name, args)

    class Gw(FakeGateway):
        @contextlib.asynccontextmanager
        async def session(self, access_token):
            yield Combined()

    app = create_app(db_path=str(tmp_path / "e.db"), app_secret="x" * 32, base_url="http://test", gateway=Gw())
    with TestClient(app, follow_redirects=False) as c:
        token = register(c)
        r = c.post("/silpo/connect", data={"csrf": token})
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        c.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
        assert "Прибрати" in c.get("/silpo/cart").text
        r = c.post("/silpo/cart/change", data={"csrf": token, "line": "line-meat", "action": "set", "quantity": "0,5"})
        assert r.status_code == 303 and shared.items["meat"] == 0.5
        c.post("/silpo/cart/change", data={"csrf": token, "line": "line-eggs", "action": "remove", "quantity": "0"})
        assert "eggs" not in shared.items
        assert c.post("/silpo/cart/change", data={"csrf": token, "line": "x", "action": "set", "quantity": "999"}).status_code == 400
        assert c.post("/silpo/cart/change", data={"csrf": "bad", "line": "x", "action": "remove"}).status_code == 400


def test_silpo_cart_clear_and_count(tmp_path):
    from test_silpo_cart import EditMCP
    shared = EditMCP(existing={"meat": 0.5, "eggs": 2, "milk": 1})

    class Combined(FakeMCP):
        async def __call__(self, name, args):
            if "cart" in name:
                return await shared(name, args)
            return await super().__call__(name, args)

    class Gw(FakeGateway):
        @contextlib.asynccontextmanager
        async def session(self, access_token):
            yield Combined()

    app = create_app(db_path=str(tmp_path / "k.db"), app_secret="x" * 32, base_url="http://test", gateway=Gw())
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/silpo/cart/count").json() == {"n": None}           # гість
        token = register(c)
        assert c.get("/silpo/cart/count").json() == {"n": None}           # без «Сільпо»
        r = c.post("/silpo/connect", data={"csrf": token})
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        c.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
        assert c.get("/silpo/cart/count").json() == {"n": 3}
        assert 'id="hcartN"' in c.get("/menu").text and ">3</b>" in c.get("/menu").text   # цифра з cookie
        assert "Очистити кошик" in c.get("/silpo/cart").text
        r = c.post("/silpo/cart/clear", data={"csrf": token})
        assert r.headers["location"] == "/silpo/cart?cleared=1" and shared.items == {}
        assert c.get("/silpo/cart/count").json() == {"n": 0}
        assert "Кошик очищено" in c.get("/silpo/cart?cleared=1").text
        assert c.post("/silpo/cart/clear", data={"csrf": "bad"}).status_code == 400


def test_profile_wizard_extras(tmp_path):
    from productcart.sources import silpo as S

    class Hist(FakeMCP):
        async def __call__(self, name, args):
            if name == "silpo_get_my_online_orders":
                return {"orders": [{"items": [{"name": "Молоко Яготинське 2,6% 900г", "quantity": 1, "brand": "Яготинське"},
                                              {"name": "Яйця курячі С1 10шт", "quantity": 1}]} for _ in range(3)]}
            if name == "silpo_get_my_offline_orders":
                return {"receipts": [{"products": [{"name": "Банани", "price": 50}, {"name": "Пакет-майка", "price": 2}]}]}
            return await super().__call__(name, args)

    class Gw(FakeGateway):
        @contextlib.asynccontextmanager
        async def session(self, access_token):
            yield Hist()

    app = create_app(db_path=str(tmp_path / "w.db"), app_secret="x" * 32, base_url="http://test", gateway=Gw())
    with TestClient(app, follow_redirects=False) as c:
        token = register(c)
        page = c.get("/profile").text
        assert "Підключіть «Сільпо» — заповнимо за вас" in page and 'id="swipe"' in page and 'id="live"' in page
        assert c.get("/profile/silpo-hints").status_code == 409
        r = c.post("/silpo/connect", data={"csrf": token})
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        c.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
        assert "Заповнити за мене" in c.get("/profile").text
        h = c.get("/profile/silpo-hints").json()
        assert h["household"] == 3 and h["diets"] == ["no_gluten", "vegetarian"]
        assert h["regulars"][:2] == ["молоко", "яйця курячі"] and "пакет-майка" not in h["regulars"]
        assert h["brands"] == ["Яготинське"] and h["orders"] == 4
        # живий підсумок: без згоди, нічого не зберігає
        d = c.post("/profile/preview", data={"csrf": token, "household": "2", "dislikes": "meat", "dish_dislikes": "plov"}).json()
        assert d["matched"] >= 1 and d["per_portion"] and d["dishes"] >= 1
        assert c.post("/profile/preview", data={"csrf": "bad"}).status_code == 400
        # «не моє» прибирає страву з меню
        c.post("/profile", data={"csrf": token, "household": "2", "dish_dislikes": "syrnyky,omelette", "dish_likes": "plov"})
        menu_page = c.get("/menu").text
        assert "Сирники" not in menu_page and "Омлет" not in menu_page
    assert S.generic_name("Сир твердий Гауда 45%") == "сир твердий"


def test_stale_offers_shown_then_refreshed(client):
    token = register(client)
    r = client.post("/silpo/connect", data={"csrf": token})
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
    client.post("/silpo/store", data={"csrf": token, "store_id": "b1", "store_title": "Франка"})
    assert "Кава зернова" in client.get("/list").text                     # перший раз — свіжо з «Сільпо»
    conn = client.app.state.conn
    conn.execute("UPDATE offers_cache SET fetched_at='2000-01-01T00:00:00+00:00'")
    page = client.get("/list").text
    assert 'id="staleNote"' not in page                                    # старше тижня — не показуємо, тягнемо заново
    conn.execute("UPDATE offers_cache SET fetched_at=?", ((__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                  - __import__("datetime").timedelta(hours=10)).isoformat(),))
    page = client.get("/list").text
    assert 'id="staleNote"' in page and "Кава зернова" in page              # 10 год — показуємо одразу й оновлюємо фоном
    assert client.post("/offers/refresh", data={"csrf": token}).json()["ok"] is True
    assert 'id="staleNote"' not in client.get("/list").text
    assert client.post("/offers/refresh", data={"csrf": "bad"}).status_code == 400


def test_turbo_and_fonts_cached(client):
    html = client.get("/").text
    assert "/static/vendor/turbo-8.0.12.js" in html and "fonts.googleapis" not in html
    assert "immutable" in client.get("/static/fonts/onest-cyrillic-wght-normal.woff2").headers["cache-control"]
    assert "app;dur=" in client.get("/login").headers["server-timing"]


def test_guest_sees_demo_deals(client):
    r = client.get("/list")
    assert r.status_code == 200 and "Популярні <span class=\"hl\">акції</span>" in r.text and 'href="/register">Увійти' in r.text
    assert r.text.count('class="pcard"') >= 10 and "hero-sum" not in r.text and "Не підійшли" not in r.text


def test_onboarding_start_and_auto_store(client):
    token = register(client)
    page = client.get("/start").text
    assert "Підключити «Сільпо»" in page and "Пропустити" in page
    r = client.post("/silpo/connect", data={"csrf": token})
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    # «Сільпо» повертає людину з Києва — магазин і склад сім'ї підтягуються самі
    r = client.get("/auth/silpo/callback", params={"code": "good-code", "state": state},
                   headers={"x-vercel-ip-latitude": "50.44", "x-vercel-ip-longitude": "30.51"})
    assert r.headers["location"] == "/menu?ready=1"
    link = client.app.state.conn.execute("SELECT store_id FROM silpo_links").fetchone()
    assert link["store_id"] == "b1"
    assert "✓ Готово" in client.get("/menu?ready=1").text
    assert 'value="3"' in client.get("/profile").text                  # сім'я з «Сільпо»
    assert client.get("/start").headers["location"] == "/menu"         # уже підключено — одразу в меню


def test_cart_tab_for_guest(client):
    page = client.get("/silpo/cart").text
    assert 'href="/register">Увійти' in page and 'href="/register"' in client.get("/").text   # значок кошика веде гостя на вхід


def test_add_deal_to_silpo_cart(tmp_path):
    from test_silpo_cart import CartMCP
    shared = CartMCP(existing={"other": 1})

    class Combined(FakeMCP):
        async def __call__(self, name, args):
            if "cart" in name or name == "silpo_find_products_batch":
                return await shared(name, args)
            return await super().__call__(name, args)

    class Gw(FakeGateway):
        @contextlib.asynccontextmanager
        async def session(self, access_token):
            yield Combined()

    app = create_app(db_path=str(tmp_path / "a.db"), app_secret="x" * 32, base_url="http://test", gateway=Gw())
    with TestClient(app, follow_redirects=False) as c:
        assert c.post("/silpo/cart/add", data={"pid": "p1"}).json()["go"] == "/register"
        token = register(c)
        assert c.post("/silpo/cart/add", data={"csrf": token, "pid": "p1"}).json()["go"] == "/silpo"
        assert 'href="/silpo">+ У кошик' in c.get("/list").text                  # демо: веде підключати «Сільпо»
        r = c.post("/silpo/connect", data={"csrf": token})
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        c.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
        c.post("/silpo/store", data={"csrf": token, "store_id": "b1", "store_title": "Франка"})
        assert 'data-pid="p2"' in c.get("/list").text                              # справжні акції — кнопка в кошик
        d = c.post("/silpo/cart/add", data={"csrf": token, "pid": "p2", "name": "Кава", "weighted": "0"}).json()
        assert d == {"ok": True, "count": 2} and shared.items["p2"] == 1
        c.post("/silpo/cart/add", data={"csrf": token, "pid": "p2", "name": "Кава"})
        assert shared.items["p2"] == 2 and shared.items["other"] == 1               # ще раз — +1, чуже не чіпаємо
        assert c.post("/silpo/cart/add", data={"csrf": "bad", "pid": "p2"}).status_code == 400


def test_deals_infinite_feed(client):
    import re as _re
    page = client.get("/list").text
    assert page.count('class="pcard"') == 20 and "feed-more" in page
    nxt = _re.search(r'data-next="([^"]+)"', page).group(1).replace("&amp;", "&")
    more = client.get(nxt).text
    assert "<html" not in more and 1 <= more.count('class="pcard"') <= 20
    # сортування «найдешевші» — з сервера, по всіх акціях
    cheap = client.get("/list?sort=price&page=1&partial=1").text
    prices = [float(x.replace(" ", "").replace(",", ".")) for x in _re.findall(r'<div class="pprice"><b>([\d\s,]+)', cheap)]
    assert prices == sorted(prices)
    # категорія
    coffee = client.get("/list?cat=Кава&partial=1").text
    assert coffee.count('class="pcard"') >= 1 and "Кава" in coffee and "Молоко" not in coffee


def test_dish_likes(client):
    r = client.post("/menu/like", data={"id": "plov"})                  # гість — у cookie
    assert r.json() == {"liked": True, "count": 1}
    assert 'data-like="' in client.get("/menu").text
    assert client.post("/menu/like", data={"id": "plov"}).json()["liked"] is False
    assert client.post("/menu/like", data={"id": "nope"}).status_code == 400
    token = register(client)
    assert client.post("/menu/like", data={"id": "borscht", "csrf": "bad"}).status_code == 400
    assert client.post("/menu/like", data={"id": "borscht", "csrf": token}).json()["liked"] is True
    prof = client.app.state.conn.execute("SELECT data FROM profiles").fetchone()["data"]
    assert '"borscht"' in prof


def test_my_dishes_in_menu_and_cart(client):
    assert "Увійти, щоб зберегти" in client.get("/dishes").text
    token = register(client)
    r = client.post("/dishes", data={"csrf": token, "title": "", "ing": ["potato"], "qty": ["500"]})
    assert r.status_code == 400 and "Додайте назву" in r.text
    r = client.post("/dishes", data={"csrf": token, "title": "Мамині деруни", "emoji": "🥔", "minutes": "40",
                                     "servings": "2", "ing": ["potato", "eggs", "bogus"], "qty": ["600", "1", "5"],
                                     "extra": "кріп, часник", "steps": "Натерти картоплю\nСмажити", "to_menu": "1"})
    assert r.status_code == 303
    did = parse_qs(urlparse(r.headers["location"]).query)["saved"][0]
    assert did.startswith("my-")
    page = client.get("/dishes").text
    assert "Мамині деруни" in page and "у меню" in page
    m = client.get("/menu").text
    assert "Мамині деруни" in m and ">моя<" in m and "Для ваших страв" in m and "кріп" in m and "Натерти картоплю" in m
    # зберігається на 4 порції: 600 г на 2 порції → 1200 г
    p = client.app.state.conn.execute("SELECT data FROM profiles").fetchone()["data"]
    assert '"potato": 1200.0' in p and "bogus" not in p
    edit = client.get(f"/dishes?edit={did}").text
    assert 'value="600"' in edit and "Змінити страву" in edit
    client.post("/dishes/delete", data={"csrf": token, "id": did})
    assert "Мамині деруни" not in client.get("/menu").text


def test_menu_setup_wizard_guest_and_user(client):
    page = client.get("/menu/setup").text
    assert "Скільки вас" in page and 'id="suDays"' in page and "/menu/preview" in page
    d = client.post("/menu/preview", data={"people": "3", "days": "0,2,4", "minutes": "30", "likes": "fish", "hide": "pork"}).json()
    assert len(d["dishes"]) == 3 and [x["day"] for x in d["dishes"]] == ["Пн", "Ср", "Пт"] and d["demo"]
    assert all(x["title"] for x in d["dishes"]) and d["pool"] > 0
    r = client.post("/menu/setup", data={"people": "3", "days": "0,2,4", "minutes": "30", "likes": "fish", "hide": "pork", "budget": "0"})
    assert r.headers["location"] == "/menu?fresh=1"
    m = client.get("/menu?fresh=1").text
    assert "fresh-in" in m and "celebrate" in m and "Ср ·" in m
    assert client.get("/menu/setup").text.count('aria-pressed="true"') >= 3      # вибрані дні збереглись у cookie гостя
    token = register(client)
    r = client.post("/menu/setup", data={"csrf": token, "people": "4", "days": "5,6", "minutes": "180", "budget": "1500"})
    assert r.status_code == 303
    p = client.app.state.conn.execute("SELECT data FROM profiles").fetchone()["data"]
    assert '"menu_days": [5, 6]' in p and '"dinners": 2' in p and '"menu_budget": 1500.0' in p
    assert "Сб" in client.get("/menu").text
    assert client.post("/menu/setup", data={"csrf": "bad", "people": "2"}).status_code == 400
