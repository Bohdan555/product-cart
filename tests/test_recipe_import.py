import json
import re

import pytest
from fastapi.testclient import TestClient

from productcart.web import recipe_import as ri
from productcart.web.app import create_app
from test_web import FakeGateway, register

PAGE = """<html><head><title>Курка з картоплею</title>
<script type="application/ld+json">""" + json.dumps({"@context": "https://schema.org", "@graph": [
    {"@type": "WebPage", "name": "x"},
    {"@type": "Recipe", "name": "Курка з картоплею в духовці", "recipeYield": "2 порції", "totalTime": "PT1H10M",
     "recipeIngredient": ["500 г курячого філе", "1 кг картоплі", "2 цибулини", "1 ст. л. олії", "2 зубчики часнику",
                          "сіль, перець за смаком"],
     "recipeInstructions": [{"@type": "HowToStep", "text": "Наріжте картоплю."},
                            {"@type": "HowToStep", "text": "Запікайте 50 хв при 200 °C."}]}]}, ensure_ascii=False) + """</script>
</head><body>...</body></html>"""


def test_jsonld_recipe_parsed_and_mapped():
    d = ri.from_jsonld(PAGE)
    assert d["title"] == "Курка з картоплею в духовці" and d["servings"] == 2 and d["minutes"] == 70
    assert d["steps"] == ["Наріжте картоплю.", "Запікайте 50 хв при 200 °C."]
    ing, extras = ri.map_ingredients(d["ingredients"], d["servings"])
    assert ing == {"chicken_fillet": 1000.0, "potato": 2000.0, "onion": 400.0, "oil": 30.0, "garlic": 20.0}   # на 4 порції
    assert extras == ["сіль, перець"]


def test_unsafe_urls_rejected():
    for bad in ("http://localhost/x", "http://127.0.0.1/", "http://10.0.0.5/r", "ftp://site.ua/r", "просто текст"):
        with pytest.raises(ri.ImportError_):
            ri.safe_url(bad)
    assert ri.safe_url("klopotenko.com/recipe") == "https://klopotenko.com/recipe"


def test_import_route_fills_form_then_saves(tmp_path, monkeypatch):
    monkeypatch.setattr(ri, "fetch", lambda url: (PAGE, "https://recepty.example.ua/kurka"))
    app = create_app(db_path=str(tmp_path / "r.db"), app_secret="x" * 32, base_url="http://test", gateway=FakeGateway())
    with TestClient(app, follow_redirects=False) as c:
        token = register(c)
        page = c.post("/dishes/import", data={"csrf": token, "url": "https://recepty.example.ua/kurka"}).text
        assert "Розібрали рецепт з <b>recepty.example.ua</b>" in page and 'value="Курка з картоплею в духовці"' in page
        assert 'value="https://recepty.example.ua/kurka"' in page and "Наріжте картоплю." in page
        # людина перевіряє й зберігає: форма відправляє те, що заповнили
        r = c.post("/dishes", data={"csrf": token, "title": "Курка з картоплею в духовці", "servings": "2", "minutes": "70",
                                    "ing": ["chicken_fillet", "potato"], "qty": ["500", "1000"], "extra": "часник",
                                    "steps": "Наріжте картоплю.", "src": "https://recepty.example.ua/kurka", "to_menu": "1"})
        did = re.search(r"saved=([\w-]+)", r.headers["location"]).group(1)
        c.post("/today/cooked", data={"csrf": token, "id": did, "n": "2"})
        assert "recepty.example.ua" in c.get(f"/cook/{did}").text

        monkeypatch.setattr(ri, "fetch", lambda url: ("<html><body>Новини дня</body></html>", url))
        monkeypatch.setattr(ri, "from_llm", lambda text, token=None: None)
        bad = c.post("/dishes/import", data={"csrf": token, "url": "https://news.example.ua/"})
        assert bad.status_code == 422 and "не знайшли рецепта" in bad.text


def test_no_markup_uses_llm(monkeypatch):
    monkeypatch.setattr(ri, "fetch", lambda url: ("<html><body><h1>Омлет</h1>3 яйця, 50 г сиру</body></html>", url))
    monkeypatch.setattr(ri, "from_llm", lambda text, token=None: {"found": True, "title": "Омлет", "servings": 1, "minutes": 10,
                                                                   "ingredients": ["3 яйця", "50 г твердого сиру"], "steps": ["Збити"]})
    d = ri.import_recipe("https://blog.example.ua/omlet")
    assert d["title"] == "Омлет" and d["ing"] == {"eggs": 12.0, "cheese": 200.0} and d["emoji"] == "🍳"


def test_redirect_to_internal_address_blocked(monkeypatch):
    import httpx

    def handler(req):
        if req.url.host == "short.example.ua":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        return httpx.Response(200, text="secret")
    real = httpx.Client

    class Client(real):
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(handler), **kw)
    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(ri, "_resolves_public", lambda host: True)
    with pytest.raises(ri.ImportError_):
        ri.fetch("https://short.example.ua/r")
