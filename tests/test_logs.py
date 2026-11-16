import json
import logging

from fastapi.testclient import TestClient

from productcart.web import logs
from productcart.web.app import create_app
from test_web import FakeGateway, register


def test_redact_secrets():
    s = logs.redact("POST https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage "
                    "Authorization: Bearer abc.def.ghijkl {'access_token': 'AT-secret-123', 'refresh_token': \"RT1\"} "
                    "/auth/silpo/callback?code=XYZ&state=S1 user bohdan@example.com postgres://u:p@h/db")
    for secret in ("AAHdqTcvCH1", "abc.def", "AT-secret-123", "RT1", "XYZ", "S1", "bohdan@example.com", "u:p@h"):
        assert secret not in s
    assert "bot<telegram-token>" in s and "<email>" in s


def lines(capsys):
    return [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]


def test_request_log_and_error_page(tmp_path, capsys):
    logs._done = False
    app = create_app(db_path=str(tmp_path / "l.db"), app_secret="x" * 32, base_url="http://test", gateway=FakeGateway())

    @app.get("/boom")
    def boom():
        raise RuntimeError("token=SECRET42 зламалось")

    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as c:
        capsys.readouterr()
        token = register(c)
        r = c.get("/menu")
        rid = r.headers["x-request-id"]
        out = lines(capsys)
        req = [l for l in out if l["msg"] == "req" and l.get("path") == "/menu"][0]
        assert req["st"] == 200 and req["rid"] == rid and req["uid"] and "ms" in req and "db" in req
        assert any(l["msg"] == "ev.signup" for l in out)
        c.post("/login", data={"email": "a@b.ua", "password": "wrong-password"})
        out = lines(capsys)
        failed = [l for l in out if l["msg"] == "auth.login_failed"][0]
        assert "a@b.ua" not in json.dumps(out, ensure_ascii=False) and failed["lvl"] == "WARNING"
        r = c.get("/boom")
        assert r.status_code == 500 and r.headers["x-request-id"] in r.text
        out = lines(capsys)
        err = [l for l in out if l["lvl"] == "ERROR" and "exc" in l][0]
        assert "RuntimeError" in err["exc"] and "SECRET42" not in json.dumps(out, ensure_ascii=False)
    logs._done = False


def test_third_party_loggers_quiet():
    logs._done = False
    logs.setup()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert not logging.getLogger("productcart").propagate
