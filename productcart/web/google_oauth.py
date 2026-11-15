"""Вхід через Google (OpenID Connect, authorization code + PKCE).

Беремо лише email і ознаку, що Google його підтвердив. Ключі — GOOGLE_CLIENT_ID і GOOGLE_CLIENT_SECRET
у змінних середовища; без них кнопка «Увійти через Google» не показується.
"""
from __future__ import annotations

import os
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleAuth:
    """Справжній клієнт Google. У тестах його підміняє підробка з тим самим інтерфейсом."""

    def __init__(self, redirect_uri: str, client_id: str | None = None, client_secret: str | None = None):
        self.redirect_uri = redirect_uri
        self.client_id = client_id if client_id is not None else os.environ.get("GOOGLE_CLIENT_ID", "")
        self.client_secret = client_secret if client_secret is not None else os.environ.get("GOOGLE_CLIENT_SECRET", "")

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def authorize_url(self, state: str, challenge: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": "openid email", "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "prompt": "select_account",
        })

    def exchange(self, code: str, verifier: str) -> dict:
        """Код → токен → {email, email_verified, sub}. Дані беремо прямо з Google по TLS, тож підпис id_token не перевіряємо."""
        with httpx.Client(timeout=15) as c:
            r = c.post(TOKEN_URL, data={"code": code, "client_id": self.client_id, "client_secret": self.client_secret,
                                        "redirect_uri": self.redirect_uri, "grant_type": "authorization_code",
                                        "code_verifier": verifier})
            r.raise_for_status()
            access = r.json()["access_token"]
            u = c.get(USERINFO_URL, headers={"Authorization": f"Bearer {access}"})
            u.raise_for_status()
            return u.json()
