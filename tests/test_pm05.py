"""PM-05: предварительный просмотр и подтверждение публикации.

Покрывает:
- PM05-T01/T02: формирование preview REMINDER / TELEGRAM
- PM05-T03: preview без caption
- PM05-T04: preview с длинным caption (сокращение с многоточием)
- PM05-T05: до подтверждения Post отсутствует
- PM05-T06: Scheduler job не регистрируется до подтверждения
- PM05-T07/T08: подтверждение REMINDER / TELEGRAM
- PM05-T09: повторный confirm — нет дубликата
- PM05-T11: cancel очищает draft и FSM
- PM05-T12: confirm после cancel — Post не создаётся
- PM05-T15: ошибка Scheduler — Post сохранён
- PM05-T16: параметры Post == параметрам preview
- PM05-T17: FSM очищается после подтверждения/отмены
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
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
    CANCEL_BUTTON_TEXT,
    CANCEL_CALLBACK_DATA,
    CONFIRM_BUTTON_TEXT,
    CONFIRM_CALLBACK_DATA,
    _format_preview_text,
    _truncate_caption,
)
from postmaster.handlers.inline_keyboards import build_post_actions_keyboard
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.dialog_service import (
    CAPTION_KEY,
    DRAFT_ID_KEY,
    PARSED_TZ_KEY,
    PARSED_UTC_KEY,
    PHOTO_FILE_ID_KEY,
    REMINDER_BUTTON,
    TARGET_ID_KEY,
    TELEGRAM_BUTTON,
    DialogOutcomeKind,
    DialogService,
)
from postmaster.services.post_service import PostService
from postmaster.services.user_service import UserService


class RecordingScheduler:
    """Заглушка Scheduler: записывает вызовы schedule_post."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, datetime]] = []
        self.fail_schedule: bool = False

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        if self.fail_schedule:
            raise RuntimeError("schedule failed")
        self.scheduled.append((post_id, run_at))

    def remove_job(self, post_id: int) -> bool:
        return True


class InMemoryStore:
    """Упрощённое хранилище состояний и черновиков для тестов DialogService."""

    def __init__(self) -> None:
        self._states: dict[tuple[int, int], str] = {}
        self._drafts: dict[tuple[int, int], dict[str, str]] = {}

    async def get_state(self, user_id: int, chat_id: int) -> str | None:
        return self._states.get((user_id, chat_id))

    async def set_state(self, user_id: int, chat_id: int, state: str) -> None:
        if (user_id, chat_id) not in self._drafts:
            self._drafts.setdefault((user_id, chat_id), {})
        self._states[(user_id, chat_id)] = state

    async def get_draft(self, user_id: int, chat_id: int) -> dict[str, str]:
        return dict(self._drafts.get((user_id, chat_id), {}))

    async def set_draft(self, user_id: int, chat_id: int, draft: dict[str, str]) -> None:
        if (user_id, chat_id) not in self._states:
            # Зеркалим поведение telebot-реализации: данные требуют состояние.
            raise RuntimeError("данные нельзя записать без состояния")
        self._drafts[(user_id, chat_id)] = dict(draft)


class FakeUsers:
    """UserService-фейк для тестов: возвращает фиксированного пользователя."""

    def __init__(self, user_id: int = 1) -> None:
        self._user_id = user_id

    async def get_by_telegram_id(self, telegram_id: int):  # noqa: ANN001
        return SimpleUser(self._user_id, telegram_id)

    async def timezone_of(self, telegram_id: int) -> str:  # noqa: ARG002
        return "UTC"


class SimpleUser:
    def __init__(self, id: int, telegram_id: int) -> None:  # noqa: A002
        self.id = id
        self.telegram_id = telegram_id
        self.username = None
        self.first_name = "Test"


def _photo(file_id: str = "file-1", caption: str | None = None) -> PhotoData:
    return PhotoData(
        file_id=file_id,
        file_unique_id=f"{file_id}-uid",
        file_size=1000,
        width=1280,
        height=720,
        caption=caption,
    )


# === preview rendering (PM05-T01..T04) ======================================


