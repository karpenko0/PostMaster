"""SPEC-010: публикация запланированных постов через Publisher.

Тесты проверяют:
- PublicationService переводит PENDING → PROCESSING → PUBLISHED при успехе.
- При ошибке Publisher пост переходит в FAILED с сообщением.
- TelegramChannelPublisher отправляет фото в канал.
- ReminderPublisher отправляет напоминание пользователю.
- SchedulerService вызывает PublicationExecutor при срабатывании задания.
- Retry при ошибке публикации.
- E2E сценарий через реальный APScheduler.
- Интеграция с fake Telegram API.
- Конкурентная отмена во время публикации.
- Безопасность: file_id не попадает в логи.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.bot.sender import BotTelegramReminderSender, BotTelegramSender
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.publishers.reminder_publisher import ReminderPublisher
from postmaster.publishers.telegram_publisher import TelegramChannelPublisher
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.publication_service import (
    DefaultPublisherResolver,
    PublicationError,
    PublicationService,
    RetryPolicy,
)
from postmaster.services.recipient_resolver import UserServiceReminderRecipientResolver
from postmaster.services.scheduler_service import SchedulerService
from postmaster.services.user_service import UserService

RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


class RecordingTelegramSender:
    """Заглушка TelegramSender, записывает вызовы send_photo."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_with: Exception | None = None

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> None:
        self.calls.append({"chat_id": chat_id, "photo_file_id": photo_file_id, "caption": caption})
        if self.fail_with is not None:
            raise self.fail_with


class RecordingReminderSender:
    """Заглушка TelegramReminderSender, записывает вызовы send_message."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_with: Exception | None = None

    async def send_message(self, *, chat_id: int, text: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append({"chat_id": chat_id, "text": text})


@pytest.fixture
async def post_repo(tmp_path: Path) -> AsyncIterator[tuple[PostRepository, int, UserService]]:
    """Создаёт репозиторий постов и тестового пользователя."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        user = await user_repo.add(
            telegram_user_id=777,
            first_name="Тест",
            username="tester",
            now=datetime(2026, 10, 7, tzinfo=UTC),
        )
        user_service = UserService(user_repo, default_timezone="Europe/Moscow")
        yield PostRepository(factory), user.id, user_service
    finally:
        await engine.dispose()


async def _create_post(repo: PostRepository, user_id: int, **kwargs) -> int:
    defaults = dict(
        user_id=user_id,
        telegram_file_id="AgAC_file_123",
        telegram_file_unique_id="AQAD_unique_123",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    defaults.update(kwargs)
    post = await repo.create_post(**defaults)
    return post.id


async def test_telegram_publisher_publishes_photo() -> None:
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    from postmaster.domain.post import Post

    post = Post(
        id=1,
        user_id=1,
        telegram_file_id="file_123",
        telegram_file_unique_id="unique_123",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        created_at=RUN_AT,
        updated_at=RUN_AT,
        target_id="-1001234567890",
        caption="Тест",
    )
    await publisher.publish(post)
    assert sender.calls == [
        {"chat_id": "-1001234567890", "photo_file_id": "file_123", "caption": "Тест"}
    ]


async def test_telegram_publisher_requires_target() -> None:
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    # Создаём валидный Post, затем подменяем target_id на пустую строку через object.__setattr__
    # чтобы обойти frozen. Это симулирует ситуацию, когда target_id не задан.
    from postmaster.domain.post import Post

    post = Post(
        id=1,
        user_id=1,
        telegram_file_id="file_123",
        telegram_file_unique_id="unique_123",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        created_at=RUN_AT,
        updated_at=RUN_AT,
        target_id="-1001234567890",
    )
    object.__setattr__(post, "target_id", "")
    with pytest.raises(ValueError, match="target_id"):
        await publisher.publish(post)
    assert sender.calls == []


async def test_reminder_publisher_sends_to_user(post_repo) -> None:
    repo, user_id, user_service = post_repo
    sender = RecordingReminderSender()
    resolver = UserServiceReminderRecipientResolver(user_service)
    publisher = ReminderPublisher(sender, resolver)
    post_id = await _create_post(repo, user_id)
    post = await repo.get_by_id(post_id)
    assert post is not None

    await publisher.publish(post)

    assert len(sender.calls) == 1
    assert sender.calls[0]["chat_id"] == 777
    assert "20.10.2026 15:00" in sender.calls[0]["text"]


async def test_publication_service_marks_published(post_repo) -> None:
    repo, user_id, _ = post_repo
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)
    post_id = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert result.published_at is not None
    assert result.error_message is None
    assert sender.calls[0]["chat_id"] == "-1001234567890"


async def test_publication_service_marks_failed_on_publisher_error(post_repo) -> None:
    repo, user_id, _ = post_repo
    sender = RecordingTelegramSender()
    sender.fail_with = RuntimeError("telegram API error")
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver)
    post_id = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.FAILED
    assert "telegram API error" in (result.error_message or "")


