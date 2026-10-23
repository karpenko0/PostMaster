"""Публикация напоминания пользователю в личный чат (SPEC-010).

Напоминание отправляется владельцу публикации. Telegram-идентификатор пользователя
получается через порт ReminderRecipientResolver, чтобы слой publishers не зависел
от слоя services.
"""

import logging
from typing import Protocol

from postmaster.domain.post import Post
from postmaster.publishers.base import Publisher
from postmaster.utils.timezone_format import format_local_datetime

logger = logging.getLogger(__name__)


class TelegramReminderSender(Protocol):
    """Контракт отправки напоминания пользователю."""

    async def send_message(self, *, chat_id: int, text: str) -> None: ...


class ReminderRecipientResolver(Protocol):
    """Порт получения Telegram ID получателя напоминания по внутреннему user_id."""

    async def get_telegram_user_id(self, user_id: int) -> int | None: ...


class ReminderPublisher(Publisher):
    """Отправляет пользователю напоминание о запланированной публикации."""

    def __init__(
        self,
        sender: TelegramReminderSender,
        recipient_resolver: ReminderRecipientResolver,
    ) -> None:
        self._sender = sender
        self._recipients = recipient_resolver

    async def publish(self, post: Post) -> None:
        telegram_user_id = await self._recipients.get_telegram_user_id(post.user_id)
        if telegram_user_id is None:
            raise ValueError("пользователь для напоминания не найден")
        local_time = format_local_datetime(post.scheduled_at_utc, post.timezone)
        text = (
            f"🔔 Напоминание!\n\n"
            f"Запланированная публикация на {local_time}.\n"
            f"Не забудьте опубликовать фотографию."
        )
        await self._sender.send_message(chat_id=telegram_user_id, text=text)
        logger.info("Reminder sent post_id=%s user_id=%s", post.id, post.user_id)
