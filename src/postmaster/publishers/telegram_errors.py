"""Единая нормализация ошибок Telegram Bot API (SPEC-021)."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from telebot.asyncio_helper import ApiTelegramException

from postmaster.domain.post import TemporaryPublicationError
from postmaster.utils.logging_config import redact

logger = logging.getLogger(__name__)


class TelegramErrorCode(StrEnum):
    """Стабильные коды Telegram API и входных ошибок Publisher."""

    TELEGRAM_API_TIMEOUT = "TELEGRAM_API_TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    TELEGRAM_API_UNAVAILABLE = "TELEGRAM_API_UNAVAILABLE"
    TELEGRAM_API_ERROR = "TELEGRAM_API_ERROR"
    TELEGRAM_PERMISSION_DENIED = "TELEGRAM_PERMISSION_DENIED"
    TELEGRAM_TARGET_UNAVAILABLE = "TELEGRAM_TARGET_UNAVAILABLE"
    BOT_BLOCKED = "BOT_BLOCKED"
    INVALID_FILE_ID = "INVALID_FILE_ID"
    MESSAGE_SEND_FAILED = "MESSAGE_SEND_FAILED"
    PHOTO_SEND_FAILED = "PHOTO_SEND_FAILED"
    TARGET_REQUIRED = "TARGET_REQUIRED"
    FILE_ID_MISSING = "FILE_ID_MISSING"
    INVALID_PUBLICATION_TYPE = "INVALID_PUBLICATION_TYPE"
    INVALID_POST_STATUS = "INVALID_POST_STATUS"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    TARGET_USER_MISSING = "TARGET_USER_MISSING"


class TelegramOperation(StrEnum):
    MESSAGE = "message"
    PHOTO = "photo"


_RETRYABLE_CODES = frozenset(
    {
        TelegramErrorCode.TELEGRAM_API_TIMEOUT,
        TelegramErrorCode.NETWORK_ERROR,
        TelegramErrorCode.TELEGRAM_API_UNAVAILABLE,
    }
)
_KNOWN_CODES = {code.value: code for code in TelegramErrorCode}


@dataclass(frozen=True, slots=True)
class TelegramError:
    """Нормализованная ошибка без зависимости вызывающего слоя от TeleBot exception."""

    code: TelegramErrorCode
    message: str
    retryable: bool


class TelegramErrorMapper:
    """Классифицирует Telegram/transport ошибки одинаково для обоих Publisher."""

    def __init__(self, *, secrets: tuple[str, ...] | list[str] = ()) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def map(
        self,
        exception: BaseException,
        *,
        operation: TelegramOperation | str | None = None,
        file_id: str | None = None,
        caption: str | None = None,
    ) -> TelegramError:
        """Преобразует exception в безопасный code/message/retryable DTO."""
        text = str(exception).strip() or type(exception).__name__
        lowered = text.lower()
        status = getattr(exception, "error_code", None)
        explicit = _KNOWN_CODES.get(status) if isinstance(status, str) else None

        if explicit is not None:
            code = explicit
        elif isinstance(exception, TimeoutError) or any(
            marker in lowered for marker in ("timed out", "timeout", "time-out")
        ):
            code = TelegramErrorCode.TELEGRAM_API_TIMEOUT
        elif _is_bot_blocked(lowered):
            code = TelegramErrorCode.BOT_BLOCKED
        elif _is_invalid_file_id(lowered):
            code = TelegramErrorCode.INVALID_FILE_ID
        elif _is_permission_denied(lowered) or (
            isinstance(exception, ApiTelegramException) and status == 403
        ):
            code = TelegramErrorCode.TELEGRAM_PERMISSION_DENIED
        elif _is_target_unavailable(lowered):
            code = TelegramErrorCode.TELEGRAM_TARGET_UNAVAILABLE
        elif isinstance(exception, ConnectionError) or _is_network_error(lowered):
            code = TelegramErrorCode.NETWORK_ERROR
        elif _is_temporary_api_error(exception, lowered, status):
            code = TelegramErrorCode.TELEGRAM_API_UNAVAILABLE
        elif isinstance(exception, ApiTelegramException) and status == 400:
            code = TelegramErrorCode.TELEGRAM_TARGET_UNAVAILABLE
        else:
            code = _fallback_code(operation)

        safe_message = self._safe_message(text, file_id=file_id, caption=caption)
        logger.debug(
            "Telegram exception mapped error_code=%s retryable=%s exception_type=%s",
            code.value,
            code in _RETRYABLE_CODES,
            type(exception).__name__,
        )
        return TelegramError(
            code=code,
            message=safe_message,
            retryable=code in _RETRYABLE_CODES,
        )

    def _safe_message(
        self,
        message: str,
        *,
        file_id: str | None,
        caption: str | None,
    ) -> str:
        safe = redact(message, self._secrets)
        if file_id:
            safe = safe.replace(file_id, "<file_id>")
        if caption:
            safe = safe.replace(caption, "<caption_redacted>")
        return safe or "Telegram API error"


def _fallback_code(operation: TelegramOperation | str | None) -> TelegramErrorCode:
    if operation == TelegramOperation.MESSAGE or operation == TelegramOperation.MESSAGE.value:
        return TelegramErrorCode.MESSAGE_SEND_FAILED
    if operation == TelegramOperation.PHOTO or operation == TelegramOperation.PHOTO.value:
        return TelegramErrorCode.PHOTO_SEND_FAILED
    return TelegramErrorCode.TELEGRAM_API_ERROR


def _is_bot_blocked(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "bot was blocked by the user",
            "bot is blocked by the user",
            "bot blocked by user",
            "bot was blocked",
            "bot is blocked",
            "user blocked the bot",
        )
    )


def _is_invalid_file_id(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "wrong file identifier",
            "invalid file_id",
            "wrong remote file identifier",
            "file identifier is invalid",
            "file_id is invalid",
        )
    )


def _is_permission_denied(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "not enough rights",
            "need administrator rights",
            "chat_write_forbidden",
            "have no rights",
            "permission denied",
            "permission",
            "kicked from the channel",
            "kicked from channel",
            "not a member of the channel",
            "bot is not a member",
            "chat_admin_required",
            "forbidden",
        )
    )


def _is_target_unavailable(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "chat not found",
            "channel not found",
            "peer_id_invalid",
            "channel_private",
            "inaccessible",
            "target unavailable",
            "chat was deactivated",
            "chat_id is empty",
        )
    )


def _is_network_error(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "network error",
            "network is unreachable",
            "network unreachable",
            "connection reset",
            "connection refused",
            "cannot connect",
            "cannot reach",
            "unreachable",
        )
    )


def _is_temporary_api_error(
    exception: BaseException,
    text: str,
    status: Any,
) -> bool:
    return (
        isinstance(exception, TemporaryPublicationError)
        or bool(getattr(exception, "is_temporary", False) or getattr(exception, "retryable", False))
        or (
            isinstance(exception, ApiTelegramException)
            and isinstance(status, int)
            and (status == 429 or status >= 500)
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
                "internal server error",
            )
        )
    )
