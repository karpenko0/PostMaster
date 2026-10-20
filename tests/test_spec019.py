"""SPEC-019: справка /help доступна без вмешательства в FSM."""

from types import SimpleNamespace
from typing import Any

import pytest

from fake_telegram import FakeTelegram
from harness import db_snapshot, draft_data, future_date_text, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.domain.dialog import DialogState
from postmaster.handlers.help import HELP_TEXT, register_help_handler


@pytest.mark.parametrize(
    "required_text",
    [
        "/start",
        "/my_posts",
        "/cancel",
        "/help",
        "ДД.ММ.ГГГГ ЧЧ:ММ",
        "07.10.2026 15:00",
        "напоминания",
        "Telegram",
    ],
)
def test_tc_019_01_02_help_text_has_commands_date_example_and_modes(required_text: str) -> None:
    assert required_text.lower() in HELP_TEXT.lower()


async def test_help_works_in_idle_without_database_or_scheduler_changes(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path
) -> None:
    before_db = db_snapshot(tmp_path / "data" / "app.db")
    before_jobs = running_app._scheduler._scheduler.get_jobs()

    fake_telegram.add_text_message("/help", chat_id=19001, user_id=19001)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)

    assert fake_telegram.sent_messages[0]["text"] == HELP_TEXT
    assert await running_app.bot.get_state(19001, 19001) is None
    assert db_snapshot(tmp_path / "data" / "app.db") == before_db
    assert running_app._scheduler._scheduler.get_jobs() == before_jobs


async def test_help_preserves_waiting_photo_state(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    fake_telegram.add_text_message("/start", chat_id=19002, user_id=19002)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    await wait_for_state(running_app, 19002, DialogState.WAITING_PHOTO)
    before_draft = await draft_data(running_app, 19002)

    fake_telegram.add_text_message("/help", chat_id=19002, user_id=19002)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    assert fake_telegram.sent_messages[1]["text"] == HELP_TEXT
    await wait_for_state(running_app, 19002, DialogState.WAITING_PHOTO)
    assert await draft_data(running_app, 19002) == before_draft == {}


async def test_help_preserves_photo_and_waiting_datetime_then_flow_continues(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path
) -> None:
    user_id = 19003
    fake_telegram.add_text_message("/start", chat_id=user_id, user_id=user_id)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    await wait_for_state(running_app, user_id, DialogState.WAITING_PHOTO)

    fake_telegram.add_photo_message("help-photo", chat_id=user_id, user_id=user_id)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
    await wait_for_state(running_app, user_id, DialogState.WAITING_DATETIME)
    before_draft = await draft_data(running_app, user_id)
    assert before_draft["telegram_file_id"] == "help-photo"
    before_db = db_snapshot(tmp_path / "data" / "app.db")
    before_jobs = running_app._scheduler._scheduler.get_jobs()

    fake_telegram.add_text_message("/help", chat_id=user_id, user_id=user_id)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 3)

    assert fake_telegram.sent_messages[2]["text"] == HELP_TEXT
    await wait_for_state(running_app, user_id, DialogState.WAITING_DATETIME)
    assert await draft_data(running_app, user_id) == before_draft
    assert db_snapshot(tmp_path / "data" / "app.db") == before_db
    assert running_app._scheduler._scheduler.get_jobs() == before_jobs

    fake_telegram.add_text_message(future_date_text(), chat_id=user_id, user_id=user_id)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 4)
    await wait_for_state(running_app, user_id, DialogState.IDLE)
    after_db = db_snapshot(tmp_path / "data" / "app.db")
    assert len(after_db["posts"]) == 1


async def test_help_send_failure_is_logged_without_fsm_access(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenBot:
        def message_handler(self, **_kwargs: Any):
            def decorate(handler):
                self.handler = handler
                return handler

            return decorate

        async def send_message(self, _chat_id: int, _text: str) -> None:
            raise RuntimeError("Telegram API details")

    bot = BrokenBot()
    register_help_handler(bot)  # type: ignore[arg-type]
    message = SimpleNamespace(
        text="/help",
        from_user=SimpleNamespace(id=19004),
        chat=SimpleNamespace(id=19004),
    )

    with caplog.at_level("ERROR", logger="postmaster.handlers.help"):
        await bot.handler(message)

    assert "Help message send failed telegram_user_id=19004" in caplog.text
    assert "Telegram API details" not in caplog.text
