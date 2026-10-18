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
    # Фотографии, которые бот отправил через sendPhoto: chat_id, photo (file_id) и caption.
    sent_photos: list[dict[str, str | None]] = field(default_factory=list)
    # Хронологический порядок всех исходящих событий (sendMessage и sendPhoto).
    outbound_events: list[dict[str, Any]] = field(default_factory=list)
    fail_send_message: tuple[int, str] | None = None
    fail_send_photo: tuple[int, str] | None = None
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
        first_name: str | None = "Tester",
        chat_type: str = "private",
    ) -> None:
        """Ставит в очередь текстовое сообщение. Команды получают сущность bot_command."""
        body: dict[str, Any] = {"text": text}
        if text.startswith("/"):
            body["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
        self.add_message(
            body,
            chat_id,
            user_id=user_id,
            username=username,
            first_name=first_name,
            chat_type=chat_type,
        )

    def add_photo_message(
        self,
        file_id: str,
        chat_id: int = 42,
        *,
        user_id: int | None = None,
        username: str | None = None,
        first_name: str | None = "Tester",
        chat_type: str = "private",
    ) -> None:
        """Ставит в очередь фотографию в двух размерах, как присылает Telegram.

        Последний элемент — максимальный размер (1280×960, file_size 150000).
        """
        sizes = [
            {
                "file_id": f"{file_id}-small",
                "file_unique_id": f"{file_id}-s",
                "width": 90,
                "height": 90,
                "file_size": 2500,
            },
            {
                "file_id": file_id,
                "file_unique_id": f"{file_id}-l",
                "width": 1280,
                "height": 960,
                "file_size": 150000,
            },
        ]
        self.add_message(
            {"photo": sizes},
            chat_id,
            user_id=user_id,
            username=username,
            first_name=first_name,
            chat_type=chat_type,
        )

    def add_media_message(
        self,
        content_type: str,
        chat_id: int = 42,
        *,
        user_id: int | None = None,
        first_name: str | None = "Tester",
        chat_type: str = "private",
    ) -> None:
        """Ставит в очередь медиа, которое не является текстом или фото (например, документ)."""
        body = {
            content_type: {
                "file_id": f"{content_type}-file",
                "file_unique_id": f"{content_type}-uid",
            }
        }
        self.add_message(body, chat_id, user_id=user_id, first_name=first_name, chat_type=chat_type)

    def add_message(
        self,
        body: dict[str, Any],
        chat_id: int = 42,
        *,
        user_id: int | None = None,
        username: str | None = None,
        first_name: str | None = "Tester",
        chat_type: str = "private",
    ) -> None:
        """Ставит в очередь сообщение с полями body. По умолчанию это личный чат с пользователем.

        Отправитель совпадает с чатом, если user_id не задан. Для групп chat_type = "group".
        """
        update_id = next(self._update_ids)
        sender: dict[str, Any] = {
            "id": chat_id if user_id is None else user_id,
            "is_bot": False,
            "first_name": first_name,
        }
        if username is not None:
            sender["username"] = username
        if chat_type == "private":
            chat: dict[str, Any] = {"id": chat_id, "type": "private", "first_name": first_name}
        else:
            chat = {"id": chat_id, "type": chat_type, "title": "Тестовая группа"}
        message: dict[str, Any] = {
            "message_id": update_id,
            "date": 1_700_000_000,
            "chat": chat,
            "from": sender,
            **body,
        }
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
        if method == "sendPhoto":
            return await self._send_photo(request)
        return _ok(True)

    async def _send_message(self, request: web.Request) -> web.Response:
        if self.fail_send_message is not None:
            code, description = self.fail_send_message
            return web.json_response(
                {"ok": False, "error_code": code, "description": description}, status=code
            )
        params = await _request_params(request)
        chat_id = params.get("chat_id", "")
        text = params.get("text", "")
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        self.outbound_events.append({"method": "sendMessage", "chat_id": chat_id, "text": text})
        # Telegram возвращает отправленное сообщение. pyTelegramBotAPI разбирает его как Message.
        return _ok(
            {
                "message_id": len(self.outbound_events),
                "date": 1_700_000_000,
                "chat": {"id": int(chat_id), "type": "private"},
                "text": text,
            }
        )

    async def _send_photo(self, request: web.Request) -> web.Response:
        if self.fail_send_photo is not None:
            code, description = self.fail_send_photo
            return web.json_response(
                {"ok": False, "error_code": code, "description": description}, status=code
            )
        params = await _request_params(request)
        chat_id = params.get("chat_id", "")
        photo = params.get("photo", "")
        caption = params.get("caption")
        self.sent_photos.append({"chat_id": chat_id, "photo": photo, "caption": caption})
        self.outbound_events.append(
            {"method": "sendPhoto", "chat_id": chat_id, "photo": photo, "caption": caption}
        )
        chat_numeric_id = int(chat_id) if str(chat_id).lstrip("-").isdigit() else -1001234567890
        chat_type = "channel" if str(chat_id).startswith(("-100", "@")) else "private"
        result: dict[str, Any] = {
            "message_id": len(self.outbound_events),
            "date": 1_700_000_000,
            "chat": {"id": chat_numeric_id, "type": chat_type},
            "photo": [
                {
                    "file_id": photo,
                    "file_unique_id": f"{photo}-u",
                    "width": 1280,
                    "height": 960,
                }
            ],
        }
        if caption is not None:
            result["caption"] = caption
        return _ok(result)

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
