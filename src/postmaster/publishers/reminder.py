"""ReminderPublisher: отправка напоминания и фото владельцу поста (SPEC-014).

Работает только с publication_type=REMINDER, определяет чат через users.telegram_user_id,
отправляет сначала текст напоминания, затем фотографию по сохранённому telegram_file_id
без скачивания файла на диск. Не управляет Scheduler и не меняет статус Post напрямую.
"""

import inspect
import logging
from enum import StrEnum
from typing import Any, Protocol

from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException

from postmaster.domain.post import PublicationType, TemporaryPublicationError
from postmaster.domain.user import User
from postmaster.publishers.base import BotTelegramClient, Publisher, PublishResult

logger = logging.getLogger(__name__)

REMINDER_TEXT = "⏰ Время публиковать!\n\nКопируй и вставляй в соцсеть."


class ReminderErrorCode(StrEnum):
    """Коды ошибок ReminderPublisher (SPEC-014 §36, §42)."""

    INVALID_PUBLICATION_TYPE = "INVALID_PUBLICATION_TYPE"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    TARGET_USER_MISSING = "TARGET_USER_MISSING"
    FILE_ID_MISSING = "FILE_ID_MISSING"
    MESSAGE_SEND_FAILED = "MESSAGE_SEND_FAILED"
    PHOTO_SEND_FAILED = "PHOTO_SEND_FAILED"
    INVALID_FILE_ID = "INVALID_FILE_ID"


class UserLookup(Protocol):
    """Контракт чтения пользователя по внутреннему users.id (SPEC-014 §21–22)."""

    async def get_by_id(self, user_id: int) -> User | None: ...


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _is_invalid_file_id_error(exc: BaseException) -> bool:
    if getattr(exc, "error_code", None) == ReminderErrorCode.INVALID_FILE_ID.value:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "wrong file identifier",
            "invalid file_id",
            "wrong remote file identifier",
        )
    )


def _is_temporary_telegram_error(exc: BaseException) -> bool:
    if isinstance(exc, (TemporaryPublicationError, TimeoutError, ConnectionError)):
        return True
    if bool(getattr(exc, "is_temporary", False) or getattr(exc, "retryable", False)):
        return True
    if isinstance(exc, ApiTelegramException):
        return exc.error_code == 429 or exc.error_code >= 500
    return False


def _safe_error_message(
    exc: BaseException,
    file_id: str | None,
    caption: str | None,
) -> str:
    raw = str(exc).strip() or type(exc).__name__
    if file_id and len(file_id) >= 4:
        raw = raw.replace(file_id, "<file_id>")
    if caption:
        raw = raw.replace(caption, "<caption_redacted>")
    return raw


class ReminderPublisher(Publisher):
    """Отправляет пользователю напоминание и фотографию по telegram_file_id (SPEC-014)."""

    def __init__(
        self,
        telegram_client: Any,
        user_repository: UserLookup | Any,
    ) -> None:
        # Поддерживаем оба порядка позиционных аргументов: (client, repo) и (repo, client).
        if hasattr(telegram_client, "get_by_id") and not hasattr(telegram_client, "send_message"):
            telegram_client, user_repository = user_repository, telegram_client
        self._client = (
            BotTelegramClient(telegram_client)
            if isinstance(telegram_client, AsyncTeleBot)
            else telegram_client
        )
        self._users = user_repository

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
            return self._fail(
                post_id,
                ReminderErrorCode.MESSAGE_SEND_FAILED,
                _safe_error_message(exc, file_id, caption),
                retryable=_is_temporary_telegram_error(exc),
            )

        try:
            if caption is not None:
                await _maybe_await(self._client.send_photo(chat_id, file_id, caption=caption))
            else:
                await _maybe_await(self._client.send_photo(chat_id, file_id))
        except Exception as exc:
            code = (
                ReminderErrorCode.INVALID_FILE_ID
                if _is_invalid_file_id_error(exc)
                else ReminderErrorCode.PHOTO_SEND_FAILED
            )
            retryable = (
                False
                if code == ReminderErrorCode.INVALID_FILE_ID
                else _is_temporary_telegram_error(exc)
            )
            return self._fail(
                post_id,
                code,
                _safe_error_message(exc, file_id, caption),
                retryable=retryable,
            )

        logger.info("Reminder publication completed post_id=%s", post_id)
        return PublishResult(success=True, error_code=None, error_message=None)

    def _fail(
        self,
        post_id: Any,
        code: ReminderErrorCode,
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
