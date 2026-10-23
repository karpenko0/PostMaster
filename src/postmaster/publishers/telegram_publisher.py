"""Публикация постов в Telegram-канал (SPEC-015).

TelegramChannelPublisher публикует photo+caption в target_id (Telegram-канал)
через Telegram Bot API. Требует PROCESSING, target_id и file_id.

Не работает с Telegram API напрямую — только через интерфейс TelegramSender.
Не управляет Scheduler, retry и финальным статусом — это ответственность PublicationService.
"""

import logging
from typing import Protocol

from postmaster.domain.post import Post, PostStatus
from postmaster.publishers.base import Publisher, PublishResult

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


class TelegramSender(Protocol):
    """Минимальный контракт отправки в Telegram, который использует Publisher."""

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> object | None: ...


# Коды ошибок Telegram API, которые означают «нет прав на публикацию».
# https://core.telegram.org/bots/api#sendphoto
_PERMISSION_ERROR_CODES = frozenset(
    {
        400,  # Bad Request: chat not found / bot was kicked / etc.
        403,  # Forbidden: bot was kicked / chat is deactivated
    }
)

# Подстроки в тексте ошибки, указывающие на permission problem.
_PERMISSION_ERROR_TEXTS = (
    "chat not found",
    "bot was kicked",
    "bot is not a member",
    "not enough rights",
    "need administrator rights",
    "chat is deactivated",
    "forbidden",
)


class TelegramChannelPublisher(Publisher):
    """Публикует пост в заданный Telegram-канал через TelegramSender (SPEC-015)."""

    def __init__(self, sender: TelegramSender) -> None:
        self._sender = sender

    async def publish(self, post: Post) -> PublishResult:
        """Отправляет photo+caption в target_id (Telegram-канал).

        Raises:
            InvalidPostStatusError: post.status != PROCESSING.
            TargetMissingError: target_id пустой.
            FileIdMissingError: telegram_file_id пустой.
            TelegramPermissionError: бот не имеет прав на публикацию в канал.
            TelegramTimeoutError: таймаут при обращении к Telegram API.
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

    @staticmethod
    def _handle_send_error(post: Post, exc: Exception) -> None:
        """Преобразует ошибки Telegram API в типизированные исключения."""
        if _is_permission_error(exc):
            logger.warning(
                "Telegram permission denied post_id=%s target=%s",
                post.id,
                post.target_id,
            )
            raise TelegramPermissionError(
                f"нет прав на публикацию в канал {post.target_id}: {exc}"
            ) from exc
        if _is_timeout(exc):
            logger.warning("Telegram timeout post_id=%s", post.id)
            raise TelegramTimeoutError(f"таймаут при обращении к Telegram API: {exc}") from exc
        # Прочие ошибки пробрасываются без обёртки.
        raise exc

    @staticmethod
    def _extract_message_id(message: object | None) -> str | None:
        if isinstance(message, dict):
            value = message.get("message_id")
            return None if value is None else str(value)
        return None


def _is_permission_error(exc: Exception) -> bool:
    """Эвристика: ошибка «нет прав на публикацию в канал»."""
    code = getattr(exc, "error_code", None)
    if code in _PERMISSION_ERROR_CODES:
        message = str(exc).lower()
        return any(text in message for text in _PERMISSION_ERROR_TEXTS)
    message = str(exc).lower()
    return any(text in message for text in _PERMISSION_ERROR_TEXTS)


def _is_timeout(exc: Exception) -> bool:
    """Эвристика: таймаут при обращении к Telegram API."""
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc).lower()
    return "timeout" in message or "timed out" in message
