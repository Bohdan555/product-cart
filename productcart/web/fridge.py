"""«Що в холодильнику»: фото → продукти, які вже є вдома → меню спершу доїдає їх.

Розпізнає фото мовна модель Claude через Vercel AI Gateway (на Vercel авторизація OIDC — без окремого ключа)
або напряму через Anthropic API, якщо задано ANTHROPIC_API_KEY. Модель лише називає продукти й зіставляє
їх із нашим довідником інгредієнтів; ціни, бали й меню, як і раніше, рахує код.
Фото ніде не зберігаємо — лише список знайденого.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time

import httpx

from .. import menu
from . import logs

log = logging.getLogger("productcart.fridge")

GATEWAY = "https://ai-gateway.vercel.sh"
DIRECT_MODEL = "claude-opus-5-5"
MAX_BYTES = 8 * 1024 * 1024
MEDIA = {"image/jpeg", "image/png", "image/webp", "image/gif"}

SYSTEM = (
    "Ти допомагаєш скласти меню вечерь з того, що вже є вдома. На фото — вміст холодильника, полиці чи чек. "
    "Перелічи харчові продукти, які справді видно (не вигадуй те, що сховано). Для кожного вкажи назву українською, "
    "ключ із довідника інгредієнтів, якщо продукт йому відповідає (інакше \"other\"), приблизну кількість, якщо її видно, "
    "і чи ти впевнений. Напої, соуси, спеції теж можна, з ключем \"other\". Якщо на фото немає їжі — поверни порожній список."
)


class NotConfigured(RuntimeError):
    """Немає ні ключа Anthropic, ні токена Vercel OIDC / AI Gateway."""


def schema() -> dict:
    keys = sorted(menu.INGREDIENTS) + ["other"]
    return {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "ingredient": {"type": "string", "enum": keys},
                    "amount": {"type": "string"},
                    "confident": {"type": "boolean"},
                },
                "required": ["name", "ingredient", "amount", "confident"],
                "additionalProperties": False,
            }},
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def dictionary() -> str:
    return "\n".join(f"{k}: {v[0]}" for k, v in sorted(menu.INGREDIENTS.items()))


_model_cache: dict[str, str] = {}


def gateway_model() -> str:
    """Точний id Claude Opus 5.5 у каталозі AI Gateway (публічний /v1/models), з кешем на процес."""
    if os.environ.get("FRIDGE_MODEL"):
        return os.environ["FRIDGE_MODEL"]
    if "id" not in _model_cache:
        ids = [m.get("id", "") for m in httpx.get(f"{GATEWAY}/v1/models", timeout=10).json().get("data", [])]
        pick = next((i for i in ids if i in ("anthropic/claude-opus-5.5", "anthropic/claude-opus-5-5")), None)
        if not pick:      # не підставляємо іншу модель мовчки — краще явна помилка в лозі
            raise RuntimeError("AI Gateway: немає Claude Opus 5.5 у каталозі; схожі: "
                               + ", ".join(i for i in ids if "opus" in i)[:300])
        _model_cache["id"] = pick
        logs.event(log, "fridge.model", model=pick)
    return _model_cache["id"]


def make_client(oidc_token: str | None):
    """(клієнт Anthropic, модель). Напряму — з ANTHROPIC_API_KEY; інакше через AI Gateway з OIDC або AI_GATEWAY_API_KEY."""
    import anthropic

    if os.environ.get("ANTHROPIC_API_KEY"):
        return anthropic.Anthropic(timeout=60, max_retries=1), DIRECT_MODEL
    token = oidc_token or os.environ.get("VERCEL_OIDC_TOKEN") or os.environ.get("AI_GATEWAY_API_KEY")
    if not token:
        raise NotConfigured("немає ключа для розпізнавання фото")
    return anthropic.Anthropic(base_url=GATEWAY, auth_token=token, api_key=None, timeout=60, max_retries=1), gateway_model()


def recognize(image: bytes, media_type: str, oidc_token: str | None = None) -> list[dict]:
    """Фото → [{name, ingredient, amount, confident}]."""
    client, model = make_client(oidc_token)
    t = time.perf_counter()
    response = client.messages.create(
        model=model,
        max_tokens=4000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema()}},
        system=SYSTEM + "\n\nДовідник інгредієнтів (ключ: назва):\n" + dictionary(),
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.standard_b64encode(image).decode()}},
            {"type": "text", "text": "Що з продуктів є на фото?"},
        ]}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("модель відмовилась розпізнавати це фото")
    text = next((b.text for b in response.content if b.type == "text"), "")
    items = json.loads(text).get("items", []) if text else []
    logs.event(log, "fridge.scan", model=model, items=len(items), ms=round((time.perf_counter() - t) * 1000),
               in_tokens=getattr(response.usage, "input_tokens", None),
               out_tokens=getattr(response.usage, "output_tokens", None))
    return clean(items)


def clean(items: list[dict]) -> list[dict]:
    """Прибирає дублікати й порожнє; невідомі ключі → other."""
    out, seen = [], set()
    for it in items[:60]:
        name = str(it.get("name") or "").strip()[:60]
        key = it.get("ingredient") if it.get("ingredient") in menu.INGREDIENTS else "other"
        sig = key if key != "other" else name.lower()
        if not name or sig in seen:
            continue
        seen.add(sig)
        out.append({"name": name, "ingredient": key, "amount": str(it.get("amount") or "")[:40],
                    "confident": bool(it.get("confident", True))})
    return out