def test_pm05_t01_preview_reminder_omits_channel() -> None:
    """PM05-T01: preview REMINDER содержит основные поля, но без канала."""
    text = _format_preview_text(
        publication_type=PublicationType.REMINDER.value,
        target_id=None,
        local_date="12.11.2026",
        local_time="14:30",
        timezone="Europe/Moscow",
        caption="Новая публикация",
    )
    assert "🔔 Напоминание" in text
    assert "12.11.2026" in text
    assert "14:30" in text
    assert "Europe/Moscow" in text
    assert "Новая публикация" in text
    assert "Канал" not in text


def test_pm05_t02_preview_telegram_shows_channel() -> None:
    """PM05-T02: preview TELEGRAM отображает канал."""
    text = _format_preview_text(
        publication_type=PublicationType.TELEGRAM.value,
        target_id="@my_channel",
        local_date="12.11.2026",
        local_time="14:30",
        timezone="Europe/Moscow",
        caption="Новая публикация",
    )
    assert "📢 Публикация в Telegram" in text
    assert "Канал: @my_channel" in text


def test_pm05_t03_preview_without_caption_omits_caption_line() -> None:
    """PM05-T03: preview без caption не содержит строки подписи."""
    text = _format_preview_text(
        publication_type=PublicationType.REMINDER.value,
        target_id=None,
        local_date="12.11.2026",
        local_time="14:30",
        timezone="UTC",
        caption=None,
    )
    assert "Подпись" not in text


def test_pm05_t04_preview_long_caption_is_truncated() -> None:
    """PM05-T04: длинная подпись сокращается с многоточием в превью."""
    long_caption = "очень " + "длинный " * 30 + "текст"
    truncated = _truncate_caption(long_caption)
    assert truncated.endswith("…")
    assert len(truncated) <= 80
    # Полный текст сохраняется в draft — обрезка только для превью.
    assert long_caption.startswith(truncated.rstrip("…").rstrip())


# === keyboard (PM05-T05/T06/T07) ============================================


def test_pm05_confirmation_keyboard_has_both_buttons() -> None:
    """Клавиатура PM-05 содержит обе кнопки: Подтвердить и Отменить."""
    # Импортируем лениво, чтобы не зависеть от внутреннего _build_confirmation_keyboard.
    from postmaster.handlers.dialog import _build_confirmation_keyboard

    markup = _build_confirmation_keyboard()
    flat = [btn for row in markup.keyboard for btn in row]
    texts = sorted(btn.text for btn in flat)
    assert texts == sorted([CONFIRM_BUTTON_TEXT, CANCEL_BUTTON_TEXT])
    callback_data = sorted(btn.callback_data for btn in flat)
    assert callback_data == sorted([CONFIRM_CALLBACK_DATA, CANCEL_CALLBACK_DATA])


def test_pm05_does_not_break_existing_post_actions_keyboard() -> None:
    """PM-05 не должен влиять на клавиатуру PM-04 (cancel/resched)."""
    markup = build_post_actions_keyboard(post_id=7)
    flat = [btn for row in markup.keyboard for btn in row]
    callback_data = sorted(btn.callback_data for btn in flat)
    assert callback_data == ["cancel:7", "resched:7"]


# === service-level behaviour (PM05-T05..T17) ================================


@pytest.fixture
async def dialog_service(
    tmp_path: Path,
) -> AsyncIterator[tuple[DialogService, RecordingScheduler, PostRepository, int]]:
    """Поднимает DialogService с in-memory store и реальной БД."""
    store = InMemoryStore()
    scheduler = RecordingScheduler()
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        user_service = UserService(user_repo, default_timezone="UTC")
        user = await user_service.get_or_create(
            telegram_user_id=9001, username=None, first_name="PM05"
        )
        post_repo = PostRepository(factory)
        post_service = PostService(post_repo, scheduler)  # type: ignore[arg-type]
        fake_users = FakeUsers(user_id=user.id)
        service = DialogService(
            store,  # type: ignore[arg-type]
            user_service=fake_users,  # type: ignore[arg-type]
            post_service=post_service,
        )
        yield service, scheduler, post_repo, user.id
    finally:
        await engine.dispose()


