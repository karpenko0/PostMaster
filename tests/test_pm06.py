"""PM-06: стабилизация и исправление доработок PM-03/04/05.

Регрессионные тесты к дефектам BUG-01…BUG-07:

- BUG-01: устаревшие inline-кнопки не подтверждают и не отменяют другой черновик.
- BUG-02: один draft_id — не более одного Post (идемпотентность, гонки, рестарт).
- BUG-03: preview устойчив к пользовательскому тексту и ошибкам Telegram API.
- BUG-04: устаревшее задание Scheduler не публикует пост не вовремя.
- BUG-05: перенос не уничтожает активный черновик.
- BUG-06: клавиатура удаляется только после успешного завершения операции.
- BUG-07: FSM и черновики переживают перезапуск, TTL очищает устаревшие.

Тесты не обращаются к сети и к реальному Telegram: используются fake_telegram и
локальные двойники сервисов.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from harness import db_path, future_date_text, wait_for_state, wait_until
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.migrations import (
    MIGRATION_POSTS_DRAFT_ID,
    apply_migrations,
    rollback_migrations,
    schema_version,
)
from postmaster.database.schema import create_schema
from postmaster.domain.dialog import DialogState, token_matches
from postmaster.domain.photo import PhotoData
from postmaster.domain.post import PublicationType
from postmaster.handlers.dialog import (
    CALLBACK_DATA_MAX,
    _build_confirmation_keyboard,
    _render_preview_for_caption,
    build_confirmation_callback_data,
    escape_markdown,
    parse_confirmation_callback,
)
from postmaster.repositories.dialog_state_repository import DialogStateRepository
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.state_storage import SqlAlchemyStateStorage
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.dialog_service import (
    DRAFT_ID_KEY,
    PARSED_TZ_KEY,
    PARSED_UTC_KEY,
    PHOTO_FILE_ID_KEY,
    PHOTO_UNIQUE_ID_KEY,
    REMINDER_BUTTON,
    TARGET_ID_KEY,
    DialogOutcomeKind,
    DialogService,
)
from postmaster.services.post_service import PostService
from postmaster.services.reschedule_service import PostNotPendingError, RescheduleService
from postmaster.services.scheduler_service import SchedulerService
from postmaster.services.user_service import UserService

USER = 9001
CHAT = 9001


# === двойники ==============================================================


class RecordingScheduler:
    """Scheduler-двойник: запоминает вызовы и умеет имитировать сбой."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, datetime]] = []
        self.fail_times = 0

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("scheduler unavailable")
        self.scheduled.append((post_id, run_at))

    def remove_job(self, post_id: int) -> bool:
        return True


class FakeUsers:
    """UserService-двойник: один пользователь с поясом UTC."""

    def __init__(self, user_id: int = 1) -> None:
        self.user_id = user_id

    async def get_by_telegram_id(self, telegram_id: int):  # noqa: ANN001, ARG002
        from types import SimpleNamespace

        return SimpleNamespace(id=self.user_id, telegram_id=self.user_id)

    async def timezone_of(self, telegram_id: int) -> str:  # noqa: ARG002
        return "UTC"


class FailingOnSecondCallPostService(PostService):
    """Падение после успешного commit: имитирует сбой между записью Post и FSM."""

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        super().__init__(*args, **kwargs)
        self.calls = 0
        self.crash_after_commit = True

    async def create_scheduled_post(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        post = await super().create_scheduled_post(**kwargs)
        if self.crash_after_commit:
            self.crash_after_commit = False
            # Post уже в БД, но вызывающий код «падает» до записи FSM.
            raise RuntimeError("процесс упал между commit и обновлением FSM")
        return post


def _photo(file_id: str = "file-1", caption: str | None = None) -> PhotoData:
    return PhotoData(
        file_id=file_id,
        file_unique_id=f"{file_id}-uid",
        file_size=150000,
        width=1280,
        height=960,
        caption=caption,
    )


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator:
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def env(engine) -> AsyncIterator[dict]:
    """DialogService с персистентным FSM-хранилищем, реальной БД и двойником Scheduler."""
    factory = create_session_factory(engine)
    user_service = UserService(UserRepository(factory), default_timezone="UTC")
    user = await user_service.get_or_create(telegram_user_id=USER, username=None, first_name="PM06")
    scheduler = RecordingScheduler()
    post_repo = PostRepository(factory)
    store = DialogStateRepository(factory)
    service = DialogService(
        store,
        user_service=FakeUsers(user.id),  # type: ignore[arg-type]
        post_service=PostService(post_repo, scheduler),  # type: ignore[arg-type]
        reschedule_service=RescheduleService(post_repo, scheduler),
    )
    yield {
        "service": service,
        "scheduler": scheduler,
        "repo": post_repo,
        "store": store,
        "factory": factory,
        "user": user,
        "engine": engine,
    }


async def _to_confirmation(service: DialogService, *, caption: str | None = None) -> str:
    """Проходит сценарий до WAITING_CONFIRMATION. Возвращает draft_id черновика."""
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, _photo(caption=caption))
    await service.receive_text(USER, CHAT, REMINDER_BUTTON)
    outcome = await service.receive_text(USER, CHAT, future_date_text(days=2))
    assert outcome.kind == DialogOutcomeKind.PREVIEW_SHOWN
    assert outcome.draft_id is not None
    return outcome.draft_id


# === BUG-01: устаревшие callback ==========================================


async def test_bug01_current_draft_confirm_creates_post(env) -> None:
    """Подтверждение актуального черновика создаёт ровно один Post."""
    service = env["service"]
    draft_id = await _to_confirmation(service)

    outcome = await service.confirm_post(USER, CHAT, draft_id=draft_id)

    assert outcome.kind == DialogOutcomeKind.POST_CREATED
    assert len(await env["repo"].list_pending_by_user(env["user"].id)) == 1


