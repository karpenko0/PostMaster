"""SPEC-005: Telegram → handler → parser → timezone → сервисный контракт SPEC-009."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fake_telegram import FakeTelegram
from harness import db_path, db_snapshot, draft_data, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.domain.dialog import DialogState
from postmaster.domain.photo import PhotoData
from postmaster.handlers.dialog import INVALID_FORMAT_TEXT, PAST_DATETIME_TEXT
from postmaster.services import dialog_service
from postmaster.services.post_service import PostService

USER = CHAT = 42


class Store:
    def __init__(self) -> None:
        self.state: str | None = None
        self.draft: dict[str, str] = {}

    async def get_state(self, user_id: int, chat_id: int) -> str | None:
        return self.state

    async def set_state(self, user_id: int, chat_id: int, state: str) -> None:
        self.state = state

    async def get_draft(self, user_id: int, chat_id: int) -> dict[str, str]:
        return dict(self.draft)

    async def set_draft(self, user_id: int, chat_id: int, draft: dict[str, str]) -> None:
        self.draft = dict(draft)


class Users:
    async def timezone_of(self, user_id: int) -> str:
        return "Europe/Moscow"


class Posts(PostService):
    def __init__(self) -> None:
        self.created = []

    async def create_post(self, **kwargs):
        result = await super().create_post(**kwargs)
        self.created.append(result)
        return result


IMAGE = PhotoData(file_id="image", file_unique_id="uid", file_size=1000, width=10, height=10)

FROZEN_NOW = datetime(2026, 10, 7, 11, 59, tzinfo=UTC)  # Москва: 14:59


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("завтра в пять", "INVALID_FORMAT"),
        ("07/10/2026 15:00", "INVALID_FORMAT"),
        ("2026-10-07 15:00", "INVALID_FORMAT"),
        ("31.02.2026 15:00", "INVALID_DATE"),
        ("07.10.2026 24:30", "INVALID_TIME"),
        ("07.10.2026 15:60", "INVALID_TIME"),
        ("07.10.2026 14:00", "PAST_DATETIME"),
        ("07.10.2026 14:59", "PAST_DATETIME"),
    ],
)
async def test_error_does_not_call_post_service_or_discard_photo(
    monkeypatch: pytest.MonkeyPatch, text: str, expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(dialog_service, "utc_now", lambda: FROZEN_NOW)
    store = Store()
    posts = Posts()
    service = dialog_service.DialogService(
        store,
        user_service=Users(),
        post_service=posts,  # type: ignore[arg-type]
    )
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, IMAGE)
    before = await store.get_draft(USER, CHAT)

    with caplog.at_level("INFO", logger=dialog_service.__name__):
        result = await service.receive_text(USER, CHAT, text)
    assert result.error_code == expected
    assert result.parsed_datetime is None
    assert result.state == DialogState.WAITING_DATETIME
    assert await store.get_draft(USER, CHAT) == before
    assert posts.created == []
    assert text not in caplog.text
    if expected == "PAST_DATETIME":
        assert "Past publication datetime rejected user_id=42" in caplog.text
    else:
        assert "Invalid publication datetime format user_id=42" in caplog.text


async def test_success_hands_utc_local_timezone_and_photo_to_next_service(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(dialog_service, "utc_now", lambda: FROZEN_NOW)
    store = Store()
    posts = Posts()
    service = dialog_service.DialogService(
        store,
        user_service=Users(),
        post_service=posts,  # type: ignore[arg-type]
    )
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, IMAGE)
    with caplog.at_level("INFO", logger=dialog_service.__name__):
        result = await service.receive_text(USER, CHAT, "07.10.2026 15:00")

    assert result.parsed_datetime is not None
    assert result.parsed_datetime.utc_datetime == datetime(2026, 10, 7, 12, tzinfo=UTC)
    assert result.parsed_datetime.local_datetime.utcoffset() == timedelta(hours=3)
    assert result.parsed_datetime.timezone == "Europe/Moscow"
    (post,) = posts.created  # сервис SPEC-009 пока возвращает DTO, не сохраняет запись
    assert post.photo_file_id == "image"
    assert post.scheduled_at == result.parsed_datetime.utc_datetime
    assert post.local_datetime == result.parsed_datetime.local_datetime
    assert post.timezone == "Europe/Moscow"
    assert "Publication datetime accepted user_id=42" in caplog.text
    assert "07.10.2026 15:00" not in caplog.text


async def test_telegram_error_then_correction_without_resending_photo(
    running_app: Application,
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # IT-005-01…03, E2E-005 ошибка → исправление; дата в примере проверяется при замороженном now.
    monkeypatch.setattr(dialog_service, "utc_now", lambda: FROZEN_NOW)
    fake_telegram.add_text_message("/start", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    fake_telegram.add_photo_message("image", chat_id=42, user_id=42)
    await wait_for_state(running_app, 42, DialogState.WAITING_DATETIME)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
    initial_draft = await draft_data(running_app, 42)
    before = db_snapshot(db_path(tmp_path))

    fake_telegram.add_text_message("завтра в пять", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 3)
    assert fake_telegram.sent_messages[2]["text"] == INVALID_FORMAT_TEXT
    await wait_for_state(running_app, 42, DialogState.WAITING_DATETIME)
    assert await draft_data(running_app, 42) == initial_draft
    assert db_snapshot(db_path(tmp_path)) == before

    fake_telegram.add_text_message("07.10.2026 14:00", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 4)
    assert fake_telegram.sent_messages[3]["text"] == PAST_DATETIME_TEXT
    assert await draft_data(running_app, 42) == initial_draft
    assert db_snapshot(db_path(tmp_path)) == before

    fake_telegram.add_text_message("07.10.2026 15:00", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 5)
    assert fake_telegram.sent_messages[4]["text"].startswith(
        "Публикация создана на 07.10.2026 15:00 (Europe/Moscow)"
    )
    await wait_for_state(running_app, 42, DialogState.IDLE)
    # Таблица posts и настоящая запись/подтверждение остаются задачей SPEC-008/009.
    assert db_snapshot(db_path(tmp_path)) == before


def test_user_messages_match_spec() -> None:
    spec = (Path(__file__).resolve().parents[1] / "docs/specs/SPEC-005.md").read_text(
        encoding="utf-8"
    )
    for heading, message in (
        ("# 26. Обработка неправильного формата", INVALID_FORMAT_TEXT),
        ("# 27. Обработка прошедшей даты", PAST_DATETIME_TEXT),
    ):
        fenced = spec.split(heading, 1)[1].split("```text\n", 1)[1].split("\n```", 1)[0]
        assert fenced == message


async def test_telegram_prefers_timezone_saved_in_user_profile(
    running_app: Application,
    fake_telegram: FakeTelegram,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # IT-005-01, BR-005-07: не только парсер, но и настоящий UserService читает профиль.
    from sqlalchemy import update

    from postmaster.database.models import UserModel

    monkeypatch.setattr(dialog_service, "utc_now", lambda: datetime(2026, 10, 7, 5, 59, tzinfo=UTC))
    fake_telegram.add_text_message("/start", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    async with running_app._engine.begin() as conn:
        await conn.execute(
            update(UserModel).where(UserModel.telegram_user_id == 42).values(timezone="Asia/Tokyo")
        )
    fake_telegram.add_photo_message("image", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    fake_telegram.add_text_message("07.10.2026 15:00", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 3)
    assert "15:00 (Asia/Tokyo)" in fake_telegram.sent_messages[2]["text"]
    await wait_for_state(running_app, 42, DialogState.IDLE)
