"""Нормализация ошибок Telegram API (SPEC-021).

TelegramErrorMapper превращает любое исключение, полученное от Telegram API,
в единый нормализованный вид: ``code`` / ``message`` / ``retryable``.

Правила (SPEC-021 §4):
- retryable: TIMEOUT, NETWORK, UNAVAILABLE;
- non-retryable: PERMISSION, BLOCKED, INVALID_FILE, UNKNOWN.

Текст ошибки, который уходит дальше по error flow (в лог, в Post.error_message),
очищается от Bot Token: секрет не должен попасть в логи или в базу (SPEC-021 §7).
"""

import re
from dataclasses import dataclass
from enum import StrEnum

# Токен Telegram Bot API: "<числовой id бота>:<секретная часть>".
# Тот же формат, что в config.py и utils/logging_config.py.
_TOKEN_PATTERN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")

# Маска, которая подставляется вместо секрета.
MASK = "***"

# Максимальная длина нормализованного сообщения. Post.error_message в БД
# ограничен, а текст Telegram-ошибки может быть длинным.
MAX_MESSAGE_LENGTH = 500


class TelegramErrorCode(StrEnum):
    """Нормализованный код ошибки Telegram API (SPEC-021 §1)."""

    TIMEOUT = "TIMEOUT"
    """Истекло время ожидания ответа Telegram API. Повтор возможен."""

    NETWORK = "NETWORK"
    """Сетевая ошибка: соединение не установлено или разорвано. Повтор возможен."""

    UNAVAILABLE = "UNAVAILABLE"
    """Telegram API временно недоступен (429/502/503). Повтор возможен."""

    PERMISSION = "PERMISSION"
    """Нет прав на публикацию в чат. Повтор не имеет смысла."""

    BLOCKED = "BLOCKED"
    """Пользователь заблокировал бота либо чат недоступен. Повтор не имеет смысла."""

    INVALID_FILE = "INVALID_FILE"
    """Файл не найден или невалиден (например, file_id протух). Повтор не имеет смысла."""

    UNKNOWN = "UNKNOWN"
    """Ошибка не распознана. Повтор по умолчанию не выполняется."""


#: Коды, при которых публикацию имеет смысл повторить (SPEC-021 §4).
RETRYABLE_CODES: frozenset[TelegramErrorCode] = frozenset(
    {
        TelegramErrorCode.TIMEOUT,
        TelegramErrorCode.NETWORK,
        TelegramErrorCode.UNAVAILABLE,
    }
)


@dataclass(frozen=True, slots=True)
class TelegramError:
    """Нормализованная ошибка Telegram API (SPEC-021 §1, §4).

    code — машиночитаемый код; message — безопасный текст без секретов;
    retryable — признак того, что публикацию нужно повторить.
    """

    code: TelegramErrorCode
    message: str
    retryable: bool

    @property
    def is_retryable(self) -> bool:
        """Совпадает с полем retryable; оставлено для читаемости в вызывающем коде."""
        return self.retryable


# ---------------------------------------------------------------------------
# Признаки классов ошибок в тексте ответа Telegram API.
# https://core.telegram.org/bots/api#making-requests
# ---------------------------------------------------------------------------

_TIMEOUT_TEXTS = (
    "timeout",
    "timed out",
    "read timeout",
    "connect timeout",
)

_NETWORK_TEXTS = (
    "network",
    "connection error",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection refused",
    "failed to establish a new connection",
    "new connection error",
    "temporary failure in name resolution",
    "name or service not known",
    "getaddrinfo",
    "nodename nor servname",
    "network is unreachable",
    "host unreachable",
    "ssl",
)

_UNAVAILABLE_TEXTS = (
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "too many requests",
    "try again later",
    "server error",
)

_PERMISSION_TEXTS = (
    "chat not found",
    "bot was kicked",
    "bot is not a member",
    "not enough rights",
    "need administrator rights",
    "chat is deactivated",
    "forbidden",
    "not allowed",
    "have no rights to send",
    "kicked from the supergroup",
)

