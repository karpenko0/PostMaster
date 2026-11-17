"""PM-06-R2-001/004/005/006: привязка callback к черновику и защита FSM.

Тесты воспроизводят дефекты ревизии 2: общие callback PM-05, отсутствие проверки
draft_id, перезапись активного FSM при start_reschedule, потеря черновика при
ошибках Telegram preview.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path, future_date_text
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.dialog import DialogState
from postmaster.domain.photo import PhotoData
from postmaster.domain.post import PublicationType
from postmaster.handlers.dialog import (
    PREVIEW_CAPTION_MAX,
    _build_confirmation_keyboard,
    _format_preview_text,
    _render_preview,
    reply_text,
)
from postmaster.handlers.inline_keyboards import (
    MAX_CALLBACK_LENGTH,
    build_confirmation_keyboard,
    new_draft_id,
    parse_confirmation_callback,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.dialog_service import (
    CAPTION_KEY,
    DRAFT_ID_KEY,
    PARSED_LOCAL_KEY,
    PARSED_TZ_KEY,
    PARSED_UTC_KEY,
    PHOTO_FILE_ID_KEY,
    PHOTO_UNIQUE_ID_KEY,
    PUBLICATION_TYPE_KEY,
    DialogOutcomeKind,
    DialogService,
)
from postmaster.services.post_service import PostService

USER_TG = 71001
CHAT = USER_TG


class MemoryStore:
    """Хранилище FSM в памяти для unit-тестов DialogService."""

    def __init__(self) -> None:
        self.states: dict[tuple[int, int], str] = {}
        self.drafts: dict[tuple[int, int], dict[str, str]] = {}

    async def get_state(self, user_id: int, chat_id: int) -> str | None:
        return self.states.get((user_id, chat_id))

    async def set_state(self, user_id: int, chat_id: int, state: str) -> None:
        self.states[(user_id, chat_id)] = state

    async def get_draft(self, user_id: int, chat_id: int) -> dict[str, str]:
        return dict(self.drafts.get((user_id, chat_id), {}))

    async def set_draft(self, user_id: int, chat_id: int, draft: dict[str, str]) -> None:
        self.drafts[(user_id, chat_id)] = dict(draft)

    async def save(self, user_id: int, chat_id: int, state: str, draft: dict[str, str]) -> None:
        self.states[(user_id, chat_id)] = state
        self.drafts[(user_id, chat_id)] = dict(draft)


class RecordingScheduler:
    def __init__(self) -> None:
        self.scheduled: list[tuple[int, datetime]] = []
        self.fail_schedule = False

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        if self.fail_schedule:
            raise RuntimeError("schedule failed")
        self.scheduled.append((post_id, run_at))

    def remove_job(self, post_id: int) -> bool:
        return True


class FakeUsers:
    def __init__(self, user_id: int = 1) -> None:
        self._user_id = user_id

    async def get_by_telegram_id(self, telegram_id: int):
        return type("U", (), {"id": self._user_id, "telegram_id": telegram_id})()

    async def timezone_of(self, telegram_id: int) -> str:
        return "UTC"


def _photo(caption: str | None = None) -> PhotoData:
    return PhotoData(
        file_id="file-1",
        file_unique_id="file-1-uid",
        file_size=1000,
        width=1280,
        height=720,
        caption=caption,
    )


@pytest.fixture
async def dialog_service(
    tmp_path: Path,
) -> AsyncIterator[tuple[DialogService, PostRepository, int]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        user = await user_repo.add(
            telegram_user_id=USER_TG, username=None, first_name="PM06", now=datetime.now(UTC)
        )
        repo = PostRepository(factory)
        service = DialogService(
            MemoryStore(),
            user_service=FakeUsers(user.id),  # type: ignore[arg-type]
            post_service=PostService(repo, RecordingScheduler()),  # type: ignore[arg-type]
        )
        yield service, repo, user.id
    finally:
        await engine.dispose()


async def _drive_to_confirmation(service: DialogService) -> str:
    await service.start(USER_TG, CHAT)
    await service.receive_photo(USER_TG, CHAT, _photo())
    await service.receive_text(USER_TG, CHAT, "🔔 Напомнить мне")
    await service.receive_text(USER_TG, CHAT, future_date_text(days=2))
    draft = await service._store.get_draft(USER_TG, CHAT)
    return draft[DRAFT_ID_KEY]


# === PM06-R2-001 ===========================================================


def test_001_callback_data_is_short_and_bound_to_draft() -> None:
    draft_id = new_draft_id()
    markup = build_confirmation_keyboard(draft_id)
    buttons = [button for row in markup.keyboard for button in row]
    assert len(buttons) == 2
    for button in buttons:
        assert button.callback_data is not None
        assert len(button.callback_data.encode("utf-8")) <= MAX_CALLBACK_LENGTH
        parsed = parse_confirmation_callback(button.callback_data)
        assert parsed is not None
        assert parsed[1] == draft_id
    actions = {parse_confirmation_callback(b.callback_data)[0] for b in buttons}
    assert actions == {"confirm", "cancel"}


@pytest.mark.parametrize(
    "data",
    ["pm05:confirm", "pm05:cancel", "pm05c:", "pm05c:NOPE", "", "pm05c:" + "z" * 80],
)
def test_001_malformed_callback_is_rejected(data: str) -> None:
    assert parse_confirmation_callback(data) is None


async def test_001_stale_callback_does_not_touch_new_draft(dialog_service) -> None:
    service, repo, user_id = dialog_service
    old_draft_id = await _drive_to_confirmation(service)
    await service.start(USER_TG, CHAT)
    new_draft_id = await _drive_to_confirmation(service)
    assert old_draft_id != new_draft_id

    stale = await service.confirm_post(USER_TG, CHAT, draft_id=old_draft_id)
    assert stale.kind == DialogOutcomeKind.STALE_CALLBACK
    assert await repo.list_pending_by_user(user_id) == []
    assert await service._store.get_draft(USER_TG, CHAT) is not None
    assert (await service._store.get_draft(USER_TG, CHAT))[DRAFT_ID_KEY] == new_draft_id

    fresh = await service.confirm_post(USER_TG, CHAT, draft_id=new_draft_id)
    assert fresh.kind == DialogOutcomeKind.POST_CREATED
    assert len(await repo.list_pending_by_user(user_id)) == 1


async def test_001_repeated_and_parallel_callbacks_create_one_post(dialog_service) -> None:
    service, repo, user_id = dialog_service
    draft_id = await _drive_to_confirmation(service)

    results = await asyncio.gather(
        *[service.confirm_post(USER_TG, CHAT, draft_id=draft_id) for _ in range(6)]
    )
    assert all(r.kind == DialogOutcomeKind.POST_CREATED for r in results)
    assert len(await repo.list_pending_by_user(user_id)) == 1
    again = await service.confirm_post(USER_TG, CHAT, draft_id=draft_id)
    assert again.kind == DialogOutcomeKind.POST_CREATED
    assert len(await repo.list_pending_by_user(user_id)) == 1


@pytest.mark.parametrize("bad", ["", "not-an-id", "0" * 32, "pm05c:zz"])
async def test_001_invalid_draft_id_never_creates_post(dialog_service, bad) -> None:
    service, repo, user_id = dialog_service
    await _drive_to_confirmation(service)
    outcome = await service.confirm_post(USER_TG, CHAT, draft_id=bad)
    assert outcome.kind in (DialogOutcomeKind.STALE_CALLBACK, DialogOutcomeKind.INVALID_MESSAGE)
    assert await repo.list_pending_by_user(user_id) == []


async def test_001_stale_cancel_does_not_clear_active_draft(dialog_service) -> None:
    service, _repo, _user_id = dialog_service
    old_draft_id = await _drive_to_confirmation(service)
    await service.start(USER_TG, CHAT)
    new_draft_id = await _drive_to_confirmation(service)

    outcome = await service.cancel_creation(USER_TG, CHAT, draft_id=old_draft_id)
    assert outcome.kind == DialogOutcomeKind.STALE_CALLBACK
    draft = await service._store.get_draft(USER_TG, CHAT)
    assert draft[DRAFT_ID_KEY] == new_draft_id
    assert await service._current_state(USER_TG, CHAT) == DialogState.WAITING_CONFIRMATION


def test_006_stale_callback_has_distinct_user_message(dialog_service) -> None:
    from postmaster.services.dialog_service import DialogOutcome

    stale = DialogOutcome(DialogOutcomeKind.STALE_CALLBACK, DialogState.WAITING_CONFIRMATION)
    ok = DialogOutcome(DialogOutcomeKind.POST_CREATED, DialogState.IDLE)
    assert "устарел" in reply_text(stale).lower()
    assert reply_text(stale) != reply_text(ok)


# === PM06-R2-004 ===========================================================


def test_004_preview_uses_plain_text_and_truncates_caption() -> None:
    special = "*жирный* _курсив_ [ссылка] `код` 😀🎯 <b>тег</b>"
    text = _format_preview_text(
        publication_type=PublicationType.REMINDER.value,
        target_id=None,
        local_date="12.11.2026",
        local_time="14:30",
        timezone="Europe/Moscow",
        caption=special,
    )
    # Markdown-разметка бота не применяется к пользовательскому тексту.
    assert "**" not in text
    assert special[:10] in text

    huge = "😀" * 3000
    preview = _render_preview(
        {CAPTION_KEY: huge, PUBLICATION_TYPE_KEY: "REMINDER"},
        datetime.now(UTC),
        "UTC",
    )
    assert len(preview.encode("utf-16-le")) // 2 <= PREVIEW_CAPTION_MAX + 100
    assert len(preview) <= 1024


async def test_004_expired_datetime_is_rejected_on_confirm(dialog_service, monkeypatch) -> None:
    from postmaster.services import dialog_service as ds_module

    service, repo, user_id = dialog_service
    draft_id = await _drive_to_confirmation(service)
    draft = await service._store.get_draft(USER_TG, CHAT)
    past = datetime.now(UTC) - timedelta(hours=1)
    draft[PARSED_UTC_KEY] = past.isoformat()
    draft[PARSED_LOCAL_KEY] = past.isoformat()
    draft[PARSED_TZ_KEY] = "UTC"
    draft[PHOTO_FILE_ID_KEY] = "file-1"
    draft[PHOTO_UNIQUE_ID_KEY] = "file-1-uid"
    await service._store.set_draft(USER_TG, CHAT, draft)
    monkeypatch.setattr(ds_module, "utc_now", lambda: datetime.now(UTC))

    outcome = await service.confirm_post(USER_TG, CHAT, draft_id=draft_id)
    assert outcome.kind == DialogOutcomeKind.DATETIME_EXPIRED
    assert await repo.list_pending_by_user(user_id) == []
    assert await service._current_state(USER_TG, CHAT) == DialogState.WAITING_CONFIRMATION


# === PM06-R2-005 ===========================================================


async def test_005_start_reschedule_does_not_overwrite_active_fsm(dialog_service) -> None:
    service, _repo, _user_id = dialog_service
    draft_id = await _drive_to_confirmation(service)
    before = await service._store.get_draft(USER_TG, CHAT)

    outcome = await service.start_reschedule(USER_TG, CHAT, 123)
    assert outcome.kind == DialogOutcomeKind.FSM_CONFLICT
    assert await service._store.get_draft(USER_TG, CHAT) == before
    assert await service._current_state(USER_TG, CHAT) == DialogState.WAITING_CONFIRMATION
    assert before[DRAFT_ID_KEY] == draft_id


async def test_005_old_reschedule_button_keeps_operation_id() -> None:
    """Старый callback переноса не меняет черновик активной операции."""
    store = MemoryStore()
    scheduler = RecordingScheduler()
    service = DialogService(
        store,
        user_service=FakeUsers(),  # type: ignore[arg-type]
        post_service=PostService(None, scheduler),  # type: ignore[arg-type]
    )
    first = await service.start_reschedule(USER_TG, CHAT, 1)
    assert first.kind == DialogOutcomeKind.RESCHEDULE_PROMPT
    draft_before = await store.get_draft(USER_TG, CHAT)

    second = await service.start_reschedule(USER_TG, CHAT, 2)
    assert second.kind == DialogOutcomeKind.FSM_CONFLICT
    assert await store.get_draft(USER_TG, CHAT) == draft_before


async def test_005_reschedule_checks_owner_and_status(tmp_path: Path) -> None:
    from postmaster.services.dialog_service import RESCHEDULE_OPERATION_ID_KEY

    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        repo = PostRepository(factory)
        users = UserRepository(factory)
        owner = await users.add(
            telegram_user_id=72001, username=None, first_name="Owner", now=datetime.now(UTC)
        )
        other = await users.add(
            telegram_user_id=72002, username=None, first_name="Other", now=datetime.now(UTC)
        )
        post = await repo.create_post(
            user_id=owner.id,
            telegram_file_id="f",
            telegram_file_unique_id="u",
            scheduled_at_utc=datetime.now(UTC) + timedelta(days=1),
            timezone="UTC",
            publication_type=PublicationType.REMINDER,
        )
        service = DialogService(
            MemoryStore(),
            user_service=FakeUsers(other.id),  # type: ignore[arg-type]
            post_service=PostService(repo, RecordingScheduler()),  # type: ignore[arg-type]
            post_repository=repo,
        )
        outcome = await service.start_reschedule(72002, 72002, post.id)
        assert outcome.kind == DialogOutcomeKind.RESCHEDULE_REJECTED
        draft = await service._store.get_draft(72002, 72002)
        assert RESCHEDULE_OPERATION_ID_KEY not in draft
    finally:
        await engine.dispose()


def test_legacy_keyboard_remains_importable() -> None:
    """PM-05 legacy-контракт клавиатуры сохранён для обратной совместимости."""
    markup = _build_confirmation_keyboard()
    buttons = [button for row in markup.keyboard for button in row]
    assert {button.callback_data for button in buttons} == {"pm05:confirm", "pm05:cancel"}