async def _drive_to_confirmation(
    service: DialogService,
    *,
    telegram_user_id: int = 9001,
    publication_type: str = PublicationType.REMINDER.value,
    target_id: str | None = None,
    caption: str | None = None,
) -> None:
    """Прогоняет сценарий от /start до состояния WAITING_CONFIRMATION."""
    chat = telegram_user_id  # для простоты личного чата
    await service.start(telegram_user_id, chat)
    await service.receive_photo(telegram_user_id, chat, _photo(caption=caption))
    if publication_type == PublicationType.REMINDER.value:
        await service.receive_text(telegram_user_id, chat, REMINDER_BUTTON)
    else:
        await service.receive_text(telegram_user_id, chat, TELEGRAM_BUTTON)
        await service.receive_text(telegram_user_id, chat, target_id or "@channel")
    await service.receive_text(telegram_user_id, chat, future_date_text(days=2))


async def test_pm05_t05_no_post_before_confirmation(dialog_service) -> None:
    """PM05-T05: до подтверждения запись Post отсутствует в БД."""
    service, scheduler, post_repo, _user_id = dialog_service
    chat = 9001
    await _drive_to_confirmation(service)

    posts = await post_repo.list_pending_by_user(_user_id)
    assert posts == []
    assert scheduler.scheduled == []
    state = await service._current_state(9001, chat)
    assert state == DialogState.WAITING_CONFIRMATION


async def test_pm05_t07_confirmation_reminder_creates_post(dialog_service) -> None:
    """PM05-T07: подтверждение REMINDER создаёт один Post и регистрирует Scheduler."""
    service, scheduler, post_repo, _user_id = dialog_service
    await _drive_to_confirmation(
        service, publication_type=PublicationType.REMINDER.value, caption="hello"
    )
    chat = 9001

    outcome = await service.confirm_post(9001, chat)

    assert outcome.kind == DialogOutcomeKind.POST_CREATED
    assert outcome.post is not None
    assert outcome.post.publication_type == PublicationType.REMINDER
    assert outcome.post.caption == "hello"

    posts = await post_repo.list_pending_by_user(_user_id)
    assert len(posts) == 1
    assert len(scheduler.scheduled) == 1
    scheduled_pid, _ = scheduler.scheduled[0]
    assert scheduled_pid == posts[0].id


async def test_pm05_t08_confirmation_telegram_creates_post(dialog_service) -> None:
    """PM05-T08: подтверждение TELEGRAM создаёт Post с target_id."""
    service, scheduler, post_repo, _user_id = dialog_service
    await _drive_to_confirmation(
        service,
        publication_type=PublicationType.TELEGRAM.value,
        target_id="@my_channel",
    )
    chat = 9001

    outcome = await service.confirm_post(9001, chat)

    assert outcome.kind == DialogOutcomeKind.POST_CREATED
    assert outcome.post is not None
    assert outcome.post.publication_type == PublicationType.TELEGRAM
    assert outcome.post.target_id == "@my_channel"
    assert scheduler.scheduled and scheduler.scheduled[0][0] == outcome.post.id


async def test_pm05_t09_duplicate_confirm_does_not_create_extra_post(dialog_service) -> None:
    """PM05-T09: повторный callback confirm не создаёт дубль."""
    service, scheduler, post_repo, _user_id = dialog_service
    await _drive_to_confirmation(service)
    chat = 9001

    first = await service.confirm_post(9001, chat)
    second = await service.confirm_post(9001, chat)
    third = await service.confirm_post(9001, chat)

    assert first.kind == DialogOutcomeKind.POST_CREATED
    assert second.kind == DialogOutcomeKind.POST_CREATED
    assert third.kind == DialogOutcomeKind.POST_CREATED
    # Все три ответа ссылаются на один и тот же пост.
    assert second.post is None  # повтор: post не возвращается, есть только время
    posts = await post_repo.list_pending_by_user(_user_id)
    assert len(posts) == 1
    assert len(scheduler.scheduled) == 1


async def test_pm05_t11_cancel_clears_draft_and_fsm(dialog_service) -> None:
    """PM05-T11: cancel очищает draft и FSM (PM05-BR-09..BR-12)."""
    service, scheduler, post_repo, _user_id = dialog_service
    await _drive_to_confirmation(service)
    chat = 9001
    draft = await service._store.get_draft(9001, chat)
    assert DRAFT_ID_KEY in draft  # черновик существует до отмены

    outcome = await service.cancel_creation(9001, chat)

    assert outcome.kind == DialogOutcomeKind.CONFIRMATION_CANCELLED
    assert await service._current_state(9001, chat) == DialogState.IDLE
    assert await service._store.get_draft(9001, chat) == {}
    posts = await post_repo.list_pending_by_user(_user_id)
    assert posts == []
    assert scheduler.scheduled == []


