"""Клієнт офіційного MCP «Сільпо» з OAuth 2.1 (PKCE, динамічна реєстрація клієнта).

Вхід — від імені покупця через auth.silpo.ua (телефон + код). Токени зберігаються локально
у файлі з правами 0600 і в git не потрапляють. Потрібен пакет `mcp` 1.x: `pip install -e .[silpo]`.

Використання:
    async with silpo_session() as call:
        data = await call("silpo_list_branches", {"limit": 10})
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MCP_URL = os.environ.get("SILPO_MCP_URL", "https://mcp.silpo.ua/mcp")
CALLBACK_PORT = int(os.environ.get("PRODUCTCART_CALLBACK_PORT", "8765"))
REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"
SECRETS_DIR = Path(os.environ.get("PRODUCTCART_SECRETS", Path.home() / ".config" / "product-cart"))


def _sdk():
    try:
        from mcp import ClientSession
        from mcp.client.auth import OAuthClientProvider, TokenStorage
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
    except ImportError as e:  # pragma: no cover
        raise SystemExit("Потрібен пакет mcp 1.x: pip install -e '.[silpo]'") from e
    return ClientSession, OAuthClientProvider, TokenStorage, streamablehttp_client, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken


class FileTokenStorage:
    """Токени й реєстрація клієнта в одному JSON-файлі (0600)."""

    def __init__(self, path: Path | None = None):
        self.path = path or SECRETS_DIR / "silpo.json"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    async def get_tokens(self):
        *_, OAuthToken = _sdk()
        t = self._read().get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens) -> None:
        d = self._read()
        d["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(d)

    async def get_client_info(self):
        _, _, _, _, OAuthClientInformationFull, _, _ = _sdk()
        c = self._read().get("client")
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def set_client_info(self, info) -> None:
        d = self._read()
        d["client"] = info.model_dump(mode="json", exclude_none=True)
        self._write(d)

    def has_tokens(self) -> bool:
        return bool(self._read().get("tokens", {}).get("access_token"))

    def forget(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


class _OneShotCallback(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        q = parse_qs(urlparse(self.path).query)
        _OneShotCallback.result = {k: v[0] for k, v in q.items()}
        ok = "code" in q
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Вхід у «Сільпо» виконано. Можна закрити вкладку." if ok else "Помилка входу: немає коду авторизації."
        self.wfile.write(f"<p style='font:18px system-ui'>{msg}</p>".encode())

    def log_message(self, *a):  # тиша в консолі
        pass


def _wait_callback(timeout: float = 300) -> tuple[str, str | None]:
    """Чекає один редірект на 127.0.0.1:{port}/callback. Якщо браузер на іншій машині,
    можна вставити адресу з рядка браузера вручну (PRODUCTCART_PASTE=1)."""
    if os.environ.get("PRODUCTCART_PASTE") == "1":
        url = input("Вставте адресу, на яку перекинув браузер після входу: ").strip()
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    else:
        _OneShotCallback.result = {}
        srv = HTTPServer(("127.0.0.1", CALLBACK_PORT), _OneShotCallback)
        srv.timeout = timeout
        t = threading.Thread(target=srv.handle_request, daemon=True)
        t.start()
        t.join(timeout)
        srv.server_close()
        q = _OneShotCallback.result
    if "code" not in q:
        raise RuntimeError(f"Вхід не завершено: {q.get('error_description') or q.get('error') or 'немає коду'}")
    return q["code"], q.get("state")


def _unwrap(result):
    """CallToolResult → dict: structuredContent або JSON у першому текстовому блоці."""
    if getattr(result, "isError", False):
        text = result.content[0].text if result.content else "помилка інструмента"
        raise RuntimeError(text)
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


@contextlib.asynccontextmanager
async def silpo_session(storage: FileTokenStorage | None = None, interactive: bool = True):
    """Відкриває MCP-сесію. Якщо токенів немає — запускає вхід (лише коли interactive=True)."""
    ClientSession, OAuthClientProvider, _, streamablehttp_client, _, OAuthClientMetadata, _ = _sdk()
    storage = storage or FileTokenStorage()
    if not interactive and not storage.has_tokens():
        raise RuntimeError("Немає входу в «Сільпо». Виконайте: productcart silpo login")

    async def redirect_handler(url: str) -> None:
        print("\nВідкрийте в браузері й увійдіть у свій акаунт «Сільпо»:\n" + url + "\n", flush=True)
        with contextlib.suppress(Exception):
            webbrowser.open(url)

    async def callback_handler() -> tuple[str, str | None]:
        return await asyncio.to_thread(_wait_callback)

    provider = OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Product Cart",
            redirect_uris=[REDIRECT_URI],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with streamablehttp_client(MCP_URL, auth=provider) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name: str, args: dict | None = None):
                return _unwrap(await session.call_tool(name, args or {}))

            call.session = session  # type: ignore[attr-defined]
            yield call
