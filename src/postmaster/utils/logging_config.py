"""Логирование приложения (SPEC-023, SPEC-025) с маскировкой секретов.

Все записи выводятся в stdout, что удобно для Docker. Итоговая строка записи,
включая трассировку исключения, очищается от секретов до вывода. Поэтому
токен не попадает в лог даже тогда, когда библиотека или трассировка
содержит URL вида ``/bot<токен>/getUpdates``.

Канонические события (SPEC-025 §1) задаются константами EVENT_*. Хелпер
``log_event`` форматирует запись как ``event=NAME key=value key=value …`` —
первый токен ``event=…`` фиксирован для grep/парсинга, остальные поля
свободно расширяются. Редактирование секретов применяется ко всей строке
форматтером ``RedactingFormatter``.
"""

import logging
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any

MASK = "***"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

# Токен Telegram Bot API: "<числовой id бота>:<секретная часть>".
_TOKEN_PATTERN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")

# Логгер pyTelegramBotAPI. При импорте библиотека добавляет ему собственный
# обработчик в stderr, который обходит маскировку. Поэтому обработчик удаляется.
_TELEBOT_LOGGER = "TeleBot"

# Длина обрезки длинных значений по умолчанию (SPEC-025 §7: caption не логируется целиком).
DEFAULT_LOG_VALUE_LIMIT = 40


# === Канонические события SPEC-025 §1 =====================================
# Имена стабильны и предназначены для машинного парсинга (grep / SIEM).

EVENT_STARTUP = "startup"
EVENT_SHUTDOWN = "shutdown"
EVENT_STARTUP_ERROR = "startup_error"

EVENT_POST_CREATED = "post_created"
EVENT_CLAIM = "claim"
EVENT_CLAIM_SKIPPED = "claim_skipped"
EVENT_PUBLISH_OK = "publish_ok"
EVENT_POST_FAILED = "post_failed"
EVENT_POST_CANCELLED = "post_cancelled"
EVENT_STATUS_CHANGED = "status_changed"

EVENT_SCHEDULER_START = "scheduler_start"
EVENT_SCHEDULER_STOP = "scheduler_stop"
EVENT_SCHEDULER_JOB_ADDED = "scheduler_job_added"
EVENT_SCHEDULER_JOB_REMOVED = "scheduler_job_removed"

EVENT_RECOVERY_START = "recovery_start"
EVENT_RECOVERY_DONE = "recovery_done"
EVENT_RECOVERY_ERROR = "recovery_error"

EVENT_PUBLICATION_START = "publication_start"
EVENT_PUBLICATION_ATTEMPT_FAILED = "publication_attempt_failed"
EVENT_PUBLICATION_OK = "publication_ok"
EVENT_PUBLICATION_FAILED = "publication_failed"
EVENT_RETRY = "retry"

EVENT_PUBLISH_TELEGRAM = "publish_telegram"
EVENT_PUBLISH_REMINDER = "publish_reminder"
EVENT_PUBLISH_ERROR = "publish_error"

EVENT_COMMAND_START = "command_start"
EVENT_COMMAND_CANCEL = "command_cancel"
EVENT_COMMAND_MY_POSTS = "command_my_posts"
EVENT_COMMAND_HELP = "command_help"
EVENT_COMMAND_FAILED = "command_failed"

EVENT_MISSED = "missed"
EVENT_ERROR = "error"


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Возвращает текст, в котором заменены переданные секреты и любые токены."""
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return _TOKEN_PATTERN.sub(MASK, text)


class RedactingFormatter(logging.Formatter):
    """Форматтер, который маскирует секреты в итоговой строке, включая трассировку."""

    def __init__(self, fmt: str = LOG_FORMAT, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


def configure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """Настраивает корневой логгер. Повторный вызов заменяет обработчик, а не добавляет новый."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(secrets=secrets))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Записи библиотеки идут через общий обработчик. Уровень не задаём явно,
    # чтобы он управлялся переменной LOG_LEVEL.
    telebot_logger = logging.getLogger(_TELEBOT_LOGGER)
    telebot_logger.handlers.clear()
    telebot_logger.propagate = True
    telebot_logger.setLevel(logging.NOTSET)


# === Структурированные события SPEC-025 ====================================


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    fields: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> None:
    """Записывает структурированное событие в формате ``event=NAME key=value``.

    Первый токен ``event=NAME`` фиксирован для grep/парсинга. Все значения
    полей проходят через ``str()``. Секреты редактируются форматтером
    ``RedactingFormatter``, поэтому в значения можно безопасно подставлять
    идентификаторы, потенциально содержащие токен (например, ``file_id``).

    Args:
        logger: целевой логгер.
        event: имя события (используйте константы ``EVENT_*``).
        level: уровень логирования (по умолчанию INFO).
        fields: явный словарь полей. Дубликат ``**kwargs`` — для удобства.
        **kwargs: пары ``key=value`` для записи.
    """
    merged: dict[str, Any] = {}
    if fields:
        merged.update(fields)
    merged.update(kwargs)
    parts = [f"event={event}"]
    for key, value in merged.items():
        parts.append(f"{key}={value}")
    logger.log(level, " ".join(parts))


def truncate_for_log(value: str, *, limit: int = DEFAULT_LOG_VALUE_LIMIT) -> str:
    """Обрезает строку до ``limit`` символов; в конце добавляет ``...``.

    Используется для защиты чувствительных или длинных полей (caption, текст
    ошибки Telegram) от попадания в логи целиком (SPEC-025 §7).
    """
    if limit < 0:
        raise ValueError("limit должен быть >= 0")
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def caption_for_log(caption: str | None, *, limit: int = DEFAULT_LOG_VALUE_LIMIT) -> str:
    """Безопасное представление caption для лога.

    ``None`` → ``"-"``. Длинный текст обрезается до ``limit`` символов с
    маркером ``...``. Полный caption никогда не попадает в лог (SPEC-025 §7).
    """
    if caption is None:
        return "-"
    return truncate_for_log(caption, limit=limit)
