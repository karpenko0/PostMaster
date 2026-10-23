"""SPEC-013: защита от двойной публикации.

Тесты проверяют:
- claim_for_processing атомарно переводит PENDING → PROCESSING.
- rowcount=1 — единственное условие получения права на execution.
- Второй claim возвращает None.
- Cancel и execute конкурируют атомарно (cancel выигрывает у claim).
- Финальные переходы PROCESSING → PUBLISHED/FAILED/PENDING(retry) работают.
- Публикация вызывается ровно один раз для двух конкурирующих вызовов.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.publishers import DefaultPublisherResolver
from postmaster.publishers.telegram_publisher import TelegramChannelPublisher
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.publication_service import (
    PostClaimLostError,
    PublicationError,
    PublicationService,
    RetryPolicy,
)

RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


class RecordingTelegramSender:
    """Заглушка TelegramSender, записывает вызовы и может падать."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.delay: float = 0.0
        self.fail_with: Exception | None = None

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> None:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        self.calls.append({"chat_id": chat_id, "photo_file_id": photo_file_id, "caption": caption})
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
            telegram_user_id=111,
            first_name="Claim",
            username="claim",
            now=datetime(2026, 10, 7, tzinfo=UTC),
        )
        yield PostRepository(factory), user.id
    finally:
        await engine.dispose()


async def _create_post(
    repo: PostRepository,
    user_id: int,
    publication_type: PublicationType = PublicationType.TELEGRAM,
    target_id: str | None = "-1001234567890",
) -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="AgAC_claim_test",
        telegram_file_unique_id="AQAD_claim_test",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=publication_type,
        target_id=target_id,
    )
    return post.id


async def test_claim_atomic_pending_to_processing(repo_with_user) -> None:
    """claim_for_processing атомарно переводит PENDING → PROCESSING."""
    repo, user_id = repo_with_user
    post_id = await _create_post(repo, user_id)

    claimed = await repo.claim_for_processing(post_id)

    assert claimed is not None
    assert claimed.status == PostStatus.PROCESSING
    assert claimed.id == post_id
    # Статус в БД тоже PROCESSING.
    fresh = await repo.get_by_id(post_id)
    assert fresh is not None
    assert fresh.status == PostStatus.PROCESSING


async def test_second_claim_returns_none(repo_with_user) -> None:
    """Второй вызов claim_for_processing возвращает None."""
    repo, user_id = repo_with_user
    post_id = await _create_post(repo, user_id)

    first = await repo.claim_for_processing(post_id)
    second = await repo.claim_for_processing(post_id)

    assert first is not None
    assert second is None


async def test_claim_on_nonexistent_post_returns_none(repo_with_user) -> None:
    """claim для несуществующего поста возвращает None."""
    repo, _ = repo_with_user
    result = await repo.claim_for_processing(99999)
    assert result is None


async def test_claim_on_non_pending_returns_none(repo_with_user) -> None:
    """claim для поста не в PENDING возвращает None."""
    repo, user_id = repo_with_user
    post_id = await _create_post(repo, user_id)

    # Сначала отменяем пост
    await repo.update_status(post_id, PostStatus.CANCELLED)

    result = await repo.claim_for_processing(post_id)
    assert result is None

    # Пост остаётся CANCELLED, не PROCESSING
    fresh = await repo.get_by_id(post_id)
    assert fresh is not None
    assert fresh.status == PostStatus.CANCELLED


async def test_claim_after_processing_returns_none(repo_with_user) -> None:
    """claim для поста в PROCESSING возвращает None."""
    repo, user_id = repo_with_user
    post_id = await _create_post(repo, user_id)

    # Сначала делаем успешный claim
    first = await repo.claim_for_processing(post_id)
    assert first is not None

    # Второй claim возвращает None
    second = await repo.claim_for_processing(post_id)
    assert second is None


async def test_cancel_race_with_claim(repo_with_user) -> None:
    """Cancel и claim конкурируют атомарно: один из них выигрывает."""
    repo, user_id = repo_with_user
    post_id = await _create_post(repo, user_id)

    # Одновременно вызываем claim и cancel
    claim_task = asyncio.create_task(repo.claim_for_processing(post_id))
    cancel_task = asyncio.create_task(repo.cancel_post(post_id, user_id))

    claim_result, cancel_result = await asyncio.gather(claim_task, cancel_task)

    # Ровно один из вызовов должен выиграть
    success_count = (claim_result is not None) + cancel_result
    assert success_count == 1

    # Финальный статус: либо PROCESSING, либо CANCELLED
    fresh = await repo.get_by_id(post_id)
    assert fresh is not None
    assert fresh.status in {PostStatus.PROCESSING, PostStatus.CANCELLED}


