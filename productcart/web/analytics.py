"""Продуктова аналітика: події користувачів і сукупна статистика для власника.

Події пишемо в ту саму базу. Статистика — лише сукупні числа, без email та інших персональних даних.
Запис події ніколи не ламає сторінку: помилки аналітики ковтаємо.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
from datetime import timedelta
from urllib.parse import urlencode

from . import security

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id {pk},
  ts TEXT NOT NULL,
  user_id INTEGER,
  event TEXT NOT NULL,
  chain TEXT,
  store_id TEXT,
  product_id TEXT,
  category TEXT,
  discount REAL,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_event_ts ON events (event, ts);
CREATE INDEX IF NOT EXISTS ix_events_user ON events (user_id, ts)
"""

EVENTS = ("signup", "login", "profile_saved", "silpo_connected", "store_selected",
          "list_viewed", "list_refreshed", "offer_click")
FUNNEL = [("signup", "Зареєструвалися"), ("profile_saved", "Заповнили профіль"),
          ("silpo_connected", "Підключили «Сільпо»"), ("store_selected", "Обрали магазин"),
          ("list_viewed", "Відкрили список"), ("offer_click", "Перейшли на товар")]


def init(conn) -> None:
    conn.executescript(SCHEMA.format(pk="SERIAL PRIMARY KEY" if conn.is_pg else "INTEGER PRIMARY KEY AUTOINCREMENT"))


# Turbo завантажує сторінки наперед (при наведенні/дотику) — такі запити не рахуємо як перегляди
prefetching: contextvars.ContextVar[bool] = contextvars.ContextVar("prefetching", default=False)


def track(conn, user_id: int | None, event: str, *, chain: str | None = None, store_id: str | None = None,
          product_id: str | None = None, category: str | None = None, discount: float | None = None, **meta) -> None:
    if prefetching.get():
        return
    try:
        conn.execute(
            "INSERT INTO events (ts, user_id, event, chain, store_id, product_id, category, discount, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (security.iso(security.now()), user_id, event, chain, store_id, product_id, category, discount,
             json.dumps(meta, ensure_ascii=False) if meta else None))
    except Exception as e:  # аналітика не має ламати сайт
        log.warning("track %s failed: %s", event, e)


# ---------- підписані посилання на товари ----------

def _sig(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]


def click_url(secret: str, offer) -> str | None:
    """Посилання /go на товар: рахуємо перехід і перенаправляємо. Підпис не дає зробити з сайту відкритий редірект."""
    if not offer.url:
        return None
    fields = {"u": offer.url, "c": offer.chain, "s": offer.store_id or "", "p": offer.product_id,
              "k": offer.category or "", "d": f"{offer.discount_pct:.0f}"}
    payload = "|".join(fields[k] for k in "ucspkd")
    return "/go?" + urlencode(dict(fields, sig=_sig(secret, payload)))


def verify_click(secret: str, q: dict) -> dict | None:
    try:
        payload = "|".join(q.get(k, "") for k in "ucspkd")
        if not hmac.compare_digest(_sig(secret, payload), q.get("sig", "")):
            return None
        if not q["u"].startswith("https://"):
            return None
        return q
    except KeyError:
        return None


# ---------- статистика ----------

def _count(conn, sql: str, params=()) -> int:
    r = conn.execute(sql, params).fetchone()
    return int(list(r.values())[0] if isinstance(r, dict) else r[0]) if r else 0


def stats(conn, days: int = 30) -> dict:
    now = security.now()
    since = lambda d: security.iso(now - timedelta(days=d))  # noqa: E731
    out: dict = {}
    out["users_total"] = _count(conn, "SELECT COUNT(*) FROM users")
    out["users_new_7"] = _count(conn, "SELECT COUNT(*) FROM users WHERE created_at >= ?", (since(7),))
    out["users_new_30"] = _count(conn, "SELECT COUNT(*) FROM users WHERE created_at >= ?", (since(30),))
    out["wau"] = _count(conn, "SELECT COUNT(DISTINCT user_id) FROM events WHERE event='list_viewed' AND ts >= ?", (since(7),))
    out["mau"] = _count(conn, "SELECT COUNT(DISTINCT user_id) FROM events WHERE event='list_viewed' AND ts >= ?", (since(30),))
    out["silpo_linked"] = _count(conn, "SELECT COUNT(*) FROM silpo_links")
    out["stores_chosen"] = _count(conn, "SELECT COUNT(*) FROM silpo_links WHERE store_id IS NOT NULL")
    out["clicks_30"] = _count(conn, "SELECT COUNT(*) FROM events WHERE event='offer_click' AND ts >= ?", (since(30),))
    out["clickers_30"] = _count(conn, "SELECT COUNT(DISTINCT user_id) FROM events WHERE event='offer_click' AND ts >= ?", (since(30),))
    out["views_30"] = _count(conn, "SELECT COUNT(*) FROM events WHERE event='list_viewed' AND ts >= ?", (since(30),))
    out["ctr"] = round(out["clicks_30"] / out["views_30"] * 100, 1) if out["views_30"] else 0.0

    out["funnel"] = []
    for ev, label in FUNNEL:
        n = _count(conn, "SELECT COUNT(DISTINCT user_id) FROM events WHERE event=? AND user_id IS NOT NULL", (ev,))
        out["funnel"].append({"event": ev, "label": label, "users": n})
    top = out["funnel"][0]["users"] or 1
    for f in out["funnel"]:
        f["pct"] = round(f["users"] / top * 100)

    rows = conn.execute(
        "SELECT substr(ts, 1, 10) AS day, event, COUNT(*) AS n, COUNT(DISTINCT user_id) AS u FROM events "
        "WHERE ts >= ? AND event IN ('signup', 'list_viewed', 'offer_click') GROUP BY 1, 2 ORDER BY 1", (since(days),)).fetchall()
    by_day: dict[str, dict] = {}
    for r in rows:
        d = by_day.setdefault(r["day"], {"day": r["day"], "signup": 0, "list_viewed": 0, "offer_click": 0, "active": 0})
        d[r["event"]] = int(r["n"])
        if r["event"] == "list_viewed":
            d["active"] = int(r["u"])
    out["days"] = [by_day[k] for k in sorted(by_day)][-14:]
    out["days_max"] = max([max(d["list_viewed"], d["offer_click"], d["signup"]) for d in out["days"]] or [1]) or 1

    def top_by(col: str, limit: int = 8):
        return [dict(name=r["name"] or "—", n=int(r["n"])) for r in conn.execute(
            f"SELECT {col} AS name, COUNT(*) AS n FROM events WHERE event='offer_click' AND ts >= ? "
            f"GROUP BY {col} ORDER BY n DESC LIMIT {int(limit)}", (since(30),)).fetchall()]

    out["top_categories"] = top_by("category")
    out["top_chains"] = top_by("chain")
    out["top_stores"] = top_by("store_id")
    buckets = [(0, 15, "до 15%"), (15, 25, "15–25%"), (25, 40, "25–40%"), (40, 101, "40% і більше")]
    out["discounts"] = [{"label": lbl, "n": _count(conn, "SELECT COUNT(*) FROM events WHERE event='offer_click' AND ts >= ? "
                                                    "AND discount >= ? AND discount < ?", (since(30), lo, hi))}
                        for lo, hi, lbl in buckets]
    return out
