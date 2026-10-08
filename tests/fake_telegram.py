"""Локальная имитация Telegram Bot API для тестов Long Polling (AC-05).

Сервер отвечает по тому же HTTP-протоколу, что и api.telegram.org: запросы вида
``/bot<token>/<method>`` и JSON-ответы с полями ``ok`` и ``result``. Внешняя сеть не используется.
"""

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

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


async def _request_params(request: web.Request) -> dict[str, str]:
    """Параметры из строки запроса и тела.

    pyTelegramBotAPI шлёт getUpdates методом GET с телом формы. request.post() для GET
    возвращает пустой словарь, поэтому для GET тело читается вручную. Иначе offset не доходил
    бы до фейка, и каждое обновление доставлялось бы повторно.
    """
    params = dict(request.query)
    if request.method == "GET":
        body = await request.read()
        params.update(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
    else:
        params.update({key: str(value) for key, value in (await request.post()).items()})
    return params


@dataclass
class FakeTelegram:
    """Fake Telegram API на 127.0.0.1 со случайным свободным портом."""

    token: str = TEST_TOKEN
    reject_token: bool = False
    api_url: str = ""
    methods: list[str] = field(default_factory=list)
    # Тексты, которые бот отправил через sendMessage: chat_id и text в виде строк.
    sent_messages: list[dict[str, str]] = field(default_factory=list)
    get_updates_called: asyncio.Event = field(default_factory=asyncio.Event)
    update_delivered: asyncio.Event = field(default_factory=asyncio.Event)
    _pending: list[dict[str, Any]] = field(default_factory=list)
    _update_ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _runner: web.AppRunner | None = None

    def add_text_message(
        self,
        text: str,
        chat_id: int = 42,
        *,
        user_id: int | None = None,
        username: str | None = None,
        first_name: str = "Tester",
    ) -> None:
        """Ставит в очередь сообщение, которое вернётся при следующем getUpdates.

        По умолчанию отправитель совпадает с чатом и не имеет username. Параметры user_id,
        username и first_name задают отправителя явно.
        """
        update_id = next(self._update_ids)
        sender: dict[str, Any] = {
            "id": chat_id if user_id is None else user_id,
            "is_bot": False,
            "first_name": first_name,
        }
        if username is not None:
            sender["username"] = username
        message: dict[str, Any] = {
            "message_id": update_id,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private", "first_name": first_name},
            "from": sender,
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
        if method == "sendMessage":
            return await self._send_message(request)
        return _ok(True)

    async def _send_message(self, request: web.Request) -> web.Response:
        params = await _request_params(request)
        chat_id = params.get("chat_id", "")
        text = params.get("text", "")
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        # Telegram возвращает отправленное сообщение. pyTelegramBotAPI разбирает его как Message.
        return _ok(
            {
                "message_id": len(self.sent_messages),
                "date": 1_700_000_000,
                "chat": {"id": int(chat_id), "type": "private"},
                "text": text,
            }
        )

    async def _get_updates(self, request: web.Request) -> web.Response:
        params = await _request_params(request)
        offset = int(params.get("offset") or 0)
        # Сообщения с меньшим update_id уже подтверждены клиентом и больше не отдаются.
        self._pending = [update for update in self._pending if update["update_id"] >= offset]
        self.get_updates_called.set()
        if not self._pending:
            await asyncio.sleep(0.05)
            return _ok([])
        self.update_delivered.set()
        return _ok(self._pending)
