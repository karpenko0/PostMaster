"""SPEC-013: атомарный захват Post, защита от двойной публикации, гонки и E2E-сценарии."""

import asyncio
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from harness import wait_until
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import (
    Post,
    PostStatus,
    PublicationErrorCode,
    PublicationType,
    TemporaryPublicationError,
)
from postmaster.publishers.base import Publisher
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.post_service import PostService
from postmaster.services.publication_service import PublicationService
from postmaster.services.scheduler_service import SchedulerService

CREATED_AT = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 10, 15, 15, 0, tzinfo=UTC)
CLAIM_AT = datetime(2026, 10, 15, 15, 0, 1, tzinfo=UTC)
PUBLISHED_AT = datetime(2026, 10, 15, 15, 0, 2, tzinfo=UTC)


class RecordingPublisher(Publisher):
    """Тестовый Publisher: записывает вызовы и статус Post в момент публикации."""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        errors: list[BaseException] | None = None,
        on_publish: asyncio.Event | None = None,
        proceed: asyncio.Event | None = None,
    ) -> None:
        self.calls: list[Post] = []
        self.statuses_seen: list[PostStatus] = []
        self._delay = delay
        self._errors = list(errors or [])
        self._on_publish = on_publish
        self._proceed = proceed

    async def publish(self, post: Post) -> None:
        self.calls.append(post)
        self.statuses_seen.append(post.status)
        if self._on_publish is not None:
            self._on_publish.set()
        if self._proceed is not None:
            await asyncio.wait_for(self._proceed.wait(), timeout=2.0)
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if self._errors:
            raise self._errors.pop(0)


@pytest.fixture
async def spec013_db(
    tmp_path: Path,
) -> AsyncIterator[tuple[Path, AsyncEngine, async_sessionmaker[AsyncSession], PostRepository, int]]:
    path = tmp_path / "spec013.db"
    engine = create_database_engine(f"sqlite+aiosqlite:///{path}")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user = await UserRepository(factory).add(
            telegram_user_id=50001,
            username="spec013_user",
            first_name="Иван",
            now=CREATED_AT,
        )
        yield path, engine, factory, PostRepository(factory), user.id
    finally:
        await engine.dispose()


async def _create_post(
    repo: PostRepository,
    user_id: int,
    *,
    publication_type: PublicationType = PublicationType.REMINDER,
    target_id: str | None = None,
    scheduled_at_utc: datetime = SCHEDULED_AT,
    now: datetime = CREATED_AT,
) -> Post:
    return await repo.create_post(
        user_id=user_id,
        telegram_file_id="AgAC_spec013_photo",
        telegram_file_unique_id="AQAD_spec013_unique",
        scheduled_at_utc=scheduled_at_utc,
        timezone="Europe/Moscow",
        publication_type=publication_type,
        target_id=target_id,
        now=now,
    )


# ---------------------------------------------------------------------------
# §49 Unit Tests (TC-013-01 … TC-013-04) & §48 Acceptance Criteria (AC-013-01…05)
# ---------------------------------------------------------------------------


async def test_tc_013_01_claim_pending_post_succeeds_and_moves_to_processing(
    spec013_db, caplog: pytest.LogCaptureFixture
) -> None:
    """TC-013-01, AC-013-01, AC-013-11, §43: PENDING → PROCESSING, возвращает True."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)

    with caplog.at_level("INFO", logger="postmaster.repositories.post_repository"):
        claimed = await repo.claim_post_for_processing(post.id, now=CLAIM_AT)

    assert claimed is True
    loaded = await repo.get_by_id(post.id)
    assert loaded is not None
    assert loaded.status == PostStatus.PROCESSING
    assert loaded.updated_at == CLAIM_AT
    assert loaded.created_at == CREATED_AT
    assert f"Post claimed for processing post_id={post.id}" in caplog.text


async def test_tc_013_02_claim_processing_post_returns_false(
    spec013_db, caplog: pytest.LogCaptureFixture
) -> None:
    """TC-013-02, AC-013-02, RT-013-02, §43: повторный claim на PROCESSING возвращает False."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    assert await repo.claim_post_for_processing(post.id, now=CLAIM_AT) is True

    later = CLAIM_AT + timedelta(seconds=10)
    with caplog.at_level("INFO", logger="postmaster.repositories.post_repository"):
        second_claim = await repo.claim_post_for_processing(post.id, now=later)

    assert second_claim is False
    loaded = await repo.get_by_id(post.id)
    assert loaded is not None
    assert loaded.status == PostStatus.PROCESSING
    assert loaded.updated_at == CLAIM_AT
    assert f"Post claim skipped post_id={post.id}" in caplog.text


