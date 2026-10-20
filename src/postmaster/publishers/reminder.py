"""ReminderPublisher: отправка напоминания и фото владельцу поста (SPEC-014).

Работает только с publication_type=REMINDER, определяет чат через users.telegram_user_id,
отправляет сначала текст напоминания, затем фотографию по сохранённому telegram_file_id
без скачивания файла на диск. Не управляет Scheduler и не меняет статус Post напрямую.
"""

import inspect
import logging
from typing import Any, Protocol

from telebot.async_telebot import AsyncTeleBot

from postmaster.domain.post import PublicationType
from postmaster.domain.user import User
from postmaster.publishers.base import BotTelegramClient, Publisher, PublishResult
from postmaster.publishers.telegram_errors import (
    TelegramErrorCode,
    TelegramErrorMapper,
    TelegramOperation,
)

logger = logging.getLogger(__name__)

REMINDER_TEXT = "⏰ Время публиковать!\n\nКопируй и вставляй в соцсеть."


# Backwards-compatible name; all publisher codes come from one shared enum.
ReminderErrorCode = TelegramErrorCode


class UserLookup(Protocol):
    """Контракт чтения пользователя по внутреннему users.id (SPEC-014 §21–22)."""

    async def get_by_id(self, user_id: int) -> User | None: ...


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class ReminderPublisher(Publisher):
    """Отправляет пользователю напоминание и фотографию по telegram_file_id (SPEC-014)."""

    def __init__(
        self,
        telegram_client: Any,
        user_repository: UserLookup | Any,
        *,
        error_mapper: TelegramErrorMapper | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        # Поддерживаем оба порядка позиционных аргументов: (client, repo) и (repo, client).
        if hasattr(telegram_client, "get_by_id") and not hasattr(telegram_client, "send_message"):
            telegram_client, user_repository = user_repository, telegram_client
        token = getattr(telegram_client, "token", None)
        self._client = (
            BotTelegramClient(telegram_client)
            if isinstance(telegram_client, AsyncTeleBot)
            else telegram_client
        )
        self._users = user_repository
        mapper_secrets = (*secrets, *((token,) if isinstance(token, str) else ()))
        self._error_mapper = error_mapper or TelegramErrorMapper(secrets=mapper_secrets)

    async def publish(self, post: Any) -> PublishResult:
        """Выполняет отправку напоминания и фото, возвращая PublishResult (SPEC-014 §13–15)."""
        post_id = getattr(post, "id", None)
        user_id = getattr(post, "user_id", None)
        logger.info("Reminder publication started post_id=%s user_id=%s", post_id, user_id)

        if getattr(post, "publication_type", None) != PublicationType.REMINDER:
            return self._fail(
                post_id,
                ReminderErrorCode.INVALID_PUBLICATION_TYPE,
                "ReminderPublisher поддерживает только publication_type=REMINDER",
            )

        file_id = getattr(post, "telegram_file_id", None)
        if not isinstance(file_id, str) or not file_id.strip():
            return self._fail(
                post_id,
                ReminderErrorCode.FILE_ID_MISSING,
                "Отсутствует telegram_file_id",
            )

        user = await _maybe_await(self._users.get_by_id(user_id))
        if user is None:
            return self._fail(
                post_id,
                ReminderErrorCode.USER_NOT_FOUND,
                "Владелец публикации не найден",
            )

        chat_id = getattr(user, "telegram_user_id", None)
        if chat_id is None or isinstance(chat_id, bool) or not isinstance(chat_id, int):
            return self._fail(
                post_id,
                ReminderErrorCode.TARGET_USER_MISSING,
                "У пользователя отсутствует telegram_user_id",
            )

        caption = getattr(post, "caption", None)
        try:
            await _maybe_await(self._client.send_message(chat_id, REMINDER_TEXT))
        except Exception as exc:
            error = self._error_mapper.map(
                exc,
                operation=TelegramOperation.MESSAGE,
                file_id=file_id,
                caption=caption,
            )
            return self._fail(
                post_id,
                error.code,
                error.message,
                retryable=error.retryable,
            )

        try:
            if caption is not None:
                await _maybe_await(self._client.send_photo(chat_id, file_id, caption=caption))
            else:
                await _maybe_await(self._client.send_photo(chat_id, file_id))
        except Exception as exc:
            error = self._error_mapper.map(
                exc,
                operation=TelegramOperation.PHOTO,
                file_id=file_id,
                caption=caption,
            )
            return self._fail(
                post_id,
                error.code,
                error.message,
                retryable=error.retryable,
            )

        logger.info("Reminder publication completed post_id=%s", post_id)
        return PublishResult(success=True, error_code=None, error_message=None)

    def _fail(
        self,
        post_id: Any,
        code: TelegramErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> PublishResult:
        logger.error(
            "Reminder publication failed post_id=%s error_code=%s",
            post_id,
            code.value,
        )
        return PublishResult(
            success=False,
            error_code=code.value,
            error_message=message,
            retryable=retryable,
        )
