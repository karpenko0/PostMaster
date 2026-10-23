"""Публикация напоминания пользователю в личный чат (SPEC-014).

ReminderPublisher отправляет владельцу поста:
1. Текст «Время публиковать!» с локальным временем публикации.
2. Фотографию по telegram_file_id (без скачивания на сервер).
3. Caption, если он задан в Post.

Не работает с Telegram API напрямую — только через интерфейс TelegramReminderSender.
Возвращает PublishResult. Финальный статус (PUBLISHED) выставляет PublicationService.
"""

import logging
from typing import Protocol

from postmaster.domain.post import Post
from postmaster.publishers.base import Publisher, PublishResult
from postmaster.utils.timezone_format import format_local_datetime

logger = logging.getLogger(__name__)


class UserNotFoundError(Exception):
    """Владелец поста не найден в БД (SPEC-014 §8 missing user)."""


class PhotoDataMissingError(Exception):
    """В посте отсутствует telegram_file_id (SPEC-014 §8 missing file)."""


class TelegramBlockedError(Exception):
    """Пользователь заблокировал бота — Telegram вернул 403 (SPEC-014 §8 blocked bot)."""


class TelegramReminderSender(Protocol):
    """Контракт отправки напоминания пользователю в личный чат.

    Реализация в production-коде (BotTelegramReminderSender) может выбрасывать
    ApiTelegramException с error_code=403 — это конвертируется в TelegramBlockedError.
    """

    async def send_message(self, *, chat_id: int, text: str) -> object | None: ...

    async def send_photo(
        self, *, chat_id: int, photo_file_id: str, caption: str | None = None
    ) -> object | None: ...


class ReminderRecipientResolver(Protocol):
    """Порт получения Telegram ID получателя напоминания по внутреннему user_id."""

    async def get_telegram_user_id(self, user_id: int) -> int | None: ...


REMINDER_TEXT_TEMPLATE = "🔔 Время публиковать!\n\nЗапланированная публикация: {local_time}."


class ReminderPublisher(Publisher):
    """Отправляет пользователю напоминание о запланированной публикации (SPEC-014)."""

    def __init__(
        self,
        sender: TelegramReminderSender,
        recipient_resolver: ReminderRecipientResolver,
    ) -> None:
        self._sender = sender
        self._recipients = recipient_resolver

    async def publish(self, post: Post) -> PublishResult:
        """Отправляет текст + фото пользователю-владельцу поста.

        Raises:
            PhotoDataMissingError: telegram_file_id пустой.
            UserNotFoundError: владелец поста не найден в БД.
            TelegramBlockedError: пользователь заблокировал бота.
        """
        if not post.telegram_file_id or not post.telegram_file_id.strip():
            raise PhotoDataMissingError("telegram_file_id обязателен для Reminder")

        telegram_user_id = await self._recipients.get_telegram_user_id(post.user_id)
        if telegram_user_id is None:
            raise UserNotFoundError(
                f"пользователь для напоминания не найден user_id={post.user_id}"
            )

        local_time = format_local_datetime(post.scheduled_at_utc, post.timezone)
        text = REMINDER_TEXT_TEMPLATE.format(local_time=local_time)

        try:
            text_message = await self._sender.send_message(chat_id=telegram_user_id, text=text)
        except Exception as exc:
            if _is_bot_blocked(exc):
                raise TelegramBlockedError(
                    f"пользователь заблокировал бота user_id={post.user_id}"
                ) from exc
            raise

        try:
            photo_message = await self._sender.send_photo(
                chat_id=telegram_user_id,
                photo_file_id=post.telegram_file_id,
                caption=post.caption,
            )
        except Exception as exc:
            if _is_bot_blocked(exc):
                raise TelegramBlockedError(
                    f"пользователь заблокировал бота user_id={post.user_id}"
                ) from exc
            raise

        external_id = self._extract_message_id(photo_message) or self._extract_message_id(
            text_message
        )
        logger.info(
            "Reminder sent post_id=%s user_id=%s message_id=%s",
            post.id,
            post.user_id,
            external_id,
        )
        return PublishResult(post_id=post.id, external_id=external_id)

    @staticmethod
    def _extract_message_id(message: object | None) -> str | None:
        if isinstance(message, dict):
            value = message.get("message_id")
            return None if value is None else str(value)
        return None


def _is_bot_blocked(exc: Exception) -> bool:
    """Эвристика: ошибка «бот заблокирован пользователем»."""
    if getattr(exc, "error_code", None) == 403:
        return True
    message = str(exc).lower()
    return "bot was blocked" in message or "user is deactivated" in message