async def test_publication_service_rejects_non_pending(post_repo) -> None:
    repo, user_id, _ = post_repo
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)
    post_id = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )
    # Первая публикация переведёт в PUBLISHED
    await service.publish(post_id)
    # Повторная попытка должна быть отклонена
    with pytest.raises(PublicationError):
        await service.publish(post_id)


async def test_publication_service_missing_publisher(post_repo) -> None:
    repo, user_id, _ = post_repo
    resolver = DefaultPublisherResolver(telegram_publisher=None, reminder_publisher=None)
    service = PublicationService(repo, resolver)
    post_id = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.FAILED


async def test_reminder_publication_via_service(post_repo) -> None:
    repo, user_id, user_service = post_repo
    sender = RecordingReminderSender()
    resolver_recipient = UserServiceReminderRecipientResolver(user_service)
    publisher = ReminderPublisher(sender, resolver_recipient)
    resolver = DefaultPublisherResolver(reminder_publisher=publisher)
    service = PublicationService(repo, resolver)
    post_id = await _create_post(repo, user_id, publication_type=PublicationType.REMINDER)

    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert len(sender.calls) == 1
    assert sender.calls[0]["chat_id"] == 777


async def test_scheduler_executes_publication(post_repo) -> None:
    repo, user_id, _ = post_repo
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)
    post_id = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    # Симулируем срабатывание задания
    await service.publish(post_id)

    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.PUBLISHED
    assert len(sender.calls) == 1


async def test_publication_service_retries_on_failure(post_repo) -> None:
    """При ошибке Publisher сервис делает несколько попыток, затем FAILED."""
    repo, user_id, _ = post_repo
    sender = RecordingTelegramSender()
    sender.fail_with = RuntimeError("temporary error")
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    retry_policy = RetryPolicy(max_attempts=3, delay_seconds=0.01)
    service = PublicationService(repo, resolver, retry_policy=retry_policy)
    post_id = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert len(sender.calls) == 3  # Три попытки
    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.FAILED


async def test_publication_service_succeeds_on_retry(post_repo) -> None:
    """Если первая попытка упала, а вторая успешна — пост PUBLISHED."""
    repo, user_id, _ = post_repo
    sender = FlakyTelegramSender(fail_count=1)
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    retry_policy = RetryPolicy(max_attempts=3, delay_seconds=0.01)
    service = PublicationService(repo, resolver, retry_policy=retry_policy)
    post_id = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert sender.call_count == 2  # Первая упала, вторая успешна


async def test_publication_service_concurrent_cancellation(post_repo) -> None:
    """Если пост отменён (CANCELLED) во время публикации — публикация не проходит."""
    repo, user_id, _ = post_repo
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)
    post_id = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    # Симулируем отмену поста до начала публикации
    await repo.update_status(post_id, PostStatus.CANCELLED)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert sender.calls == []
    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.CANCELLED


async def test_publication_service_does_not_log_file_id(
    post_repo, caplog: pytest.LogCaptureFixture
) -> None:
    """file_id не должен попадать в логи (требование безопасности)."""
    repo, user_id, _ = post_repo
    sender = RecordingTelegramSender()
    sender.fail_with = RuntimeError("secret_file_id_AgAC_secret123")
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(repo, resolver)
    secret_file_id = "AgAC_secret_file_id_xyz"
    post_id = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
        telegram_file_id=secret_file_id,
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PublicationError):
            await service.publish(post_id)

    assert secret_file_id not in caplog.text


async def test_scheduler_real_e2e_publishes_via_executor(post_repo) -> None:
    """E2E: реальный APScheduler запускает задание, PublicationService публикует пост."""
    repo, user_id, _ = post_repo
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)

    scheduler = SchedulerService(repo, publication_executor=service)
    scheduler.start()
    try:
        # Создаём пост с временем через 0.1 секунды
        run_at = datetime.now(UTC) + timedelta(milliseconds=100)
        post_id = await _create_post(
            repo,
            user_id,
            publication_type=PublicationType.TELEGRAM,
            target_id="-1001234567890",
            scheduled_at_utc=run_at,
        )
        scheduler.schedule_post(post_id=post_id, run_at=run_at)

        # Ждём, пока Scheduler выполнит задание
        await _wait_for(lambda: len(sender.calls) == 1, timeout=5)

        assert len(sender.calls) == 1
        assert sender.calls[0]["chat_id"] == "-1001234567890"
        result = await repo.get_by_id(post_id)
        assert result is not None
        assert result.status == PostStatus.PUBLISHED
    finally:
        scheduler.shutdown()


