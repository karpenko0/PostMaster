"""PM-07: восстановление контрактов публикации и retry-механизм (SPEC-021/022, SPEC-010/016).

Закрываемые дефекты:

- BUG-08 (P0): ``PublishResult`` не объявлял ``post_id``/``external_id``, которые передаёт
  живой Publisher, поэтому ЛЮБАЯ реальная публикация падала с ``TypeError`` и пост уходил
  в FAILED. Тесты фиксируют рабочий сквозной путь и отсутствие регрессии.
- BUG-09 (P0): retry-механизм (SPEC-022) отсутствовал: не было ``RetryPolicy``,
  задержек +30s/+2m/+10m, повторного подбора постов, вернувшихся в PENDING.
- BUG-10 (P1): публичный контракт `DefaultPublisherResolver` / `publication_executor`
  был утерян, из-за чего 6 модулей тестов не запускались вовсе.

Тесты не обращаются к сети: Publisher подменяется локальным двойником.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.publishers.base import Publisher, PublishResult
from postmaster.publishers.resolver import DefaultPublisherResolver
from postmaster.publishers.telegram_errors import TelegramErrorMapper
from postmaster.publishers.telegram_publisher import TelegramChannelPublisher, TelegramTimeoutError
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.missed_post_handler import MissedPostHandler
from postmaster.services.publication_service import (
    NO_RETRY_POLICY,
    PostClaimLostError,
    PublicationError,
    PublicationService,
    RetryPolicy,
)
from postmaster.services.recovery_service import RecoveryService
from postmaster.services.scheduler_service import SchedulerService
from postmaster.services.user_service import UserService

RUN_AT = datetime(2026, 11, 11, 11, 11, tzinfo=UTC)


class RecordingSender:
    """Двойник TelegramSender: записывает вызовы и падает по заданному сценарию."""

    def __init__(self, errors: list[BaseException | None] | None = None) -> None:
        self._errors = list(errors or [])
        self.calls: list[dict] = []

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> dict:
        self.calls.append({"chat_id": chat_id, "photo_file_id": photo_file_id, "caption": caption})
        if self.calls and len(self.calls) <= len(self._errors):
            error = self._errors[len(self.calls) - 1]
            if error is not None:
                raise error
        return {"message_id": len(self.calls)}


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[dict]:
    """Изолированная БД с репозиториями и сервисом пользователей."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_service = UserService(UserRepository(factory), default_timezone="UTC")
        user = await user_service.get_or_create(
            telegram_user_id=7001, username=None, first_name="Пользователь"
        )
        yield {
            "engine": engine,
            "repo": PostRepository(factory),
            "user_id": user.id,
        }
    finally:
        await engine.dispose()


async def _create_post(repo: PostRepository, user_id: int, *, run_at: datetime = RUN_AT) -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="file_pm07",
        telegram_file_unique_id="unique_pm07",
        scheduled_at_utc=run_at,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )
    return post.id


def _service(
    repo: PostRepository,
    publisher: Publisher,
    *,
    retry_policy: RetryPolicy | None = None,
    error_mapper: TelegramErrorMapper | None = None,
) -> PublicationService:
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    return PublicationService(
        repo,
        resolver,
        retry_policy=retry_policy,
        error_mapper=error_mapper,
    )


# === BUG-08: сквозная публикация больше не падает с TypeError ================


async def test_publish_result_accepts_publisher_payload() -> None:
    """``PublishResult`` объявляет поля, которые передаёт Publisher (BUG-08)."""
    result = PublishResult(post_id=7, external_id="42")

    assert result.success is True
    assert result.post_id == 7
    assert result.external_id == "42"
    # Позиционный контракт «success» сохранён: старые вызовы не ломаются.
    assert PublishResult(False, "CODE").success is False


async def test_real_publication_marks_post_published(db: dict) -> None:
    """Реальная цепочка PublicationService → TelegramChannelPublisher публикует пост.

    До исправления здесь был FAILED с ``TypeError: unexpected keyword argument 'post_id'``.
    """
    sender = RecordingSender()
    publisher = TelegramChannelPublisher(sender, error_mapper=TelegramErrorMapper())
    service = _service(db["repo"], publisher)
    post_id = await _create_post(db["repo"], db["user_id"])

    assert await service.execute(post_id) is True

    saved = await db["repo"].get_by_id(post_id)
    assert saved is not None
    assert saved.status == PostStatus.PUBLISHED
    assert saved.error_message is None
    assert sender.calls == [
        {"chat_id": "-1001234567890", "photo_file_id": "file_pm07", "caption": None}
    ]


