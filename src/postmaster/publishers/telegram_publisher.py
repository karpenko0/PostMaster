"""Публикация постов в Telegram-канал (SPEC-015).

TelegramChannelPublisher публикует photo+caption в target_id (Telegram-канал)
через Telegram Bot API. Требует PROCESSING, target_id и file_id.

Не работает с Telegram API напрямую — только через интерфейс TelegramSender.
Не управляет Scheduler, retry и финальным статусом — это ответственность PublicationService.

Ошибки Telegram API нормализуются через ``TelegramErrorMapper`` (SPEC-021):
специфические случаи превращаются в ``TelegramPermissionError``/``TelegramTimeoutError``,
остальные — в ``TelegramAPIError`` с нормализованным ``TelegramError``.
"""

import logging
from typing import Protocol

from postmaster.domain.post import Post, PostStatus
from postmaster.publishers.base import Publisher, PublishResult
from postmaster.publishers.telegram_errors import (
    TelegramError,
    TelegramErrorCode,
    TelegramErrorMapper,
)

logger = logging.getLogger(__name__)


class TargetMissingError(Exception):
    """target_id не задан или пустой (SPEC-015 §8 target missing)."""


class FileIdMissingError(Exception):
    """telegram_file_id не задан или пустой (SPEC-015 §8 file missing)."""


class InvalidPostStatusError(Exception):
    """Пост не в PROCESSING — TelegramChannelPublisher работает только после claim."""


class TelegramPermissionError(Exception):
    """Бот не имеет права публикации в канал (SPEC-015 §8 permission)."""


class TelegramTimeoutError(Exception):
    """Таймаут при обращении к Telegram API (SPEC-015 §8 timeout)."""


class TelegramAPIError(Exception):
    """Нормализованная ошибка Telegram API (SPEC-021 §1).

    Несёт в себе ``TelegramError`` с полями code/message/retryable.
    Поднимается, когда ошибка не относится к уже существующим
    TelegramPermissionError/TelegramTimeoutError, но также прошла нормализацию
    и готова к error flow сервиса.
    """

    def __init__(self, error: TelegramError) -> None:
        self.error = error
        super().__init__(error.message)


class TelegramSender(Protocol):
    """Минимальный контракт отправки в Telegram, который использует Publisher."""

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> object | None: ...


def _is_permission_error(exc: Exception) -> bool:
    """Совместимость с SPEC-015: ошибка «нет прав на публикацию в канал».

    Источник истины — TelegramErrorMapper (SPEC-021).
    """
    return TelegramErrorMapper().map_exception(exc).code == TelegramErrorCode.PERMISSION


def _is_timeout(exc: Exception) -> bool:
    """Совместимость с SPEC-015: таймаут при обращении к Telegram API.

    Источник истины — TelegramErrorMapper (SPEC-021).
    """
    return TelegramErrorMapper().map_exception(exc).code == TelegramErrorCode.TIMEOUT


class TelegramChannelPublisher(Publisher):
    """Публикует пост в заданный Telegram-канал через TelegramSender (SPEC-015)."""

    def __init__(
        self, sender: TelegramSender, error_mapper: TelegramErrorMapper | None = None
    ) -> None:
        self._sender = sender
        self._error_mapper = error_mapper or TelegramErrorMapper()

    async def publish(self, post: Post) -> PublishResult:
        """Отправляет photo+caption в target_id (Telegram-канал).

        Raises:
            InvalidPostStatusError: post.status != PROCESSING.
            TargetMissingError: target_id пустой.
            FileIdMissingError: telegram_file_id пустой.
            TelegramPermissionError: бот не имеет прав на публикацию в канал.
            TelegramTimeoutError: таймаут при обращении к Telegram API.
            TelegramAPIError: прочая нормализованная ошибка Telegram API.
        """
        self._validate_post(post)
        try:
            message = await self._sender.send_photo(
                chat_id=post.target_id,
                photo_file_id=post.telegram_file_id,
                caption=post.caption,
            )
        except Exception as exc:
            self._handle_send_error(post, exc)

        external_id = self._extract_message_id(message)
        logger.info(
            "Post published via Telegram post_id=%s target=%s message_id=%s",
            post.id,
            post.target_id,
            external_id,
        )
        return PublishResult(post_id=post.id, external_id=external_id)

    @staticmethod
    def _validate_post(post: Post) -> None:
        """Проверяет preconditions публикации (SPEC-015 §1, §4)."""
        if post.status != PostStatus.PROCESSING:
            raise InvalidPostStatusError(
                f"TelegramChannelPublisher требует PROCESSING, получен {post.status}"
            )
        if not post.target_id or not post.target_id.strip():
            raise TargetMissingError("target_id обязателен для TELEGRAM-публикации")
        if not post.telegram_file_id or not post.telegram_file_id.strip():
            raise FileIdMissingError("telegram_file_id обязателен для TELEGRAM-публикации")

    def _handle_send_error(self, post: Post, exc: Exception) -> None:
        """Преобразует ошибки Telegram API в типизированные исключения (SPEC-021).

        Все исключения сначала нормализуются через ``TelegramErrorMapper``,
        благодаря чему текст ошибки очищается от Bot Token до того, как он попадёт
        в логи или в ``Post.error_message`` (SPEC-021 §7).
        """
        error = self._error_mapper.map_exception(exc)
        if error.code == TelegramErrorCode.PERMISSION:
            logger.warning(
                "Telegram permission denied post_id=%s target=%s",
                post.id,
                post.target_id,
            )
            raise TelegramPermissionError(error.message) from exc
        if error.code == TelegramErrorCode.TIMEOUT:
            logger.warning(
                "Telegram timeout post_id=%s code=%s",
                post.id,
                error.code.value,
            )
            raise TelegramTimeoutError(error.message) from exc
        logger.warning(
            "Telegram API error post_id=%s code=%s retryable=%s",
            post.id,
            error.code.value,
            error.retryable,
        )
        raise TelegramAPIError(error) from exc

    @staticmethod
    def _extract_message_id(message: object | None) -> str | None:
        if isinstance(message, dict):
            value = message.get("message_id")
            return None if value is None else str(value)
        return None
