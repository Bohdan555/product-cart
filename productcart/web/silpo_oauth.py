"""OAuth 2.1 + PKCE до MCP «Сільпо» для сайту: одна реєстрація застосунку, токени на кожного користувача.

Потік: /silpo/connect → сторінка входу «Сільпо» (телефон + код) → /auth/silpo/callback → обмін коду на токени.
Метадані сервера (перевірено 2026-11-14): /.well-known/oauth-protected-resource/mcp →
authorization_servers = [https://mcp.silpo.ua]; /.well-known/oauth-authorization-server →
authorize, token, register; PKCE S256; публічний клієнт (token_endpoint_auth_method=none).
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx

from . import db, logs

MCP_URL = "https://mcp.silpo.ua/mcp"


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class SilpoGateway:
    """Справжній шлюз до «Сільпо». У тестах його підміняє підробка з тим самим інтерфейсом."""

    def __init__(self, conn, redirect_uri: str, mcp_url: str = MCP_URL, client_name: str = "Product Cart"):
        self.conn, self.redirect_uri, self.mcp_url, self.client_name = conn, redirect_uri, mcp_url, client_name
        self._meta: dict | None = None

    # ---------- метадані й реєстрація ----------
    def _metadata(self) -> dict:
        if self._meta:
            return self._meta
        u = urlparse(self.mcp_url)
        base = f"{u.scheme}://{u.netloc}"
        path = u.path.rstrip("/")
        with httpx.Client(timeout=20) as c:
            pr = c.get(f"{base}/.well-known/oauth-protected-resource{path}").json()
            issuer = (pr.get("authorization_servers") or [base])[0].rstrip("/")
            self._meta = c.get(f"{issuer}/.well-known/oauth-authorization-server").json()
        return self._meta

    def client_id(self) -> str:
        key = f"silpo_client:{self.redirect_uri}"
        stored = db.get_setting(self.conn, key)
        if stored:
            return json.loads(stored)["client_id"]
        meta = self._metadata()
        body = {"client_name": self.client_name, "redirect_uris": [self.redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none"}
        r = httpx.post(meta["registration_endpoint"], json=body, timeout=20)
        r.raise_for_status()
        info = r.json()
        db.set_setting(self.conn, key, json.dumps(info))
        return info["client_id"]

    # ---------- кроки входу ----------
    def authorize_url(self, state: str, challenge: str) -> str:
        meta = self._metadata()
        q = {"response_type": "code", "client_id": self.client_id(), "redirect_uri": self.redirect_uri,
             "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": self.mcp_url}
        return meta["authorization_endpoint"] + "?" + urlencode(q)

    def _token(self, data: dict) -> dict:
        meta = self._metadata()
        r = httpx.post(meta["token_endpoint"], data=dict(data, client_id=self.client_id(), resource=self.mcp_url), timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(f"Сільпо відмовило у токені: {r.status_code} {r.text[:200]}")
        t = r.json()
        t["obtained_at"] = int(time.time())
        return t

    def exchange(self, code: str, verifier: str) -> dict:
        return self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                            "code_verifier": verifier})

    def refresh(self, tokens: dict) -> dict:
        new = self._token({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
        new.setdefault("refresh_token", tokens["refresh_token"])
        return new

    # ---------- виклики MCP від імені користувача ----------
    @contextlib.asynccontextmanager
    async def session(self, access_token: str):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        from ..silpo_mcp import _unwrap

        async with streamablehttp_client(self.mcp_url, headers={"Authorization": f"Bearer {access_token}"}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()

                async def call(name: str, args: dict | None = None):
                    t = time.perf_counter()
                    try:
                        res = _unwrap(await s.call_tool(name, args or {}))
                    except BaseException as e:
                        logs.silpo_call_done(name, t, e)
                        raise
                    logs.silpo_call_done(name, t)
                    return res

                call.session = s  # type: ignore[attr-defined]  # щоб прочитати схеми інструментів
                yield call


def expires_soon(tokens: dict, margin: int = 120) -> bool:
    exp = tokens.get("expires_in")
    if not exp:
        return False
    return time.time() > tokens.get("obtained_at", 0) + int(exp) - margin
