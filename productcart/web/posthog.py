"""PostHog (EU): ті самі події, що й власна аналітика, + скрипт у браузері після згоди.

Ключ проєкту — змінна POSTHOG_KEY (Vercel). Без ключа нічого не відправляємо.
Персональних даних не шлемо: замість email чи id — псевдонім (HMAC від id з секретом сайту).
Події збираємо за запит і відправляємо одним пакетом ПІСЛЯ відповіді, щоб не гальмувати сторінку.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import uuid
from urllib.parse import unquote

from . import security

log = logging.getLogger(__name__)

HOST = os.environ.get("POSTHOG_HOST", "https://eu.i.posthog.com").rstrip("/")
ASSETS = HOST.replace("://eu.i.", "://eu-assets.i.").replace("://us.i.", "://us-assets.i.")
DROP = {"email", "lat", "lon", "address", "name", "phone"}

batch: contextvars.ContextVar = contextvars.ContextVar("ph_batch", default=None)
anon: contextvars.ContextVar = contextvars.ContextVar("ph_anon", default=None)     # id з cookie браузерного PostHog
path: contextvars.ContextVar = contextvars.ContextVar("ph_path", default=None)


def key() -> str:
    return os.environ.get("POSTHOG_KEY", "").strip()


def user_alias(secret: str, user_id: int) -> str:
    return "u_" + hmac.new(secret.encode(), f"ph:{user_id}".encode(), hashlib.sha256).hexdigest()[:20]


def browser_id(cookies: dict) -> str | None:
    """distinct_id, який браузерний PostHog зберіг у своїй cookie (лише якщо людина дала згоду)."""
    k = key()
    raw = cookies.get(f"ph_{k}_posthog") if k else None
    if not raw:
        return None
    try:
        d = json.loads(unquote(raw)).get("distinct_id")
    except (ValueError, AttributeError):
        return None
    return str(d)[:200] if d else None


def start(cookies: dict, request_path: str) -> None:
    batch.set([] if key() else None)
    anon.set(browser_id(cookies))
    path.set(request_path)


def capture(secret: str, user_id: int | None, event: str, props: dict) -> None:
    b = batch.get()
    if b is None:
        return
    p = {k: v for k, v in props.items() if v is not None and k not in DROP}
    p["$lib"] = "productcart-server"
    if path.get():
        p["$pathname"] = path.get()
    if user_id is not None:
        did = user_alias(secret, user_id)
    elif anon.get():
        did = anon.get()
    else:                       # гість без згоди на cookie — подія без профілю людини
        did, p["$process_person_profile"] = "anon_" + uuid.uuid4().hex, False
    b.append({"event": event, "distinct_id": did, "properties": p, "timestamp": security.iso(security.now())})


def take() -> list | None:
    b = batch.get()
    batch.set(None)
    return b or None


async def send(events: list) -> None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.post(f"{HOST}/batch/", json={"api_key": key(), "batch": events})
            if r.status_code >= 400:
                log.warning("PostHog: %s %s", r.status_code, r.text[:200])
    except Exception as e:  # аналітика не має ламати сайт
        log.warning("PostHog недоступний: %s", str(e)[:200])