# === BUG-09: retry-механизм SPEC-022 ========================================


def test_retry_policy_follows_spec_delays() -> None:
    """Задержки по умолчанию — +30s, +2m, +10m, всего 4 попытки (SPEC-022 §4)."""
    policy = RetryPolicy()

    assert policy.delays == (30.0, 120.0, 600.0)
    assert policy.max_attempts == 4
    assert [policy.delay_after_attempt(n) for n in (1, 2, 3)] == [30.0, 120.0, 600.0]
    assert policy.should_retry(3) is True
    assert policy.should_retry(4) is False


def test_no_retry_policy_is_single_attempt() -> None:
    """Политика без задержек — одна попытка (поведение до SPEC-022)."""
    assert NO_RETRY_POLICY.max_attempts == 1
    assert NO_RETRY_POLICY.should_retry(1) is False


async def test_transient_error_is_retried_and_succeeds(db: dict) -> None:
    """Timeout (retryable) → повтор → пост опубликован, без создания нового Post."""
    sender = RecordingSender([TelegramTimeoutError("timeout")])
    service = _service(
        db["repo"],
        TelegramChannelPublisher(sender),
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
    )
    post_id = await _create_post(db["repo"], db["user_id"])

    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert len(sender.calls) == 2
    assert len(await db["repo"].list_pending()) == 0


async def test_permanent_error_is_not_retried(db: dict) -> None:
    """Ошибка прав (non-retryable) не повторяется: одна попытка и FAILED."""
    from postmaster.publishers.telegram_publisher import TelegramPermissionError

    sender = RecordingSender([TelegramPermissionError("chat not found")])
    service = _service(
        db["repo"],
        TelegramChannelPublisher(sender),
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
    )
    post_id = await _create_post(db["repo"], db["user_id"])

    with pytest.raises(PublicationError) as exc_info:
        await service.publish(post_id)

    assert exc_info.value.retryable is False
    assert exc_info.value.error_code == "PERMISSION"
    assert len(sender.calls) == 1
    saved = await db["repo"].get_by_id(post_id)
    assert saved is not None and saved.status == PostStatus.FAILED


async def test_retry_exhaustion_marks_failed_with_last_error(db: dict) -> None:
    """После исчерпания попыток пост в FAILED с последней ошибкой (SPEC-022 §4)."""
    sender = RecordingSender([TelegramTimeoutError(f"fail {i}") for i in range(10)])
    service = _service(
        db["repo"],
        TelegramChannelPublisher(sender),
        retry_policy=RetryPolicy(delays=(0, 0)),
    )
    post_id = await _create_post(db["repo"], db["user_id"])

    with pytest.raises(PublicationError) as exc_info:
        await service.publish(post_id)

    assert exc_info.value.attempts == 3
    assert len(sender.calls) == 3
    saved = await db["repo"].get_by_id(post_id)
    assert saved is not None
    assert saved.status == PostStatus.FAILED
    assert "fail 2" in (saved.error_message or "")


async def test_retry_keeps_scheduled_at_utc(db: dict) -> None:
    """Повтор не меняет scheduled_at_utc и не создаёт новый Post (SPEC-022 §7)."""
    sender = RecordingSender([TelegramTimeoutError("timeout")])
    service = _service(
        db["repo"],
        TelegramChannelPublisher(sender),
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
    )
    post_id = await _create_post(db["repo"], db["user_id"])

    await service.publish(post_id)

    saved = await db["repo"].get_by_id(post_id)
    assert saved is not None
    assert saved.scheduled_at_utc == RUN_AT
    assert [post.id for post in await db["repo"].list_pending()] == []


async def test_no_policy_keeps_post_pending_for_next_claim(db: dict) -> None:
    """Без явной политики временная ошибка оставляет пост в PENDING (SPEC-013 §28)."""
    sender = RecordingSender([TelegramTimeoutError("timeout")])
    service = _service(db["repo"], TelegramChannelPublisher(sender))
    post_id = await _create_post(db["repo"], db["user_id"])

    with pytest.raises(PublicationError) as exc_info:
        await service.publish(post_id)

    assert exc_info.value.retryable is True
    saved = await db["repo"].get_by_id(post_id)
    assert saved is not None and saved.status == PostStatus.PENDING


