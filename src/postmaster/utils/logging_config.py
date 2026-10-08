"""Логирование приложения (SPEC-025) с маскировкой секретов (SPEC-023).

Все записи выводятся в stdout, что удобно для Docker. Итоговая строка записи,
включая трассировку исключения, очищается от секретов до вывода. Поэтому
токен не попадает в лог даже тогда, когда библиотека или трассировка
содержит URL вида ``/bot<токен>/getUpdates``.
"""

import logging
import re
import sys
from collections.abc import Iterable

MASK = "***"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

# Токен Telegram Bot API: "<числовой id бота>:<секретная часть>".
_TOKEN_PATTERN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{20,}")

# Логгер pyTelegramBotAPI. При импорте библиотека добавляет ему собственный
# обработчик в stderr, который обходит маскировку. Поэтому обработчик удаляется.
_TELEBOT_LOGGER = "TeleBot"


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
