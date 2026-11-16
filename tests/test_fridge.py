import json

from fastapi.testclient import TestClient

from productcart.web import fridge
from productcart.web.app import create_app
from test_web import FakeGateway, register

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


def app_with(vision, tmp_path):
    return create_app(db_path=str(tmp_path / "f.db"), app_secret="x" * 32, base_url="http://test",
                      gateway=FakeGateway(), vision=vision)


def test_scan_save_and_menu_uses_home_food(tmp_path):
    seen = []

    def vision(data, media, oidc):
        seen.append((len(data), media, oidc))
        return fridge.clean([{"name": "Курячі стегна", "ingredient": "chicken_thigh", "amount": "1 кг", "confident": True},
                             {"name": "Картопля", "ingredient": "potato", "amount": "", "confident": True},
                             {"name": "Кетчуп", "ingredient": "other", "amount": "", "confident": False},
                             {"name": "Ще картопля", "ingredient": "potato", "amount": "", "confident": True}])

    with TestClient(app_with(vision, tmp_path), follow_redirects=False) as c:
        assert "Увійти, щоб спробувати" in c.get("/fridge").text
        assert c.post("/fridge/scan", files={"photo": ("f.png", PNG, "image/png")}).status_code == 401
        token = register(c)
        assert "Сфотографувати холодильник" in c.get("/fridge").text
        assert c.post("/fridge/scan", data={"csrf": token}, files={"photo": ("f.txt", b"hi", "text/plain")}).status_code == 400
        r = c.post("/fridge/scan", data={"csrf": token}, files={"photo": ("f.png", PNG, "image/png")},
                   headers={"x-vercel-oidc-token": "oidc-1"})
        items = r.json()["items"]
        assert [i["ingredient"] for i in items] == ["chicken_thigh", "potato", "other"]     # дубль картоплі прибрано
        assert items[0]["label"] == "Стегно куряче" and items[0]["emoji"] and seen[0][1:] == ("image/png", "oidc-1")

        r = c.post("/fridge/save", data={"csrf": token, "have": ["chicken_thigh", "potato", "bogus"], "other": ["Кетчуп"]})
        assert r.headers["location"] == "/menu?fresh=1"
        m = c.get("/menu").text
        assert "Доїдаємо те, що вдома" in m and "🧊 Є вдома" in m and "🧊 є вдома" in m
        assert "Курячі стегна з картоплею" in m        # страва, що доїдає стегна й картоплю, потрапила в меню
        page = c.get("/fridge").text
        assert 'data-key="potato"' in page and 'data-other="Кетчуп"' in page and "bogus" not in page

        c.post("/fridge/save", data={"csrf": token})
        assert "Що в холодильнику?" in c.get("/menu").text


def test_not_configured_and_errors(tmp_path, monkeypatch):
    def broken(data, media, oidc):
        raise fridge.NotConfigured("x")

    with TestClient(app_with(broken, tmp_path), follow_redirects=False) as c:
        token = register(c)
        r = c.post("/fridge/scan", data={"csrf": token}, files={"photo": ("f.jpg", PNG, "image/jpeg")})
        assert r.status_code == 503 and "вручну" in r.json()["error"]
    for k in ("ANTHROPIC_API_KEY", "VERCEL_OIDC_TOKEN", "AI_GATEWAY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    try:
        fridge.make_client(None)
        assert False
    except fridge.NotConfigured:
        pass


def test_schema_lists_dictionary():
    s = fridge.schema()
    enum = s["properties"]["items"]["items"]["properties"]["ingredient"]["enum"]
    assert "chicken_thigh" in enum and enum[-1] == "other"
    assert json.dumps(s)   # серіалізується для API
