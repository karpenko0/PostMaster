"""TelegramPublisher: автоматическая публикация фотографии в Telegram-канал (SPEC-015).

Работает только с publication_type=TELEGRAM и status=PROCESSING, отправляет фотографию
в целевой канал post.target_id по сохранённому telegram_file_id без скачивания на диск,
передаёт caption при наличии и возвращает PublishResult в PublicationService.
"""

import inspect
import logging
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException

from postmaster.domain.post import PostStatus, PublicationType, TemporaryPublicationError
from postmaster.publishers.base import BotTelegramClient, Publisher, PublishResult
from postmaster.utils.logging_config import redact

logger = logging.getLogger(__name__)


class TelegramErrorCode(StrEnum):
    """Коды ошибок TelegramPublisher (SPEC-015 §34)."""

    INVALID_PUBLICATION_TYPE = "INVALID_PUBLICATION_TYPE"
    INVALID_POST_STATUS = "INVALID_POST_STATUS"
    TARGET_REQUIRED = "TARGET_REQUIRED"
    FILE_ID_MISSING = "FILE_ID_MISSING"
    TELEGRAM_PERMISSION_DENIED = "TELEGRAM_PERMISSION_DENIED"
    TELEGRAM_TARGET_UNAVAILABLE = "TELEGRAM_TARGET_UNAVAILABLE"
    INVALID_FILE_ID = "INVALID_FILE_ID"
    TELEGRAM_API_TIMEOUT = "TELEGRAM_API_TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    TELEGRAM_API_UNAVAILABLE = "TELEGRAM_API_UNAVAILABLE"


_KNOWN_CODES = {code.value for code in TelegramErrorCode}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _classify_telegram_error(exc: BaseException) -> tuple[TelegramErrorCode, bool]:
    """Определяет доменный код ошибки SPEC-015 (§34–38) и признак временного сбоя."""
    explicit_code = getattr(exc, "error_code", None)
    if isinstance(explicit_code, str) and explicit_code in _KNOWN_CODES:
        code = TelegramErrorCode(explicit_code)
        is_temp = code in {
            TelegramErrorCode.TELEGRAM_API_TIMEOUT,
            TelegramErrorCode.NETWORK_ERROR,
            TelegramErrorCode.TELEGRAM_API_UNAVAILABLE,
        }
        return code, is_temp

    text = str(exc).lower()

    if isinstance(exc, TimeoutError) or "timeout" in text or "timed out" in text:
        return TelegramErrorCode.TELEGRAM_API_TIMEOUT, True

    if any(
        marker in text
        for marker in (
            "wrong file identifier",
            "invalid file_id",
            "wrong remote file identifier",
            "file error",
            "invalid file",
        )
    ):
        return TelegramErrorCode.INVALID_FILE_ID, False

    if any(
        marker in text
        for marker in (
            "not enough rights",
            "need administrator rights",
            "chat_write_forbidden",
            "have no rights",
            "permission",
            "kicked from the channel",
            "not a member of the channel",
            "bot is not a member",
            "chat_admin_required",
            "forbidden",
        )
    ) or (isinstance(exc, ApiTelegramException) and exc.error_code == 403):
        return TelegramErrorCode.TELEGRAM_PERMISSION_DENIED, False

    if any(
        marker in text
        for marker in (
            "chat not found",
            "channel not found",
            "peer_id_invalid",
            "channel_private",
            "inaccessible",
            "target unavailable",
            "chat was deactivated",
        )
    ):
        return TelegramErrorCode.TELEGRAM_TARGET_UNAVAILABLE, False

    if isinstance(exc, ConnectionError) or any(
        marker in text
        for marker in (
            "network error",
            "network",
            "connection reset",
            "connection refused",
            "cannot connect",
            " unreachable",
        )
    ):
        return TelegramErrorCode.NETWORK_ERROR, True

    if (
        isinstance(exc, TemporaryPublicationError)
        or bool(getattr(exc, "is_temporary", False) or getattr(exc, "retryable", False))
        or (
            isinstance(exc, ApiTelegramException)
            and (exc.error_code == 429 or exc.error_code >= 500)
        )
        or any(
            marker in text
            for marker in (
                "temporarily unavailable",
                "service unavailable",
                "bad gateway",
                "gateway timeout",
                "too many requests",
                "telegram unavailable",
            )
        )
    ):
        return TelegramErrorCode.TELEGRAM_API_UNAVAILABLE, True

    if isinstance(exc, ApiTelegramException) and exc.error_code == 400:
        return TelegramErrorCode.TELEGRAM_TARGET_UNAVAILABLE, False

    return TelegramErrorCode.TELEGRAM_API_UNAVAILABLE, False