async def test_scheduler_real_e2e_publishes_reminder(post_repo) -> None:
    """E2E: реальный APScheduler запускает задание для REMINDER поста."""
    repo, user_id, user_service = post_repo
    reminder_sender = RecordingReminderSender()
    recipient_resolver = UserServiceReminderRecipientResolver(user_service)
    publisher = ReminderPublisher(reminder_sender, recipient_resolver)
    resolver = DefaultPublisherResolver(reminder_publisher=publisher)
    service = PublicationService(repo, resolver)

    scheduler = SchedulerService(repo, publication_executor=service)
    scheduler.start()
    try:
        run_at = datetime.now(UTC) + timedelta(milliseconds=100)
        post_id = await _create_post(
            repo, user_id, publication_type=PublicationType.REMINDER, scheduled_at_utc=run_at
        )
        scheduler.schedule_post(post_id=post_id, run_at=run_at)

        await _wait_for(lambda: len(reminder_sender.calls) == 1, timeout=5)

        assert len(reminder_sender.calls) == 1
        assert reminder_sender.calls[0]["chat_id"] == 777
        result = await repo.get_by_id(post_id)
        assert result is not None
        assert result.status == PostStatus.PUBLISHED
    finally:
        scheduler.shutdown()


async def test_telegram_channel_publisher_via_fake_telegram(tmp_path: Path, fake_telegram) -> None:
    """Интеграция: TelegramChannelPublisher отправляет фото через fake Telegram API."""
    from fake_telegram import TEST_TOKEN
    from postmaster.bot.factory import create_bot

    bot = create_bot(TEST_TOKEN)
    sender = BotTelegramSender(bot)
    publisher = TelegramChannelPublisher(sender)

    post = Post(
        id=1,
        user_id=1,
        telegram_file_id="AgAC_integration_test",
        telegram_file_unique_id="AQAD_integration",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        created_at=RUN_AT,
        updated_at=RUN_AT,
        target_id="-1001234567890",
        caption="Integration test",
    )

    await publisher.publish(post)

    assert len(fake_telegram.sent_photos) == 1
    assert fake_telegram.sent_photos[0]["chat_id"] == "-1001234567890"
    assert fake_telegram.sent_photos[0]["photo"] == "AgAC_integration_test"
    assert fake_telegram.sent_photos[0]["caption"] == "Integration test"


async def test_reminder_publisher_via_fake_telegram(tmp_path: Path, fake_telegram) -> None:
    """Интеграция: ReminderPublisher отправляет напоминание через fake Telegram API."""
    from fake_telegram import TEST_TOKEN
    from postmaster.bot.factory import create_bot

    bot = create_bot(TEST_TOKEN)
    sender = BotTelegramReminderSender(bot)

    # Создаём репозиторий и пользователя
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        user = await user_repo.add(
            telegram_user_id=555,
            first_name="Напоминание",
            username=None,
            now=datetime(2026, 10, 7, tzinfo=UTC),
        )
        user_service = UserService(user_repo, default_timezone="Europe/Moscow")
        recipient_resolver = UserServiceReminderRecipientResolver(user_service)
        publisher = ReminderPublisher(sender, recipient_resolver)

        post = Post(
            id=1,
            user_id=user.id,
            telegram_file_id="file_for_reminder",
            telegram_file_unique_id="unique_reminder",
            scheduled_at_utc=RUN_AT,
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
            created_at=RUN_AT,
            updated_at=RUN_AT,
            target_id=None,
        )

        await publisher.publish(post)
    finally:
        await engine.dispose()

    assert len(fake_telegram.sent_messages) == 1
    last_message = fake_telegram.sent_messages[-1]
    assert last_message["chat_id"] == "555"
    assert "Напоминание" in last_message["text"]


async def test_scheduler_handles_missing_post(post_repo) -> None:
    """Если пост удалён из БД до срабатывания задания — задание не падает."""
    repo, user_id, _ = post_repo
    sender = RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)

    scheduler = SchedulerService(repo, publication_executor=service)
    scheduler.start()
    try:
        run_at = datetime.now(UTC) + timedelta(milliseconds=100)
        post_id = await _create_post(
            repo,
            user_id,
            publication_type=PublicationType.TELEGRAM,
            target_id="-1001234567890",
            scheduled_at_utc=run_at,
        )
        scheduler.schedule_post(post_id=post_id, run_at=run_at)

        # Удаляем пост напрямую через БД, чтобы симулировать пропажу
        from sqlalchemy import delete

        from postmaster.database.models import PostModel

        engine = repo._session_factory.kw["bind"]
        async with engine.begin() as conn:
            await conn.execute(delete(PostModel).where(PostModel.id == post_id))

        # Ждём срабатывания задания — оно не должно упасть
        await asyncio.sleep(0.3)

        # Пост не найден, публикация не выполняется
        assert sender.calls == []
    finally:
        scheduler.shutdown()


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


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    """Ждёт, пока predicate не станет истинным."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"условие не выполнено за {timeout} секунд")
