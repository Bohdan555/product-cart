"""Сповіщення в Telegram: прив'язка акаунта, підбір цікавих акцій, тексти повідомлень.

Токен бота — лише в змінній середовища TELEGRAM_BOT_TOKEN (на Vercel). Ми зберігаємо тільки chat_id
того, хто сам натиснув «Start» за посиланням зі своєї сторінки сповіщень; /stop його видаляє.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import time

import httpx

from ..models import Offer, Profile

log = logging.getLogger("productcart.telegram")

LINK_TTL = 24 * 3600          # посилання «Підключити Telegram» діє добу
MAX_ITEMS = 6                 # акцій в одному повідомленні
BIG_DISCOUNT = 25             # % — «велика знижка» на те, що людина купує регулярно


class Telegram:
    """Тонкий клієнт Bot API. Без токена — вимкнено (enabled=False)."""

    def __init__(self, token: str | None = None, bot: str | None = None):
        self.token = (token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self.bot = (bot or os.environ.get("TELEGRAM_BOT") or "Product_basket_bot").lstrip("@")

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def call(self, method: str, payload: dict) -> dict:
        r = httpx.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=10)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description', r.status_code)}")
        return data.get("result") or {}

    def send(self, chat_id: str, text: str, buttons: list[tuple[str, str]] | None = None) -> bool:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "url": u}] for t, u in buttons]}
        try:
            self.call("sendMessage", payload)
            return True
        except Exception as e:   # заблокував бота тощо — не валимо розсилку
            log.warning("Telegram: не надіслано: %s", str(e)[:200])
            return False

    def set_webhook(self, url: str, secret: str) -> dict:
        return self.call("setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["message"],
                                        "drop_pending_updates": True})


def is_menu_day() -> bool:
    """Щотижневе меню — у понеділок (крон о 7:00 UTC = 9–10 ранку за Києвом)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).weekday() == 0


# ---------- прив'язка: t.me/<bot>?start=<код> ----------

def _sig(secret: str, msg: str, n: int = 16) -> str:
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()[:n]


def link_code(secret: str, user_id: int, now: float | None = None) -> str:
    exp = int((now or time.time()) + LINK_TTL)
    body = f"{user_id}_{exp:x}"
    return f"{body}_{_sig(secret, 'tg:' + body)}"


def check_code(secret: str, code: str, now: float | None = None) -> int | None:
    try:
        uid, exp, sig = code.split("_")
        if not hmac.compare_digest(sig, _sig(secret, f"tg:{uid}_{exp}")) or int(exp, 16) < (now or time.time()):
            return None
        return int(uid)
    except (ValueError, AttributeError):
        return None


def webhook_secret(secret: str) -> str:
    return _sig(secret, "tg-webhook", 32)


# ---------- що надсилати ----------

def _has(name: str, kw: str) -> bool:
    """Слово з початку будь-якого слова назви: «кава» знайде «Кава зернова», але не «какао»."""
    kw, low = kw.strip().lower(), " " + " ".join(name.lower().replace("«", " ").replace("»", " ").split())
    return len(kw) >= 2 and (" " + kw) in low


def offer_key(o: Offer) -> str:
    return f"o:{o.product_id or o.name}:{round(o.price)}"


def pick_deals(offers: list[Offer], p: Profile, sent: set[str]) -> list[tuple[Offer, str]]:
    """[(акція, чому)] — обрані товари зі знижкою від їхнього порогу, «купую щотижня» — лише велика. Без повторів."""
    out, seen = [], set()
    for o in sorted(offers, key=lambda o: -(o.discount_pct or 0)):
        k = offer_key(o)
        if k in sent or k in seen or not o.discount_pct:
            continue
        why = next((f"обране: {w}" + (f", від {p.watch_min[w]}%" if p.watch_min.get(w) else "")
                    for w in p.watch if _has(o.name, w) and (o.discount_pct or 0) >= p.watch_min.get(w, 0)), None)
        if not why and (o.discount_pct or 0) >= BIG_DISCOUNT:
            why = next((f"ви купуєте: {w}" for w in p.regulars if _has(o.name, w)), None)
        if why:
            out.append((o, why))
            seen.add(k)
    return out[:MAX_ITEMS]


def uah(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",").replace(",00", "") + " ₴"


def deals_text(items: list[tuple[Offer, str]]) -> str:
    lines = ["🔔 <b>Цікаві акції у «Сільпо»</b>", ""]
    for o, why in items:
        old = f" <s>{uah(o.old_price)}</s>" if o.old_price else ""
        lines.append(f"• <b>{html.escape(o.name)}</b> −{round(o.discount_pct or 0)}%\n"
                     f"  {uah(o.price)}{old} · <i>{html.escape(why)}</i>")
    return "\n".join(lines)


def deals_short(items: list[tuple[Offer, str]]) -> str:
    """Для push: коротко, без розмітки. «Сир Гауда −30% · Кава −25% і ще 2»."""
    head = [f"{o.name[:40]} −{round(o.discount_pct or 0)}%" for o, _ in items[:2]]
    rest = len(items) - len(head)
    return " · ".join(head) + (f" і ще {rest}" if rest > 0 else "")


def menu_text(m, days: list[str]) -> str:
    lines = ["🍽 <b>Меню на тиждень готове</b>",
             f"{len(m.dishes)} вечерь · порція ≈ {round(m.per_portion)} ₴ · економія {round(m.saving)} ₴", ""]
    for d, day in zip(m.dishes, days):
        lines.append(f"{d.emoji} {day}: {html.escape(d.title)}")
    return "\n".join(lines)
