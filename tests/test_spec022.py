"""SPEC-022: Retry-механизм.

Тесты проверяют:
- retry1, retry2, retry3: попытки с задержками +30s, +2m, +10m.
- limit: после исчерпания повторов пост в FAILED.
- permanent error: не-retryable ошибка → FAILED сразу.
- cancel between retries: отмена во время retry останавливает повторы.
- concurrency: параллельные повторы используют atomic claim.
- success: PUBLISHED при первой удачной попытке.
- invariants: scheduled_at_utc не меняется, новая запись не создаётся.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.publishers import DefaultPublisherResolver
from postmaster.publishers.reminder_publisher import TelegramBlockedError
from postmaster.publishers.telegram_publisher import (
    TelegramPermissionError,
    TelegramTimeoutError,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.publication_service import (
    PublicationError,
    PublicationService,
    RetryPolicy,
)
from postmaster.services.user_service import UserService

logger = logging.getLogger(__name__)


class CountingTelegramSender:
    """Заглушка TelegramSender с записью вызовов и настраиваемым fail-сценарием."""

    def __init__(self, errors: list[Exception | None]) -> None:
        self._errors = errors  # список: None = успех, Exception = ошибка
        self.calls: list[dict] = []
        self.call_count = 0

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> dict | None:
        self.calls.append({"chat_id": chat_id, "photo_file_id": photo_file_id, "caption": caption})
        self.call_count += 1
        if self.call_count <= len(self._errors):
            err = self._errors[self.call_count - 1]
            if err is not None:
                raise err
        return {"message_id": self.call_count}


@pytest.fixture
async def post_repo(tmp_path: Path) -> AsyncIterator[tuple[PostRepository, UserService]]:
    """Создаёт репозиторий постов и UserService."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        yield PostRepository(factory), UserService(user_repo, default_timezone="UTC")
    finally:
        await engine.dispose()


async def _make_user(user_service: UserService, telegram_user_id: int) -> int:
    user = await user_service.get_or_create(
        telegram_user_id=telegram_user_id,
        username=None,
        first_name=f"User{telegram_user_id}",
    )
    return user.id


async def _create_post(
    repo: PostRepository,
    user_id: int,
    *,
    publication_type: PublicationType = PublicationType.TELEGRAM,
    target_id: str | None = "-1001234567890",
    scheduled_at: datetime | None = None,
) -> int:
    if scheduled_at is None:
        scheduled_at = datetime.now(UTC) + timedelta(hours=2)
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="AgAC_retry_test",
        telegram_file_unique_id="AQAD_retry_test",
        scheduled_at_utc=scheduled_at,
        timezone="UTC",
        publication_type=publication_type,
        target_id=target_id,
    )
    return post.id


# === RetryPolicy unit tests (SPEC-022 §1) ===


def test_retry_policy_default_delays() -> None:
    """Дефолтные задержки: +30s, +2m, +10m (SPEC-022 §4)."""
    policy = RetryPolicy()
    assert policy.delays == (30, 120, 600)
    assert policy.max_attempts == 4  # 1 + 3 retries


def test_retry_policy_max_attempts() -> None:
    """max_attempts = 1 + len(delays)."""
    assert RetryPolicy(delays=(1,)).max_attempts == 2
    assert RetryPolicy(delays=(1, 2)).max_attempts == 3
    assert RetryPolicy(delays=(1, 2, 3)).max_attempts == 4
    assert RetryPolicy(delays=()).max_attempts == 1  # без повторов


def test_retry_policy_should_retry() -> None:
    """should_retry: True пока есть задержка для следующей попытки."""
    policy = RetryPolicy(delays=(30, 120, 600))
    assert policy.should_retry(1) is True
    assert policy.should_retry(2) is True
    assert policy.should_retry(3) is True
    assert policy.should_retry(4) is False  # после 3 retry — стоп


def test_retry_policy_should_retry_empty() -> None:
    """Без задержек — повторов нет."""
    policy = RetryPolicy(delays=())
    assert policy.should_retry(1) is False


