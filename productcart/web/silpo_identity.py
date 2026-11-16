"""Одноразова перевірка: чи дає «Сільпо» стабільний ідентифікатор покупця — для «Увійти через Сільпо».

Пишемо в лог ЛИШЕ назви полів (структуру), ніколи значення: ні телефону, ні імені, ні id.
"""
from __future__ import annotations

import base64
import json
import logging

from . import logs

log = logging.getLogger("productcart.silpo")


def shape(x, depth: int = 0):
    """Структура без значень: {"id": "str", "phone": "str", "family": ["dict"]}."""
    if depth > 3:
        return type(x).__name__
    if isinstance(x, dict):
        return {k: shape(v, depth + 1) for k, v in list(x.items())[:40]}
    if isinstance(x, list):
        return [shape(x[0], depth + 1)] if x else []
    return type(x).__name__


def jwt_claims(token: str) -> list[str] | None:
    """Назви полів (claims) у JWT-токені, якщо це JWT. Значення не повертаємо."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return sorted(payload) if isinstance(payload, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None


async def probe(call, tokens: dict) -> None:
    try:
        tools = await call.session.list_tools()
        names = sorted(t.name for t in tools.tools)
        prof = [n for n in names if any(w in n for w in ("profile", "user", "me", "account", "customer"))]
        result = {}
        for n in prof[:3]:
            schema = next((t.inputSchema for t in tools.tools if t.name == n), {}) or {}
            if schema.get("required"):
                continue
            try:
                result[n] = shape(await call(n, {}))
            except Exception as e:
                result[n] = f"error: {type(e).__name__}"
        logs.event(log, "silpo.identity_probe", token_keys=sorted(tokens), access_jwt_claims=jwt_claims(tokens.get("access_token", "")),
                   id_token=bool(tokens.get("id_token")), profile_tools=prof, profile_shapes=json.dumps(result, ensure_ascii=False)[:3000],
                   all_tools=len(names))
    except Exception as e:
        log.warning("Сільпо: перевірка ідентифікатора не вдалась: %s", type(e).__name__)
