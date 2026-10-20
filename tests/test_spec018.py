"""SPEC-018: отмена собственной ожидающей публикации."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fake_telegram import FakeTelegram
from harness import db_path, wait_until
from postmaster.app import Application
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.domain.user import User
from postmaster.handlers.cancel import (
    INVALID_POST_ID_TEXT,
    POST_ID_REQUIRED_TEXT,
    parse_post_id,
    register_cancel_handler,
)
from postmaster.publishers.base import Publisher, PublishResult
from postmaster.publishers.resolver import PublisherResolver
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.post_cancellation_service import CancelOutcome, PostCancellationService
from postmaster.services.publication_service import PublicationService
from postmaster.services.scheduler_service import SchedulerService
from postmaster.services.user_service import UserService

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
SCHEDULED = NOW + timedelta(days=2)


@pytest.fixture
async def spec018_db(
    tmp_path: Path,
) -> AsyncIterator[tuple[Any, PostRepository, UserRepository, User, User]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/spec018.db")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        users = UserRepository(factory)
        posts = PostRepository(factory)
        owner = await users.add(
            telegram_user_id=18001, username="owner", first_name="Owner", now=NOW
        )
        other = await users.add(
            telegram_user_id=18002, username="other", first_name="Other", now=NOW
        )
        yield engine, posts, users, owner, other
    finally:
        await engine.dispose()


async def _make_post(
    repository: PostRepository,
    user_id: int,
    *,
    scheduled_at: datetime = SCHEDULED,
) -> Post:
    stamp = str(int(scheduled_at.timestamp()))
    return await repository.create_post(
        user_id=user_id,
        telegram_file_id=f"photo-{user_id}-{stamp}",
        telegram_file_unique_id=f"unique-{user_id}-{stamp}",
        scheduled_at_utc=scheduled_at,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        now=NOW,
    )


def test_tc_018_01_02_03_post_id_parsing() -> None:
    assert parse_post_id("/cancel 42") == (42, None)
    assert parse_post_id("/cancel") == (None, POST_ID_REQUIRED_TEXT)
    assert parse_post_id("/cancel abc") == (None, INVALID_POST_ID_TEXT)
    assert parse_post_id("/cancel 0") == (None, INVALID_POST_ID_TEXT)
    assert parse_post_id("/cancel 42 extra") == (None, INVALID_POST_ID_TEXT)


async def test_cancel_handler_parses_valid_id_and_does_not_call_service_for_invalid_input() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.handler = None
            self.sent: list[str] = []

        def message_handler(self, **_kwargs: Any):
            def decorate(handler):
                self.handler = handler
                return handler

            return decorate

        async def send_message(self, _chat_id: int, text: str) -> None:
            self.sent.append(text)

    class FakeCancellationService:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        async def cancel(self, telegram_user_id: int, post_id: int) -> CancelOutcome:
            self.calls.append((telegram_user_id, post_id))
            return CancelOutcome.CANCELLED

    service = FakeCancellationService()
    bot = FakeBot()
    register_cancel_handler(bot, service)  # type: ignore[arg-type]

    def message(text: str) -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            from_user=SimpleNamespace(id=18001),
            chat=SimpleNamespace(id=18001),
        )

    await bot.handler(message("/cancel 42"))
    assert service.calls == [(18001, 42)]
    assert bot.sent == ["✅ Публикация #42 отменена."]

    await bot.handler(message("/cancel abc"))
    assert service.calls == [(18001, 42)]
    assert bot.sent[-1] == INVALID_POST_ID_TEXT

    await bot.handler(message("/cancel"))
    assert service.calls == [(18001, 42)]
    assert bot.sent[-1] == POST_ID_REQUIRED_TEXT


async def test_repository_cancel_is_atomic_owner_scoped_and_pending_only(spec018_db) -> None:
    _, posts, _, owner, other = spec018_db
    own_post = await _make_post(posts, owner.id)
    foreign_post = await _make_post(posts, owner.id, scheduled_at=SCHEDULED + timedelta(minutes=1))
    processing = await _make_post(posts, owner.id, scheduled_at=SCHEDULED + timedelta(minutes=2))
    assert await posts.claim_post_for_processing(processing.id, now=NOW)

    assert await posts.cancel_pending_post(own_post.id, owner.id, now=NOW + timedelta(minutes=1))
    assert not await posts.cancel_pending_post(own_post.id, owner.id)
    assert not await posts.cancel_pending_post(foreign_post.id, other.id)
    assert not await posts.cancel_pending_post(processing.id, owner.id)

    assert (await posts.get_by_id(own_post.id)).status == PostStatus.CANCELLED
    assert (await posts.get_by_id(foreign_post.id)).status == PostStatus.PENDING
    assert (await posts.get_by_id(processing.id)).status == PostStatus.PROCESSING


async def test_cancel_and_claim_race_allows_exactly_one_transition(spec018_db) -> None:
    _, posts, _, owner, _ = spec018_db
    post = await _make_post(posts, owner.id)
    cancel_result, claim_result = await asyncio.gather(
        posts.cancel_pending_post(post.id, owner.id, now=NOW + timedelta(seconds=1)),
        posts.claim_post_for_processing(post.id, now=NOW + timedelta(seconds=1)),
    )
    assert sorted((cancel_result, claim_result)) == [False, True]
    final = await posts.get_by_id(post.id)
    assert final.status in {PostStatus.CANCELLED, PostStatus.PROCESSING}


async def test_cancellation_service_removes_scheduler_job_and_keeps_cancel_on_remove_error(
    spec018_db,
) -> None:
    _, posts, users, owner, _ = spec018_db
    user_service = UserService(users, default_timezone="UTC")
    post = await _make_post(posts, owner.id)

    class RecordingScheduler:
        def __init__(self, error: Exception | None = None) -> None:
            self.removed: list[int] = []
            self.error = error

        def remove_post(self, post_id: int) -> bool:
            self.removed.append(post_id)
            if self.error:
                raise self.error
            return True

    scheduler = RecordingScheduler()
    service = PostCancellationService(user_service, posts, scheduler)
    assert await service.cancel(owner.telegram_user_id, post.id) == CancelOutcome.CANCELLED
    assert scheduler.removed == [post.id]
    assert (await posts.get_by_id(post.id)).status == PostStatus.CANCELLED

    other_post = await _make_post(posts, owner.id, scheduled_at=SCHEDULED + timedelta(minutes=1))
    broken_scheduler = RecordingScheduler(RuntimeError("job store unavailable"))
    service = PostCancellationService(user_service, posts, broken_scheduler)
    assert await service.cancel(owner.telegram_user_id, other_post.id) == CancelOutcome.CANCELLED
    assert (await posts.get_by_id(other_post.id)).status == PostStatus.CANCELLED


async def test_failed_or_missing_scheduler_job_cannot_publish_cancelled_post(spec018_db) -> None:
    _, posts, users, owner, _ = spec018_db
    post = await _make_post(posts, owner.id)

    class RecordingPublisher(Publisher):
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def publish(self, post: Post) -> PublishResult:
            self.calls.append(post.id)
            return PublishResult(success=True)

    class FailingRemove:
        def remove_post(self, _post_id: int) -> bool:
            raise RuntimeError("scheduler unavailable")

    publisher = RecordingPublisher()
    publication = PublicationService(
        posts,
        publisher_resolver=PublisherResolver(publishers={PublicationType.REMINDER: publisher}),
    )
    scheduler = SchedulerService(posts, publication)
    scheduler.start()
    try:
        scheduler.schedule_post(post_id=post.id, run_at=SCHEDULED)
        cancellation = PostCancellationService(
            UserService(users, default_timezone="UTC"),
            posts,
            FailingRemove(),
        )
        assert await cancellation.cancel(owner.telegram_user_id, post.id) == CancelOutcome.CANCELLED
        # Simulate an already queued callback after remove_post failed.
        await scheduler._on_post_due(post.id)
        assert publisher.calls == []
        assert (await posts.get_by_id(post.id)).status == PostStatus.CANCELLED
    finally:
        scheduler.shutdown()


async def test_scheduler_remove_post_is_idempotent_for_missing_job(spec018_db) -> None:
    _, posts, users, owner, _ = spec018_db
    scheduler = SchedulerService(posts)
    post = await _make_post(posts, owner.id)
    scheduler.start()
    try:
        cancellation = PostCancellationService(
            UserService(users, default_timezone="UTC"), posts, scheduler
        )
        assert await cancellation.cancel(owner.telegram_user_id, post.id) == CancelOutcome.CANCELLED
        assert (await posts.get_by_id(post.id)).status == PostStatus.CANCELLED
        assert scheduler.remove_post(999) is False
    finally:
        scheduler.shutdown()


async def test_cancel_command_e2e_updates_post_removes_job_and_hides_it_from_my_posts(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    path = db_path(tmp_path)
    engine = create_database_engine(f"sqlite+aiosqlite:///{path}")
    try:
        factory = create_session_factory(engine)
        users = UserRepository(factory)
        posts = PostRepository(factory)
        user = await users.add(
            telegram_user_id=18003, username="cancel_user", first_name="Cancel", now=NOW
        )
        post = await _make_post(posts, user.id)
        running_app._scheduler.schedule_post(post_id=post.id, run_at=SCHEDULED)
        assert running_app._scheduler._scheduler.get_job(f"post:{post.id}") is not None

        fake_telegram.add_text_message("/cancel " + str(post.id), chat_id=user.telegram_user_id)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
        assert fake_telegram.sent_messages[0]["text"] == f"✅ Публикация #{post.id} отменена."
        assert (await posts.get_by_id(post.id)).status == PostStatus.CANCELLED
        assert running_app._scheduler._scheduler.get_job(f"post:{post.id}") is None

        fake_telegram.add_text_message("/my_posts", chat_id=user.telegram_user_id)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
        assert fake_telegram.sent_messages[1]["text"] == "У вас нет запланированных публикаций."
    finally:
        await engine.dispose()


async def test_cancel_returns_user_not_found_without_touching_posts(spec018_db) -> None:
    _, posts, _, _, _ = spec018_db

    class MissingUserService:
        async def get_by_telegram_id(self, _telegram_user_id: int) -> None:
            return None

    class NoopScheduler:
        def remove_post(self, _post_id: int) -> bool:
            raise AssertionError("scheduler must not run")

    service = PostCancellationService(MissingUserService(), posts, NoopScheduler())  # type: ignore[arg-type]
    assert await service.cancel(99999, 42) == CancelOutcome.USER_NOT_FOUND


async def test_cancel_rejects_non_pending_statuses_and_foreign_or_missing_posts(spec018_db) -> None:
    _, posts, users, owner, other = spec018_db
    processing = await _make_post(posts, owner.id)
    published = await _make_post(posts, owner.id, scheduled_at=SCHEDULED + timedelta(minutes=1))
    failed = await _make_post(posts, owner.id, scheduled_at=SCHEDULED + timedelta(minutes=2))
    cancelled = await _make_post(posts, owner.id, scheduled_at=SCHEDULED + timedelta(minutes=3))
    foreign = await _make_post(posts, other.id)

    assert await posts.claim_post_for_processing(processing.id, now=NOW)
    assert await posts.claim_post_for_processing(published.id, now=NOW)
    await posts.mark_published(published.id, published_at=NOW, now=NOW)
    assert await posts.claim_post_for_processing(failed.id, now=NOW)
    await posts.mark_failed(failed.id, error_message="failure", now=NOW)
    assert await posts.cancel_pending_post(cancelled.id, owner.id, now=NOW)

    class NoopScheduler:
        def remove_post(self, _post_id: int) -> bool:
            raise AssertionError("scheduler removal must only run after a successful cancel")

    service = PostCancellationService(
        UserService(users, default_timezone="UTC"), posts, NoopScheduler()
    )
    outcomes = {
        processing.id: CancelOutcome.PROCESSING,
        published.id: CancelOutcome.PUBLISHED,
        failed.id: CancelOutcome.FAILED,
        cancelled.id: CancelOutcome.ALREADY_CANCELLED,
        foreign.id: CancelOutcome.NOT_CANCELLABLE,
        foreign.id + 10000: CancelOutcome.NOT_CANCELLABLE,
    }
    for post_id, expected in outcomes.items():
        assert await service.cancel(owner.telegram_user_id, post_id) == expected
    assert (await posts.get_by_id(foreign.id)).status == PostStatus.PENDING