async def test_bug01_confirm_after_start_is_rejected(env) -> None:
    """Подтверждение старого черновика после /start отклоняется, новый черновик жив."""
    service = env["service"]
    old_draft = await _to_confirmation(service)

    await service.start(USER, CHAT)  # пользователь начал новый сценарий
    outcome = await service.confirm_post(USER, CHAT, draft_id=old_draft)

    assert outcome.kind == DialogOutcomeKind.STALE_CALLBACK
    assert await service._current_state(USER, CHAT) == DialogState.WAITING_PHOTO
    new_draft = await env["store"].get_draft(USER, CHAT)
    assert new_draft.get(DRAFT_ID_KEY) != old_draft
    assert await env["repo"].list_pending_by_user(env["user"].id) == []


async def test_bug01_cancel_after_start_does_not_destroy_active_draft(env) -> None:
    """Отмена старого черновика не удаляет активный черновик нового сценария."""
    service = env["service"]
    old_draft = await _to_confirmation(service)
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, _photo("file-2"))

    outcome = await service.cancel_creation(USER, CHAT, draft_id=old_draft)

    assert outcome.kind == DialogOutcomeKind.STALE_CALLBACK
    assert await service._current_state(USER, CHAT) == DialogState.WAITING_PUBLICATION_TYPE
    assert (await env["store"].get_draft(USER, CHAT))[PHOTO_FILE_ID_KEY] == "file-2"


async def test_bug01_two_open_previews_old_one_is_stale(env) -> None:
    """Два открытых preview: подтверждение работает только у последнего."""
    service = env["service"]
    first_draft = await _to_confirmation(service)
    # Пользователь снова проходит сценарий, не закрывая первое превью.
    second_draft = await _to_confirmation(service)
    assert first_draft != second_draft

    stale = await service.confirm_post(USER, CHAT, draft_id=first_draft)
    fresh = await service.confirm_post(USER, CHAT, draft_id=second_draft)

    assert stale.kind == DialogOutcomeKind.STALE_CALLBACK
    assert fresh.kind == DialogOutcomeKind.POST_CREATED
    assert len(await env["repo"].list_pending_by_user(env["user"].id)) == 1


async def test_bug01_repeated_confirm_is_idempotent(env) -> None:
    """Повторное нажатие «Подтвердить» не создаёт второй Post."""
    service = env["service"]
    draft_id = await _to_confirmation(service)

    first = await service.confirm_post(USER, CHAT, draft_id=draft_id)
    second = await service.confirm_post(USER, CHAT, draft_id=draft_id)
    third = await service.confirm_post(USER, CHAT)  # callback без draft_id

    assert first.kind == DialogOutcomeKind.POST_CREATED
    assert second.kind == DialogOutcomeKind.POST_CREATED
    assert third.kind == DialogOutcomeKind.POST_CREATED
    assert len(await env["repo"].list_pending_by_user(env["user"].id)) == 1


async def test_bug01_unknown_and_malformed_callback_data(env) -> None:
    """Неизвестный и испорченный callback_data не приводят к операции."""
    assert parse_confirmation_callback("") is None
    assert parse_confirmation_callback(None) is None
    assert parse_confirmation_callback("pm05:unknown:abc") is None
    assert parse_confirmation_callback("pm04:confirm") is None
    assert parse_confirmation_callback("pm05:confirm:" + "x" * 40) is None
    # Корректные варианты.
    assert parse_confirmation_callback("pm05:confirm") == ("confirm", None)
    assert parse_confirmation_callback("pm05:cancel:abc123") == ("cancel", "abc123")


async def test_bug01_invalid_draft_id_is_rejected(env) -> None:
    """Некорректный (чужой) draft_id не подтверждает и не отменяет черновик."""
    service = env["service"]
    await _to_confirmation(service)

    confirm = await service.confirm_post(USER, CHAT, draft_id="not-a-real-draft")
    cancel = await service.cancel_creation(USER, CHAT, draft_id="not-a-real-draft")

    assert confirm.kind == DialogOutcomeKind.STALE_CALLBACK
    assert cancel.kind == DialogOutcomeKind.STALE_CALLBACK
    assert await service._current_state(USER, CHAT) == DialogState.WAITING_CONFIRMATION
    assert await env["repo"].list_pending_by_user(env["user"].id) == []


async def test_bug01_callback_data_fits_telegram_limit(env) -> None:
    """callback_data укладывается в 64 байта и однозначно определяет черновик."""
    draft_id = "6f1e0c1a-9c3d-4c8e-8f0a-1b2c3d4e5f60"
    confirm = build_confirmation_callback_data("confirm", draft_id)
    cancel = build_confirmation_callback_data("cancel", draft_id)

    assert len(confirm.encode()) <= CALLBACK_DATA_MAX
    assert len(cancel.encode()) <= CALLBACK_DATA_MAX
    assert "confirm" in confirm and "cancel" in cancel
    assert token_matches(draft_id, confirm.rsplit(":", 1)[1])
    assert not token_matches("00000000-0000-0000-0000-000000000000", confirm.rsplit(":", 1)[1])

    keyboard = _build_confirmation_keyboard(draft_id)
    datas = [button.callback_data for row in keyboard.keyboard for button in row]
    assert all(len(data.encode()) <= CALLBACK_DATA_MAX for data in datas)
    assert all(data.startswith("pm05:") for data in datas)


async def test_bug01_cancel_of_active_draft_clears_it(env) -> None:
    """Отмена актуального черновика завершает сценарий и очищает данные."""
    service = env["service"]
    draft_id = await _to_confirmation(service)

    outcome = await service.cancel_creation(USER, CHAT, draft_id=draft_id)

    assert outcome.kind == DialogOutcomeKind.CONFIRMATION_CANCELLED
    assert await env["store"].get_draft(USER, CHAT) == {}
    assert await service._current_state(USER, CHAT) == DialogState.IDLE


# === BUG-02: идемпотентность ==============================================


async def test_bug02_parallel_confirms_create_single_post(env) -> None:
    """Два параллельных подтверждения одного draft_id создают один Post."""
    service = env["service"]
    draft_id = await _to_confirmation(service)

    results = await asyncio.gather(
        service.confirm_post(USER, CHAT, draft_id=draft_id),
        service.confirm_post(USER, CHAT, draft_id=draft_id),
    )

    assert all(result.kind == DialogOutcomeKind.POST_CREATED for result in results)
    posts = await env["repo"].list_pending_by_user(env["user"].id)
    assert len(posts) == 1
    assert posts[0].draft_id == draft_id


