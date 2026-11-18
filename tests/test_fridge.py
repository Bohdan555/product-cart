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
        page = c.get("/fridge").text
        assert "Сфотографуйте холодильник" in page and page.count("class=\"pick-in\" data-photo") == 2 and "capture=\"environment\"" in page
        assert c.post("/fridge/scan", data={"csrf": token}, files={"photo": ("f.txt", b"hi", "text/plain")}).status_code == 400
        r = c.post("/fridge/scan", data={"csrf": token}, files={"photo": ("f.png", PNG, "image/png")},
                   headers={"x-vercel-oidc-token": "oidc-1"})
        items = r.json()["items"]
        assert [i["ingredient"] for i in items] == ["chicken_thigh", "potato", "other"]     # дубль картоплі прибрано
        assert items[0]["label"] == "Стегно куряче" and items[0]["emoji"] and seen[0][1:] == ("image/png", "oidc-1")

        r = c.post("/fridge/save", data={"csrf": token, "have": ["chicken_thigh", "potato", "bogus"], "other": ["Кетчуп"]})
        assert r.headers["location"] == "/menu/setup?first=1"        # перше меню — через конструктор зі смаками
        assert c.post("/menu/setup", data={"csrf": token, "people": "2"}).headers["location"] == "/menu?fresh=1"
        m = c.get("/menu").text
        assert "✓ є вдома: стегно куряче, картопля" in m and "✓ Вже є вдома" in c.get("/buy").text
        assert "Курячі стегна з картоплею" in m        # страва, що доїдає стегна й картоплю, потрапила в меню
        page = c.get("/fridge").text
        assert 'data-key="potato"' in page and 'data-other="Кетчуп"' in page and "bogus" not in page

        assert "Прибрати все" in page and "Прибрати: Картопля" in page
        r = c.post("/fridge/remove", data={"csrf": token, "key": "potato"})
        assert r.headers["location"] == "/buy"
        assert 'data-key="potato"' not in c.get("/fridge").text and 'data-key="chicken_thigh"' in c.get("/fridge").text
        c.post("/fridge/save", data={"csrf": token})
        m = c.get("/menu").text
        assert "✓ Вже є вдома" not in c.get("/buy").text and 'href="/fridge"><i>1</i>Що є' in m


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


def test_skips_models_blocked_on_free_tier(monkeypatch):
    import anthropic
    import httpx2

    req = httpx2.Request("POST", "https://ai-gateway.vercel.sh/v1/messages")
    blocked = anthropic.PermissionDeniedError(
        "Free tier users do not have access to this model", body=None,
        response=httpx2.Response(403, request=req))

    class Block:
        type, text = "text", json.dumps({"items": [{"name": "Морква", "ingredient": "carrot", "amount": "", "confident": True}]})

    class Resp:
        stop_reason, content = "end_turn", [Block()]

        class usage:
            input_tokens, output_tokens = 10, 5

    calls = []

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                calls.append(kw["model"])
                if kw["model"] == "anthropic/claude-sonnet-5.5":
                    raise blocked
                return Resp()

    fridge._model_cache.clear()
    monkeypatch.setattr(fridge, "make_client", lambda token: (Client(), ["anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-4.5"]))
    items = fridge.recognize(b"x", "image/jpeg")
    assert [i["ingredient"] for i in items] == ["carrot"]
    assert calls == ["anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-4.5"]
    assert fridge._model_cache["good"] == "anthropic/claude-haiku-4.5"
    fridge._model_cache.clear()
