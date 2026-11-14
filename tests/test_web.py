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


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "web.db"), app_secret="x" * 32, base_url="http://test", gateway=FakeGateway())
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

    page = client.get("/silpo", params={"city": "Київ"}).text
    assert "Київ, вул. Івана Франка, 1" in page
    r = client.post("/silpo/store", data={"csrf": token, "store_id": "b1", "store_title": "Київ, вул. Івана Франка, 1"})
    page = client.get(r.headers["location"]).text
    assert "Кава зернова Арабіка 1кг" in page and "демо" not in page and "ви купуєте «кава зернова»" in page

    client.post("/silpo/disconnect", data={"csrf": token})
    assert "Підключити «Сільпо»" in client.get("/silpo").text