async def test_bug02_unique_constraint_rejects_second_post(env) -> None:
    """Уникальный индекс БД не даёт создать второй Post с тем же draft_id."""
    repo: PostRepository = env["repo"]
    user_id = env["user"].id
    params = {
        "user_id": user_id,
        "telegram_file_id": "f",
        "telegram_file_unique_id": "u",
        "scheduled_at_utc": datetime.now(UTC) + timedelta(days=1),
        "timezone": "UTC",
        "publication_type": PublicationType.REMINDER,
        "draft_id": "draft-unique",
    }
    first = await repo.create_post(**params)
    # Идемпотентный повтор возвращает ту же запись.
    second = await repo.create_post(**params)

    assert first.id == second.id
    # Прямая вставка в обход репозитория нарушает уникальность.
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(db_path(Path(env["engine"].url.database).parent.parent)) as conn:
            conn.execute(
                "INSERT INTO posts (user_id, telegram_file_id, telegram_file_unique_id, "
                "scheduled_at_utc, timezone, status, publication_type, target_id, "
                "error_message, created_at, updated_at, published_at, caption, draft_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    "f2",
                    "u2",
                    "2026-12-01 10:00:00",
                    "UTC",
                    "PENDING",
                    "REMINDER",
                    None,
                    None,
                    "2026-11-01 10:00:00",
                    "2026-11-01 10:00:00",
                    None,
                    None,
                    "draft-unique",
                ),
            )
            conn.commit()


async def test_bug02_crash_between_commit_and_fsm_does_not_duplicate(env) -> None:
    """Сбой между commit и записью FSM не приводит ко второй публикации."""
    factory = env["factory"]
    repo: PostRepository = env["repo"]
    crashing = FailingOnSecondCallPostService(repo, env["scheduler"])
    store = DialogStateRepository(factory)
    service = DialogService(
        store,
        user_service=FakeUsers(env["user"].id),  # type: ignore[arg-type]
        post_service=crashing,
    )
    draft_id = await _to_confirmation(service)

    crashed = await service.confirm_post(USER, CHAT, draft_id=draft_id)
    assert crashed.kind == DialogOutcomeKind.POST_FAILED
    # Post уже в БД, FSM остался на подтверждении.
    assert await service._current_state(USER, CHAT) == DialogState.WAITING_CONFIRMATION

    recovered = await service.confirm_post(USER, CHAT, draft_id=draft_id)

    assert recovered.kind == DialogOutcomeKind.POST_CREATED
    posts = await repo.list_pending_by_user(env["user"].id)
    assert len(posts) == 1
    assert posts[0].draft_id == draft_id


async def test_bug02_confirm_after_restart_returns_existing_post(env) -> None:
    """Повторное подтверждение после перезапуска возвращает уже созданный Post."""
    service = env["service"]
    draft_id = await _to_confirmation(service)
    created = await service.confirm_post(USER, CHAT, draft_id=draft_id)
    assert created.post is not None

    # «Перезапуск»: новые экземпляры хранилища и сервисов на той же БД.
    factory = env["factory"]
    restarted = DialogService(
        DialogStateRepository(factory),
        user_service=FakeUsers(env["user"].id),  # type: ignore[arg-type]
        post_service=PostService(env["repo"], env["scheduler"]),  # type: ignore[arg-type]
    )

    outcome = await restarted.confirm_post(USER, CHAT, draft_id=draft_id)

    assert outcome.kind == DialogOutcomeKind.POST_CREATED
    assert len(await env["repo"].list_pending_by_user(env["user"].id)) == 1


async def test_bug02_scheduler_failure_keeps_single_post(env) -> None:
    """Ошибка Scheduler после успешного commit не создаёт второй Post при повторе."""
    service = env["service"]
    env["scheduler"].fail_times = 1
    draft_id = await _to_confirmation(service)

    first = await service.confirm_post(USER, CHAT, draft_id=draft_id)
    second = await service.confirm_post(USER, CHAT, draft_id=draft_id)

    assert first.kind == DialogOutcomeKind.POST_SAVED_UNSCHEDULED
    assert first.scheduler_synced is False
    assert second.kind == DialogOutcomeKind.POST_CREATED
    assert len(await env["repo"].list_pending_by_user(env["user"].id)) == 1


async def test_bug02_legacy_posts_without_draft_id_are_supported(env) -> None:
    """Старые записи без draft_id читаются и не конфликтуют по уникальности."""
    repo: PostRepository = env["repo"]
    user_id = env["user"].id
    common = {
        "user_id": user_id,
        "telegram_file_id": "legacy",
        "telegram_file_unique_id": "legacy-uid",
        "scheduled_at_utc": datetime.now(UTC) + timedelta(days=1),
        "timezone": "UTC",
        "publication_type": PublicationType.REMINDER,
    }
    first = await repo.create_post(**common)
    second = await repo.create_post(**common)  # два NULL draft_id — не конфликт

    assert first.id != second.id
    assert first.draft_id is None and second.draft_id is None
    assert await repo.get_by_draft_id("missing") is None


# === BUG-03: preview и Markdown ===========================================


@pytest.mark.parametrize(
    "caption",
    [
        "__bold__ and _italic_",
        "*звёздочки* `код` [ссылка](http://x)",
        "100% скидка $$$ и \\ обратный слэш",
        "эмодзи 😀🎉 и юникод ĄĆĘŁŃÓŚŹŻ",
        "кириллица + 中文 + العربية",
        "",
    ],
)
def test_bug03_escape_markdown_keeps_text_safe(caption: str) -> None:
    """Спецсимволы Markdown экранируются, Unicode и эмодзи не меняются."""
    escaped = escape_markdown(caption)
    for special in ("_", "*", "`", "["):
        assert f"\\{special}" in escaped or special not in caption
    # Экранирование не теряет и не меняет символы: снятие экранов возвращает оригинал.
    restored = escaped
    for special in ("_", "*", "`", "["):
        restored = restored.replace(f"\\{special}", special)
    assert restored == caption
    # UTF-8 не повреждается.
    assert escaped.encode("utf-8").decode("utf-8") == escaped