async def test_tc_013_03_claim_published_post_returns_false(spec013_db) -> None:
    """TC-013-03, AC-013-03, BR-013-08, INV-013-04: PUBLISHED не переводится в PROCESSING."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    await repo.claim_post_for_processing(post.id, now=CLAIM_AT)
    await repo.mark_published(post.id, PUBLISHED_AT)

    later = PUBLISHED_AT + timedelta(minutes=5)
    assert await repo.claim_post_for_processing(post.id, now=later) is False
    loaded = await repo.get_by_id(post.id)
    assert loaded is not None
    assert loaded.status == PostStatus.PUBLISHED
    assert loaded.published_at == PUBLISHED_AT
    assert loaded.updated_at == PUBLISHED_AT


async def test_tc_013_04_claim_cancelled_post_returns_false(spec013_db) -> None:
    """TC-013-04, AC-013-04, BR-013-09, INV-013-05: CANCELLED не переводится в PROCESSING."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    assert await repo.cancel_post(post.id, user_id, now=CLAIM_AT) is True

    later = CLAIM_AT + timedelta(minutes=1)
    assert await repo.claim_post_for_processing(post.id, now=later) is False
    loaded = await repo.get_by_id(post.id)
    assert loaded is not None
    assert loaded.status == PostStatus.CANCELLED
    assert loaded.updated_at == CLAIM_AT


async def test_ac_013_05_claim_failed_post_returns_false_and_missing_post_returns_false(
    spec013_db,
) -> None:
    """AC-013-05, ERR-013-01: FAILED и несуществующий Post не захватываются."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    await repo.claim_post_for_processing(post.id, now=CLAIM_AT)
    await repo.mark_failed(post.id, "Permanent failure", now=PUBLISHED_AT)

    later = PUBLISHED_AT + timedelta(minutes=1)
    assert await repo.claim_post_for_processing(post.id, now=later) is False
    assert (await repo.get_by_id(post.id)).status == PostStatus.FAILED
    assert await repo.claim_post_for_processing(post.id + 9999, now=CLAIM_AT) is False


# ---------------------------------------------------------------------------
# §50 Repository Tests (RT-013-01 … RT-013-05) & §40 Single Atomic SQL
# ---------------------------------------------------------------------------


async def test_rt_013_01_atomic_claim_executes_single_conditional_update_without_select(
    spec013_db,
) -> None:
    """RT-013-01, §14, §16, §40: claim выполняет один UPDATE без предварительного SELECT."""
    _, engine, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement: str, _parameters, _context, _executemany) -> None:
        normalized = statement.strip().upper()
        if normalized.startswith(("SELECT", "UPDATE", "INSERT", "DELETE")):
            statements.append(normalized)

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        claimed = await repo.claim_post_for_processing(post.id, now=CLAIM_AT)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    assert claimed is True
    assert len(statements) == 1
    assert statements[0].startswith("UPDATE POSTS SET")
    assert "WHERE POSTS.ID =" in statements[0] and "POSTS.STATUS =" in statements[0]


async def test_rt_013_03_mark_published_transitions_from_processing_and_clears_error(
    spec013_db, caplog: pytest.LogCaptureFixture
) -> None:
    """RT-013-03, §21–24, INV-013-06: PROCESSING → PUBLISHED, published_at, error_message=NULL."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    # Имитируем предыдущую временную ошибку, затем новый захват и успех.
    await repo.claim_post_for_processing(post.id, now=CLAIM_AT)
    await repo.release_post_for_retry(post.id, "Temporary glitch", now=CLAIM_AT)
    reclaimed_at = CLAIM_AT + timedelta(seconds=5)
    assert await repo.claim_post_for_processing(post.id, now=reclaimed_at) is True

    with caplog.at_level("INFO", logger="postmaster.repositories.post_repository"):
        updated = await repo.mark_published(post.id, PUBLISHED_AT)

    assert updated is True
    published = await repo.get_by_id(post.id)
    assert published is not None
    assert published.status == PostStatus.PUBLISHED
    assert published.published_at == PUBLISHED_AT
    assert published.updated_at == PUBLISHED_AT
    assert published.error_message is None
    assert f"Post published post_id={post.id}" in caplog.text

    # INV-013-06: mark_published работает только из PROCESSING.
    pending_post = await _create_post(repo, user_id)
    assert await repo.mark_published(pending_post.id, PUBLISHED_AT) is False
    assert (await repo.get_by_id(pending_post.id)).status == PostStatus.PENDING