async def test_pm05_t12_confirm_after_cancel_creates_no_post(dialog_service) -> None:
    """PM05-T12: confirm после cancel не создаёт Post (черновик очищен)."""
    service, _scheduler, post_repo, _user_id = dialog_service
    await _drive_to_confirmation(service)
    chat = 9001
    await service.cancel_creation(9001, chat)

    outcome = await service.confirm_post(9001, chat)

    assert outcome.kind in (
        DialogOutcomeKind.INVALID_MESSAGE,
        DialogOutcomeKind.POST_FAILED,
    )
    posts = await post_repo.list_pending_by_user(_user_id)
    assert posts == []


async def test_pm05_t15_scheduler_failure_keeps_post(dialog_service) -> None:
    """PM05-T15: ошибка Scheduler оставляет Post в БД (Recovery восстановит)."""
    service, scheduler, post_repo, _user_id = dialog_service
    scheduler.fail_schedule = True
    await _drive_to_confirmation(service)
    chat = 9001

    # Подменяем PostService, чтобы он выбрасывал SchedulerRegistrationError
    # при сбое Scheduler (PostService уже делает это самостоятельно).
    outcome = await service.confirm_post(9001, chat)

    assert outcome.kind == DialogOutcomeKind.POST_SAVED_UNSCHEDULED
    assert outcome.post is not None
    posts = await post_repo.list_pending_by_user(_user_id)
    assert len(posts) == 1
    # Scheduler не зарегистрировал задачу, но запись существует.
    assert scheduler.scheduled == []


async def test_pm05_t16_post_parameters_match_preview(dialog_service) -> None:
    """PM05-T16: параметры созданного Post совпадают с preview (PM05-BR-08)."""
    service, _scheduler, post_repo, _user_id = dialog_service
    await _drive_to_confirmation(
        service,
        publication_type=PublicationType.TELEGRAM.value,
        target_id="@channel",
        caption="полный текст подписи",
    )
    chat = 9001
    draft = await service._store.get_draft(9001, chat)

    outcome = await service.confirm_post(9001, chat)
    assert outcome.kind == DialogOutcomeKind.POST_CREATED
    assert outcome.post is not None

    # Все ключевые поля preview соответствуют Post.
    assert outcome.post.publication_type == PublicationType.TELEGRAM
    assert outcome.post.target_id == draft.get(TARGET_ID_KEY)
    assert outcome.post.caption == draft.get(CAPTION_KEY)
    assert outcome.post.timezone == draft.get(PARSED_TZ_KEY)
    assert outcome.post.scheduled_at_utc.isoformat() == draft.get(PARSED_UTC_KEY)
    assert outcome.post.telegram_file_id == draft.get(PHOTO_FILE_ID_KEY)


async def test_pm05_t17_fsm_cleared_after_success(dialog_service) -> None:
    """PM05-T17: после подтверждения FSM переходит в IDLE."""
    service, _scheduler, _post_repo, _user_id = dialog_service
    await _drive_to_confirmation(service)
    chat = 9001

    await service.confirm_post(9001, chat)

    assert await service._current_state(9001, chat) == DialogState.IDLE


async def test_pm05_invalid_message_outside_confirmation(dialog_service) -> None:
    """PM05-T10/BR-05: confirm вне состояния WAITING_CONFIRMATION возвращает INVALID_MESSAGE."""
    service, _scheduler, post_repo, _user_id = dialog_service
    chat = 9001
    # Сценарий не запущен — state == IDLE.
    outcome = await service.confirm_post(9001, chat)
    assert outcome.kind == DialogOutcomeKind.INVALID_MESSAGE
    posts = await post_repo.list_pending_by_user(_user_id)
    assert posts == []


async def test_pm05_caption_preserved_in_draft_when_truncated(dialog_service) -> None:
    """PM05-T04 (продолжение): в draft сохраняется полный caption, не обрезанный."""
    service, _scheduler, _post_repo, _user_id = dialog_service
    long_caption = "А" * 200
    await _drive_to_confirmation(service, caption=long_caption)
    chat = 9001
    draft = await service._store.get_draft(9001, chat)
    assert draft.get(CAPTION_KEY) == long_caption