def test_bug03_caption_with_markdown_is_escaped_in_preview() -> None:
    """Подпись пользователя не ломает parse_mode="Markdown" в превью."""
    text = _render_preview_for_caption(
        {
            PHOTO_FILE_ID_KEY: "f",
            "publication_type": PublicationType.REMINDER.value,
            "caption": "__подчёркивания__ и *звёзды*",
        },
        datetime.now(UTC) + timedelta(days=1),
        "UTC",
    )
    assert "\\_\\_подчёркивания\\_\\_" in text
    assert "\\*звёзды\\*" in text
    # Разметка самого бота остаётся рабочей.
    assert text.startswith("*Проверь публикацию перед сохранением*")


def test_bug03_long_caption_fits_telegram_limit() -> None:
    """Длинная подпись не выходит за лимит caption (1024) и не рвёт UTF-8."""
    caption = "ä" * 5000
    text = _render_preview_for_caption(
        {
            PHOTO_FILE_ID_KEY: "f",
            "publication_type": PublicationType.REMINDER.value,
            "caption": caption,
        },
        datetime.now(UTC) + timedelta(days=1),
        "UTC",
    )
    assert len(text) <= 1024
    assert text.encode("utf-8").decode("utf-8") == text
    assert text.endswith("…")


def test_bug03_empty_caption_omits_line() -> None:
    """Пустая подпись не добавляет строку «Подпись»."""
    text = _render_preview_for_caption(
        {PHOTO_FILE_ID_KEY: "f", "publication_type": PublicationType.REMINDER.value},
        datetime.now(UTC) + timedelta(days=1),
        "UTC",
    )
    assert "Подпись" not in text


async def test_bug03_send_photo_failure_falls_back_to_message(
    fake_telegram, running_app_factory
) -> None:
    """Недоступный file_id / ошибка sendPhoto: превью уходит текстом, черновик цел."""
    app, fake = await running_app_factory()
    fake.fail_send_photo = (400, "Bad Request: wrong file identifier")
    await _e2e_start(app, fake)
    await _e2e_photo(app, fake)
    await _e2e_text(app, fake, REMINDER_BUTTON, DialogState.WAITING_DATETIME)

    fake.add_text_message(future_date_text(days=2), chat_id=USER, user_id=USER)
    await wait_until(lambda: any("Проверь публикацию" in m["text"] for m in fake.sent_messages))
    await wait_for_state(app, USER, DialogState.WAITING_CONFIRMATION)


async def test_bug03_send_message_failure_reports_clearly(fake_telegram, running_app_factory):
    """Ошибка отправки превью: пользователь получает понятный ответ, черновик сохранён."""
    app, fake = await running_app_factory()
    await _e2e_start(app, fake)
    await _e2e_photo(app, fake)
    await _e2e_text(app, fake, REMINDER_BUTTON, DialogState.WAITING_DATETIME)

    fake.fail_send_photo = (400, "Bad Request: wrong file identifier")
    fake.fail_send_message = (500, "Internal Server Error")
    fake.add_text_message(future_date_text(days=2), chat_id=USER, user_id=USER)

    # Превью не ушло, но состояние подтверждения сохранено — данные не потеряны.
    await wait_for_state(app, USER, DialogState.WAITING_CONFIRMATION)
    storage = app._state_storage
    draft = await storage.get_data(USER, USER)
    assert draft.get(DRAFT_ID_KEY)
    # Предупреждение о неудаче тоже не доставлено (Telegram недоступен), поэтому
    # проверяем только сохранность черновика и отсутствие публикации.
    assert await app._dialog_service.resolve_confirmed_post(USER, USER) is None


# === BUG-04: Scheduler и устаревшие задания ===============================


async def test_bug04_reschedule_updates_db_and_scheduler(env) -> None:
    """Успешный перенос обновляет и БД, и задание Scheduler."""
    repo: PostRepository = env["repo"]
    user_id = env["user"].id
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )
    service: RescheduleService = env["service"]._reschedule
    new_time = datetime.now(UTC) + timedelta(days=3)

    result = await service.reschedule_with_status(
        post_id=post.id, user_id=user_id, scheduled_at_utc=new_time
    )

    assert result.scheduler_synced is True
    actual = await repo.get_by_id(post.id)
    assert actual is not None
    assert actual.scheduled_at_utc.replace(microsecond=0) == new_time.replace(microsecond=0)
    assert env["scheduler"].scheduled[-1] == (post.id, new_time)


async def test_bug04_reschedule_reports_scheduler_error(env) -> None:
    """Ошибка Scheduler не скрывается: результату не выставляется «всё хорошо»."""
    repo: PostRepository = env["repo"]
    user_id = env["user"].id
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )
    env["scheduler"].fail_times = 5  # обе попытки синхронизации падают

    result = await env["service"]._reschedule.reschedule_with_status(
        post_id=post.id,
        user_id=user_id,
        scheduled_at_utc=datetime.now(UTC) + timedelta(days=3),
    )

    assert result.scheduler_synced is False
    actual = await repo.get_by_id(post.id)
    assert actual is not None and actual.scheduled_at_utc > datetime.now(UTC)


async def test_bug04_stale_job_does_not_publish(env) -> None:
    """Срабатывание задания со старым временем не публикует пост."""
    repo: PostRepository = env["repo"]
    user_id = env["user"].id
    old_time = datetime.now(UTC) + timedelta(hours=1)
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=old_time,
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )
    new_time = datetime.now(UTC) + timedelta(days=2)
    await repo.reschedule_post(post_id=post.id, user_id=user_id, scheduled_at_utc=new_time)

    published: list[int] = []

    class RecordingExecution:
        async def execute(self, post_id: int) -> bool:
            published.append(post_id)
            return True

    scheduler = SchedulerService(repo, RecordingExecution())  # type: ignore[arg-type]
    scheduler.start()
    try:
        # Старое задание (версия времени = old_time) срабатывает после переноса.
        await scheduler._on_post_due(post.id, expected_run_at=old_time)
        assert published == []
        # Задание перерегистрировано на актуальное время из БД.
        job = scheduler._scheduler.get_job(f"post:{post.id}")
        assert job is not None
        # Актуальное задание публикует пост.
        await scheduler._on_post_due(post.id, expected_run_at=new_time)
        assert published == [post.id]
    finally:
        scheduler.shutdown()


