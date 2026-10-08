"""Команда /start через Long Polling и fake Telegram API: US-01, US-02, AC-03, AC-04 (SPEC-002).

Приложение собирается через Application.from_settings, как в боевом запуске. Проверяются
приветствие, состояние WAITING_PHOTO, строки в таблице users и поведение при ошибке.
"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from pathlib import Path

import pytest

from fake_telegram import TEST_TOKEN, FakeTelegram
from postmaster.app import Application
from postmaster.config import Settings
from postmaster.domain.user import UserState
from postmaster.handlers.start import START_GREETING
from postmaster.repositories.user_repository import UserRepository

WAIT_SECONDS = 10


@pytest.fixture
async def started_app(tmp_path: Path, fake_telegram: FakeTelegram) -> AsyncIterator[Application]:
    """Запускает приложение с Long Polling. После теста останавливает его штатно."""
    settings = Settings(
        bot_token=TEST_TOKEN,
        database_url=f"sqlite+aiosqlite:///{_db_path(tmp_path)}",
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


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "app.db"


def _send_start(fake_telegram: FakeTelegram, user_id: int, username: str | None, name: str) -> None:
    # В личном чате chat_id совпадает с id пользователя.
    fake_telegram.add_text_message(
        "/start", chat_id=user_id, user_id=user_id, username=username, first_name=name
    )


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), WAIT_SECONDS)


async def _wait_for_state(app: Application, user_id: int, expected: str) -> None:
    async def poll() -> None:
        while await app.bot.get_state(user_id, user_id) != expected:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), WAIT_SECONDS)


def _rows_for(db_path: Path, telegram_user_id: int) -> list[tuple[int, str | None, str]]:
    query = "SELECT id, username, first_name FROM users WHERE telegram_user_id = ?"
    with closing(sqlite3.connect(db_path)) as connection:
        return list(connection.execute(query, (telegram_user_id,)).fetchall())


async def test_start_greets_new_user_and_waits_for_photo(
    started_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    _send_start(fake_telegram, 42, "ivan", "Иван")

    await _wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    await _wait_for_state(started_app, 42, UserState.WAITING_PHOTO.value)

    assert fake_telegram.sent_messages == [
        {"chat_id": "42", "text": START_GREETING.format(first_name="Иван")}
    ]
    rows = _rows_for(_db_path(tmp_path), 42)
    assert [(username, first_name) for _, username, first_name in rows] == [("ivan", "Иван")]


async def test_repeated_start_greets_again_without_duplicate(
    started_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    _send_start(fake_telegram, 42, "ivan", "Иван")
    await _wait_until(lambda: len(fake_telegram.sent_messages) == 1)

    _send_start(fake_telegram, 42, "ivan", "Иван")
    await _wait_until(lambda: len(fake_telegram.sent_messages) == 2)
    await _wait_for_state(started_app, 42, UserState.WAITING_PHOTO.value)

    assert len(_rows_for(_db_path(tmp_path), 42)) == 1


async def test_user_without_username_is_registered(
    started_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    _send_start(fake_telegram, 43, None, "Пётр")

    await _wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    await _wait_for_state(started_app, 43, UserState.WAITING_PHOTO.value)

    rows = _rows_for(_db_path(tmp_path), 43)
    assert [(username, first_name) for _, username, first_name in rows] == [(None, "Пётр")]


async def test_failed_registration_keeps_data_and_state(
    started_app: Application,
    fake_telegram: FakeTelegram,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _send_start(fake_telegram, 42, "ivan", "Иван")
    await _wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    await _wait_for_state(started_app, 42, UserState.WAITING_PHOTO.value)

    async def unavailable_database(self: UserRepository, telegram_user_id: int) -> None:
        raise RuntimeError("база данных недоступна")

    monkeypatch.setattr(UserRepository, "find_by_telegram_id", unavailable_database)

    def error_logged() -> bool:
        return any("Не удалось зарегистрировать" in r.getMessage() for r in caplog.records)

    _send_start(fake_telegram, 42, "ivan", "Иван")
    await _wait_until(error_logged)

    # Ошибка не должна испортить данные и состояние, полученные раньше (§7 п. 4).
    assert len(fake_telegram.sent_messages) == 1
    assert await started_app.bot.get_state(42, 42) == UserState.WAITING_PHOTO.value
    assert len(_rows_for(_db_path(tmp_path), 42)) == 1
