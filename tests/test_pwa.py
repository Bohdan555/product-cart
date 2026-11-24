"""PWA: маніфест, service worker, іконки; Web Push — ключ, шифрування, підписка, розсилка."""
import base64
import json
import os
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from productcart.web import telegram as tgm
from productcart.web import webpush as wp
from productcart.web.app import create_app
from test_web import FakeGateway, register

SECRET = "x" * 32
FCM = "https://fcm.googleapis.com/fcm/send/abc123"


def ua_keys():
    k = ec.generate_private_key(ec.SECP256R1())
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return k, wp.b64u(pub), os.urandom(16)


def decrypt(body: bytes, ua_key, auth: bytes) -> bytes:
    """Як браузер (RFC 8291) — щоб перевірити наше шифрування."""
    salt, idlen = body[:16], body[20]
    as_pub, ct = body[21:21 + idlen], body[21 + idlen:]
    ua_pub = ua_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ecdh = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    h = lambda s, i, info, n: HKDF(algorithm=hashes.SHA256(), length=n, salt=s, info=info).derive(i)
    ikm = h(auth, ecdh, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    pt = AESGCM(h(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)).decrypt(h(salt, ikm, b"Content-Encoding: nonce\x00", 12), ct, None)
    assert pt.endswith(b"\x02")
    return pt[:-1]


def test_encrypt_roundtrip_and_vapid_jwt():
    k, p256dh, auth = ua_keys()
    assert decrypt(wp.encrypt("Знижка −30% 🔔".encode(), p256dh, wp.b64u(auth)), k, auth).decode() == "Знижка −30% 🔔"
    v = wp.Vapid.from_secret(SECRET, "https://example.test")
    assert v.public_key == wp.Vapid.from_secret(SECRET, "x").public_key != wp.Vapid.from_secret("y" * 32, "x").public_key
    hdr = v.header(FCM)
    jwt = hdr.split("t=")[1].split(",")[0]
    head, claims, sig = jwt.split(".")
    assert json.loads(wp.unb64u(claims))["aud"] == "https://fcm.googleapis.com"
    raw = wp.unb64u(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wp.unb64u(v.public_key))
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))     # кидає виняток, якщо підпис хибний


def test_only_known_push_services():
    assert wp.allowed_endpoint(FCM) and wp.allowed_endpoint("https://web.push.apple.com/QGx")
    assert not wp.allowed_endpoint("https://evil.test/push") and not wp.allowed_endpoint("http://fcm.googleapis.com/x")
    assert not wp.allowed_endpoint("https://fcm.googleapis.com.evil.test/x")


def test_manifest_sw_icons_offline(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), app_secret=SECRET, base_url="http://test", gateway=FakeGateway())
    with TestClient(app) as c:
        m = c.get("/manifest.webmanifest").json()
        assert m["display"] == "standalone" and m["start_url"].startswith("/")
        for icon in m["icons"]:
            assert c.get(icon["src"]).status_code == 200
        sw = c.get("/sw.js")
        assert sw.headers["service-worker-allowed"] == "/" and "__CSS_V__" not in sw.text and "__VERSION__" not in sw.text
        assert c.get("/offline").status_code == 200 and c.get("/favicon.ico").status_code == 200
        page = c.get("/me").text
        assert 'rel="manifest"' in page and "apple-touch-icon" in page and "data-install" in page


class FakePush:
    def __init__(self):
        self.sent, self.gone = [], set()

    def __call__(self, vapid, sub, message, **kw):
        if sub["endpoint"] in self.gone:
            raise wp.Gone("410")
        self.sent.append((sub["endpoint"], message))
        return True


