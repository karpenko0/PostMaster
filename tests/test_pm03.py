"""Per-post publication selection and validation."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from fake_telegram import TEST_TOKEN, FakeTelegram
from harness import db_path, future_date_text, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.config import Settings
from postmaster.domain.dialog import DialogState
from postmaster.services.dialog_service import REMINDER_BUTTON, TELEGRAM_BUTTON
from postmaster.services.post_service import PostCreationError, PostService


async def test_user_can_schedule_both_types(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
):
    async def text(value, count):
        fake_telegram.add_text_message(value, chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) >= count)

    async def confirm():
        # PM-05: confirm создаёт публикацию и отвечает сообщением.
        before = len(fake_telegram.sent_messages)
        fake_telegram.add_callback_query("pm05:confirm", chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) >= before + 1)

    await text("/start", 1)
    fake_telegram.add_photo_message("one", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) >= 2)
    assert fake_telegram.sent_messages[-1]["text"] == "Как опубликовать этот пост?"
    await text(future_date_text(), 3)
    await wait_for_state(running_app, 42, DialogState.WAITING_PUBLICATION_TYPE)
    await text(REMINDER_BUTTON, 4)
    await wait_for_state(running_app, 42, DialogState.WAITING_DATETIME)
    # PM-05: валидная дата показывает preview через sendPhoto — счётчик текстовых
    # сообщений не растёт. Дожидаемся preview-фото и состояния WAITING_CONFIRMATION.
    fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_photos) >= 1)
    await wait_for_state(running_app, 42, DialogState.WAITING_CONFIRMATION)
    await confirm()
    await wait_for_state(running_app, 42, DialogState.IDLE)
    fake_telegram.add_photo_message("two", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) >= 6)
    await text(TELEGRAM_BUTTON, 7)
    assert fake_telegram.sent_messages[-1]["text"] == "Укажите Telegram-канал для публикации."
    await text("not a channel", 8)
    await wait_for_state(running_app, 42, DialogState.WAITING_TARGET)
    await text("@Channel_name", 9)
    fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_photos) >= 2)
    await wait_for_state(running_app, 42, DialogState.WAITING_CONFIRMATION)
    await confirm()
    await wait_for_state(running_app, 42, DialogState.IDLE)
    with sqlite3.connect(db_path(tmp_path)) as db:
        rows = db.execute("SELECT publication_type, target_id FROM posts ORDER BY id").fetchall()
    assert rows == [("REMINDER", None), ("TELEGRAM", "@channel_name")]
    assert len(running_app._scheduler._scheduler.get_jobs()) == 2
    # A fresh application recovers both jobs from the persisted database.
    restarted = Application.from_settings(
        Settings(
            bot_token=TEST_TOKEN,
            database_url=f"sqlite+aiosqlite:///{db_path(tmp_path)}",
            DEFAULT_POST_MODE="TELEGRAM",
        )
    )
    await restarted.startup()
    try:
        assert len(restarted._scheduler._scheduler.get_jobs()) == 2
        repo = restarted._scheduler._posts
        posts = await repo.list_pending()
        resolver = restarted._scheduler._publication_service.publisher_resolver
        assert [type(resolver.resolve(post.publication_type)).__name__ for post in posts] == [
            "ReminderPublisher",
            "TelegramPublisher",
        ]
        assert [post.target_id for post in posts] == [None, "@channel_name"]
        before = len(fake_telegram.outbound_events)
        for post in posts:
            await restarted._scheduler._on_post_due(post.id)
        events = fake_telegram.outbound_events[before:]
        assert [(event["method"], event["chat_id"]) for event in events] == [
            ("sendMessage", "42"),
            ("sendPhoto", "42"),
            ("sendPhoto", "@channel_name"),
        ]
        with sqlite3.connect(db_path(tmp_path)) as db:
            assert db.execute("SELECT status FROM posts ORDER BY id").fetchall() == [
                ("PUBLISHED",),
                ("PUBLISHED",),
            ]
    finally:
        await restarted.stop()


@pytest.mark.parametrize(
    "kind,target,allowlist,valid",
    [
        ("REMINDER", None, "", True),
        ("REMINDER", "@channel", "", False),
        ("TELEGRAM", None, "", False),
        ("TELEGRAM", "bad", "", False),
        ("TELEGRAM", "@CHANNEL", "@channel", True),
        ("TELEGRAM", "-1001234567890", "", True),
        ("TELEGRAM", "@channel", "@otherchannel", False),
    ],
)
async def test_post_service_enforces_target(kind, target, allowlist, valid):
    repository = Mock()
    repository.create_post = AsyncMock(return_value=Mock(id=1, scheduled_at_utc=datetime.now(UTC)))
    scheduler = Mock()
    service = PostService(repository, scheduler, telegram_target_allowlist=allowlist)
    kwargs = dict(
        user_id=1,
        telegram_file_id="file",
        telegram_file_unique_id="unique",
        scheduled_at_utc=datetime.now(UTC) + timedelta(days=1),
        timezone="UTC",
        publication_type=kind,
        target_id=target,
    )
    if valid:
        await service.create_scheduled_post(**kwargs)
        scheduler.schedule_post.assert_called_once()
    else:
        with pytest.raises(PostCreationError):
            await service.create_scheduled_post(**kwargs)
        repository.create_post.assert_not_awaited()
        scheduler.schedule_post.assert_not_called()


def test_config_compatibility(monkeypatch):
    monkeypatch.setenv("POST_MODE", "TELEGRAM")
    assert Settings(bot_token="12345:" + "a" * 20).default_post_mode == "TELEGRAM"
    monkeypatch.setenv("DEFAULT_POST_MODE", "REMINDER")
    assert Settings(bot_token="12345:" + "a" * 20).default_post_mode == "REMINDER"