async def test_bug04_cancelled_post_is_not_published(env) -> None:
    """Отменённый пост не публикуется даже при срабатывании задания."""
    repo: PostRepository = env["repo"]
    user_id = env["user"].id
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=1),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )
    await repo.cancel_post(post.id, user_id)
    published: list[int] = []

    class RecordingExecution:
        async def execute(self, post_id: int) -> bool:
            published.append(post_id)
            return True

    scheduler = SchedulerService(repo, RecordingExecution())  # type: ignore[arg-type]
    scheduler.start()
    try:
        await scheduler._on_post_due(post.id)
        assert published == []
    finally:
        scheduler.shutdown()


async def test_bug04_resync_after_restart(env) -> None:
    """После рестарта задание восстанавливается по актуальному времени из БД."""
    repo: PostRepository = env["repo"]
    user_id = env["user"].id
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(days=1),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )
    scheduler = SchedulerService(repo, None)  # type: ignore[arg-type]
    scheduler.start()
    try:
        assert await scheduler.resync_post(post.id) is True
        job = scheduler._scheduler.get_job(f"post:{post.id}")
        assert job is not None
        assert scheduler.remove_job(post.id) is True
        assert scheduler._scheduler.get_job(f"post:{post.id}") is None
    finally:
        scheduler.shutdown()


async def test_bug04_double_reschedule_and_cancel_race(env) -> None:
    """Двойной перенос и отмена во время переноса не ломают состояние поста."""
    repo: PostRepository = env["repo"]
    user_id = env["user"].id
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )
    service: RescheduleService = env["service"]._reschedule
    first_time = datetime.now(UTC) + timedelta(days=2)
    second_time = datetime.now(UTC) + timedelta(days=4)

    first = await service.reschedule_with_status(
        post_id=post.id, user_id=user_id, scheduled_at_utc=first_time
    )
    second = await service.reschedule_with_status(
        post_id=post.id, user_id=user_id, scheduled_at_utc=second_time
    )
    assert first.scheduler_synced and second.scheduler_synced

    actual = await repo.get_by_id(post.id)
    assert actual is not None
    assert actual.scheduled_at_utc.replace(microsecond=0) == second_time.replace(microsecond=0)

    # Отмена уже перенесённого поста: перенос после отмены отклоняется.
    await repo.cancel_post(post.id, user_id)
    with pytest.raises(PostNotPendingError):
        await service.reschedule_with_status(
            post_id=post.id,
            user_id=user_id,
            scheduled_at_utc=datetime.now(UTC) + timedelta(days=6),
        )


# === BUG-05: перенос не уничтожает черновик ================================


async def test_bug05_reschedule_from_idle(env) -> None:
    """Перенос из IDLE запускается штатно."""
    repo: PostRepository = env["repo"]
    post = await repo.create_post(
        user_id=env["user"].id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )
    outcome = await env["service"].start_reschedule(USER, CHAT, post.id)

    assert outcome.kind == DialogOutcomeKind.RESCHEDULE_PROMPT
    assert outcome.state == DialogState.WAITING_RESCHEDULE_DATETIME


@pytest.mark.parametrize(
    ("setup_state", "expected_state"),
    [
        ("WAITING_PUBLICATION_TYPE", DialogState.WAITING_PUBLICATION_TYPE),
        ("WAITING_DATETIME", DialogState.WAITING_DATETIME),
    ],
)
async def test_bug05_reschedule_during_active_scenario_is_rejected(
    env, setup_state: str, expected_state: DialogState
) -> None:
    """Перенос во время активного ввода не уничтожает черновик."""
    service: DialogService = env["service"]
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, _photo())
    if setup_state == "WAITING_DATETIME":
        await service.receive_text(USER, CHAT, REMINDER_BUTTON)
    draft_before = await env["store"].get_draft(USER, CHAT)
    repo: PostRepository = env["repo"]
    post = await repo.create_post(
        user_id=env["user"].id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )

    outcome = await service.start_reschedule(USER, CHAT, post.id)

    assert outcome.kind == DialogOutcomeKind.RESCHEDULE_CONFLICT
    assert await service._current_state(USER, CHAT) == expected_state
    assert await env["store"].get_draft(USER, CHAT) == draft_before


async def test_bug05_reschedule_during_confirmation_replaces_preview(env) -> None:
    """Перенос из состояния подтверждения допустим: Post ещё не создан."""
    service: DialogService = env["service"]
    await _to_confirmation(service)
    repo: PostRepository = env["repo"]
    post = await repo.create_post(
        user_id=env["user"].id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )

    outcome = await service.start_reschedule(USER, CHAT, post.id)

    assert outcome.kind == DialogOutcomeKind.RESCHEDULE_PROMPT
    assert await service._current_state(USER, CHAT) == DialogState.WAITING_RESCHEDULE_DATETIME


async def test_bug05_repeated_reschedule_click_is_safe(env) -> None:
    """Повторное нажатие «Перенести» перезапускает сценарий переноса без потери данных."""
    repo: PostRepository = env["repo"]
    user_id = env["user"].id
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )
    service: DialogService = env["service"]
    await service.start_reschedule(USER, CHAT, post.id)
    await service.start_reschedule(USER, CHAT, post.id)

    assert await service._current_state(USER, CHAT) == DialogState.WAITING_RESCHEDULE_DATETIME
    outcome = await service.receive_text(USER, CHAT, future_date_text(days=3))
    assert outcome.kind == DialogOutcomeKind.RESCHEDULE_OK


# === BUG-06 / BUG-07: клавиатура, персистентность, TTL ===================


async def test_bug06_callback_outcomes_define_keyboard_lifecycle() -> None:
    """Клавиатура снимается только для завершённых исходов (логика в handlers)."""
    from postmaster.handlers import dialog as dialog_handlers

    terminal = dialog_handlers._TERMINAL_CALLBACK_OUTCOMES
    assert DialogOutcomeKind.CONFIRMATION_CANCELLED in terminal
    assert DialogOutcomeKind.POST_SAVED_UNSCHEDULED in terminal
    # Восстановимые исходы клавиатуру не снимают.
    assert DialogOutcomeKind.POST_FAILED not in terminal
    assert DialogOutcomeKind.STALE_CALLBACK not in terminal
    assert DialogOutcomeKind.INVALID_MESSAGE not in terminal