async def test_rt_013_04_mark_failed_transitions_from_processing_and_stores_error(
    spec013_db, caplog: pytest.LogCaptureFixture
) -> None:
    """RT-013-04, §25–27, §45, AC-013-09: PROCESSING → FAILED с сохранением error_message."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    await repo.claim_post_for_processing(post.id, now=CLAIM_AT)

    with caplog.at_level("ERROR", logger="postmaster.repositories.post_repository"):
        updated = await repo.mark_failed(post.id, "Chat not found", now=PUBLISHED_AT)

    assert updated is True
    failed = await repo.get_by_id(post.id)
    assert failed is not None
    assert failed.status == PostStatus.FAILED
    assert failed.error_message == "Chat not found"
    assert failed.published_at is None
    assert failed.updated_at == PUBLISHED_AT
    assert f"Post publication failed post_id={post.id}" in caplog.text

    # Без PROCESSING mark_failed не изменяет запись.
    pending_post = await _create_post(repo, user_id)
    assert await repo.mark_failed(pending_post.id, "Should not apply", now=PUBLISHED_AT) is False
    assert (await repo.get_by_id(pending_post.id)).status == PostStatus.PENDING

    with pytest.raises(ValueError, match="error_message"):
        await repo.mark_failed(post.id, "   ")


async def test_rt_013_05_release_post_for_retry_transitions_from_processing_to_pending(
    spec013_db, caplog: pytest.LogCaptureFixture
) -> None:
    """RT-013-05, §28–30, §46, AC-013-10: PROCESSING → PENDING при временной ошибке."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    await repo.claim_post_for_processing(post.id, now=CLAIM_AT)

    retry_at = CLAIM_AT + timedelta(seconds=3)
    with caplog.at_level("WARNING", logger="postmaster.repositories.post_repository"):
        released = await repo.release_post_for_retry(
            post.id, "Telegram 429 Too Many Requests", now=retry_at
        )

    assert released is True
    pending_again = await repo.get_by_id(post.id)
    assert pending_again is not None
    assert pending_again.status == PostStatus.PENDING
    assert pending_again.error_message == "Telegram 429 Too Many Requests"
    assert pending_again.published_at is None
    assert pending_again.updated_at == retry_at
    assert f"Post returned to pending for retry post_id={post.id}" in caplog.text

    # Если пост уже PUBLISHED, release_post_for_retry его не возвращает в PENDING.
    await repo.claim_post_for_processing(post.id, now=PUBLISHED_AT)
    await repo.mark_published(post.id, PUBLISHED_AT)
    assert await repo.release_post_for_retry(post.id, "late retry") is False
    assert (await repo.get_by_id(post.id)).status == PostStatus.PUBLISHED


# ---------------------------------------------------------------------------
# §47 Ошибки PublicationService (ERR-013-01, ERR-013-02, ERR-013-03)
# ---------------------------------------------------------------------------