def test_retry_policy_delay_after_attempt() -> None:
    """delay_after_attempt: задержка после неудачной попытки."""
    policy = RetryPolicy(delays=(30, 120, 600))
    assert policy.delay_after_attempt(1) == 30
    assert policy.delay_after_attempt(2) == 120
    assert policy.delay_after_attempt(3) == 600


def test_retry_policy_negative_delay_rejected() -> None:
    """Отрицательные задержки запрещены."""
    with pytest.raises(ValueError, match="задержка"):
        RetryPolicy(delays=(-1, 30))


# === Retry semantics (SPEC-022 §8 retry1/2/3, limit, permanent error) ===


async def test_retry1_succeeds_after_first_failure(post_repo) -> None:
    """Первая попытка упала, вторая (через +30s) — успешна."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=100)
    errors = [TelegramTimeoutError("first fail"), None]  # 2-я успешна
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    post_id = await _create_post(repo, user_id)

    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert sender.call_count == 2  # 1 fail + 1 success


async def test_retry2_succeeds_after_two_failures(post_repo) -> None:
    """Две попытки упали (через +30s, +2m), третья — успешна."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=101)
    errors = [
        TelegramTimeoutError("first"),
        TelegramTimeoutError("second"),
        None,
    ]
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    post_id = await _create_post(repo, user_id)

    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert sender.call_count == 3


async def test_retry3_succeeds_after_three_failures(post_repo) -> None:
    """Три попытки упали (через +30s, +2m, +10m), четвёртая — успешна."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=102)
    errors = [
        TelegramTimeoutError("first"),
        TelegramTimeoutError("second"),
        TelegramTimeoutError("third"),
        None,
    ]
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    post_id = await _create_post(repo, user_id)

    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert sender.call_count == 4


async def test_retry_limit_persistent_error_marks_failed(post_repo) -> None:
    """После исчерпания повторов (4 попытки) пост в FAILED (SPEC-022 §4)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=103)
    errors = [TelegramTimeoutError(f"fail {i}") for i in range(10)]  # всегда fail
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert sender.call_count == 4  # 1 + 3 retries
    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.FAILED
    assert "fail 3" in (result.error_message or "")  # последняя ошибка


async def test_permanent_error_marks_failed_immediately(post_repo) -> None:
    """Permanent (non-retryable) ошибка → FAILED без повторов (SPEC-022 §4)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=104)
    errors = [TelegramPermissionError("chat not found")]
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert sender.call_count == 1  # только первая попытка
    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.FAILED


async def test_blocked_bot_error_not_retried(post_repo) -> None:
    """TelegramBlockedError → FAILED сразу (не retryable)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=105)
    errors = [TelegramBlockedError("user blocked")]
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert sender.call_count == 1


# === Cancel between retries (SPEC-022 §8) ===


async def test_cancel_between_retries_stops_publisher_calls(post_repo) -> None:
    """Если пост отменён во время retry-ожидания, дальнейшие повторы не делаются."""
    import asyncio as _asyncio

    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=106)

    cancel_event = _asyncio.Event()
    call_count = 0

    class CancelingSender:
        async def send_photo(
            self, *, chat_id: str, photo_file_id: str, caption: str | None = None
        ) -> dict | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Сигнализируем внешнему коду, что первая попытка упала.
                cancel_event.set()
                raise TelegramTimeoutError("first fail")
            raise AssertionError("should not be called after cancel")

    async def cancel_after_first_failure() -> None:
        await cancel_event.wait()
        # Имитируем короткое ожидание между первой неудачей и retry.
        await _asyncio.sleep(0.01)
        # Отменяем пост.
        posts = await repo.list_pending_by_user(user_id)
        if posts:
            await repo.cancel_post(posts[0].id, user_id)

    sender = CancelingSender()
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    # Используем короткий, но не нулевой delay, чтобы cancel успел сработать.
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(1, 1, 1)))
    post_id = await _create_post(repo, user_id)

    cancel_task = _asyncio.create_task(cancel_after_first_failure())

    from postmaster.services.publication_service import PostClaimLostError

    with pytest.raises((PublicationError, PostClaimLostError)):
        await service.publish(post_id)

    await cancel_task
    assert call_count == 1  # второй вызов не произошёл