async def test_bug07_state_survives_restart(env) -> None:
    """Черновик и шаг сценария сохраняются после перезапуска приложения."""
    service: DialogService = env["service"]
    draft_id = await _to_confirmation(service, caption="переживёт рестарт")

    restarted = DialogService(
        DialogStateRepository(env["factory"]),
        user_service=FakeUsers(env["user"].id),  # type: ignore[arg-type]
        post_service=PostService(env["repo"], env["scheduler"]),  # type: ignore[arg-type]
    )

    assert await restarted._current_state(USER, CHAT) == DialogState.WAITING_CONFIRMATION
    draft = await restarted._store.get_draft(USER, CHAT)
    assert draft[DRAFT_ID_KEY] == draft_id
    assert draft[PHOTO_FILE_ID_KEY] == "file-1"
    assert PARSED_UTC_KEY in draft and PARSED_TZ_KEY in draft

    confirmed = await restarted.confirm_post(USER, CHAT, draft_id=draft_id)
    assert confirmed.kind == DialogOutcomeKind.POST_CREATED


async def test_bug07_restart_during_reschedule(env) -> None:
    """Перенос переживает перезапуск: состояние и ID поста сохранены."""
    repo: PostRepository = env["repo"]
    post = await repo.create_post(
        user_id=env["user"].id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )
    await env["service"].start_reschedule(USER, CHAT, post.id)

    restarted = DialogService(
        DialogStateRepository(env["factory"]),
        user_service=FakeUsers(env["user"].id),  # type: ignore[arg-type]
        post_service=PostService(env["repo"], env["scheduler"]),  # type: ignore[arg-type]
        reschedule_service=RescheduleService(env["repo"], env["scheduler"]),
    )
    assert await restarted._current_state(USER, CHAT) == DialogState.WAITING_RESCHEDULE_DATETIME
    outcome = await restarted.receive_text(USER, CHAT, future_date_text(days=3))
    assert outcome.kind == DialogOutcomeKind.RESCHEDULE_OK


async def test_bug07_ttl_expires_stale_draft(engine) -> None:
    """TTL удаляет незавершённый черновик и очищает состояние."""
    factory = create_session_factory(engine)
    storage = SqlAlchemyStateStorage(factory)
    await storage.set_state(CHAT, USER, DialogState.WAITING_DATETIME.value)
    await storage.set_data(CHAT, USER, PHOTO_FILE_ID_KEY, "file-1")
    assert await storage.get_state(CHAT, USER) == DialogState.WAITING_DATETIME.value

    removed = await storage.purge_expired(now=datetime.now(UTC) + timedelta(days=2))

    assert removed == 1
    assert await storage.get_state(CHAT, USER) is None
    assert await storage.get_data(CHAT, USER) == {}


async def test_bug07_expired_draft_is_invisible_without_purge(engine) -> None:
    """Просроченная запись не отдаётся как актуальная даже до очистки."""
    factory = create_session_factory(engine)
    storage = SqlAlchemyStateStorage(factory, ttl=timedelta(seconds=-1))
    await storage.set_state(CHAT, USER, DialogState.WAITING_PHOTO.value)

    assert await storage.get_state(CHAT, USER) is None


async def test_bug07_shared_state_between_instances(engine) -> None:
    """Два экземпляра приложения видят одно состояние (общая БД)."""
    factory = create_session_factory(engine)
    first = SqlAlchemyStateStorage(factory)
    second = SqlAlchemyStateStorage(factory)

    await first.set_state(CHAT, USER, DialogState.WAITING_TARGET.value)
    await second.set_data(CHAT, USER, TARGET_ID_KEY, "@channel")

    assert await first.get_state(CHAT, USER) == DialogState.WAITING_TARGET.value
    assert (await first.get_data(CHAT, USER))[TARGET_ID_KEY] == "@channel"
    assert await second.get_state(CHAT, USER) == DialogState.WAITING_TARGET.value


async def test_bug07_no_secrets_in_draft(env) -> None:
    """В черновике нет токенов: только данные сценария."""
    service: DialogService = env["service"]
    await _to_confirmation(service)
    draft = await env["store"].get_draft(USER, CHAT)

    serialized = " ".join(f"{key}={value}" for key, value in draft.items())
    assert "token" not in serialized.lower()
    assert set(draft) <= {
        PHOTO_FILE_ID_KEY,
        PHOTO_UNIQUE_ID_KEY,
        "file_size",
        "width",
        "height",
        "caption",
        "publication_type",
        TARGET_ID_KEY,
        "parsed_utc",
        "parsed_local",
        "parsed_tz",
        DRAFT_ID_KEY,
        "draft_stage",
    }


# === миграции =============================================================


async def test_migration_adds_draft_id_to_existing_schema(tmp_path: Path) -> None:
    """Миграция добавляет posts.draft_id в уже существующую базу и откатывается."""
    path = db_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER, status VARCHAR, "
            "publication_type VARCHAR)"
        )
        conn.execute(
            "INSERT INTO posts (user_id, status, publication_type) VALUES (1,'PENDING','REMINDER')"
        )
        conn.commit()

    engine = create_database_engine(f"sqlite+aiosqlite:///{path}")
    try:
        version = await apply_migrations(engine)
        assert version >= MIGRATION_POSTS_DRAFT_ID.version
        async with engine.connect() as connection:
            columns = (await connection.execute(text("PRAGMA table_info(posts)"))).all()
        assert "draft_id" in {column[1] for column in columns}
        # Данные сохранены.
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1

        # Повторный запуск идемпотентен.
        assert await apply_migrations(engine) == version

        rolled_back = await rollback_migrations(engine)
        assert rolled_back == 0
        async with engine.connect() as connection:
            columns_after = (await connection.execute(text("PRAGMA table_info(posts)"))).all()
        assert "draft_id" not in {column[1] for column in columns_after}
        assert await schema_version(engine) == 0
    finally:
        await engine.dispose()


# === вспомогательные функции E2E ==========================================


