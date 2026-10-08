"""Логирование и маскировка секретов (SPEC-023, SPEC-025; BR плана 2.2)."""

import logging
import sys

# Импорт добавляет логгеру TeleBot собственный обработчик. Тест проверяет его обход.
import telebot  # noqa: F401

from fake_telegram import TEST_TOKEN
from postmaster.utils.logging_config import (
    MASK,
    RedactingFormatter,
    configure_logging,
    redact,
)


def test_redact_replaces_explicit_secret() -> None:
    assert redact(f"key={TEST_TOKEN}", [TEST_TOKEN]) == f"key={MASK}"


def test_redact_masks_telegram_token_without_explicit_secret() -> None:
    result = redact(f"Request failed: https://api.telegram.org/bot{TEST_TOKEN}/getMe")

    assert TEST_TOKEN not in result
    assert MASK in result


def test_redact_skips_empty_secrets() -> None:
    assert redact("plain text", [""]) == "plain text"


def test_formatter_masks_secret_inside_traceback() -> None:
    try:
        raise RuntimeError(f"failed: https://api.telegram.org/bot{TEST_TOKEN}/getMe")
    except RuntimeError:
        record = logging.LogRecord(
            "postmaster.test", logging.ERROR, __file__, 1, "request failed", None, sys.exc_info()
        )

    rendered = RedactingFormatter(secrets=[TEST_TOKEN]).format(record)

    assert "Traceback" in rendered
    assert TEST_TOKEN not in rendered


def test_configure_logging_writes_redacted_lines_to_stdout(capsys) -> None:
    configure_logging("INFO", secrets=[TEST_TOKEN])

    logging.getLogger("postmaster.test").info("token=%s", TEST_TOKEN)

    out = capsys.readouterr().out
    assert TEST_TOKEN not in out
    assert MASK in out
    assert "postmaster.test" in out


def test_configure_logging_does_not_duplicate_handlers(capsys) -> None:
    configure_logging("INFO")
    configure_logging("INFO")

    logging.getLogger("postmaster.test").warning("once")

    assert capsys.readouterr().out.count("once") == 1
    assert len(logging.getLogger().handlers) == 1


def test_configure_logging_applies_level(capsys) -> None:
    configure_logging("WARNING")

    logging.getLogger("postmaster.test").info("hidden")
    logging.getLogger("postmaster.test").warning("shown")

    out = capsys.readouterr().out
    assert "hidden" not in out
    assert "shown" in out


def test_telebot_logger_output_goes_through_redacting_handler(capsys) -> None:
    # Предусловие: библиотека сама добавила обработчик, который обходит маскировку.
    assert any(
        isinstance(handler, logging.StreamHandler)
        for handler in logging.getLogger("TeleBot").handlers
    )

    configure_logging("INFO", secrets=[TEST_TOKEN])
    telebot_logger = logging.getLogger("TeleBot")
    assert telebot_logger.handlers == []
    assert telebot_logger.propagate is True

    telebot_logger.error("GET https://api.telegram.org/bot%s/getUpdates failed", TEST_TOKEN)

    captured = capsys.readouterr()
    assert TEST_TOKEN not in captured.out + captured.err
    assert "getUpdates failed" in captured.out
