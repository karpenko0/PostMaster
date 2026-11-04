"""SPEC-009: контракт сервиса, commit → Scheduler, сообщения, FSM и сквозной поток."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fake_telegram import TEST_TOKEN, FakeTelegram
from harness import db_path, draft_data, future_date_text, users_rows, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.config import Settings
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.dialog import DialogState
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.handlers.dialog import reply_text
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.dialog_service import DialogOutcome, DialogOutcomeKind
from postmaster.services.post_service import (
    PostCreationError,
    PostCreationErrorCode,
    PostService,
    SchedulerRegistrationError,
)
from postmaster.services.scheduler_service import SchedulerService

RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


class RecordingScheduler:
    def __init__(self, path: Path, *, fail: bool = False) -> None:
        self.path = path
        self.fail = fail
        self.calls: list[tuple[int, datetime]] = []
        self.committed: list[bool] = []

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        self.calls.append((post_id, run_at))
        with sqlite3.connect(self.path) as connection:
            self.committed.append(
                connection.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
                == ("PENDING",)
            )
        if self.fail:
            raise RuntimeError("registration failed")


@pytest.fixture
async def repository_db(
    tmp_path: Path,
) -> AsyncIterator[tuple[PostRepository, RecordingScheduler, int]]:
    path = tmp_path / "spec009.db"
    engine = create_database_engine(f"sqlite+aiosqlite:///{path}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user = await UserRepository(factory).add(
            telegram_user_id=42567,
            first_name="Тест",
            username=None,
            now=datetime(2026, 10, 7, tzinfo=UTC),
        )
        yield PostRepository(factory), RecordingScheduler(path), user.id
    finally:
        await engine.dispose()


async def create(service: PostService, owner_id: int | None, **changes):
    args = dict(
        user_id=owner_id,
        telegram_file_id="AgAC...",
        telegram_file_unique_id="AQAD...",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        target_id=None,
        caption=None,
    )
    args.update(changes)
    return await service.create_scheduled_post(**args)


@pytest.mark.parametrize("kind", [PublicationType.REMINDER, PublicationType.TELEGRAM])
async def test_service_commits_before_scheduler_and_passes_only_id_time(
    repository_db, kind: PublicationType
) -> None:
    repo, scheduler, user_id = repository_db
    service = PostService(repo, scheduler)
    post = await create(
        service,
        user_id,
        publication_type=kind,
        target_id="-1001234567890" if kind == PublicationType.TELEGRAM else None,
        caption="Подпись",
    )
    assert post == await repo.get_by_id(post.id)
    assert post.user_id == user_id and post.status == PostStatus.PENDING
    assert post.publication_type == kind
    assert post.caption == "Подпись"
    assert post.created_at == post.updated_at
    assert post.created_at.tzinfo is UTC
    assert post.published_at is post.error_message is None
    assert scheduler.calls == [(post.id, RUN_AT)]
    assert scheduler.committed == [True]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"user_id": None}, PostCreationErrorCode.USER_NOT_FOUND),
        ({"user_id": 99999}, PostCreationErrorCode.USER_NOT_FOUND),
        ({"telegram_file_id": None}, PostCreationErrorCode.PHOTO_DATA_MISSING),
        ({"telegram_file_unique_id": None}, PostCreationErrorCode.PHOTO_DATA_MISSING),
        ({"scheduled_at_utc": None}, PostCreationErrorCode.DATETIME_MISSING),
        ({"timezone": None}, PostCreationErrorCode.POST_CREATE_FAILED),
        ({"publication_type": "VK"}, PostCreationErrorCode.INVALID_PUBLICATION_TYPE),
        ({"publication_type": "TELEGRAM"}, PostCreationErrorCode.TARGET_REQUIRED),
    ],
)
async def test_invalid_data_never_creates_post_or_scheduler_job(
    repository_db, changes, code
) -> None:
    repo, scheduler, user_id = repository_db
    with pytest.raises(PostCreationError) as exc:
        await create(PostService(repo, scheduler), user_id, **changes)
    assert exc.value.code == code
    assert await repo.list_pending() == []
    assert scheduler.calls == []


async def test_db_failure_preserves_fsm_contract_and_skips_scheduler(
    repository_db, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repo, scheduler, user_id = repository_db

    async def broken_create(**kwargs):
        raise RuntimeError("secret AgAC... SQL failure")

    monkeypatch.setattr(repo, "create_post", broken_create)
    with caplog.at_level("ERROR"):
        with pytest.raises(PostCreationError) as exc:
            await create(PostService(repo, scheduler), user_id)
    assert exc.value.code == PostCreationErrorCode.POST_CREATE_FAILED
    assert scheduler.calls == []
    assert await repo.list_pending() == []
    assert "Post creation failed user_id=" in caplog.text
    assert "AgAC..." not in caplog.text


async def test_scheduler_failure_keeps_committed_pending_post(repository_db) -> None:
    repo, scheduler, user_id = repository_db
    scheduler.fail = True
    with pytest.raises(SchedulerRegistrationError) as exc:
        await create(PostService(repo, scheduler), user_id)
    post = exc.value.post
    assert exc.value.code == PostCreationErrorCode.SCHEDULER_REGISTRATION_FAILED
    assert post == await repo.get_by_id(post.id)
    assert [p.id for p in await repo.list_pending()] == [post.id]
    assert scheduler.committed == [True]


async def test_confirmation_messages(repository_db) -> None:
    repo, scheduler, user_id = repository_db
    service = PostService(repo, scheduler)
    reminder = await create(service, user_id)
    telegram = await create(
        service, user_id, publication_type=PublicationType.TELEGRAM, target_id="-100123"
    )
    assert reply_text(
        DialogOutcome(DialogOutcomeKind.POST_CREATED, DialogState.IDLE, post=reminder)
    ) == (
        "✅ Принято!\n\nФото запланировано:\n"
        "📅 20 октября 2026\n⏰ 15:00\n\nЯ напомню тебе точно в это время."
    )
    assert reply_text(
        DialogOutcome(DialogOutcomeKind.POST_CREATED, DialogState.IDLE, post=telegram)
    ) == (
        "✅ Запланировано!\n\n📅 20 октября 2026\n⏰ 15:00\n\n"
        "Фото будет автоматически опубликовано в канале."
    )
    assert "✅" not in reply_text(
        DialogOutcome(DialogOutcomeKind.POST_SAVED_UNSCHEDULED, DialogState.IDLE, post=reminder)
    )
    assert "✅" not in reply_text(
        DialogOutcome(DialogOutcomeKind.POST_FAILED, DialogState.WAITING_DATETIME)
    )


async def test_real_scheduler_creates_date_job_with_id_and_utc(repository_db) -> None:
    repo, _, user_id = repository_db

    class _NoopExecutor:
        async def publish(self, post_id: int) -> None:
            return None

    scheduler = SchedulerService(repo, publication_executor=_NoopExecutor())
    scheduler.start()
    try:
        post = await create(
            PostService(repo, scheduler),
            user_id,
            scheduled_at_utc=datetime.now(UTC) + timedelta(days=2),
        )
        job = scheduler._scheduler.get_job(f"post:{post.id}")
        assert job is not None
        assert job.args == (post.id,)
        assert job.next_run_time == post.scheduled_at_utc
    finally:
        scheduler.shutdown()


async def test_due_job_reloads_post_by_id_instead_of_storing_object(
    repository_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fake_scheduler, user_id = repository_db

    class _NoopExecutor:
        async def publish(self, post_id: int) -> None:
            return None

    scheduler = SchedulerService(repo, publication_executor=_NoopExecutor())
    original = repo.get_by_id
    loaded: list[int] = []

    async def record_get(post_id: int):
        loaded.append(post_id)
        return await original(post_id)

    monkeypatch.setattr(repo, "get_by_id", record_get)
    post = await create(PostService(repo, fake_scheduler), user_id)
    await scheduler._on_post_due(post.id)
    assert loaded == [post.id]


async def test_e2e_reminder_creates_post_job_and_resets_fsm(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    fake_telegram.add_text_message("/start", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    fake_telegram.add_photo_message("one", chat_id=42, user_id=42)
    await wait_for_state(running_app, 42, DialogState.WAITING_DATETIME)
    date = future_date_text()
    fake_telegram.add_text_message(date, chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 3)
    await wait_for_state(running_app, 42, DialogState.IDLE)
    assert fake_telegram.sent_messages[-1]["text"].startswith("✅ Принято!")
    assert await draft_data(running_app, 42) == {}
    with sqlite3.connect(db_path(tmp_path)) as connection:
        rows = connection.execute(
            "SELECT id,user_id,telegram_file_id,telegram_file_unique_id,"
            "scheduled_at_utc,timezone,status,publication_type,published_at,"
            "error_message FROM posts"
        ).fetchall()
    assert len(rows) == 1
    post_id, owner_id, file_id, unique_id, scheduled, zone, status, kind, published, error = rows[0]
    assert (owner_id, file_id, unique_id, zone, status, kind, published, error) == (
        users_rows(db_path(tmp_path), 42)[0][0],
        "one",
        "one-l",
        "Europe/Moscow",
        "PENDING",
        "REMINDER",
        None,
        None,
    )
    job = running_app._scheduler._scheduler.get_job(f"post:{post_id}")
    assert job is not None and job.args == (post_id,)
    expected_utc = (
        datetime.strptime(date, "%d.%m.%Y %H:%M")
        .replace(tzinfo=ZoneInfo("Europe/Moscow"))
        .astimezone(UTC)
    )
    assert scheduled.startswith(expected_utc.strftime("%Y-%m-%d %H:%M"))

    # Новая фотография после IDLE не использует старые file_id/дату.
    fake_telegram.add_photo_message("two", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 4)
    await wait_for_state(running_app, 42, DialogState.WAITING_DATETIME)
    assert (await draft_data(running_app, 42))["telegram_file_id"] == "two"
    fake_telegram.add_text_message(date, chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 5)
    with sqlite3.connect(db_path(tmp_path)) as connection:
        assert connection.execute("SELECT telegram_file_id FROM posts ORDER BY id").fetchall() == [
            ("one",),
            ("two",),
        ]


async def test_e2e_db_failure_keeps_photo_and_never_confirms(
    running_app: Application,
    fake_telegram: FakeTelegram,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_telegram.add_text_message("/start", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    fake_telegram.add_photo_message("photo", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
    original = PostRepository.create_post

    async def fail_db(self, **kwargs):
        raise RuntimeError("DB unavailable")

    with monkeypatch.context() as scope:
        scope.setattr(PostRepository, "create_post", fail_db)
        fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 3)
        assert "✅" not in fake_telegram.sent_messages[-1]["text"]
        assert "сохранить" in fake_telegram.sent_messages[-1]["text"]
        await wait_for_state(running_app, 42, DialogState.WAITING_DATETIME)
        assert (await draft_data(running_app, 42))["telegram_file_id"] == "photo"
        with sqlite3.connect(db_path(tmp_path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        assert running_app._scheduler._scheduler.get_jobs() == []
    assert PostRepository.create_post is original
    fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 4)
    assert fake_telegram.sent_messages[-1]["text"].startswith("✅ Принято!")
    await wait_for_state(running_app, 42, DialogState.IDLE)


async def test_e2e_scheduler_failure_keeps_post_and_avoids_duplicate_on_new_date(
    running_app: Application,
    fake_telegram: FakeTelegram,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_telegram.add_text_message("/start", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    fake_telegram.add_photo_message("photo", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    def fail_scheduler(self, *, post_id, run_at):
        raise RuntimeError("scheduler unavailable")

    with monkeypatch.context() as scope:
        scope.setattr(SchedulerService, "schedule_post", fail_scheduler)
        fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 3)
    assert "✅" not in fake_telegram.sent_messages[-1]["text"]
    assert "сохранена" in fake_telegram.sent_messages[-1]["text"]
    await wait_for_state(running_app, 42, DialogState.IDLE)
    assert await draft_data(running_app, 42) == {}
    with sqlite3.connect(db_path(tmp_path)) as connection:
        assert connection.execute("SELECT status FROM posts").fetchall() == [("PENDING",)]
    assert running_app._scheduler._scheduler.get_jobs() == []
    fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 4)
    with sqlite3.connect(db_path(tmp_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1


async def test_e2e_telegram_mode_persists_target_and_confirms_locally(
    tmp_path: Path, fake_telegram: FakeTelegram
) -> None:
    settings = Settings(
        bot_token=TEST_TOKEN,
        database_url=f"sqlite+aiosqlite:///{db_path(tmp_path)}",
        default_timezone="Europe/Moscow",
        post_mode="TELEGRAM",
        telegram_target_id="-1001234567890",
    )
    app = Application.from_settings(settings)
    task = asyncio.create_task(app.run())
    await asyncio.wait_for(fake_telegram.get_updates_called.wait(), timeout=10)
    try:
        fake_telegram.add_text_message("/start", chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
        fake_telegram.add_photo_message("telegram-photo", chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
        date = (
            (datetime.now(UTC) + timedelta(days=2))
            .astimezone(ZoneInfo("Europe/Moscow"))
            .strftime("%d.%m.%Y %H:%M")
        )
        fake_telegram.add_text_message(date, chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 3)
        await wait_for_state(app, 42, DialogState.IDLE)
        assert "✅ Запланировано!" in fake_telegram.sent_messages[-1]["text"]
        assert f"⏰ {date[-5:]}" in fake_telegram.sent_messages[-1]["text"]
        with sqlite3.connect(db_path(tmp_path)) as connection:
            post_id, kind, target = connection.execute(
                "SELECT id, publication_type, target_id FROM posts"
            ).fetchone()
        assert (kind, target) == ("TELEGRAM", "-1001234567890")
        assert app._scheduler._scheduler.get_job(f"post:{post_id}") is not None
    finally:
        await app.stop()
        await asyncio.wait_for(task, timeout=10)
