"""IT-007-01…03 и E2E-007: Telegram → /start → UserService → Repository → SQLite."""

import sqlite3
from pathlib import Path

import pytest

from fake_telegram import FakeTelegram
from harness import db_path, users_rows, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.domain.dialog import DialogState
from postmaster.handlers.start import START_GREETING, START_GREETING_NO_NAME


async def test_start_with_missing_name_then_profile_changes_preserve_user_id_and_timezone(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    fake_telegram.add_text_message("/start", chat_id=71, user_id=71, first_name=None, username=None)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    await wait_for_state(running_app, 71, DialogState.WAITING_PHOTO)
    assert fake_telegram.sent_messages[0]["text"] == START_GREETING_NO_NAME
    [(internal_id, username, name)] = users_rows(db_path(tmp_path), 71)
    assert internal_id > 0 and username is None and name is None

    # Пояс не приходит из Telegram. Имитируем уже настроенный профиль (UI — SPEC-041).
    with sqlite3.connect(db_path(tmp_path)) as conn:
        conn.execute("UPDATE users SET timezone = ? WHERE id = ?", ("Europe/Moscow", internal_id))

    fake_telegram.add_text_message(
        "/start", chat_id=71, user_id=71, first_name="Анна", username="profile"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
    assert fake_telegram.sent_messages[1]["text"] == START_GREETING.format(first_name="Анна")
    assert users_rows(db_path(tmp_path), 71) == [(internal_id, "profile", "Анна")]
    with sqlite3.connect(db_path(tmp_path)) as conn:
        [(timezone,)] = conn.execute("SELECT timezone FROM users WHERE id = ?", (internal_id,))
    assert timezone == "Europe/Moscow"

    # Telegram username исчез, имя отсутствует: обновление NULL не меняет users.id.
    fake_telegram.add_text_message("/start", chat_id=71, user_id=71, first_name=None, username=None)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 3)
    assert fake_telegram.sent_messages[2]["text"] == START_GREETING_NO_NAME
    assert users_rows(db_path(tmp_path), 71) == [(internal_id, None, None)]
    await wait_for_state(running_app, 71, DialogState.WAITING_PHOTO)


async def test_two_users_with_same_username_have_separate_profiles(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    fake_telegram.add_text_message(
        "/start", chat_id=111, user_id=111, username="same", first_name="Первый"
    )
    fake_telegram.add_text_message(
        "/start", chat_id=222, user_id=222, username="same", first_name="Второй"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
    a = users_rows(db_path(tmp_path), 111)
    b = users_rows(db_path(tmp_path), 222)
    assert len(a) == len(b) == 1
    assert a[0][0] != b[0][0]
    assert a[0][1:] == ("same", "Первый")
    assert b[0][1:] == ("same", "Второй")


async def test_failed_registration_does_not_log_personal_data_or_create_fictive_id(
    running_app: Application,
    fake_telegram: FakeTelegram,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from postmaster.handlers.start import START_FAILED
    from postmaster.repositories.user_repository import UserRepository

    async def database_error(self: UserRepository, **kwargs):
        raise RuntimeError("private_name_007 SQL params contain personal data")

    monkeypatch.setattr(UserRepository, "add", database_error)
    with caplog.at_level("ERROR"):
        fake_telegram.add_text_message(
            "/start", chat_id=72, user_id=72, first_name="private_name_007"
        )
        await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    assert fake_telegram.sent_messages[0]["text"] == START_FAILED
    assert "Не удалось зарегистрировать пользователя" in caplog.text
    assert "private_name_007" not in caplog.text
    assert users_rows(db_path(tmp_path), 72) == []
    assert await running_app.bot.get_state(72, 72) is None
