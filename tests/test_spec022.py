"""SPEC-022: интервалы retry, лимит, повторный claim, cancel и финальные статусы."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.domain.user import User
from postmaster.publishers.base import Publisher, PublishResult
from postmaster.publishers.telegram_errors import TelegramErrorCode
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.publication_service import PublicationService
from postmaster.services.retry_service import RETRY_DELAYS, RetryService
from postmaster.services.scheduler_service import SchedulerService


class SequencePublisher(Publisher):
    def __init__(self, results: list[PublishResult], *, delay: float = 0) -> None:
        self.results = list(results)
        self.delay = delay
        self.calls: list[int] = []

    async def publish(self, post: Post) -> PublishResult:
        self.calls.append(post.id)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.results.pop(0)


def temporary_failure(code: str = TelegramErrorCode.TELEGRAM_API_TIMEOUT.value) -> PublishResult:
    return PublishResult(
        success=False,
        error_code=code,
        error_message=f"last diagnostic: {code}",
        retryable=True,
    )


@pytest.fixture
async def retry_db(
    tmp_path: Path,
) -> AsyncIterator[tuple[UserRepository, PostRepository, User]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/spec022.db")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        users = UserRepository(factory)
        posts = PostRepository(factory)
        user = await users.add(
            telegram_user_id=922022,
            username="retry_user",
            first_name="Retry",
            now=datetime.now(UTC),
        )
        yield users, posts, user
    finally:
        await engine.dispose()


async def make_post(posts: PostRepository, user: User) -> Post:
    now = datetime.now(UTC)
    return await posts.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_retry_photo",
        telegram_file_unique_id="AQAD_retry_photo",
        scheduled_at_utc=now + timedelta(days=1),
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        now=now,
    )


def build_services(
    posts: PostRepository,
    publisher: Publisher,
) -> tuple[PublicationService, SchedulerService]:
    publication = PublicationService(posts, publisher)
    scheduler = SchedulerService(posts, publication)
    publication.set_retry_service(RetryService(posts, scheduler))
    return publication, scheduler


@pytest.mark.parametrize("attempt,delay", [(1, 30), (2, 120), (3, 600)])
def test_retry_policy_has_specified_delays(attempt: int, delay: int) -> None:
    assert RETRY_DELAYS[attempt] == delay


@pytest.mark.asyncio
async def test_timeout_retries_then_success_uses_publication_service_and_preserves_schedule(
    retry_db,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, posts, user = retry_db
    post = await make_post(posts, user)
    publisher = SequencePublisher([temporary_failure(), PublishResult(success=True)])
    publication, scheduler = build_services(posts, publisher)
    scheduler.start()
    try:
        assert await publication.execute(post.id) is False
        pending = await posts.get_by_id(post.id)
        assert pending is not None and pending.status == PostStatus.PENDING
        assert pending.error_message == "last diagnostic: TELEGRAM_API_TIMEOUT"
        assert pending.scheduled_at_utc == post.scheduled_at_utc

        job = scheduler._scheduler.get_job(f"retry:{post.id}")
        assert job is not None
        assert job.args == (post.id, 1)
        assert (job.next_run_time - datetime.now(UTC)).total_seconds() == pytest.approx(30, abs=2)
        scheduler.remove_retry(post.id)

        # Повторный вход проходит тем же PublicationService и заново делает atomic claim.
        with caplog.at_level("INFO", logger="postmaster.services.publication_service"):
            assert await scheduler._on_post_due(post.id, attempt=1) is None
        assert f"Retry succeeded post_id={post.id} attempt=1" in caplog.text
        published = await posts.get_by_id(post.id)
        assert published is not None and published.status == PostStatus.PUBLISHED
        assert published.published_at is not None
        assert published.error_message is None
        assert published.scheduled_at_utc == post.scheduled_at_utc
        assert publisher.calls == [post.id, post.id]
    finally:
        scheduler.shutdown()


@pytest.mark.asyncio
async def test_all_three_retries_use_intervals_then_mark_failed(retry_db) -> None:
    _, posts, user = retry_db
    post = await make_post(posts, user)
    publisher = SequencePublisher([temporary_failure() for _ in range(4)])
    publication, scheduler = build_services(posts, publisher)
    scheduler.start()
    try:
        assert await publication.execute(post.id) is False
        for retry_number, delay in ((1, 30), (2, 120), (3, 600)):
            job = scheduler._scheduler.get_job(f"retry:{post.id}")
            assert job is not None and job.args == (post.id, retry_number)
            assert (job.next_run_time - datetime.now(UTC)).total_seconds() == pytest.approx(
                delay, abs=2
            )
            scheduler.remove_retry(post.id)
            assert await scheduler._on_post_due(post.id, attempt=retry_number) is None

        failed = await posts.get_by_id(post.id)
        assert failed is not None and failed.status == PostStatus.FAILED
        assert failed.error_message == "last diagnostic: TELEGRAM_API_TIMEOUT"
        assert failed.scheduled_at_utc == post.scheduled_at_utc
        assert scheduler._scheduler.get_job(f"retry:{post.id}") is None
        assert len(publisher.calls) == 4  # основная попытка + максимум три retry
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize(
    "error_code",
    [
        TelegramErrorCode.TELEGRAM_PERMISSION_DENIED.value,
        TelegramErrorCode.INVALID_FILE_ID.value,
    ],
)
@pytest.mark.asyncio
async def test_permanent_error_fails_immediately_without_retry_job(
    retry_db, error_code: str
) -> None:
    _, posts, user = retry_db
    post = await make_post(posts, user)
    publisher = SequencePublisher(
        [
            PublishResult(
                success=False,
                error_code=error_code,
                error_message="permission denied",
                retryable=False,
            )
        ]
    )
    publication, scheduler = build_services(posts, publisher)
    scheduler.start()
    try:
        assert await publication.execute(post.id) is False
        failed = await posts.get_by_id(post.id)
        assert failed is not None and failed.status == PostStatus.FAILED
        assert failed.error_message == "permission denied"
        assert scheduler._scheduler.get_job(f"retry:{post.id}") is None
        assert publisher.calls == [post.id]
    finally:
        scheduler.shutdown()


@pytest.mark.asyncio
async def test_cancelled_pending_retry_cannot_publish(retry_db) -> None:
    _, posts, user = retry_db
    post = await make_post(posts, user)
    publisher = SequencePublisher([temporary_failure(), PublishResult(success=True)])
    publication, scheduler = build_services(posts, publisher)
    scheduler.start()
    try:
        assert await publication.execute(post.id) is False
        assert (await posts.get_by_id(post.id)).status == PostStatus.PENDING
        assert await posts.cancel_pending_post(post.id, user.id) is True

        # Даже если callback уже enqueued, repository остаётся источником истины.
        assert await scheduler._on_post_due(post.id, attempt=1) is None
        cancelled = await posts.get_by_id(post.id)
        assert cancelled is not None and cancelled.status == PostStatus.CANCELLED
        assert publisher.calls == [post.id]
    finally:
        scheduler.shutdown()


@pytest.mark.asyncio
async def test_concurrent_retry_callbacks_claim_and_publish_once(retry_db) -> None:
    _, posts, user = retry_db
    post = await make_post(posts, user)
    publisher = SequencePublisher([temporary_failure(), temporary_failure()], delay=0.03)
    publication, scheduler = build_services(posts, publisher)
    scheduler.start()
    try:
        assert await publication.execute(post.id) is False
        scheduler.remove_retry(post.id)
        await asyncio.gather(
            scheduler._on_post_due(post.id, attempt=1),
            scheduler._on_post_due(post.id, attempt=1),
        )
        assert publisher.calls.count(post.id) == 2  # initial + exactly one concurrent retry
        current = await posts.get_by_id(post.id)
        assert current is not None and current.status == PostStatus.PENDING
        next_job = scheduler._scheduler.get_job(f"retry:{post.id}")
        assert next_job is not None and next_job.args == (post.id, 2)
    finally:
        scheduler.shutdown()


@pytest.mark.asyncio
async def test_scheduler_failure_during_retry_marks_post_failed(retry_db) -> None:
    _, posts, user = retry_db
    post = await make_post(posts, user)
    publisher = SequencePublisher([temporary_failure()])
    publication, scheduler = build_services(posts, publisher)
    # Оставляем scheduler остановленным: add_job отвергает retry, не считая post опубликованным.
    assert await publication.execute(post.id) is False
    failed = await posts.get_by_id(post.id)
    assert failed is not None and failed.status == PostStatus.FAILED
    assert failed.error_message == "last diagnostic: TELEGRAM_API_TIMEOUT"
    assert publisher.calls == [post.id]
