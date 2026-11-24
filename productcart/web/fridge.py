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


RECEIPT = (
    "Ти допомагаєш вести список продуктів удома. На фото — касовий чек магазину. Перелічи лише харчові продукти з чека "
    "(без пакетів, побутової хімії, косметики, знижок і підсумків). Для кожного дай коротку назву українською без бренду "
    "й ваги (\"Молоко\", \"Філе куряче\"), ключ із довідника інгредієнтів, якщо продукт йому відповідає (інакше \"other\"), "
    "кількість чи вагу з чека, і чи ти впевнений. Окремо — дату покупки з чека у форматі РРРР-ММ-ДД "
    "(порожній рядок, якщо дати не видно). Якщо це не чек — поверни порожній список."
)


class NotConfigured(RuntimeError):
    """Немає ні ключа Anthropic, ні токена Vercel OIDC / AI Gateway."""


def schema(receipt: bool = False) -> dict:
    keys = sorted(menu.INGREDIENTS) + ["other"]
    out = {
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
    if receipt:
        out["properties"]["date"] = {"type": "string"}
        out["required"] = ["items", "date"]
    return out


def dictionary() -> str:
    return "\n".join(f"{k}: {v[0]}" for k, v in sorted(menu.INGREDIENTS.items()))


# Кандидати через AI Gateway — від кращої дешевої моделі. Безкоштовні кредити Vercel пускають не до всіх моделей
# (Claude Opus 5.5 — лише на платних), тож беремо першу, яку шлюз дозволяє, і запам'ятовуємо її.
PREFERRED = ["anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-4.5",
             "anthropic/claude-sonnet-5", "anthropic/claude-opus-5.5"]
NO_EFFORT = ("haiku-4.5", "sonnet-4.5", "haiku-4-5", "sonnet-4-5")   # ці моделі не знають output_config.effort

_model_cache: dict = {}


def gateway_candidates() -> list[str]:
    """Моделі для спроби: FRIDGE_MODEL (якщо задано) або PREFERRED, що є в живому каталозі шлюзу."""
    if os.environ.get("FRIDGE_MODEL"):
        return [os.environ["FRIDGE_MODEL"]]
    if "ids" not in _model_cache:
        _model_cache["ids"] = [m.get("id", "") for m in
                               httpx.get(f"{GATEWAY}/v1/models", timeout=10).json().get("data", [])]
    ids = _model_cache["ids"]
    found = [m for m in PREFERRED if m in ids]
    if not found:
        raise RuntimeError("AI Gateway: немає жодної з моделей Claude; є: "
                           + ", ".join(i for i in ids if i.startswith("anthropic/"))[:300])
    good = _model_cache.get("good")
    return ([good] if good in found else []) + [m for m in found if m != good and m not in _model_cache.get("blocked", set())]


def _blocked(e: Exception) -> bool:
    """Шлюз не пускає до цієї моделі (безкоштовний рівень, немає провайдера) — пробуємо наступну."""
    text = str(e)
    return any(x in text for x in ("Free tier", "no_providers_available", "model_not_found", "not found"))


def make_client(oidc_token: str | None):
    """(клієнт Anthropic, [моделі]). Напряму — з ANTHROPIC_API_KEY; інакше AI Gateway з OIDC або AI_GATEWAY_API_KEY."""
    import anthropic

    if os.environ.get("ANTHROPIC_API_KEY"):
        return anthropic.Anthropic(timeout=60, max_retries=1), [DIRECT_MODEL]
    token = oidc_token or os.environ.get("VERCEL_OIDC_TOKEN") or os.environ.get("AI_GATEWAY_API_KEY")
    if not token:
        raise NotConfigured("немає ключа для розпізнавання фото")
    client = anthropic.Anthropic(base_url=GATEWAY, auth_token=token, api_key=None, timeout=60, max_retries=1)
    return client, gateway_candidates()


def _ask(client, model: str, image: bytes, media_type: str, receipt: bool = False):
    out = {"format": {"type": "json_schema", "schema": schema(receipt)}}
    if not any(x in model for x in NO_EFFORT):
        out["effort"] = "low"
    return client.messages.create(
        model=model,
        max_tokens=4000,
        output_config=out,
        system=(RECEIPT if receipt else SYSTEM) + "\n\nДовідник інгредієнтів (ключ: назва):\n" + dictionary(),
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.standard_b64encode(image).decode()}},
            {"type": "text", "text": "Які продукти куплено за цим чеком?" if receipt else "Що з продуктів є на фото?"},
        ]}],
    )


def recognize(image: bytes, media_type: str, oidc_token: str | None = None, kind: str = "fridge") -> list[dict]:
    """Фото → [{name, ingredient, amount, confident}]. Перша модель, до якої пускає шлюз.
    kind="receipt" — фото чека: до кожного продукту додаємо "bought" — дату покупки з чека (або "")."""
    receipt = kind == "receipt"
    import anthropic

    client, models = make_client(oidc_token)
    t = time.perf_counter()
    last: Exception | None = None
    for model in models:
        try:
            response = _ask(client, model, image, media_type, receipt)
        except (anthropic.PermissionDeniedError, anthropic.NotFoundError, anthropic.BadRequestError) as e:
            if not _blocked(e) and not isinstance(e, anthropic.NotFoundError):
                raise
            _model_cache.setdefault("blocked", set()).add(model)
            logs.event(log, "fridge.model_skipped", logging.WARNING, model=model, err=str(e)[:200])
            last = e
            continue
        if _model_cache.get("good") != model:
            _model_cache["good"] = model
            logs.event(log, "fridge.model", model=model)
        if response.stop_reason == "refusal":
            raise RuntimeError("модель відмовилась розпізнавати це фото")
        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(text) if text else {}
        items = data.get("items", [])
        logs.event(log, "fridge.scan", model=model, items=len(items), kind=kind, ms=round((time.perf_counter() - t) * 1000),
                   in_tokens=getattr(response.usage, "input_tokens", None),
                   out_tokens=getattr(response.usage, "output_tokens", None))
        out = clean(items)
        if receipt:
            bought = receipt_date(str(data.get("date") or ""))
            for it in out:
                it["bought"] = bought
        return out
    raise last or RuntimeError("немає доступної моделі")


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


def receipt_date(text: str, today=None) -> str:
    """Дата з чека → «РРРР-ММ-ДД», лише якщо вона правдоподібна: не в майбутньому й не давніше 60 днів."""
    from datetime import date, timedelta
    today = today or date.today()
    try:
        d = date.fromisoformat(text.strip()[:10])
    except ValueError:
        return ""
    return d.isoformat() if today - timedelta(days=60) <= d <= today else ""