# === Concurrency (SPEC-022 §8) ===


async def test_concurrent_retries_use_atomic_claim(post_repo) -> None:
    """Параллельные повторы используют atomic claim — только один исполнитель работает."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=107)
    errors = [TelegramTimeoutError("fail"), None]
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    post_id = await _create_post(repo, user_id)

    # Запускаем две параллельные публикации
    results = await asyncio.gather(
        service.publish(post_id),
        service.publish(post_id),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    errors_raised = [r for r in results if isinstance(r, BaseException)]

    # Только один publish реально сработал
    assert len(successes) == 1
    assert len(errors_raised) == 1
    # Один исполнитель сделал 2 вызова (1 fail + 1 success)
    assert sender.call_count == 2


# === Invariants (SPEC-022 §7) ===


async def test_retry_does_not_create_new_post(post_repo) -> None:
    """Retry не создаёт новый Post — работает с тем же post_id (SPEC-022 §7)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=108)
    errors = [TelegramTimeoutError("fail"), None]
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    post_id = await _create_post(repo, user_id)

    pending_before = len(await repo.list_pending_by_user(user_id))
    await service.publish(post_id)
    pending_after = len(await repo.list_pending_by_user(user_id))

    assert pending_before == 1
    assert pending_after == 0  # один пост, который теперь PUBLISHED


async def test_retry_does_not_change_scheduled_at_utc(post_repo) -> None:
    """scheduled_at_utc не меняется при retry (SPEC-022 §7)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=109)
    errors = [TelegramTimeoutError("fail"), None]
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    scheduled = datetime(2026, 11, 11, 11, 11, tzinfo=UTC)
    post_id = await _create_post(repo, user_id, scheduled_at=scheduled)

    original = await repo.get_by_id(post_id)
    assert original is not None
    assert original.scheduled_at_utc == scheduled

    await service.publish(post_id)

    after = await repo.get_by_id(post_id)
    assert after is not None
    assert after.scheduled_at_utc == scheduled


async def test_retry_uses_atomic_claim_on_each_attempt(post_repo) -> None:
    """Каждый retry проходит atomic claim (SPEC-022 §4)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=110)
    errors = [
        TelegramTimeoutError("fail1"),
        TelegramTimeoutError("fail2"),
        None,
    ]
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    post_id = await _create_post(repo, user_id)

    # Каждый retry должен перечитывать пост через claim
    await service.publish(post_id)

    assert sender.call_count == 3
    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.PUBLISHED


# === Success on first attempt (SPEC-022) ===


async def test_success_on_first_attempt_no_retries(post_repo) -> None:
    """Успешная первая попытка — повторов нет."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=111)
    sender = CountingTelegramSender([None])  # успех
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    post_id = await _create_post(repo, user_id)

    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert sender.call_count == 1


async def test_no_retries_when_policy_disabled(post_repo) -> None:
    """RetryPolicy(delays=()) — повторов нет, первая неудача → FAILED."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=112)
    errors = [TelegramTimeoutError("fail"), TelegramTimeoutError("would retry")]
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=()))
    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert sender.call_count == 1  # без retries
    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.FAILED


# === Error message safety (SPEC-021 §7) ===


async def test_error_message_stored_after_retry(post_repo) -> None:
    """Текст ошибки сохраняется в Post.error_message после исчерпания retries."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=113)
    errors = [TelegramTimeoutError(f"fail {i}") for i in range(10)]
    sender = CountingTelegramSender(errors)
    publisher = _telegram_publisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver, retry_policy=RetryPolicy(delays=(0, 0, 0)))
    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.error_message is not None
    assert "fail" in result.error_message


# === Helpers ===


def _telegram_publisher(sender):
    """Создаёт TelegramChannelPublisher с минимальным Sender."""
    from postmaster.publishers.telegram_publisher import TelegramChannelPublisher

    return TelegramChannelPublisher(sender)
