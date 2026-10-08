"""Общие фикстуры тестов: чистое окружение, откат логирования и локальный fake Telegram API."""

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import telebot.asyncio_helper as telebot_http

from fake_telegram import TEST_TOKEN, FakeTelegram
from harness import WAIT_SECONDS, db_path
from postmaster.app import Application
from postmaster.bot.factory import close_bot_session
from postmaster.config import Settings

# Переменные настроек. Тесты очищают их, потому что load_dotenv() пишет в os.environ.
SETTINGS_ENV_NAMES = ("BOT_TOKEN", "DATABASE_URL", "LOG_LEVEL", "DEFAULT_TIMEZONE")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убирает переменные настроек. Откат выполняет monkeypatch, даже если их записал .env."""
    for name in SETTINGS_ENV_NAMES:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """Возвращает корневой логгер и логгер TeleBot в исходное состояние после каждого теста."""
    root = logging.getLogger()
    telebot_logger = logging.getLogger("TeleBot")
    saved_root = (list(root.handlers), root.level)
    saved_telebot = (
        list(telebot_logger.handlers),
        telebot_logger.level,
        telebot_logger.propagate,
    )
    yield
    root.handlers[:] = saved_root[0]
    root.setLevel(saved_root[1])
    telebot_logger.handlers[:] = saved_telebot[0]
    telebot_logger.setLevel(saved_telebot[1])
    telebot_logger.propagate = saved_telebot[2]


@pytest.fixture(autouse=True)
async def _close_bot_http_session() -> AsyncIterator[None]:
    """Закрывает HTTP-сессию pyTelegramBotAPI до окончания event loop теста."""
    yield
    await close_bot_session()


@pytest.fixture
async def fake_telegram(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeTelegram]:
    """Запускает fake Telegram API и направляет на него pyTelegramBotAPI."""
    server = FakeTelegram()
    await server.start()
    monkeypatch.setattr(telebot_http, "API_URL", server.api_url)
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def running_app(tmp_path: Path, fake_telegram: FakeTelegram) -> AsyncIterator[Application]:
    """Запускает приложение с Long Polling на fake API и штатно останавливает его после теста."""
    settings = Settings(
        bot_token=TEST_TOKEN,
        database_url=f"sqlite+aiosqlite:///{db_path(tmp_path)}",
        log_level="INFO",
    )
    app = Application.from_settings(settings)
    run_task = asyncio.create_task(app.run())
    await asyncio.wait_for(fake_telegram.get_updates_called.wait(), timeout=WAIT_SECONDS)
    try:
        yield app
    finally:
        await app.stop()
        await asyncio.wait_for(run_task, timeout=WAIT_SECONDS)
