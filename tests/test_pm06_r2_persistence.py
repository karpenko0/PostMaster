"""PM-06-R2-002/003/007/008: идемпотентность, Scheduler, FSM в БД, Recovery.

RED-тесты ревизии 2: постоянно хранимый idempotency key, защита от устаревших
заданий APScheduler, персистентное состояние FSM и единственная инициализация
RecoveryService.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.repositories.dialog_store import DatabaseDialogStore
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.publication_service import PublicationService
from postmaster.services.reschedule_service import RescheduleService, SchedulerRescheduleError
from postmaster.services.scheduler_service import SchedulerService
from postmaster.utils.clock import utc_now

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
async def repo_with_user(tmp_path: Path) -> AsyncIterator[tuple[PostRepository, int, Path]]:
    path = db_path(tmp_path)
    engine = create_database_engine(f"sqlite+aiosqlite:///{path}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user = await UserRepository(factory).add(
            telegram_user_id=73001, username=None, first_name="PM06", now=NOW
        )
        yield PostRepository(factory), user.id, path
    finally:
        await engine.dispose()


def _post_kwargs(user_id: int, when: datetime | None = None) -> dict:
    return {
        "user_id": user_id,
        "telegram_file_id": "file-id",
        "telegram_file_unique_id": "unique-id",
        "scheduled_at_utc": when or NOW + timedelta(days=1),
        "timezone": "UTC",
        "publication_type": PublicationType.REMINDER,
    }


# === PM06-R2-002 ===========================================================


async def test_002_db_unique_index_rejects_second_post_for_same_draft(repo_with_user) -> None:
    repo, user_id, _ = repo_with_user
    first = await repo.create_post_idempotent(**_post_kwargs(user_id), source_draft_id="draft-0001")
    second = await repo.create_post_idempotent(
        **_post_kwargs(user_id), source_draft_id="draft-0001"
    )
    assert second.id == first.id
    assert await repo.get_by_source_draft_id("draft-0001") == first
    assert len(await repo.list_pending_by_user(user_id)) == 1


async def test_002_concurrent_confirmations_create_one_post(repo_with_user) -> None:
    repo, user_id, _ = repo_with_user
    posts = await asyncio.gather(
        *[
            repo.create_post_idempotent(**_post_kwargs(user_id), source_draft_id="draft-parallel")
            for _ in range(8)
        ]
    )
    assert len({post.id for post in posts}) == 1
    assert len(await repo.list_pending_by_user(user_id)) == 1


async def test_002_idempotency_survives_restart(repo_with_user) -> None:
    repo, user_id, path = repo_with_user
    first = await repo.create_post_idempotent(
        **_post_kwargs(user_id), source_draft_id="draft-restart"
    )
    engine2 = create_database_engine(f"sqlite+aiosqlite:///{path}")
    try:
        factory2 = create_session_factory(engine2)
        repo2 = PostRepository(factory2)
        again = await repo2.create_post_idempotent(
            **_post_kwargs(user_id), source_draft_id="draft-restart"
        )
        assert again.id == first.id
        assert len(await repo2.list_pending_by_user(user_id)) == 1
    finally:
        await engine2.dispose()


async def test_002_scheduler_error_never_creates_second_post(tmp_path: Path) -> None:
    from postmaster.services.post_service import PostService, SchedulerRegistrationError

    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user = await UserRepository(factory).add(
            telegram_user_id=73002, username=None, first_name="P", now=NOW
        )
        repo = PostRepository(factory)

        class FailingScheduler:
            def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
                raise RuntimeError("scheduler down")

        service = PostService(repo, FailingScheduler())  # type: ignore[arg-type]
        with pytest.raises(SchedulerRegistrationError):
            await service.create_scheduled_post(
                **_post_kwargs(user.id), source_draft_id="draft-scheduler-error"
            )
        with pytest.raises(SchedulerRegistrationError):
            await service.create_scheduled_post(
                **_post_kwargs(user.id), source_draft_id="draft-scheduler-error"
            )
        assert len(await repo.list_pending_by_user(user.id)) == 1
    finally:
        await engine.dispose()


async def test_002_direct_duplicate_insert_is_rejected(repo_with_user) -> None:
    from postmaster.database.models import PostModel

    repo, user_id, _ = repo_with_user
    await repo.create_post_idempotent(**_post_kwargs(user_id), source_draft_id="draft-unique")
    factory = repo._session_factory
    async with factory() as session, session.begin():
        model = PostModel(
            user_id=user_id,
            telegram_file_id="other-file",
            telegram_file_unique_id="other-unique",
            scheduled_at_utc=(NOW + timedelta(days=2)).replace(tzinfo=None),
            timezone="UTC",
            status=PostStatus.PENDING.value,
            publication_type=PublicationType.REMINDER.value,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
            source_draft_id="draft-unique",
            schedule_version=0,
        )
        session.add(model)
        with pytest.raises(IntegrityError):
            await session.flush()


def _legacy_schema(path: Path) -> None:
    """БД «до PM-06-R2»: таблицы users/posts без новых колонок и dialog_states."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL UNIQUE,
                username VARCHAR, first_name VARCHAR, timezone VARCHAR,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            );
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                telegram_file_id TEXT NOT NULL,
                telegram_file_unique_id TEXT NOT NULL,
                scheduled_at_utc DATETIME NOT NULL,
                timezone VARCHAR NOT NULL,
                status VARCHAR NOT NULL DEFAULT 'PENDING',
                publication_type VARCHAR NOT NULL,
                target_id VARCHAR, error_message TEXT,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
                published_at DATETIME, caption TEXT
            );
            INSERT INTO users VALUES (1, 73003, NULL, 'Legacy', 'UTC',
                                      '2026-01-01 00:00:00', '2026-01-01 00:00:00');
            INSERT INTO posts (id, user_id, telegram_file_id, telegram_file_unique_id,
                               scheduled_at_utc, timezone, status, publication_type,
                               created_at, updated_at)
            VALUES (7, 1, 'legacy-file', 'legacy-unique', '2027-01-01 00:00:00',
                    'UTC', 'PENDING', 'REMINDER',
                    '2026-01-01 00:00:00', '2026-01-01 00:00:00');
            """
        )


async def test_002_migration_of_existing_db_is_idempotent(tmp_path: Path) -> None:
    path = db_path(tmp_path)
    _legacy_schema(path)
    engine = create_database_engine(f"sqlite+aiosqlite:///{path}")
    try:
        await create_schema(engine)
        await create_schema(engine)  # повторное применение безопасно
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        repo = PostRepository(factory)
        post = await repo.get_by_id(7)
        assert post is not None
        assert post.telegram_file_id == "legacy-file"
        assert post.source_draft_id is None
        assert post.schedule_version == 0
        with sqlite3.connect(path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(posts)")}
            indexes = {row[1] for row in db.execute("PRAGMA index_list(posts)")}
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert {"source_draft_id", "schedule_version"} <= columns
        assert "uq_posts_source_draft_id" in indexes
        assert "dialog_states" in tables
    finally:
        await engine.dispose()


# === PM06-R2-003 ===========================================================


async def test_003_future_post_cannot_be_claimed(repo_with_user) -> None:
    repo, user_id, _ = repo_with_user
    post = await repo.create_post(**_post_kwargs(user_id, NOW + timedelta(hours=2)))
    assert await repo.claim_post_for_processing(post.id, now=NOW) is False
    assert (await repo.get_by_id(post.id)).status == PostStatus.PENDING
    assert await repo.claim_post_for_processing(post.id, now=NOW + timedelta(hours=2)) is True


async def test_003_stale_scheduler_job_does_not_publish(repo_with_user) -> None:
    repo, user_id, _ = repo_with_user
    old_time = NOW + timedelta(hours=1)
    new_time = NOW + timedelta(hours=3)
    post = await repo.create_post(**_post_kwargs(user_id, old_time))
    moved = await repo.reschedule_post(post_id=post.id, user_id=user_id, scheduled_at_utc=new_time)
    assert moved is not None
    assert moved.schedule_version == post.schedule_version + 1

    class CountingPublisher:
        def __init__(self) -> None:
            self.calls = 0

        async def publish(self, post) -> None:
            self.calls += 1

    publisher = CountingPublisher()
    service = PublicationService(repo, publisher, now_fn=lambda: old_time)
    assert (
        await service.execute(
            post.id,
            expected_schedule_version=post.schedule_version,
            expected_run_at=old_time,
        )
        is False
    )
    assert publisher.calls == 0
    assert (await repo.get_by_id(post.id)).status == PostStatus.PENDING

    fresh = PublicationService(repo, publisher, now_fn=lambda: new_time)
    assert (
        await fresh.execute(
            post.id,
            expected_schedule_version=moved.schedule_version,
            expected_run_at=new_time,
        )
        is True
    )
    assert publisher.calls == 1
    assert (await repo.get_by_id(post.id)).status == PostStatus.PUBLISHED


async def test_003_reschedule_registration_failure_is_not_silent(repo_with_user) -> None:
    repo, user_id, _ = repo_with_user
    post = await repo.create_post(**_post_kwargs(user_id))

    class FailingScheduler:
        def schedule_post(self, *, post_id: int, run_at: datetime, schedule_version=None) -> None:
            raise RuntimeError("APScheduler offline")

    service = RescheduleService(repo, FailingScheduler(), strict_scheduler=True)
    with pytest.raises(SchedulerRescheduleError):
        await service.reschedule(
            post_id=post.id,
            user_id=user_id,
            scheduled_at_utc=NOW + timedelta(days=3),
        )
    # БД — источник истины: новое время сохранено,Recovery/реконсиляция восстановят задачу.
    stored = await repo.get_by_id(post.id)
    assert stored is not None
    assert stored.scheduled_at_utc == NOW + timedelta(days=3)


async def test_003_scheduler_reconciles_pending_jobs(repo_with_user) -> None:
    repo, user_id, _ = repo_with_user
    # Время берём от реальных часов: иначе задание оказывается просроченным и
    # APScheduler исполняет его сразу, что делает тест зависимым от таймингов.
    planned_at = utc_now() + timedelta(days=2)
    post = await repo.create_post(**_post_kwargs(user_id, planned_at))
    scheduler = SchedulerService(repo)
    scheduler.start()
    try:
        assert scheduler._scheduler.get_job(f"post:{post.id}") is None
        changed = await scheduler.reconcile_pending()
        assert changed == 1
        job = scheduler._scheduler.get_job(f"post:{post.id}")
        assert job is not None
        assert job.next_run_time == post.scheduled_at_utc
        assert await scheduler.reconcile_pending() == 0
    finally:
        scheduler.shutdown()


async def test_003_overdue_post_is_still_publishable(repo_with_user) -> None:
    repo, user_id, _ = repo_with_user
    post = await repo.create_post(**_post_kwargs(user_id, NOW - timedelta(minutes=5)))

    class OkPublisher:
        def __init__(self) -> None:
            self.calls = 0

        async def publish(self, post) -> None:
            self.calls += 1

    publisher = OkPublisher()
    assert await PublicationService(repo, publisher, now_fn=lambda: NOW).execute(post.id) is True
    assert publisher.calls == 1


# === PM06-R2-007 ===========================================================


async def test_007_dialog_state_is_persistent_and_atomic(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        store = DatabaseDialogStore(factory)
        await store.save(1, 2, "WAITING_CONFIRMATION", {"draft_id": "abc"})
        assert await store.get_state(1, 2) == "WAITING_CONFIRMATION"
        assert await store.get_draft(1, 2) == {"draft_id": "abc"}

        engine2 = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
        try:
            store2 = DatabaseDialogStore(create_session_factory(engine2))
            assert await store2.get_state(1, 2) == "WAITING_CONFIRMATION"
            assert await store2.get_draft(1, 2) == {"draft_id": "abc"}
        finally:
            await engine2.dispose()
    finally:
        await engine.dispose()


async def test_007_ttl_cleanup_and_concurrent_updates(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        store = DatabaseDialogStore(factory, ttl_seconds=1, clock=lambda: NOW)
        await store.save(1, 1, "IDLE", {"a": "1"})
        await store.save(2, 2, "IDLE", {"b": "2"})
        # Конкурентные записи одного диалога выполняются уже после истечения TTL
        # первых строк, поэтому обновлённая строка получает новый expires_at.
        later = NOW + timedelta(seconds=2)
        fresh_store = DatabaseDialogStore(factory, ttl_seconds=1, clock=lambda: later)
        await asyncio.gather(
            *[fresh_store.save(3, 3, "WAITING_PHOTO", {"n": str(i)}) for i in range(10)]
        )
        assert await fresh_store.get_state(3, 3) == "WAITING_PHOTO"
        # Черновик не «полузаписан»: число строк = число диалогов, draft читается.
        assert len(await fresh_store.load_all()) == 1
        draft = await fresh_store.get_draft(3, 3)
        # Последняя запись конкурентной группы wins; проверяем целостность пары
        # state+draft, а не конкретный порядок завершения корутин.
        assert set(draft) == {"n"}
        assert draft["n"] in {str(index) for index in range(10)}

        # Очистка удаляет только просроченные строки (1,1) и (2,2).
        assert store.purge_expired(clock=lambda: later) == 2
        assert await store.get_state(1, 1) is None
        assert await store.get_state(2, 2) is None
        assert await fresh_store.get_state(3, 3) == "WAITING_PHOTO"
    finally:
        await engine.dispose()


# === PM06-R2-008 ===========================================================


async def test_008_application_initializes_recovery_once(tmp_path: Path, monkeypatch) -> None:
    from fake_telegram import TEST_TOKEN
    from postmaster import app as app_module
    from postmaster.config import Settings

    recovery_calls: list[object] = []
    missed_calls: list[object] = []
    original_recovery = app_module.RecoveryService
    original_missed = app_module.MissedPostHandler

    def counting_recovery(*args, **kwargs):
        recovery_calls.append(args)
        return original_recovery(*args, **kwargs)

    def counting_missed(*args, **kwargs):
        missed_calls.append(args)
        return original_missed(*args, **kwargs)

    monkeypatch.setattr(app_module, "RecoveryService", counting_recovery)
    monkeypatch.setattr(app_module, "MissedPostHandler", counting_missed)
    settings = Settings(
        bot_token=TEST_TOKEN,
        database_url=f"sqlite+aiosqlite:///{db_path(tmp_path)}",
        log_level="INFO",
    )
    app = app_module.Application.from_settings(settings)
    try:
        assert len(recovery_calls) == 1
        assert len(missed_calls) == 1
        assert app._recovery_service is not None
        assert app._missed_post_handler is not None
    finally:
        await app.shutdown()
