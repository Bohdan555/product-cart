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
    assert r.status_code == 303 and r.headers["location"].startswith("/profile")
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
    for path in ("/list", "/profile", "/silpo"):
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
    assert "демо" in page and "Молоко 2,5% 900 г" not in page and "безлактозне" in page
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

    assert "Оберіть свій магазин" in client.get("/list").text
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
