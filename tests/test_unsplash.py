import json

import httpx

from productcart.web import db, unsplash


def fake_client(seen):
    def handler(req: httpx.Request):
        seen.append(str(req.url))
        if req.url.path == "/search/photos":
            return httpx.Response(200, json={"results": [
                {"id": "p1", "premium": True, "urls": {"raw": "https://images.unsplash.com/x?ixid=1"}},
                {"id": "p2", "urls": {"raw": "https://images.unsplash.com/y?ixid=2"},
                 "links": {"html": "https://unsplash.com/photos/p2", "download_location": "https://api.unsplash.com/photos/p2/download"},
                 "user": {"name": "Ann", "links": {"html": "https://unsplash.com/@ann"}}}]})
        return httpx.Response(200, json={})
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.unsplash.com")


def test_sync_stores_free_photo_and_triggers_download(tmp_path, monkeypatch):
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "k")
    unsplash._mem.clear()
    conn = db.connect(tmp_path / "t.db")
    seen = []
    assert unsplash.sync(conn, ["plov", "no-such-dish"], fake_client(seen)) == 1
    saved = json.loads(db.get_setting(conn, unsplash.KEY))
    assert saved["plov"]["pid"] == "p2" and saved["plov"]["by"] == "Ann"
    assert any(u.endswith("/photos/p2/download") for u in seen)
    assert unsplash.img_url(saved["plov"]["raw"]).startswith("https://images.unsplash.com/y?ixid=2&w=720")
    unsplash._mem.clear()


def test_no_key_does_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
    conn = db.connect(tmp_path / "t.db")
    assert unsplash.sync(conn, ["plov"]) == 0
    assert unsplash.claim(conn, ["plov"]) == []
    assert db.get_setting(conn, unsplash.TRY_KEY) is None


def test_every_recipe_has_query():
    from productcart import menu
    assert {r.id for r in menu.RECIPES} <= set(unsplash.QUERIES)


def test_skips_used_and_redo():
    res = [{"id": "a", "urls": {"raw": "r"}}, {"id": "b", "urls": {"raw": "r"}}]
    assert unsplash.pick(res, {"a"})["id"] == "b"
    assert not unsplash.usable("liver", {"raw": "r"})
    assert unsplash.usable("liver", {"raw": "r", "q": unsplash.QUERIES["liver"]})
    assert unsplash.usable("plov", {"raw": "r"})