def test_subscribe_notify_and_cleanup(tmp_path, monkeypatch):
    push = FakePush()
    app = create_app(db_path=str(tmp_path / "t.db"), app_secret=SECRET, base_url="http://test",
                     gateway=FakeGateway(), push_sender=push)
    _, p256dh, auth = ua_keys()
    sub = {"endpoint": FCM, "keys": {"p256dh": p256dh, "auth": wp.b64u(auth)}}
    with TestClient(app, follow_redirects=False) as c:
        assert c.post("/push/subscribe", json=sub).status_code == 401                 # гість
        token = register(c)
        assert c.post("/push/subscribe", json=sub, headers={"x-csrf": "bad"}).status_code == 400
        bad = dict(sub, endpoint="https://evil.test/x")
        assert c.post("/push/subscribe", json=bad, headers={"x-csrf": token}).status_code == 400
        assert c.post("/push/subscribe", json=sub, headers={"x-csrf": token}).json() == {"ok": True, "sent": 1}
        assert push.sent[-1][1]["title"].startswith("Сповіщення ввімкнено")
        assert "усього пристроїв" not in c.get("/notify").text and "Сповіщення" in c.get("/me").text

        # щоденна розсилка — лише push, без Telegram
        c.post("/profile", data={"csrf": token, "regulars": "кава зернова", "min_discount": "10"})
        r = c.post("/silpo/connect", data={"csrf": token})
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        c.get("/auth/silpo/callback", params={"code": "good-code", "state": state})
        c.post("/silpo/store", data={"csrf": token, "store_id": "b1", "store_title": "Франка"})
        monkeypatch.setattr(tgm, "is_menu_day", lambda: True)
        push.sent.clear()
        monkeypatch.setenv("CRON_SECRET", "test-cron")
        st = c.get("/cron/notify", headers={"authorization": "Bearer test-cron"}).json()
        assert st["deals"] == 1 and st["menus"] == 1
        assert [m["tag"] for _, m in push.sent] == ["deals", "menu"] and "−" in push.sent[0][1]["body"]

        # «скоро зіпсується»: фарш позавчора — вранці push зі стравами; удруге того ж дня не шлемо
        import json as _json
        from datetime import date as _d, timedelta as _td
        conn = c.app.state.conn
        row = conn.execute("SELECT user_id, data FROM profiles").fetchone()
        data = _json.loads(row["data"])
        data.update(fridge=["minced", "pasta", "kefir"], fridge_at=__import__("productcart.web.security", fromlist=["iso"]).iso(
            __import__("productcart.web.security", fromlist=["now"]).now()),
            fridge_since={"minced": (_d.today() - _td(days=2)).isoformat(), "pasta": _d.today().isoformat(),
                          "kefir": (_d.today() - _td(days=3)).isoformat()})
        conn.execute("UPDATE profiles SET data=? WHERE user_id=?", (_json.dumps(data), row["user_id"]))
        conn.execute("DELETE FROM settings WHERE key='tg_cron_at'")
        push.sent.clear()
        st = c.get("/cron/notify", headers={"authorization": "Bearer test-cron"}).json()
        xp = [m for _, m in push.sent if m["tag"] == "expiry"]
        assert st.get("expiry") == 1 and len(xp) == 1 and xp[0]["url"].startswith("/today/dishes?")       # один зведений push
        assert "фарш — сьогодні" in xp[0]["body"] and "кефір — післязавтра" in xp[0]["body"] and "2 продукти" in xp[0]["title"]
        conn.execute("DELETE FROM settings WHERE key='tg_cron_at'")
        push.sent.clear()
        c.get("/cron/notify", headers={"authorization": "Bearer test-cron"})
        assert not [m for _, m in push.sent if m["tag"] == "expiry"]

        # браузер відписався — сервіс каже 410 — підписку прибираємо
        push.gone.add(FCM)
        c.app.state.conn.execute("DELETE FROM settings WHERE key='tg_cron_at'")
        c.app.state.conn.execute("DELETE FROM tg_sent")
        c.get("/cron/notify", headers={"authorization": "Bearer test-cron"})
        assert not c.app.state.conn.execute("SELECT 1 FROM push_subs").fetchall()

        c.post("/push/subscribe", json=sub, headers={"x-csrf": token})   # знову (у FakePush ще «gone» — одразу прибереться)
        push.gone.clear()
        c.post("/push/subscribe", json=sub, headers={"x-csrf": token})
        assert c.post("/push/unsubscribe", json={"endpoint": FCM}, headers={"x-csrf": token}).json() == {"ok": True}
        assert not c.app.state.conn.execute("SELECT 1 FROM push_subs").fetchall()


def test_healthz_and_fast_open_sw(tmp_path):
    app = create_app(db_path=str(tmp_path / "h.db"), app_secret=SECRET, base_url="http://test", gateway=FakeGateway())
    with TestClient(app) as c:
        r = c.get("/healthz")
        assert r.json() == {"ok": True} and r.headers["cache-control"] == "no-store"
        sw = c.get("/sw.js").text
        assert "FAST" in sw and "'fresh'" in sw and "pages-v2" in sw
