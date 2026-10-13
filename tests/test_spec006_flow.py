"""IT-006-01/04: профиль users → date parser → UTC → сохранённый Post."""

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import update

from fake_telegram import FakeTelegram
from harness import db_path, db_snapshot, users_rows, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.database.models import UserModel
from postmaster.domain.dialog import DialogState
from postmaster.services import dialog_service
from postmaster.services.post_service import PostService
from postmaster.services.scheduler_service import SchedulerService


@pytest.mark.parametrize(
    ("stored_timezone", "resolved_timezone", "scheduled_utc"),
    [
        (None, "Europe/Moscow", datetime(2026, 10, 7, 12, tzinfo=UTC)),
        ("Invalid/Timezone", "Europe/Moscow", datetime(2026, 10, 7, 12, tzinfo=UTC)),
        ("Europe", "Europe/Moscow", datetime(2026, 10, 7, 12, tzinfo=UTC)),
        ("Asia/Tokyo", "Asia/Tokyo", datetime(2026, 10, 7, 6, tzinfo=UTC)),
    ],
)
async def test_profile_timezone_fallback_conversion_and_handoff(
    running_app: Application,
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stored_timezone: str | None,
    resolved_timezone: str,
    scheduled_utc: datetime,
) -> None:
    # Окружение сервера намеренно не совпадает с поясом пользователя.
    previous_tz = os.environ.get("TZ")
    original_create = PostService.create_scheduled_post
    received: list[dict[str, object]] = []

    async def record_create(self: PostService, **kwargs):
        received.append(kwargs)
        return await original_create(self, **kwargs)

    monkeypatch.setattr(PostService, "create_scheduled_post", record_create)
    monkeypatch.setattr(dialog_service, "utc_now", lambda: datetime(2026, 10, 7, 5, 30, tzinfo=UTC))
    try:
        if hasattr(time, "tzset"):
            monkeypatch.setenv("TZ", "Pacific/Honolulu")
            time.tzset()
        fake_telegram.add_text_message("/start", chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
        if stored_timezone is not None:
            async with running_app._engine.begin() as connection:
                await connection.execute(
                    update(UserModel)
                    .where(UserModel.telegram_user_id == 42)
                    .values(timezone=stored_timezone)
                )
        fake_telegram.add_photo_message("image", chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
        await wait_for_state(running_app, 42, DialogState.WAITING_DATETIME)
        before = db_snapshot(db_path(tmp_path))
        fake_telegram.add_text_message("07.10.2026 15:00", chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 3)
        await wait_for_state(running_app, 42, DialogState.IDLE)
        assert "📅 7 октября 2026\n⏰ 15:00" in fake_telegram.sent_messages[2]["text"]
        assert len(received) == 1
        assert received[0]["scheduled_at_utc"] == scheduled_utc
        assert received[0]["timezone"] == resolved_timezone
        assert received[0]["telegram_file_id"] == "image"
        assert received[0]["telegram_file_unique_id"] == "image-l"
        assert received[0]["user_id"] == users_rows(db_path(tmp_path), 42)[0][0]
        posts = db_snapshot(db_path(tmp_path))["posts"]
        assert len(posts) == len(before["posts"]) + 1
        assert posts[-1][4].startswith(scheduled_utc.strftime("%Y-%m-%d %H:%M"))
        assert posts[-1][5] == resolved_timezone
    finally:
        if hasattr(time, "tzset"):
            if previous_tz is None:
                monkeypatch.delenv("TZ", raising=False)
            else:
                monkeypatch.setenv("TZ", previous_tz)
            time.tzset()


def test_scheduler_configuration_uses_utc() -> None:
    # До SPEC-010 регистрация заданий отсутствует; конфигурация планировщика уже UTC.
    assert str(SchedulerService()._scheduler.timezone) == "UTC"