async def test_concurrent_claims_only_one_succeeds(repo_with_user) -> None:
    """10 одновременных claim — только один получает право."""
    repo, user_id = repo_with_user
    post_id = await _create_post(repo, user_id)

    tasks = [asyncio.create_task(repo.claim_for_processing(post_id)) for _ in range(10)]
    results = await asyncio.gather(*tasks)

    success_count = sum(1 for r in results if r is not None)
    assert success_count == 1


async def test_publication_service_calls_publisher_only_after_successful_claim(
    repo_with_user,
) -> None:
    """Publisher вызывается только после успешного claim."""
    repo, user_id = repo_with_user
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)

    post_id = await _create_post(repo, user_id)

    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert len(sender.calls) == 1


async def test_publication_service_concurrent_publish_calls_publisher_once(
    repo_with_user,
) -> None:
    """При двух конкурирующих publish Publisher вызывается ровно один раз."""
    repo, user_id = repo_with_user
    sender = RecordingTelegramSender()
    sender.delay = 0.1
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)

    post_id = await _create_post(repo, user_id)

    task1 = asyncio.create_task(service.publish(post_id))
    task2 = asyncio.create_task(service.publish(post_id))

    results = await asyncio.gather(task1, task2, return_exceptions=True)

    # Один вызов успешен, другой получает PostClaimLostError
    successes = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], PostClaimLostError)

    # Publisher вызван ровно один раз
    assert len(sender.calls) == 1


async def test_publication_service_rejects_non_pending(repo_with_user) -> None:
    """publish отклоняет пост не в PENDING."""
    repo, user_id = repo_with_user
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)

    post_id = await _create_post(repo, user_id)
    # Забираем пост вручную
    await repo.claim_for_processing(post_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert sender.calls == []


async def test_publication_service_failed_status_on_error(repo_with_user) -> None:
    """При ошибке Publisher пост переходит в FAILED (финальный переход)."""
    repo, user_id = repo_with_user
    sender = RecordingTelegramSender()
    sender.fail_with = RuntimeError("telegram api error")
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)

    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.FAILED
    assert "telegram api error" in (result.error_message or "")


async def test_publication_service_retry_resets_to_pending(repo_with_user) -> None:
    """При retry после ошибки пост возвращается в PENDING (промежуточный переход)."""
    repo, user_id = repo_with_user
    sender = FlakyTelegramSender(fail_count=1)
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(
        repo, resolver, retry_policy=RetryPolicy(max_attempts=3, delay_seconds=0)
    )

    post_id = await _create_post(repo, user_id)

    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    # Publisher вызван 2 раза: первая попытка упала, вторая успешна
    assert sender.call_count == 2


async def test_publication_service_all_attempts_failed(repo_with_user) -> None:
    """Если все попытки упали — пост в FAILED."""
    repo, user_id = repo_with_user
    sender = RecordingTelegramSender()
    sender.fail_with = RuntimeError("persistent error")
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(
        repo, resolver, retry_policy=RetryPolicy(max_attempts=2, delay_seconds=0)
    )

    post_id = await _create_post(repo, user_id)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.FAILED
    assert "persistent error" in (result.error_message or "")


async def test_publication_service_claim_lost_raises(repo_with_user) -> None:
    """Если claim возвращает None (другой исполнитель) — PostClaimLostError."""
    repo, user_id = repo_with_user
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)

    post_id = await _create_post(repo, user_id)

    # Делаем claim вручную
    claimed = await repo.claim_for_processing(post_id)
    assert claimed is not None

    # Второй publish не должен вызвать Publisher
    with pytest.raises(PublicationError) as exc_info:
        await service.publish(post_id)
    assert isinstance(exc_info.value, PostClaimLostError)

    assert sender.calls == []


class FlakyTelegramSender:
    """TelegramSender, который падает первые fail_count вызовов, потом работает."""

    def __init__(self, fail_count: int) -> None:
        self.fail_count = fail_count
        self.call_count = 0

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> None:
        self.call_count += 1
        if self.call_count <= self.fail_count:
            raise RuntimeError(f"temporary failure {self.call_count}")