async def _e2e_start(app, fake) -> None:
    fake.add_text_message("/start", chat_id=USER, user_id=USER, first_name="PM06")
    await wait_until(lambda: len(fake.sent_messages) >= 1)
    await wait_for_state(app, USER, DialogState.WAITING_PHOTO)


async def _e2e_photo(app, fake) -> None:
    fake.add_photo_message("pm06-photo", chat_id=USER, user_id=USER)
    await wait_for_state(app, USER, DialogState.WAITING_PUBLICATION_TYPE)


async def _e2e_text(app, fake, text: str, expected: DialogState) -> None:
    fake.add_text_message(text, chat_id=USER, user_id=USER)
    await wait_for_state(app, USER, expected)


@pytest.fixture
async def running_app_factory(fake_telegram):
    """Фабрика запущенных приложений на fake Telegram API."""
    from fake_telegram import TEST_TOKEN
    from postmaster.app import Application
    from postmaster.config import Settings

    started: list = []

    async def factory(directory: Path | None = None):
        settings = Settings(
            bot_token=TEST_TOKEN,
            database_url=f"sqlite+aiosqlite:///{db_path(directory or Path('/tmp/pm06'))}",
            log_level="INFO",
        )
        app = Application.from_settings(settings)
        task = asyncio.create_task(app.run())
        await wait_until(lambda: fake_telegram.get_updates_called.is_set())
        started.append((app, task))
        return app, fake_telegram

    yield factory
    for app, task in started:
        await app.stop()
        await asyncio.wait_for(task, timeout=10)


# === E2E: старые кнопки через реальный Update-поток ========================


async def _drive_to_preview(app, fake) -> str:
    """Доводит сценарий до превью и возвращает callback_data кнопки «Подтвердить»."""
    await _e2e_start(app, fake)
    await _e2e_photo(app, fake)
    await _e2e_text(app, fake, REMINDER_BUTTON, DialogState.WAITING_DATETIME)
    before = len(fake.sent_photos)
    fake.add_text_message(future_date_text(days=2), chat_id=USER, user_id=USER)
    await wait_until(lambda: len(fake.sent_photos) > before)
    await wait_for_state(app, USER, DialogState.WAITING_CONFIRMATION)
    keyboard = fake.sent_inline_markups[-1]["reply_markup"]["inline_keyboard"]
    confirm = next(
        button["callback_data"]
        for row in keyboard
        for button in row
        if button["text"].startswith("✅")
    )
    return confirm


async def test_pm06_e2e_stale_button_cannot_confirm_new_draft(
    running_app_factory, tmp_path: Path
) -> None:
    """E2E: кнопка старого превью после /start не подтверждает новый черновик."""
    app, fake = await running_app_factory(tmp_path)
    old_callback = await _drive_to_preview(app, fake)

    # Новый сценарий: /start сбрасывает черновик, старые кнопки остаются в чате.
    await _e2e_start(app, fake)
    before = len(fake.sent_messages)
    fake.add_callback_query(old_callback, chat_id=USER, user_id=USER)
    await wait_until(lambda: len(fake.sent_messages) > before)

    assert "устарела" in fake.sent_messages[-1]["text"]
    # Новый черновик не тронут, публикаций нет.
    await wait_for_state(app, USER, DialogState.WAITING_PHOTO)
    assert await app._dialog_service.resolve_confirmed_post(USER, USER) is None


async def test_pm06_e2e_current_button_confirms_once(running_app_factory, tmp_path: Path) -> None:
    """E2E: актуальная кнопка создаёт ровно одну публикацию при двойном нажатии."""
    app, fake = await running_app_factory(tmp_path)
    callback = await _drive_to_preview(app, fake)

    fake.add_callback_query(callback, chat_id=USER, user_id=USER)
    await wait_for_state(app, USER, DialogState.IDLE)
    before = len(fake.sent_messages)
    fake.add_callback_query(callback, chat_id=USER, user_id=USER)  # повторное нажатие
    await wait_until(lambda: len(fake.sent_messages) > before)

    with sqlite3.connect(db_path(tmp_path)) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM posts").fetchone()
    assert rows[0] == 1


async def test_pm06_e2e_draft_survives_restart(running_app_factory, tmp_path: Path) -> None:
    """E2E: после перезапуска черновик на шаге подтверждения не теряется."""
    app, fake = await running_app_factory(tmp_path)
    callback = await _drive_to_preview(app, fake)
    await app.stop()

    # Второй экземпляр приложения на той же БД (перезапуск процесса).
    second_app, second_fake = await running_app_factory(tmp_path)
    state = await second_app.bot.get_state(USER, USER)
    assert state == DialogState.WAITING_CONFIRMATION
    draft = await second_app._state_storage.get_data(USER, USER)
    assert draft.get(DRAFT_ID_KEY)

    before = len(second_fake.sent_messages)
    second_fake.add_callback_query(callback, chat_id=USER, user_id=USER)
    await wait_until(lambda: len(second_fake.sent_messages) > before)
    await wait_for_state(second_app, USER, DialogState.IDLE)
    assert "✅" in second_fake.sent_messages[-1]["text"]


async def test_pm06_e2e_reschedule_during_scenario_keeps_draft(
    running_app_factory, tmp_path: Path
) -> None:
    """E2E: «Перенести» во время активного сценария не уничтожает черновик."""
    app, fake = await running_app_factory(tmp_path)
    await _e2e_start(app, fake)
    await _e2e_photo(app, fake)

    # Публикация, которую пользователь пытается перенести через /my_posts.
    post_id = await _insert_pending_post(app)
    before = len(fake.sent_messages)
    fake.add_callback_query(f"resched:{post_id}", chat_id=USER, user_id=USER)
    await wait_until(lambda: len(fake.sent_messages) > before)

    assert "Сейчас идёт другой сценарий" in fake.sent_messages[-1]["text"]
    assert await app.bot.get_state(USER, USER) == DialogState.WAITING_PUBLICATION_TYPE