async def test_err_013_01_claim_failed_skips_get_by_id_and_publisher(
    spec013_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ERR-013-01, BR-013-01/03, INV-013-01: при claim=False ни get_by_id, ни Publisher не ждут."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    await repo.cancel_post(post.id, user_id, now=CLAIM_AT)

    get_calls: list[int] = []
    original_get = repo.get_by_id

    async def spy_get(post_id: int) -> Post | None:
        get_calls.append(post_id)
        return await original_get(post_id)

    monkeypatch.setattr(repo, "get_by_id", spy_get)
    publisher = RecordingPublisher()
    service = PublicationService(repo, publisher)

    result = await service.execute(post.id)

    assert result is False
    assert get_calls == []
    assert publisher.calls == []


async def test_err_013_02_post_not_found_after_claim_is_logged_and_skips_publisher(
    spec013_db, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ERR-013-02: после успешного claim отсутствие Post логируется, Publisher не вызывается."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)

    async def missing_after_claim(_post_id: int) -> Post | None:
        return None

    monkeypatch.setattr(repo, "get_by_id", missing_after_claim)
    publisher = RecordingPublisher()
    service = PublicationService(repo, publisher)

    with caplog.at_level("ERROR", logger="postmaster.services.publication_service"):
        result = await service.execute(post.id)

    assert result is False
    assert publisher.calls == []
    assert PublicationErrorCode.POST_NOT_FOUND_AFTER_CLAIM.value in caplog.text
    assert f"post_id={post.id}" in caplog.text


async def test_err_013_03_final_status_update_failure_is_logged_without_republishing(
    spec013_db, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ERR-013-03: сбой финального обновления статуса после Publisher критически логируется."""
    _, _, _, repo, user_id = spec013_db
    post1 = await _create_post(repo, user_id)
    post2 = await _create_post(repo, user_id)
    publisher = RecordingPublisher()
    service = PublicationService(repo, publisher)

    async def return_false(_post_id: int, *_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(repo, "mark_published", return_false)
    with caplog.at_level("ERROR", logger="postmaster.services.publication_service"):
        res1 = await service.execute(post1.id)

    assert res1 is False
    assert len(publisher.calls) == 1
    assert PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value in caplog.text

    caplog.clear()

    async def raise_db_error(_post_id: int, *_args, **_kwargs) -> bool:
        raise RuntimeError("DB write lost")

    monkeypatch.setattr(repo, "mark_published", raise_db_error)
    with caplog.at_level("ERROR", logger="postmaster.services.publication_service"):
        res2 = await service.execute(post2.id)

    assert res2 is False
    assert len(publisher.calls) == 2
    assert PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value in caplog.text


async def test_publication_service_resolves_publisher_by_publication_type(
    spec013_db,
) -> None:
    """§20: PublicationService выбирает нужный Publisher по publication_type."""
    _, _, _, repo, user_id = spec013_db
    reminder_post = await _create_post(repo, user_id, publication_type=PublicationType.REMINDER)
    telegram_post = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-100999888777",
    )
    reminder_pub = RecordingPublisher()
    telegram_pub = RecordingPublisher()
    service = PublicationService(
        repo,
        publishers={
            PublicationType.REMINDER: reminder_pub,
            PublicationType.TELEGRAM: telegram_pub,
        },
        now_fn=lambda: PUBLISHED_AT,
    )

    assert await service.execute(reminder_post.id) is True
    assert await service.execute(telegram_post.id) is True

    assert [p.id for p in reminder_pub.calls] == [reminder_post.id]
    assert [p.id for p in telegram_pub.calls] == [telegram_post.id]
    assert (await repo.get_by_id(reminder_post.id)).status == PostStatus.PUBLISHED
    assert (await repo.get_by_id(telegram_post.id)).status == PostStatus.PUBLISHED


# ---------------------------------------------------------------------------
# §51 & §52 Concurrency Tests (CT-013-01 & PublicationService)
# ---------------------------------------------------------------------------


async def test_ct_013_01_two_workers_concurrent_claim_only_one_succeeds(
    spec013_db,
) -> None:
    """CT-013-01, AC-013-06: два (и более) одновременных claim — ровно один получает True."""
    db_file, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)

    # 1) Конкурентные корутины в одном event loop.
    results = await asyncio.gather(
        repo.claim_post_for_processing(post.id, now=CLAIM_AT),
        repo.claim_post_for_processing(post.id, now=CLAIM_AT),
    )
    assert sorted(results) == [False, True]
    assert (await repo.get_by_id(post.id)).status == PostStatus.PROCESSING

    # 2) Конкурентные потоки ОС с отдельными движками SQLite и барьером синхронизации.
    post_threads = await _create_post(repo, user_id)
    barrier = threading.Barrier(2)
    db_url = f"sqlite+aiosqlite:///{db_file}"

    def thread_claim() -> bool:
        async def _run() -> bool:
            engine = create_database_engine(db_url)
            try:
                thread_repo = PostRepository(create_session_factory(engine))
                barrier.wait(timeout=5)
                return await thread_repo.claim_post_for_processing(post_threads.id, now=CLAIM_AT)
            finally:
                await engine.dispose()

        return asyncio.run(_run())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(thread_claim) for _ in range(2)]
        thread_results = [f.result(timeout=10) for f in futures]

    assert sorted(thread_results) == [False, True]
    assert (await repo.get_by_id(post_threads.id)).status == PostStatus.PROCESSING


async def test_ct_013_02_publication_service_concurrent_execution_calls_publisher_once(
    spec013_db,
) -> None:
    """§52, AC-013-07, INV-013-07: два потока вызывают execute — Publisher вызывается 1 раз."""
    db_file, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    publisher = RecordingPublisher(delay=0.05)
    service = PublicationService(repo, publisher, now_fn=lambda: PUBLISHED_AT)

    # 1) Параллельные asyncio-задачи.
    outcomes = await asyncio.gather(
        service.execute(post.id),
        service.execute(post.id),
    )
    assert sorted(outcomes) == [False, True]
    assert len(publisher.calls) == 1
    assert publisher.statuses_seen == [PostStatus.PROCESSING]
    assert (await repo.get_by_id(post.id)).status == PostStatus.PUBLISHED

    # 2) Два реальных потока ОС одновременно вызывают publication_service.execute(post_id).
    post2 = await _create_post(repo, user_id)
    thread_publisher = RecordingPublisher()
    barrier = threading.Barrier(2)
    db_url = f"sqlite+aiosqlite:///{db_file}"

    def thread_execute() -> bool:
        async def _run() -> bool:
            engine = create_database_engine(db_url)
            try:
                thread_repo = PostRepository(create_session_factory(engine))
                thread_service = PublicationService(
                    thread_repo, thread_publisher, now_fn=lambda: PUBLISHED_AT
                )
                barrier.wait(timeout=5)
                return await thread_service.execute(post2.id)
            finally:
                await engine.dispose()

        return asyncio.run(_run())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(thread_execute) for _ in range(2)]
        thread_outcomes = [f.result(timeout=10) for f in futures]

    assert sorted(thread_outcomes) == [False, True]
    assert len(thread_publisher.calls) == 1
    assert (await repo.get_by_id(post2.id)).status == PostStatus.PUBLISHED


# ---------------------------------------------------------------------------
# §53–56 Integration Tests (Scheduler, Recovery, Missed Job, Cancel)
# ---------------------------------------------------------------------------


async def test_it_013_53_scheduler_and_manual_execution_publish_only_once(
    spec013_db,
) -> None:
    """§53 Integration Test с Scheduler: Scheduler + ручной запуск → Publisher ровно 1 раз."""
    _, _, _, repo, user_id = spec013_db
    started = asyncio.Event()
    proceed = asyncio.Event()
    publisher = RecordingPublisher(on_publish=started, proceed=proceed)
    pub_service = PublicationService(repo, publisher, now_fn=lambda: PUBLISHED_AT)
    scheduler = SchedulerService(repo, pub_service)
    scheduler.start()
    try:
        run_at = datetime.now(UTC) + timedelta(milliseconds=50)
        post = await PostService(repo, scheduler).create_scheduled_post(
            user_id=user_id,
            telegram_file_id="AgAC_sched",
            telegram_file_unique_id="AQAD_sched",
            scheduled_at_utc=run_at,
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
        )
        # Ждём, пока Scheduler захватит пост и войдёт в Publisher.
        await asyncio.wait_for(started.wait(), timeout=5)
        # Вручную запускаем второй execution во время работы первого.
        manual_result = await pub_service.execute(post.id)
        proceed.set()
        await wait_until(lambda: scheduler._scheduler.get_job(f"post:{post.id}") is None)
        # Дадим корутине задания завершиться.
        await asyncio.sleep(0.05)

        assert manual_result is False
        assert len(publisher.calls) == 1
        final_post = await repo.get_by_id(post.id)
        assert final_post is not None
        assert final_post.status == PostStatus.PUBLISHED
        assert final_post.published_at == PUBLISHED_AT
    finally:
        scheduler.shutdown()


async def test_it_013_54_scheduler_and_recovery_parallel_execution_publish_only_once(
    spec013_db,
) -> None:
    """§54 Integration Test с Recovery, US-013-02: Scheduler job + Recovery → 1 публикация."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    publisher = RecordingPublisher(delay=0.03)
    pub_service = PublicationService(repo, publisher, now_fn=lambda: PUBLISHED_AT)
    scheduler = SchedulerService(repo, pub_service)

    async def recovery_worker() -> None:
        # Recovery инициирует выполнение всех найденных PENDING-постов или конкретного post_id.
        await pub_service.execute(post.id)

    await asyncio.gather(
        scheduler._on_post_due(post.id),
        recovery_worker(),
    )

    assert len(publisher.calls) == 1
    assert (await repo.get_by_id(post.id)).status == PostStatus.PUBLISHED

    # US-013-02: если Post уже PROCESSING, Recovery не видит его в list_pending и не публикует.
    in_flight = await _create_post(repo, user_id)
    assert await repo.claim_post_for_processing(in_flight.id, now=CLAIM_AT) is True
    assert [p.id for p in await repo.list_pending()] == []
    assert await pub_service.execute(in_flight.id) is False
    assert len(publisher.calls) == 1


async def test_it_013_55_missed_job_and_scheduler_simultaneous_execution_publish_only_once(
    spec013_db,
) -> None:
    """§55 Integration Test с Missed Job: MissedJobService и Scheduler дают 1 публикацию."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    publisher = RecordingPublisher(delay=0.03)
    pub_service = PublicationService(repo, publisher, now_fn=lambda: PUBLISHED_AT)
    scheduler = SchedulerService(repo, pub_service)

    async def missed_job_runner(post_id: int) -> bool:
        return await pub_service.execute(post_id)

    await asyncio.gather(
        missed_job_runner(post.id),
        scheduler._on_post_due(post.id),
    )

    assert len(publisher.calls) == 1
    final_post = await repo.get_by_id(post.id)
    assert final_post is not None
    assert final_post.status == PostStatus.PUBLISHED


async def test_it_013_56_cancel_and_claim_race_allows_only_one_transition(
    spec013_db,
) -> None:
    """§56 Integration Test с Cancel, AC-013-12: гонка claim и cancel даёт только 1 переход."""
    _, _, _, repo, user_id = spec013_db

    # Сценарий A: одновременный claim и cancel из PENDING.
    post_race = await _create_post(repo, user_id)
    claim_ok, cancel_ok = await asyncio.gather(
        repo.claim_post_for_processing(post_race.id, now=CLAIM_AT),
        repo.cancel_post(post_race.id, user_id, now=CLAIM_AT),
    )
    assert sorted([claim_ok, cancel_ok]) == [False, True]
    current = await repo.get_by_id(post_race.id)
    assert current is not None
    if claim_ok:
        assert current.status == PostStatus.PROCESSING
    else:
        assert current.status == PostStatus.CANCELLED

    # Сценарий B: пока Publisher выполняется (статус уже PROCESSING), cancel возвращает False.
    post_in_flight = await _create_post(repo, user_id)
    in_publisher = asyncio.Event()
    allow_finish = asyncio.Event()
    publisher = RecordingPublisher(on_publish=in_publisher, proceed=allow_finish)
    pub_service = PublicationService(repo, publisher, now_fn=lambda: PUBLISHED_AT)

    exec_task = asyncio.create_task(pub_service.execute(post_in_flight.id))
    await asyncio.wait_for(in_publisher.wait(), timeout=5)

    assert (await repo.get_by_id(post_in_flight.id)).status == PostStatus.PROCESSING
    assert await repo.cancel_post(post_in_flight.id, user_id, now=PUBLISHED_AT) is False
    assert (await repo.get_by_id(post_in_flight.id)).status == PostStatus.PROCESSING

    allow_finish.set()
    assert await exec_task is True
    assert (await repo.get_by_id(post_in_flight.id)).status == PostStatus.PUBLISHED


# ---------------------------------------------------------------------------
# §57–60 E2E Scenarios
# ---------------------------------------------------------------------------


async def test_e2e_57_normal_publication_and_idempotent_repeat(spec013_db) -> None:
    """§57 E2E — нормальная публикация: PENDING → PROCESSING → PUBLISHED, повтор пропускается."""
    _, _, _, repo, user_id = spec013_db
    publisher = RecordingPublisher()
    pub_service = PublicationService(repo, publisher, now_fn=lambda: PUBLISHED_AT)
    scheduler = SchedulerService(repo, pub_service)
    post = await _create_post(repo, user_id)
    assert post.status == PostStatus.PENDING

    # Наступает время публикации.
    await scheduler._on_post_due(post.id)

    assert len(publisher.calls) == 1
    assert publisher.statuses_seen == [PostStatus.PROCESSING]
    published = await repo.get_by_id(post.id)
    assert published is not None
    assert published.status == PostStatus.PUBLISHED
    assert published.published_at == PUBLISHED_AT
    assert published.error_message is None

    # Повторный execution ничего не публикует.
    assert await pub_service.execute(post.id) is False
    await scheduler._on_post_due(post.id)
    assert len(publisher.calls) == 1


async def test_e2e_58_two_triggers_scheduler_wins_recovery_skips(spec013_db) -> None:
    """§58 E2E-сценарий — два запуска: Scheduler получает claim, Recovery получает False."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    started = asyncio.Event()
    finish = asyncio.Event()
    publisher = RecordingPublisher(on_publish=started, proceed=finish)
    pub_service = PublicationService(repo, publisher, now_fn=lambda: PUBLISHED_AT)

    scheduler_task = asyncio.create_task(pub_service.execute(post.id))
    await asyncio.wait_for(started.wait(), timeout=5)

    # Recovery почти одновременно вызывает execution того же поста.
    recovery_claimed = await pub_service.execute(post.id)
    finish.set()
    scheduler_claimed = await scheduler_task

    assert scheduler_claimed is True
    assert recovery_claimed is False
    assert len(publisher.calls) == 1
    final_post = await repo.get_by_id(post.id)
    assert final_post is not None
    assert final_post.status == PostStatus.PUBLISHED
    assert final_post.published_at == PUBLISHED_AT


async def test_e2e_59_permanent_error_marks_failed_and_prevents_rerun(
    spec013_db,
) -> None:
    """§59 E2E-сценарий — ошибка: PENDING → PROCESSING → FAILED, повторно не выполняется."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    publisher = RecordingPublisher(errors=[RuntimeError("Forbidden: bot was kicked from channel")])
    pub_service = PublicationService(repo, publisher, now_fn=lambda: PUBLISHED_AT)
    scheduler = SchedulerService(repo, pub_service)

    await scheduler._on_post_due(post.id)

    assert len(publisher.calls) == 1
    assert publisher.statuses_seen == [PostStatus.PROCESSING]
    failed = await repo.get_by_id(post.id)
    assert failed is not None
    assert failed.status == PostStatus.FAILED
    assert failed.error_message == "Forbidden: bot was kicked from channel"
    assert failed.published_at is None
    assert failed.updated_at == PUBLISHED_AT

    # Обычный Scheduler повторно его не выполняет.
    await scheduler._on_post_due(post.id)
    assert await pub_service.execute(post.id) is False
    assert len(publisher.calls) == 1


async def test_e2e_60_temporary_error_returns_to_pending_and_succeeds_on_next_claim(
    spec013_db,
) -> None:
    """§60 E2E — retry: PENDING → PROCESSING → временная ошибка → PENDING → claim → PUBLISHED."""
    _, _, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    publisher = RecordingPublisher(errors=[TemporaryPublicationError("Telegram API timeout")])
    current_time = CLAIM_AT
    pub_service = PublicationService(repo, publisher, now_fn=lambda: current_time)

    # Первая попытка: временная ошибка переводит PROCESSING → PENDING.
    first_result = await pub_service.execute(post.id)
    assert first_result is False
    assert len(publisher.calls) == 1
    after_temp_error = await repo.get_by_id(post.id)
    assert after_temp_error is not None
    assert after_temp_error.status == PostStatus.PENDING
    assert after_temp_error.error_message == "Telegram API timeout"
    assert after_temp_error.published_at is None
    assert after_temp_error.updated_at == CLAIM_AT

    # Следующая попытка снова проходит через atomic claim и успешно публикует пост.
    current_time = PUBLISHED_AT
    second_result = await pub_service.execute(post.id)
    assert second_result is True
    assert len(publisher.calls) == 2
    assert publisher.statuses_seen == [PostStatus.PROCESSING, PostStatus.PROCESSING]
    final_post = await repo.get_by_id(post.id)
    assert final_post is not None
    assert final_post.status == PostStatus.PUBLISHED
    assert final_post.published_at == PUBLISHED_AT
    assert final_post.updated_at == PUBLISHED_AT
    assert final_post.error_message is None


async def test_custom_retry_policy_and_missing_publisher_handling(
    spec013_db,
) -> None:
    """Пользовательский предикат временной ошибки (SPEC-022) и отсутствие Publisher."""
    _, _, _, repo, user_id = spec013_db
    post_retry = await _create_post(repo, user_id)
    publisher = RecordingPublisher(errors=[TimeoutError("network timeout")])
    service_with_policy = PublicationService(
        repo,
        publisher,
        is_temporary_error=lambda exc: isinstance(exc, TimeoutError),
        now_fn=lambda: PUBLISHED_AT,
    )

    assert await service_with_policy.execute_post(post_retry.id) is False
    requeued = await repo.get_by_id(post_retry.id)
    assert requeued is not None and requeued.status == PostStatus.PENDING
    assert requeued.error_message == "network timeout"

    # Без зарегистрированного Publisher пост переводится в FAILED, а не зависает в PROCESSING.
    post_no_pub = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-100123456",
    )
    service_only_reminder = PublicationService(
        repo,
        publishers={PublicationType.REMINDER: RecordingPublisher()},
        now_fn=lambda: PUBLISHED_AT,
    )
    assert await service_only_reminder.execute(post_no_pub.id) is False
    failed = await repo.get_by_id(post_no_pub.id)
    assert failed is not None
    assert failed.status == PostStatus.FAILED
    assert failed.error_message is not None and "TELEGRAM" in failed.error_message

    with pytest.raises(RuntimeError, match="PostRepository"):
        await PublicationService().execute(post_retry.id)


async def test_all_status_mutations_use_single_conditional_update_and_reject_invalid_sources(
    spec013_db,
) -> None:
    """§23, §27, §30, INV-013-06: все переходы — один UPDATE с проверкой исходного статуса."""
    _, engine, _, repo, user_id = spec013_db
    post = await _create_post(repo, user_id)
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement: str, _parameters, _context, _executemany) -> None:
        normalized = statement.strip().upper()
        if normalized.startswith(("SELECT", "UPDATE", "INSERT", "DELETE")):
            statements.append(normalized)

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        assert await repo.claim_post_for_processing(post.id, now=CLAIM_AT) is True
        assert await repo.release_post_for_retry(post.id, "temp", now=CLAIM_AT) is True
        assert await repo.claim_post_for_processing(post.id, now=CLAIM_AT) is True
        assert await repo.mark_published(post.id, PUBLISHED_AT) is True
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    assert len(statements) == 4
    assert all(stmt.startswith("UPDATE POSTS SET") for stmt in statements)

    # Из терминальных статусов нельзя вызвать mark_published, mark_failed, release_post_for_retry.
    cancelled_post = await _create_post(repo, user_id)
    assert await repo.cancel_post(cancelled_post.id, user_id, now=CLAIM_AT) is True

    failed_post = await _create_post(repo, user_id)
    assert await repo.claim_post_for_processing(failed_post.id, now=CLAIM_AT) is True
    assert await repo.mark_failed(failed_post.id, "fatal", now=PUBLISHED_AT) is True

    for terminal_id in (post.id, cancelled_post.id, failed_post.id):
        before = await repo.get_by_id(terminal_id)
        assert await repo.mark_published(terminal_id, PUBLISHED_AT + timedelta(minutes=1)) is False
        assert await repo.mark_failed(terminal_id, "other error") is False
        assert await repo.release_post_for_retry(terminal_id, "retry") is False
        assert await repo.get_by_id(terminal_id) == before


async def test_empty_exception_message_and_error_transition_update_failures(
    spec013_db, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC-013-09, ERR-013-03: пустое сообщение ошибки заменяется именем класса; сбои логируются."""
    _, _, _, repo, user_id = spec013_db
    post_empty_err = await _create_post(repo, user_id)
    publisher = RecordingPublisher(errors=[RuntimeError()])
    service = PublicationService(repo, publisher, now_fn=lambda: PUBLISHED_AT)

    assert await service.execute(post_empty_err.id) is False
    failed = await repo.get_by_id(post_empty_err.id)
    assert failed is not None
    assert failed.status == PostStatus.FAILED
    assert failed.error_message == "RuntimeError"

    # Сбой БД при mark_failed или release_post_for_retry логирует FINAL_STATUS_UPDATE_FAILED.
    post_fail_update = await _create_post(repo, user_id)
    post_retry_update = await _create_post(repo, user_id)
    service_err = PublicationService(
        repo,
        RecordingPublisher(errors=[RuntimeError("perm"), TemporaryPublicationError("temp")]),
        now_fn=lambda: PUBLISHED_AT,
    )

    async def broken_update(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(repo, "mark_failed", broken_update)
    monkeypatch.setattr(repo, "release_post_for_retry", broken_update)

    with caplog.at_level("ERROR", logger="postmaster.services.publication_service"):
        assert await service_err.execute(post_fail_update.id) is False
        assert PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value in caplog.text
        caplog.clear()
        assert await service_err.execute(post_retry_update.id) is False
        assert PublicationErrorCode.FINAL_STATUS_UPDATE_FAILED.value in caplog.text
