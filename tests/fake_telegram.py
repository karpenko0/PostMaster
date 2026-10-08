"""Локальная имитация Telegram Bot API для тестов Long Polling (AC-05).

Сервер отвечает по тому же HTTP-протоколу, что и api.telegram.org: запросы вида
``/bot<token>/<method>`` и JSON-ответы с полями ``ok`` и ``result``. Внешняя сеть не используется.
"""

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

TEST_TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789"
BOT_USER = {
    "id": 123456789,
    "is_bot": True,
    "first_name": "PostMaster",
    "username": "postmaster_test_bot",
}


def _ok(result: Any) -> web.Response:
    return web.json_response({"ok": True, "result": result})


@dataclass
class FakeTelegram:
    """Fake Telegram API на 127.0.0.1 со случайным свободным портом."""

    token: str = TEST_TOKEN
    reject_token: bool = False
    api_url: str = ""
    methods: list[str] = field(default_factory=list)
    get_updates_called: asyncio.Event = field(default_factory=asyncio.Event)
    update_delivered: asyncio.Event = field(default_factory=asyncio.Event)
    _pending: list[dict[str, Any]] = field(default_factory=list)
    _update_ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _runner: web.AppRunner | None = None

    def add_text_message(self, text: str, chat_id: int = 42) -> None:
        """Ставит в очередь сообщение, которое вернётся при следующем getUpdates."""
        update_id = next(self._update_ids)
        message: dict[str, Any] = {
            "message_id": update_id,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private", "first_name": "Tester"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Tester"},
            "text": text,
        }
        if text.startswith("/"):
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
        self._pending.append({"update_id": update_id, "message": message})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/bot{token}/{method}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        port = self._runner.addresses[0][1]
        # Шаблон совпадает с telebot.asyncio_helper.API_URL: формат (токен, метод).
        self.api_url = f"http://127.0.0.1:{port}/bot{{0}}/{{1}}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        self.methods.append(method)
        if request.match_info["token"] != self.token:
            return web.json_response(
                {"ok": False, "error_code": 404, "description": "Not Found"}, status=404
            )
        if self.reject_token:
            return web.json_response(
                {"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401
            )
        if method == "getMe":
            return _ok(BOT_USER)
        if method == "getUpdates":
            return await self._get_updates(request)
        return _ok(True)

    async def _get_updates(self, request: web.Request) -> web.Response:
        params = dict(request.query)
        params.update(await request.post())
        offset = int(params.get("offset") or 0)
        # Сообщения с меньшим update_id уже подтверждены клиентом и больше не отдаются.
        self._pending = [update for update in self._pending if update["update_id"] >= offset]
        self.get_updates_called.set()
        if not self._pending:
            await asyncio.sleep(0.05)
            return _ok([])
        self.update_delivered.set()
        return _ok(self._pending)
