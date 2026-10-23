"""SPEC-012: обработка пропущенных публикаций.

Тесты проверяют:
- MissedPostHandler перечитывает Post из БД (БД — источник истины).
- Выполняются только PENDING со статусом.
- Выполняются только посты с scheduled_at_utc <= now.
- scheduled_at_utc не меняется при обработке.
- Не создаются новые записи Post.
- Обрабатываются past/equal/future, cancelled, multiple missed.
- Failure isolation: ошибка одного не блокирует остальные.
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
from postmaster.services.missed_post_handler import MissedPostHandler


class RecordingExecutor:
    """Заглушка PublicationExecutor, записывает вызовы и может падать."""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.fail_for: set[int] = set()

    async def publish(self, post_id: int) -> None:
        self.calls.append(post_id)
        if post_id in self.fail_for:
            raise RuntimeError(f"publication failed for {post_id}")


class MarkPublishedExecutor:
    """Executor, который помечает пост как PUBLISHED."""

    def __init__(self, repo: PostRepository) -> None:
        self._repo = repo
        self.calls: list[int] = []

    async def publish(self, post_id: int) -> None:
        self.calls.append(post_id)
        post = await self._repo.get_by_id(post_id)
        if post is not None and post.status == PostStatus.PENDING:
            await self._repo.update_status(post_id, PostStatus.PROCESSING)
            await self._repo.update_status(
                post_id, PostStatus.PUBLISHED, published_at=datetime.now(UTC)
            )


@pytest.fixture
async def repo_with_user(tmp_path: Path) -> AsyncIterator[tuple[PostRepository, int]]:
    """Создаёт репозиторий постов и тестового пользователя."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        user = await user_repo.add(
            telegram_user_id=999,
            first_name="Missed",
            username="missed",
            now=datetime(2026, 10, 7, tzinfo=UTC),
        )
        yield PostRepository(factory), user.id
    finally:
        await engine.dispose()


async def _create_post(
    repo: PostRepository,
    user_id: int,
    scheduled_at: datetime,
    status: PostStatus = PostStatus.PENDING,
) -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id=f"file_{user_id}_{scheduled_at.timestamp()}",
        telegram_file_unique_id=f"unique_{user_id}_{scheduled_at.timestamp()}",
        scheduled_at_utc=scheduled_at,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    if status != PostStatus.PENDING:
        await repo.update_status(post.id, status)
    return post.id


async def test_missed_handler_executes_past_post(repo_with_user) -> None:
    """Пост с прошедшим временем выполняется."""
    repo, user_id = repo_with_user
    executor = RecordingExecutor()
    handler = MissedPostHandler(repo, executor)

    past = datetime.now(UTC) - timedelta(hours=1)
    post_id = await _create_post(repo, user_id, scheduled_at=past)

    result = await handler.handle(post_id)

    assert result is not None
    assert executor.calls == [post_id]


async def test_missed_handler_skips_future_post(repo_with_user) -> None:
    """Пост с будущим временем пропускается."""
    repo, user_id = repo_with_user
    executor = RecordingExecutor()
    handler = MissedPostHandler(repo, executor)

    future = datetime.now(UTC) + timedelta(hours=1)
    post_id = await _create_post(repo, user_id, scheduled_at=future)

    result = await handler.handle(post_id)

    assert result is None
    assert executor.calls == []


async def test_missed_handler_executes_post_at_equal_time(repo_with_user) -> None:
    """Пост со временем ровно сейчас (равным now) считается пропущенным."""
    repo, user_id = repo_with_user
    executor = RecordingExecutor()
    handler = MissedPostHandler(repo, executor)

    now = datetime.now(UTC)
    post_id = await _create_post(repo, user_id, scheduled_at=now)

    result = await handler.handle(post_id)

    assert result is not None
    assert executor.calls == [post_id]


async def test_missed_handler_skips_cancelled_post(repo_with_user) -> None:
    """Отменённый пост пропускается."""
    repo, user_id = repo_with_user
    executor = RecordingExecutor()
    handler = MissedPostHandler(repo, executor)

    past = datetime.now(UTC) - timedelta(hours=1)
    post_id = await _create_post(repo, user_id, scheduled_at=past, status=PostStatus.CANCELLED)

    result = await handler.handle(post_id)

    assert result is None
    assert executor.calls == []


async def test_missed_handler_skips_published_post(repo_with_user) -> None:
    """Опубликованный пост пропускается."""
    repo, user_id = repo_with_user
    executor = RecordingExecutor()
    handler = MissedPostHandler(repo, executor)

    past = datetime.now(UTC) - timedelta(hours=1)
    post_id = await _create_post(repo, user_id, scheduled_at=past)
    await repo.update_status(post_id, PostStatus.PROCESSING)
    await repo.update_status(post_id, PostStatus.PUBLISHED, published_at=datetime.now(UTC))

    result = await handler.handle(post_id)

    assert result is None
    assert executor.calls == []


async def test_missed_handler_returns_none_for_missing_post(repo_with_user) -> None:
    """Несуществующий пост — возвращается None без ошибки."""
    repo, _ = repo_with_user
    executor = RecordingExecutor()
    handler = MissedPostHandler(repo, executor)

    result = await handler.handle(99999)

    assert result is None
    assert executor.calls == []


