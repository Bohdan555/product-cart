import re
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from productcart.models import Offer, Profile
from productcart.web import telegram as tgm
from productcart.web.app import create_app
from test_web import FakeGateway, register

SECRET = "x" * 32


class FakeTelegram(tgm.Telegram):
    def __init__(self):
        super().__init__(token="T", bot="Product_basket_bot")
        self.sent, self.hooks = [], []

    def send(self, chat_id, text, buttons=None):
        self.sent.append((chat_id, text, buttons))
        return True

    def set_webhook(self, url, secret):
        self.hooks.append((url, secret))
        return {}


def hook(c, text, chat=777, secret=None):
    return c.post("/tg/webhook", json={"message": {"chat": {"id": chat, "type": "private"}, "text": text}},
                  headers={"x-telegram-bot-api-secret-token": secret or tgm.webhook_secret(SECRET)})


def test_link_watch_and_daily_notifications(tmp_path, monkeypatch):
    tg = FakeTelegram()
    app = create_app(db_path=str(tmp_path / "t.db"), app_secret=SECRET, base_url="http://test",
                     gateway=FakeGateway(), telegram=tg)
    with TestClient(app, follow_redirects=False) as c:
        token = register(c)
        c.post("/profile", data={"csrf": token, "regulars": "кава зернова", "min_discount": "10"})
        r = c.post("/silpo/connect", data={"csrf": token})
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        c.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
        c.post("/silpo/store", data={"csrf": token, "store_id": "b1", "store_title": "Франка"})

        page = c.get("/notify").text
        code = re.search(r"t\.me/Product_basket_bot\?start=([\w-]+)", page).group(1)
        assert hook(c, f"/start {code}", secret="wrong").status_code == 403
        assert hook(c, "/start forged_1_abc").status_code == 200 and "Привіт" in tg.sent[-1][1]
        hook(c, f"/start {code}")
        assert "Готово" in tg.sent[-1][1] and "Telegram підключено" in c.get("/notify").text

        # «🔔» з картки акції і стрічка показує кнопку
        assert 'class="pbell"' in c.get("/list").text
        assert c.post("/notify/watch", data={"csrf": token, "word": "Молоко"}).json() == {"ok": True, "word": "молоко", "min": 0, "linked": True}

        assert c.get("/cron/notify").status_code == 403                       # не Vercel Cron
        monkeypatch.setattr(tgm, "is_menu_day", lambda: True)
        tg.sent.clear()
        st = c.get("/cron/notify", headers={"user-agent": "vercel-cron/1.0"}).json()
        assert st["deals"] == 1 and st["menus"] == 1
        deals = tg.sent[0][1]
        assert "Молоко" in deals and "обране: молоко" in deals and "Кава зернова" in deals and "ви купуєте" in deals
        assert "Меню на тиждень" in tg.sent[1][1]

        # наступного дня — без повторів
        c.app.state.conn.execute("DELETE FROM settings WHERE key='tg_cron_at'")
        tg.sent.clear()
        assert c.get("/cron/notify", headers={"user-agent": "vercel-cron/1.0"}).json()["deals"] == 0 and not tg.sent

        # налаштування і /stop
        c.post("/notify", data={"csrf": token, "w": ["кава", "сир"], "wmin": ["20", "0"], "tg_weekly": "1",
                                 "watch_default": "30"})
        page = c.get("/notify").text
        assert '<span class="nname">сир</span>' in page and '<option value="20" selected>' in page
        assert c.post("/notify/watch", data={"csrf": token, "word": "хек"}).json()["min"] == 30
        hook(c, "/stop")
        assert "Підключити Telegram" in c.get("/notify").text


def test_watch_threshold():
    p = Profile(watch=["кава", "чай"], watch_min={"кава": 20})
    offers = [Offer(chain="silpo", name="Кава мелена", price=90, old_price=100, product_id="a"),   # −10%
              Offer(chain="silpo", name="Чай чорний", price=90, old_price=100, product_id="b"),
              Offer(chain="silpo", name="Кава зернова", price=70, old_price=100, product_id="c")]   # −30%
    got = {o.product_id: why for o, why in tgm.pick_deals(offers, p, set())}
    assert set(got) == {"b", "c"} and got["c"] == "обране: кава, від 20%"


def test_codes_and_matching():
    code = tgm.link_code(SECRET, 5, now=1000)
    assert tgm.check_code(SECRET, code, now=1000) == 5
    assert tgm.check_code(SECRET, code, now=1000 + tgm.LINK_TTL + 1) is None
    assert tgm.check_code("y" * 32, code, now=1000) is None
    p = Profile(watch=["кава"], regulars=["сир"])
    offers = [Offer(chain="silpo", name="Какао Nesquik", price=50, old_price=100, product_id="a"),
              Offer(chain="silpo", name="Кава мелена", price=90, old_price=100, product_id="b"),
              Offer(chain="silpo", name="Сир твердий", price=90, old_price=100, product_id="c"),
              Offer(chain="silpo", name="Сирок глазурований", price=50, old_price=100, product_id="d")]
    got = [o.product_id for o, _ in tgm.pick_deals(offers, p, set())]
    assert got == ["d", "b"]           # какао ≠ кава; сир лише з великою знижкою


def test_setup_without_token(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), app_secret=SECRET, base_url="http://test",
                     gateway=FakeGateway(), telegram=tgm.Telegram(token=""))
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/tg/setup").status_code == 503
        token = register(c)
        assert "налаштовується" in c.get("/notify").text