async def test_concurrent_publish_uses_single_executor(db: dict) -> None:
    """Два одновременных запуска публикуют пост ровно один раз (SPEC-013, SPEC-022 §8)."""
    sender = RecordingSender([TelegramTimeoutError("timeout")])
    service = _service(
        db["repo"],
        TelegramChannelPublisher(sender),
        retry_policy=RetryPolicy(delays=(0, 0, 0)),
    )
    post_id = await _create_post(db["repo"], db["user_id"])

    results = await asyncio.gather(
        service.publish(post_id), service.publish(post_id), return_exceptions=True
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], PostClaimLostError)
    # Один исполнитель: первая попытка упала, повтор успешен.
    assert len(sender.calls) == 2
    saved = await db["repo"].get_by_id(post_id)
    assert saved is not None and saved.status == PostStatus.PUBLISHED


# === Пробел SPEC-022: посты в PENDING должны подбираться заново ==============


class _NoopScheduler:
    """Двойник Scheduler для RecoveryService."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, datetime]] = []

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        self.scheduled.append((post_id, run_at))


async def test_retry_pending_republishes_stuck_post(db: dict) -> None:
    """``retry_pending`` повторно публикует пост, застрявший в PENDING после сбоя.

    Раньше такой пост оставался в PENDING навсегда: задание Scheduler уже сработало,
    а другого механизма повторного подбора не существовало.
    """
    publisher = TelegramChannelPublisher(RecordingSender([TelegramTimeoutError("timeout")]))
    service = PublicationService(db["repo"], publisher)  # без политики: возврат в PENDING
    post_id = await _create_post(
        db["repo"], db["user_id"], run_at=datetime.now(UTC) - timedelta(minutes=1)
    )

    # Первая попытка падает и возвращает пост в PENDING (временная ошибка).
    assert await service.execute(post_id) is False
    stuck = await db["repo"].get_by_id(post_id)
    assert stuck is not None and stuck.status == PostStatus.PENDING

    # Подбор находит пост и публикует его — Publisher теперь успешен.
    healthy = PublicationService(db["repo"], TelegramChannelPublisher(RecordingSender()))
    recovery = RecoveryService(
        repository=db["repo"],
        scheduler=_NoopScheduler(),
        missed_post_handler=MissedPostHandler(db["repo"], healthy),
    )

    assert await recovery.retry_pending() == 1

    saved = await db["repo"].get_by_id(post_id)
    assert saved is not None and saved.status == PostStatus.PUBLISHED


async def test_retry_pending_skips_future_posts(db: dict) -> None:
    """Подбор не трогает посты, время которых ещё не наступило."""
    service = PublicationService(db["repo"], TelegramChannelPublisher(RecordingSender()))
    post_id = await _create_post(
        db["repo"], db["user_id"], run_at=datetime.now(UTC) + timedelta(hours=1)
    )
    recovery = RecoveryService(
        repository=db["repo"],
        scheduler=_NoopScheduler(),
        missed_post_handler=MissedPostHandler(db["repo"], service),
    )

    assert await recovery.retry_pending() == 0

    saved = await db["repo"].get_by_id(post_id)
    assert saved is not None and saved.status == PostStatus.PENDING


# === BUG-10: публичный контракт резолвера и Scheduler ========================


def test_scheduler_accepts_publication_executor_keyword() -> None:
    """``SchedulerService(publication_executor=…)`` — исторический публичный контракт."""

    class _Executor:
        async def execute(self, post_id: int) -> bool:
            return True

    executor = _Executor()
    scheduler = SchedulerService(None, publication_executor=executor)

    assert scheduler._publication_service is executor
    # Оба имени взаимозаменяемы, приоритет у нового.
    other = _Executor()
    assert (
        SchedulerService(None, other, publication_executor=executor)._publication_service is other
    )


async def test_default_resolver_resolves_by_type() -> None:
    """``DefaultPublisherResolver`` сопоставляет тип публикации с Publisher (SPEC-016)."""
    telegram = TelegramChannelPublisher(RecordingSender())
    resolver = DefaultPublisherResolver(telegram_publisher=telegram)

    assert resolver.resolve(PublicationType.TELEGRAM) is telegram
