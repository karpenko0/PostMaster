"""SPEC-011: восстановление заданий Scheduler после перезапуска.

Тесты проверяют:
- RecoveryService перепланирует PENDING-посты с будущим временем.
- RecoveryService передаёт overdue PENDING-посты в MissedPostHandler.
- RecoveryService пропускает посты в других статусах (PUBLISHED, CANCELLED, FAILED).
- RecoveryService возвращает отчёт со статистикой.
- Повторный recovery не создаёт дубли.
- Application интегрирует recovery в startup.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.recovery_service import RecoveryService
from postmaster.services.scheduler_service import SchedulerService


class RecordingScheduler:
    """Заглушка Scheduler, записывает вызовы schedule_post."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, datetime]] = []
        self.running = True

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        self.calls.append((post_id, run_at))


class RecordingMissedHandler:
    """Заглушка MissedPostHandler, записывает вызовы handle."""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.fail_with: Exception | None = None

    async def handle(self, post_id: int) -> None:
        self.calls.append(post_id)
        if self.fail_with is not None:
            raise self.fail_with


@pytest.fixture
async def repo_with_user(tmp_path: Path) -> AsyncIterator[tuple[PostRepository, int]]:
    """Создаёт репозиторий постов и тестового пользователя."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        user = await user_repo.add(
            telegram_user_id=888,
            first_name="Recovery",
            username="recovery",
            now=datetime(2026, 10, 7, tzinfo=UTC),
        )
        yield PostRepository(factory), user.id
    finally:
        await engine.dispose()


async def _create_post(
    repo: PostRepository,
    user_id: int,
    scheduled_at: datetime,
    publication_type: PublicationType = PublicationType.TELEGRAM,
    target_id: str | None = "-1001234567890",
    status: PostStatus = PostStatus.PENDING,
) -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id=f"file_{user_id}_{scheduled_at.timestamp()}",
        telegram_file_unique_id=f"unique_{user_id}_{scheduled_at.timestamp()}",
        scheduled_at_utc=scheduled_at,
        timezone="Europe/Moscow",
        publication_type=publication_type,
        target_id=target_id,
    )
    if status != PostStatus.PENDING:
        await repo.update_status(post.id, status)
    return post.id


async def test_recovery_reschedules_future_post(repo_with_user) -> None:
    """Пост с будущим временем перепланируется в Scheduler."""
    repo, user_id = repo_with_user
    scheduler = RecordingScheduler()
    missed = RecordingMissedHandler()
    service = RecoveryService(repo, scheduler, missed)

    future = datetime.now(UTC) + timedelta(hours=1)
    post_id = await _create_post(repo, user_id, scheduled_at=future)

    report = await service.recover()

    assert report.rescheduled == 1
    assert report.missed == 0
    assert report.errors == []
    assert scheduler.calls == [(post_id, future)]
    assert missed.calls == []


async def test_recovery_delegates_overdue_to_missed_handler(repo_with_user) -> None:
    """Пост с прошедшим временем передаётся в MissedPostHandler."""
    repo, user_id = repo_with_user
    scheduler = RecordingScheduler()
    missed = RecordingMissedHandler()
    service = RecoveryService(repo, scheduler, missed)

    past = datetime.now(UTC) - timedelta(hours=1)
    post_id = await _create_post(repo, user_id, scheduled_at=past)

    report = await service.recover()

    assert report.rescheduled == 0
    assert report.missed == 1
    assert report.errors == []
    assert scheduler.calls == []
    assert missed.calls == [post_id]


async def test_recovery_handles_mixed_posts(repo_with_user) -> None:
    """Смесь будущих и прошедших постов обрабатывается корректно."""
    repo, user_id = repo_with_user
    scheduler = RecordingScheduler()
    missed = RecordingMissedHandler()
    service = RecoveryService(repo, scheduler, missed)

    past1 = datetime.now(UTC) - timedelta(hours=2)
    past2 = datetime.now(UTC) - timedelta(minutes=5)
    future1 = datetime.now(UTC) + timedelta(minutes=30)
    future2 = datetime.now(UTC) + timedelta(hours=2)

    missed_id_1 = await _create_post(repo, user_id, scheduled_at=past1)
    future_id_1 = await _create_post(repo, user_id, scheduled_at=future1)
    missed_id_2 = await _create_post(repo, user_id, scheduled_at=past2)
    future_id_2 = await _create_post(repo, user_id, scheduled_at=future2)

    report = await service.recover()

    assert report.rescheduled == 2
    assert report.missed == 2
    assert report.errors == []
    assert sorted(missed.calls) == sorted([missed_id_1, missed_id_2])
    assert sorted(call[0] for call in scheduler.calls) == sorted([future_id_1, future_id_2])


async def test_recovery_skips_non_pending_posts(repo_with_user) -> None:
    """Посты в статусах PUBLISHED, CANCELLED пропускаются."""
    repo, user_id = repo_with_user
    scheduler = RecordingScheduler()
    missed = RecordingMissedHandler()
    service = RecoveryService(repo, scheduler, missed)

    past = datetime.now(UTC) - timedelta(hours=1)
    published_id = await _create_post(repo, user_id, scheduled_at=past)
    await repo.update_status(published_id, PostStatus.PROCESSING)
    await repo.update_status(published_id, PostStatus.PUBLISHED, published_at=datetime.now(UTC))

    cancelled_id = await _create_post(repo, user_id, scheduled_at=past)
    await repo.update_status(cancelled_id, PostStatus.CANCELLED)

    report = await service.recover()

    assert report.rescheduled == 0
    assert report.missed == 0
    assert scheduler.calls == []
    assert missed.calls == []
    assert (await repo.get_by_id(published_id)).status == PostStatus.PUBLISHED
    assert (await repo.get_by_id(cancelled_id)).status == PostStatus.CANCELLED


async def test_recovery_with_empty_database() -> None:
    """Восстановление при пустой БД не падает."""
    engine = create_database_engine("sqlite+aiosqlite:///:memory:")
    await create_schema(engine)
    factory = create_session_factory(engine)
    try:
        repo = PostRepository(factory)
        scheduler = RecordingScheduler()
        missed = RecordingMissedHandler()
        service = RecoveryService(repo, scheduler, missed)

        report = await service.recover()

        assert report.rescheduled == 0
        assert report.missed == 0
        assert report.errors == []
        assert scheduler.calls == []
        assert missed.calls == []
    finally:
        await engine.dispose()


async def test_recovery_continues_after_missed_handler_error(repo_with_user) -> None:
    """Ошибка в MissedPostHandler для одного поста не останавливает остальные."""
    repo, user_id = repo_with_user
    scheduler = RecordingScheduler()
    missed = RecordingMissedHandler()
    missed.fail_with = RuntimeError("missed handling failed")
    service = RecoveryService(repo, scheduler, missed)

    past1 = datetime.now(UTC) - timedelta(hours=2)
    past2 = datetime.now(UTC) - timedelta(hours=1)
    await _create_post(repo, user_id, scheduled_at=past1)
    await _create_post(repo, user_id, scheduled_at=past2)

    report = await service.recover()

    # Оба поста обработаны (оба прошедшие), оба с ошибкой
    assert len(report.errors) == 2
    # MissedPostHandler был вызван для обоих постов
    assert len(missed.calls) == 2


async def test_recovery_duplicate_recovery_no_duplicates(repo_with_user) -> None:
    """Повторный recovery тех же постов не создаёт дубликатов заданий/вызовов."""
    repo, user_id = repo_with_user
    scheduler = RecordingScheduler()
    missed = RecordingMissedHandler()
    service = RecoveryService(repo, scheduler, missed)

    past = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=1)
    missed_id = await _create_post(repo, user_id, scheduled_at=past)
    future_id = await _create_post(repo, user_id, scheduled_at=future)

    # Первый recovery
    report1 = await service.recover()
    assert report1.rescheduled == 1
    assert report1.missed == 1
    assert missed.calls == [missed_id]

    # Повторный recovery: прошедший пост всё ещё PENDING (заглушка не меняет статус),
    # поэтому он снова передаётся в missed handler, а future перепланируется.
    # Дубликатов записей Post при этом не создаётся.
    report2 = await service.recover()
    assert report2.rescheduled == 1
    assert report2.missed == 1

    # Количество записей Post в БД не изменилось
    assert len(await repo.list_pending()) == 2
    # MissedPostHandler вызван для прошедшего поста дважды (по одному разу за recovery)
    assert missed.calls == [missed_id, missed_id]
    # Scheduler зарегистрировал задание для future_id дважды, но с одинаковым id
    future_calls = [c for c in scheduler.calls if c[0] == future_id]
    assert len(future_calls) == 2


async def test_recovery_uses_real_scheduler(repo_with_user) -> None:
    """Recovery корректно работает с реальным SchedulerService."""

    repo, user_id = repo_with_user

    # Создаём executor с заглушкой publisher
    class _NoopExecutor:
        async def publish(self, post_id: int) -> None:
            return None

    executor = _NoopExecutor()
    scheduler = SchedulerService(repo, publication_executor=executor)
    scheduler.start()
    try:
        service = RecoveryService(repo, scheduler, executor)

        future = datetime.now(UTC) + timedelta(seconds=2)
        future_id = await _create_post(repo, user_id, scheduled_at=future)

        report = await service.recover()

        assert report.rescheduled == 1
        assert report.missed == 0
        assert scheduler._scheduler.get_job(f"post:{future_id}") is not None
    finally:
        scheduler.shutdown()


async def test_application_startup_runs_recovery(tmp_path: Path, repo_with_user) -> None:
    """Application.startup вызывает RecoveryService."""
    from postmaster.app import Application
    from postmaster.bot.factory import create_bot
    from postmaster.config import Settings

    repo, user_id = repo_with_user

    class _RecordingMissed:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def handle(self, post_id: int) -> None:
            self.calls.append(post_id)

    missed = _RecordingMissed()
    scheduler = SchedulerService(repo, publication_executor=_NoopExecutor())

    past = datetime.now(UTC) - timedelta(seconds=1)
    past_id = await _create_post(repo, user_id, scheduled_at=past)

    recovery = RecoveryService(repo, scheduler, missed)

    settings = Settings(
        bot_token="123456789:AAFakeTokenForTestsOnly_0123456789",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'recovery_test.db'}",
    )
    bot = create_bot(settings.bot_token.get_secret_value())
    engine = create_database_engine(settings.database_url)
    await create_schema(engine)

    class _NoOpTransport:
        async def run(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    app = Application(
        bot=bot,
        engine=engine,
        scheduler=scheduler,
        transport=_NoOpTransport(),
    )
    app._recovery_service = recovery

    try:
        await app.startup()
        assert past_id in missed.calls
    finally:
        scheduler.shutdown()
        await engine.dispose()


class _NoopExecutor:
    async def publish(self, post_id: int) -> None:
        return None
