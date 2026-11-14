"""Паролі (scrypt), сесії в БД, CSRF і шифрування токенів «Сільпо»."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken

SESSION_DAYS = 30
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def new_session(conn, user_id: int) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    conn.execute("INSERT INTO sessions (token, user_id, csrf, expires_at) VALUES (?, ?, ?, ?)",
                 (token, user_id, csrf, iso(now() + timedelta(days=SESSION_DAYS))))
    return token, csrf


def session_user(conn, token: str | None):
    if not token:
        return None
    return conn.execute(
        """SELECT u.id, u.email, s.csrf, s.token FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token=? AND s.expires_at > ?""", (token, iso(now()))).fetchone()


def drop_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))


class TokenBox:
    """Шифрує токени «Сільпо» ключем, похідним від APP_SECRET. Без секрету сайт не стартує."""

    def __init__(self, app_secret: str):
        if not app_secret or len(app_secret) < 16:
            raise ValueError("APP_SECRET має бути щонайменше 16 символів")
        key = base64.urlsafe_b64encode(hashlib.sha256(("silpo-tokens:" + app_secret).encode()).digest())
        self._f = Fernet(key)

    def seal(self, text: str) -> str:
        return self._f.encrypt(text.encode()).decode()

    def open(self, blob: str) -> str | None:
        try:
            return self._f.decrypt(blob.encode()).decode()
        except InvalidToken:
            return None
