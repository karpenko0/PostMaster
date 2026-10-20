"""Интерфейс Publisher, DTO результата и абстракция TelegramClient (BR-03, SPEC-014).

PublicationService публикует пост только через интерфейс Publisher. Конкретные реализации
(ReminderPublisher, позже TelegramPublisher) находятся в этом же пакете.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from telebot.async_telebot import AsyncTeleBot


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Результат выполнения Publisher (SPEC-014 §14)."""

    success: bool
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False


@runtime_checkable
class TelegramClient(Protocol):
    """Абстракция клиента Telegram Bot API (SPEC-014 §45–46, SPEC-015 §31)."""

    async def send_message(self, chat_id: str | int, text: str) -> Any: ...

    async def send_photo(
        self,
        chat_id: str | int,
        file_id: str,
        caption: str | None = None,
    ) -> Any: ...


class BotTelegramClient:
    """Адаптер AsyncTeleBot к контракту TelegramClient без скачивания файлов на диск."""

    def __init__(self, bot: AsyncTeleBot) -> None:
        self._bot = bot

    @property
    def token(self) -> str | None:
        """Токен доступен только DI для redaction; он никогда не логируется."""
        token = getattr(self._bot, "token", None)
        return token if isinstance(token, str) else None

    async def send_message(self, chat_id: str | int, text: str) -> Any:
        return await self._bot.send_message(chat_id=chat_id, text=text)

    async def send_photo(
        self,
        chat_id: str | int,
        file_id: str,
        caption: str | None = None,
    ) -> Any:
        if caption is not None:
            return await self._bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption)
        return await self._bot.send_photo(chat_id=chat_id, photo=file_id)


class Publisher(ABC):
    """Публикует пост на внешней площадке (BR-03)."""

    @abstractmethod
    async def publish(self, post: Any) -> Any:
        """Публикует пост и возвращает PublishResult (или выбрасывает исключение)."""