_BLOCKED_TEXTS = (
    "bot was blocked",
    "bot is blocked",
    "user is deactivated",
    "user_deactivated",
    "bot can't initiate conversation",
    "bot can't send messages",
)

_INVALID_FILE_TEXTS = (
    "invalid file",
    "invalid photo",
    "invalid file_id",
    "invalid file id",
    "file is too big",
    "photo_invalid_dimensions",
    "wrong file identifier",
    "file not found",
    "file_reference_expired",
    "failed to get http url content",
    "image_process_failed",
)

# Коды ответов Telegram, которые означают временную недоступность сервиса.
_UNAVAILABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Коды ответов Telegram, которые означают отказ в доступе.
_PERMISSION_STATUS_CODES = frozenset({400, 403})


class TelegramErrorMapper:
    """Приводит исключения Telegram API к нормализованному виду (SPEC-021 §1).

    Mapper не знает про Post и не обращается к БД: это чистая функция
    классификации, которую можно вызывать из Publisher и из сервисов.
    """

    def map_exception(self, exc: BaseException) -> TelegramError:
        """Классифицирует исключение и возвращает нормализованную ошибку."""
        # Уже нормализованные ошибки (TelegramAPIError из Publisher'ов) не переклассифицируем:
        # они несут готовый код и признак retryable (SPEC-021 §1, SPEC-022 §1).
        carried = getattr(exc, "error", None)
        if isinstance(carried, TelegramError):
            return carried
        code = self._classify(exc)
        return TelegramError(
            code=code,
            message=self._build_message(exc, code),
            retryable=code in RETRYABLE_CODES,
        )

    # Внутренние помощники -------------------------------------------------

    @staticmethod
    def _classify(exc: BaseException) -> TelegramErrorCode:
        """Определяет код ошибки по типу, коду ответа и тексту исключения."""
        text = str(exc).lower()
        status = _status_code(exc)

        if _is_timeout(exc, text):
            return TelegramErrorCode.TIMEOUT
        if _contains(text, _BLOCKED_TEXTS):
            return TelegramErrorCode.BLOCKED
        if _contains(text, _INVALID_FILE_TEXTS):
            return TelegramErrorCode.INVALID_FILE
        if status in _UNAVAILABLE_STATUS_CODES:
            return TelegramErrorCode.UNAVAILABLE
        if _contains(text, _UNAVAILABLE_TEXTS):
            return TelegramErrorCode.UNAVAILABLE
        if status in _PERMISSION_STATUS_CODES and _contains(text, _PERMISSION_TEXTS):
            return TelegramErrorCode.PERMISSION
        if _contains(text, _PERMISSION_TEXTS):
            return TelegramErrorCode.PERMISSION
        if _contains(text, _NETWORK_TEXTS):
            return TelegramErrorCode.NETWORK
        if isinstance(exc, (ConnectionError, OSError)) and status is None:
            return TelegramErrorCode.NETWORK
        return TelegramErrorCode.UNKNOWN

    @staticmethod
    def _build_message(exc: BaseException, code: TelegramErrorCode) -> str:
        """Собирает безопасный текст ошибки без Bot Token (SPEC-021 §7)."""
        raw = str(exc).strip()
        if not raw:
            raw = type(exc).__name__
        safe = redact_secrets(raw)
        return f"{code.value}: {safe}"[:MAX_MESSAGE_LENGTH]


def _status_code(exc: BaseException) -> int | None:
    """Возвращает HTTP-подобный код ответа из исключения, если он есть."""
    code = getattr(exc, "error_code", None)
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.isdigit():
        return int(code)
    return None


def _is_timeout(exc: BaseException, text: str) -> bool:
    """Таймаут: по типу исключения или по тексту."""
    if isinstance(exc, TimeoutError):
        return True
    return _contains(text, _TIMEOUT_TEXTS)


def _contains(text: str, needles: tuple[str, ...]) -> bool:
    """True, если хотя бы одна подстрока встречается в тексте."""
    return any(needle in text for needle in needles)


def redact_secrets(text: str) -> str:
    """Убирает из текста Bot Token и любые другие секреты (SPEC-021 §7)."""
    return _TOKEN_PATTERN.sub(MASK, text)
