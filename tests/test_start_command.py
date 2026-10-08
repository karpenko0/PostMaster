"""Команда /start через Long Polling и fake Telegram API: US-01, US-02, AC-03, AC-04 (SPEC-002).

Приложение собирается через Application.from_settings, как в боевом запуске. Проверяются
приветствие, состояние WAITING_PHOTO, строки в таблице users и поведение при ошибке.
"""

from pathlib import Path

import pytest

from fake_telegram import FakeTelegram
from harness import db_path, users_rows, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.domain.dialog import DialogState
from postmaster.handlers.start import START_GREETING
from postmaster.repositories.user_repository import UserRepository


def _send_start(fake_telegram: FakeTelegram, user_id: int, username: str | None, name: str) -> None:
    # В личном чате chat_id совпадает с id пользователя.
    fake_telegram.add_text_message(
        "/start", chat_id=user_id, user_id=user_id, username=username, first_name=name
    )


async def test_start_greets_new_user_and_waits_for_photo(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    _send_start(fake_telegram, 42, "ivan", "Иван")

    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    await wait_for_state(running_app, 42, DialogState.WAITING_PHOTO)

    assert fake_telegram.sent_messages == [
        {"chat_id": "42", "text": START_GREETING.format(first_name="Иван")}
    ]
    rows = users_rows(db_path(tmp_path), 42)
    assert [(username, first_name) for _, username, first_name in rows] == [("ivan", "Иван")]


async def test_repeated_start_greets_again_without_duplicate(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    _send_start(fake_telegram, 42, "ivan", "Иван")
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)

    _send_start(fake_telegram, 42, "ivan", "Иван")
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
    await wait_for_state(running_app, 42, DialogState.WAITING_PHOTO)

    assert len(users_rows(db_path(tmp_path), 42)) == 1


async def test_user_without_username_is_registered(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    _send_start(fake_telegram, 43, None, "Пётр")

    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    await wait_for_state(running_app, 43, DialogState.WAITING_PHOTO)

    rows = users_rows(db_path(tmp_path), 43)
    assert [(username, first_name) for _, username, first_name in rows] == [(None, "Пётр")]


async def test_failed_registration_keeps_data_and_state(
    running_app: Application,
    fake_telegram: FakeTelegram,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _send_start(fake_telegram, 42, "ivan", "Иван")
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    await wait_for_state(running_app, 42, DialogState.WAITING_PHOTO)

    async def unavailable_database(self: UserRepository, telegram_user_id: int) -> None:
        raise RuntimeError("база данных недоступна")

    monkeypatch.setattr(UserRepository, "find_by_telegram_id", unavailable_database)

    def error_logged() -> bool:
        return any("Не удалось зарегистрировать" in r.getMessage() for r in caplog.records)

    _send_start(fake_telegram, 42, "ivan", "Иван")
    await wait_until(error_logged)

    # Ошибка не должна испортить данные и состояние, полученные раньше (§7 п. 4).
    assert len(fake_telegram.sent_messages) == 1
    assert await running_app.bot.get_state(42, 42) == DialogState.WAITING_PHOTO.value
    assert len(users_rows(db_path(tmp_path), 42)) == 1
