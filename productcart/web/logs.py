"""Логи сайту: один JSON-рядок на подію, з id запиту й маскуванням секретів.

Що пишемо: кожен запит (метод, шлях, статус, час, база, виклики «Сільпо»), доменні події
(вхід, «Сільпо», кошик, Telegram, розсилка) і помилки з трасою.
Чого НЕ пишемо ніколи: токени, паролі, cookies, email, координати, вміст кошиків і історію покупок
(сирі відповіді «Сільпо» — лише на рівні DEBUG, який на хостингу вимкнений).

Рівень — LOG_LEVEL (за замовчуванням INFO). Читати на Vercel: `vercel logs --since 1h`.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

# Контекст поточного запиту: змінний словник, щоб маршрут міг дописати користувача,
# а middleware прочитати його вже після відповіді.
request_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("request_ctx", default=None)

_REDACT = [
    (re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"), "bot<telegram-token>"),
    (re.compile(r"(?i)\b(bearer|client-id)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <secret>"),
    (re.compile(r"(?i)([\"']?(?:access_token|refresh_token|id_token|token|password|secret|client_secret|code_verifier)"
                r"[\"']?\s*[:=]\s*[\"']?)[^\"'&\s,}]+"), r"\1<secret>"),
    (re.compile(r"(?i)([?&](?:code|state|token|start)=)[^&\s\"']+"), r"\1<secret>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"postgres(?:ql)?://\S+"), "postgres://<secret>"),
]


def redact(text: str) -> str:
    for rx, repl in _REDACT:
        text = rx.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        out = {"t": datetime.fromtimestamp(r.created, timezone.utc).isoformat(timespec="milliseconds"),
               "lvl": r.levelname, "log": r.name, "msg": r.getMessage()}
        ctx = request_ctx.get()
        if ctx:
            out["rid"] = ctx.get("rid")
            if ctx.get("uid"):
                out["uid"] = ctx["uid"]
        ev = getattr(r, "ev", None)
        if isinstance(ev, dict):
            out.update({k: v for k, v in ev.items() if v is not None})
        if r.exc_info:
            out["exc"] = self.formatException(r.exc_info)[-4000:]
        return redact(json.dumps(out, ensure_ascii=False, default=str))


class _Stdout(logging.StreamHandler):
    """Пише в поточний sys.stdout (його можуть підмінити — тести, сервер)."""

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _):
        pass


_done = False


def setup() -> None:
    """Налаштовує логування один раз на процес."""
    global _done
    if _done:
        return
    _done = True
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    h = _Stdout()
    h.setFormatter(JsonFormatter())
    app_log = logging.getLogger("productcart")
    app_log.handlers[:] = [h]
    app_log.setLevel(level)
    app_log.propagate = False
    # сторонні бібліотеки: httpx на INFO пише повні URL (у Telegram там токен бота) — лише попередження
    for name in ("httpx", "httpcore", "mcp", "anyio", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)


def event(log: logging.Logger, name: str, level: int = logging.INFO, **fields) -> None:
    """Доменна подія: `event(log, "silpo.connect", ok=True)` → {"msg": "silpo.connect", "ok": true, …}."""
    log.log(level, name, extra={"ev": fields})


def new_request(headers) -> dict:
    vid = headers.get("x-vercel-id") or ""
    ctx = {"rid": vid.rsplit("::", 1)[-1] if vid else uuid.uuid4().hex[:12], "uid": None,
           "silpo_n": 0, "silpo_ms": 0.0}
    return ctx


def set_user(uid) -> None:
    ctx = request_ctx.get()
    if ctx is not None and uid:
        ctx["uid"] = uid


def silpo_call_done(tool: str, started: float, error: BaseException | None = None) -> None:
    """Облік одного виклику інструмента «Сільпо»: у підсумок запиту й окремий рядок, якщо помилка чи повільно."""
    ms = (time.perf_counter() - started) * 1000
    ctx = request_ctx.get()
    if ctx is not None:
        ctx["silpo_n"] = ctx.get("silpo_n", 0) + 1
        ctx["silpo_ms"] = ctx.get("silpo_ms", 0.0) + ms
    log = logging.getLogger("productcart.silpo")
    if error is not None:
        event(log, "silpo.call", logging.WARNING, tool=tool, ms=round(ms), ok=False,
              err=f"{type(error).__name__}: {str(error)[:300]}")
    elif ms > 3000:
        event(log, "silpo.call", logging.WARNING, tool=tool, ms=round(ms), ok=True, slow=True)
    else:
        event(log, "silpo.call", logging.DEBUG, tool=tool, ms=round(ms), ok=True)