async def _insert_pending_post(app) -> int:
    """Создаёт PENDING-публикацию напрямую в БД запущенного приложения."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from postmaster.database.models import PostModel

    factory = async_sessionmaker(app._engine, expire_on_commit=False)
    user_id = 1
    now = datetime.now(UTC)
    async with factory() as session:
        model = PostModel(
            user_id=user_id,
            telegram_file_id="pm06-e2e",
            telegram_file_unique_id="pm06-e2e-uid",
            scheduled_at_utc=now + timedelta(hours=3),
            timezone="UTC",
            status="PENDING",
            publication_type=PublicationType.REMINDER.value,
            target_id=None,
            error_message=None,
            created_at=now,
            updated_at=now,
            published_at=None,
            caption=None,
        )
        session.add(model)
        await session.commit()
        return model.id


# === Дополнительные исправления PM-06 §10 =================================


async def test_pm06_reschedule_rejects_foreign_post_before_starting(env) -> None:
    """Диалог переноса не запускается для чужого поста, черновик не тронут."""
    repo: PostRepository = env["repo"]
    other_user = await UserService(
        UserRepository(env["factory"]), default_timezone="UTC"
    ).get_or_create(telegram_user_id=777, username=None, first_name="Other")
    post = await repo.create_post(
        user_id=other_user.id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )
    service: DialogService = env["service"]
    await service.start(USER, CHAT)
    draft_before = await env["store"].get_draft(USER, CHAT)

    outcome = await service.start_reschedule(USER, CHAT, post.id)

    assert outcome.kind == DialogOutcomeKind.RESCHEDULE_REJECTED
    assert await service._current_state(USER, CHAT) == DialogState.WAITING_PHOTO
    assert await env["store"].get_draft(USER, CHAT) == draft_before


async def test_pm06_reschedule_rejects_non_pending_post(env) -> None:
    """Отменённый пост нельзя перенести: диалог не стартует."""
    repo: PostRepository = env["repo"]
    user_id = env["user"].id
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="f",
        telegram_file_unique_id="u",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )
    await repo.cancel_post(post.id, user_id)

    outcome = await env["service"].start_reschedule(USER, CHAT, post.id)

    assert outcome.kind == DialogOutcomeKind.RESCHEDULE_REJECTED
    assert await env["service"]._current_state(USER, CHAT) == DialogState.IDLE


async def test_pm06_confirm_revalidates_datetime(env) -> None:
    """Превью «провисло» до момента публикации: подтверждение отклоняется без Post."""
    service: DialogService = env["service"]
    await service.start(USER, CHAT)
    await service.receive_photo(USER, CHAT, _photo())
    await service.receive_text(USER, CHAT, REMINDER_BUTTON)
    # Время в будущем на минуту, затем «ждём» его наступления.
    target = datetime.now(UTC) + timedelta(minutes=1)
    local = target.astimezone()
    outcome = await service.receive_text(USER, CHAT, local.strftime("%d.%m.%Y %H:%M"))
    assert outcome.kind == DialogOutcomeKind.PREVIEW_SHOWN

    draft = await env["store"].get_draft(USER, CHAT)
    draft["parsed_utc"] = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    await env["store"].set_draft(USER, CHAT, draft)

    confirmed = await service.confirm_post(USER, CHAT, draft_id=outcome.draft_id)

    assert confirmed.kind == DialogOutcomeKind.DATETIME_INVALID
    assert await env["repo"].list_pending_by_user(env["user"].id) == []
    # Черновик сохранён: пользователь может ввести новое время.
    assert await service._current_state(USER, CHAT) == DialogState.WAITING_CONFIRMATION


async def test_pm06_draft_traceability_for_my_posts(env) -> None:
    """Созданная публикация связана с draft_id — можно отличить дубликат от новой."""
    service: DialogService = env["service"]
    draft_id = await _to_confirmation(service)
    await service.confirm_post(USER, CHAT, draft_id=draft_id)

    found = await env["repo"].get_by_draft_id(draft_id)
    assert found is not None
    assert found.draft_id == draft_id
    assert await service.resolve_confirmed_post(USER, CHAT, draft_id=draft_id) is not None


# === Дополнительно: пагинация /my_posts при > 20 записях ==================


async def test_pm06_my_posts_pagination_beyond_limit(env) -> None:
    """При >20 публикациях видны первые 20, остальные отражены в hidden."""
    from postmaster.services.my_posts_service import MY_POSTS_LIMIT, MyPostsService

    repo: PostRepository = env["repo"]
    user = env["user"]
    base = datetime.now(UTC) + timedelta(hours=1)
    for index in range(MY_POSTS_LIMIT + 7):
        await repo.create_post(
            user_id=user.id,
            telegram_file_id=f"f{index}",
            telegram_file_unique_id=f"u{index}",
            scheduled_at_utc=base + timedelta(minutes=index),
            timezone="UTC",
            publication_type=PublicationType.REMINDER,
        )

    users = UserService(UserRepository(env["factory"]), default_timezone="UTC")
    service = MyPostsService(repo, users)
    view = await service.list_view(USER)

    assert len(view.posts) == MY_POSTS_LIMIT
    assert view.hidden == 7
    assert view.truncated
    # Порядок — по времени публикации, без пропусков в начале списка.
    times = [post.scheduled_at_utc for post in view.posts]
    assert times == sorted(times)


def test_migration_cli_roundtrip_without_bot_token(tmp_path: Path, monkeypatch) -> None:
    """CLI миграций работает без BOT_TOKEN и выполняет upgrade → downgrade → upgrade.

    Тест синхронный: ``migrate.main`` вызывает ``asyncio.run``, которому нужен
    собственный event loop (внутри теста с pytest-asyncio это невозможно).
    """
    from postmaster import migrate

    database_url = f"sqlite+aiosqlite:///{db_path(tmp_path)}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path.parent)

    assert migrate.main(["upgrade"]) == migrate.EXIT_OK
    assert migrate.main(["version"]) == migrate.EXIT_OK
    assert migrate.main(["downgrade"]) == migrate.EXIT_OK
    assert migrate.main(["upgrade"]) == migrate.EXIT_OK

    async def _version() -> int:
        engine = create_database_engine(database_url)
        try:
            return await schema_version(engine)
        finally:
            await engine.dispose()

    assert asyncio.run(_version()) == MIGRATION_POSTS_DRAFT_ID.version