async def test_missed_handler_does_not_change_scheduled_at(repo_with_user) -> None:
    """scheduled_at_utc не меняется при обработке."""
    repo, user_id = repo_with_user
    executor = RecordingExecutor()
    handler = MissedPostHandler(repo, executor)

    past = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    post_id = await _create_post(repo, user_id, scheduled_at=past)
    original = await repo.get_by_id(post_id)
    assert original is not None

    await handler.handle(post_id)

    after = await repo.get_by_id(post_id)
    assert after is not None
    assert after.scheduled_at_utc == original.scheduled_at_utc


async def test_missed_handler_does_not_create_new_post(repo_with_user) -> None:
    """Новая запись Post не создаётся при обработке."""
    repo, user_id = repo_with_user
    executor = RecordingExecutor()
    handler = MissedPostHandler(repo, executor)

    past = datetime.now(UTC) - timedelta(hours=1)
    post_id = await _create_post(repo, user_id, scheduled_at=past)

    count_before = len(await repo.list_pending())
    await handler.handle(post_id)
    count_after = len(await repo.list_pending())

    # Количество постов в БД не изменилось (если executor их не помечает)
    assert count_after == count_before
    # Пост всё ещё существует
    assert (await repo.get_by_id(post_id)) is not None


async def test_missed_handler_handle_many_with_mixed_posts(repo_with_user) -> None:
    """handle_many обрабатывает смесь past/future/cancelled."""
    repo, user_id = repo_with_user
    executor = RecordingExecutor()
    handler = MissedPostHandler(repo, executor)

    past1 = datetime.now(UTC) - timedelta(hours=2)
    past2 = datetime.now(UTC) - timedelta(minutes=5)
    future = datetime.now(UTC) + timedelta(hours=1)

    missed_id_1 = await _create_post(repo, user_id, scheduled_at=past1)
    missed_id_2 = await _create_post(repo, user_id, scheduled_at=past2)
    future_id = await _create_post(repo, user_id, scheduled_at=future)
    cancelled_id = await _create_post(
        repo, user_id, scheduled_at=past1, status=PostStatus.CANCELLED
    )

    report = await handler.handle_many([missed_id_1, future_id, cancelled_id, missed_id_2])

    assert sorted(executor.calls) == sorted([missed_id_1, missed_id_2])
    assert report.handled == 2
    assert report.skipped == 2
    assert report.errors == []


async def test_missed_handler_failure_isolation(repo_with_user) -> None:
    """Ошибка обработки одного поста не блокирует остальные."""
    repo, user_id = repo_with_user
    executor = RecordingExecutor()
    handler = MissedPostHandler(repo, executor)

    past1 = datetime.now(UTC) - timedelta(hours=2)
    past2 = datetime.now(UTC) - timedelta(hours=1)
    past3 = datetime.now(UTC) - timedelta(minutes=30)
    failed_id = await _create_post(repo, user_id, scheduled_at=past1)
    ok_id_1 = await _create_post(repo, user_id, scheduled_at=past2)
    ok_id_2 = await _create_post(repo, user_id, scheduled_at=past3)

    executor.fail_for.add(failed_id)

    report = await handler.handle_many([failed_id, ok_id_1, ok_id_2])

    assert report.handled == 2
    assert report.errors == [{"post_id": failed_id, "error": "RuntimeError"}]
    assert sorted(executor.calls) == sorted([failed_id, ok_id_1, ok_id_2])


async def test_missed_handler_find_missed(repo_with_user) -> None:
    """find_missed возвращает id PENDING постов с прошедшим временем."""
    repo, user_id = repo_with_user
    handler = MissedPostHandler(repo, RecordingExecutor())

    past1 = datetime.now(UTC) - timedelta(hours=2)
    past2 = datetime.now(UTC) - timedelta(hours=1)
    future = datetime.now(UTC) + timedelta(hours=1)

    missed_id_1 = await _create_post(repo, user_id, scheduled_at=past1)
    missed_id_2 = await _create_post(repo, user_id, scheduled_at=past2)
    future_id = await _create_post(repo, user_id, scheduled_at=future)
    cancelled_id = await _create_post(
        repo, user_id, scheduled_at=past1, status=PostStatus.CANCELLED
    )

    found = await handler.find_missed()

    assert sorted(found) == sorted([missed_id_1, missed_id_2])
    assert future_id not in found
    assert cancelled_id not in found


async def test_missed_handler_updates_status_on_success(repo_with_user) -> None:
    """При успешной публикации пост переходит в PUBLISHED."""
    repo, user_id = repo_with_user
    executor = MarkPublishedExecutor(repo)
    handler = MissedPostHandler(repo, executor)

    past = datetime.now(UTC) - timedelta(hours=1)
    post_id = await _create_post(repo, user_id, scheduled_at=past)

    await handler.handle(post_id)

    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.PUBLISHED
    assert result.published_at is not None


async def test_missed_handler_with_empty_post_ids(repo_with_user) -> None:
    """handle_many с пустым списком возвращает пустой отчёт."""
    repo, _ = repo_with_user
    executor = RecordingExecutor()
    handler = MissedPostHandler(repo, executor)

    report = await handler.handle_many([])

    assert report.handled == 0
    assert report.skipped == 0
    assert report.errors == []
    assert executor.calls == []