def _safe_error_message(
    exc: BaseException,
    file_id: str | None,
    caption: str | None,
    secrets: tuple[str, ...],
) -> str:
    raw = str(exc).strip() or type(exc).__name__
    raw = redact(raw, secrets)
    if file_id and len(file_id) >= 4:
        raw = raw.replace(file_id, "<file_id>")
    if caption:
        raw = raw.replace(caption, "<caption_redacted>")
    return raw


class TelegramPublisher(Publisher):
    """Публикует фотографию в целевой Telegram-канал по telegram_file_id (SPEC-015)."""

    def __init__(
        self,
        telegram_client: Any,
        *,
        secrets: Iterable[str] = (),
    ) -> None:
        self._client = (
            BotTelegramClient(telegram_client)
            if isinstance(telegram_client, AsyncTeleBot)
            else telegram_client
        )
        bot_token = getattr(telegram_client, "token", None)
        collected = [s for s in secrets if s]
        if isinstance(bot_token, str) and bot_token:
            collected.append(bot_token)
        self._secrets = tuple(collected)

    async def publish(self, post: Any) -> PublishResult:
        """Выполняет публикацию фото в Telegram-канал, возвращая PublishResult (SPEC-015 §13–15)."""
        post_id = getattr(post, "id", None)
        target_id = getattr(post, "target_id", None)
        logger.info(
            "Telegram publication started post_id=%s target_id=%s",
            post_id,
            target_id,
        )

        if getattr(post, "publication_type", None) != PublicationType.TELEGRAM:
            return self._fail(
                post_id,
                TelegramErrorCode.INVALID_PUBLICATION_TYPE,
                "TelegramPublisher поддерживает только publication_type=TELEGRAM",
            )

        if getattr(post, "status", None) != PostStatus.PROCESSING:
            return self._fail(
                post_id,
                TelegramErrorCode.INVALID_POST_STATUS,
                "TelegramPublisher ожидает post.status=PROCESSING",
            )

        if target_id is None or isinstance(target_id, bool):
            return self._fail(
                post_id,
                TelegramErrorCode.TARGET_REQUIRED,
                "Отсутствует target_id целевого канала",
            )
        if isinstance(target_id, str):
            if not target_id.strip():
                return self._fail(
                    post_id,
                    TelegramErrorCode.TARGET_REQUIRED,
                    "Отсутствует target_id целевого канала",
                )
        elif not isinstance(target_id, int):
            return self._fail(
                post_id,
                TelegramErrorCode.TARGET_REQUIRED,
                "Отсутствует target_id целевого канала",
            )

        file_id = getattr(post, "telegram_file_id", None)
        if not isinstance(file_id, str) or not file_id.strip():
            return self._fail(
                post_id,
                TelegramErrorCode.FILE_ID_MISSING,
                "Отсутствует telegram_file_id",
            )

        caption = getattr(post, "caption", None)
        try:
            if caption is not None:
                await _maybe_await(self._client.send_photo(target_id, file_id, caption=caption))
            else:
                await _maybe_await(self._client.send_photo(target_id, file_id))
        except Exception as exc:
            code, retryable = _classify_telegram_error(exc)
            return self._fail(
                post_id,
                code,
                _safe_error_message(exc, file_id, caption, self._secrets),
                retryable=retryable,
            )

        logger.info("Telegram publication completed post_id=%s", post_id)
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
            "Telegram publication failed post_id=%s error_code=%s",
            post_id,
            code.value,
        )
        return PublishResult(
            success=False,
            error_code=code.value,
            error_message=message,
            retryable=retryable,
        )
