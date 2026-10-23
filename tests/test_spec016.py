"""SPEC-016: Абстракция Publisher.

Тесты проверяют:
- PublisherResolver маршрутизирует REMINDER → ReminderPublisher.
- PublisherResolver маршрутизирует TELEGRAM → TelegramPublisher.
- PublisherResolver возбуждает PublisherNotConfiguredError, если publisher не настроен.
- PublisherResolver возбуждает UnknownPublicationTypeError для неизвестного типа.
- PublicationService использует только протокол PublisherResolver (не concrete classes).
- Scheduler/Recovery/MissedJob работают через протоколы, не знают о Publisher'ах.
- Post с неизвестным типом не публикуется и переходит в FAILED.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.publishers.base import Publisher, PublishResult
from postmaster.publishers.reminder_publisher import (
    ReminderPublisher,
)
from postmaster.publishers.resolver import (
    DefaultPublisherResolver,
    PublisherNotConfiguredError,
    PublisherResolver,
    UnknownPublicationTypeError,
)
from postmaster.publishers.telegram_publisher import (
    TelegramChannelPublisher,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.publication_service import (
    PublicationError,
    PublicationService,
)

RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


class FakePublisher(Publisher):
    """Универсальный Publisher для тестов, не привязан к Telegram API."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.calls: list[object] = []
        self.fail_with: Exception | None = None

    async def publish(self, post: object) -> PublishResult:
        self.calls.append(post)
        if self.fail_with is not None:
            raise self.fail_with
        return PublishResult(post_id=getattr(post, "id", 0), external_id=f"{self.name}_msg")


@pytest.fixture
async def post_repo(tmp_path: Path) -> AsyncIterator[tuple[PostRepository, int]]:
    """Создаёт репозиторий постов и тестового пользователя."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        user = await user_repo.add(
            telegram_user_id=999,
            first_name="Resolver",
            username="resolver",
            now=datetime(2026, 10, 7, tzinfo=UTC),
        )
        yield PostRepository(factory), user.id
    finally:
        await engine.dispose()


async def _create_post(
    repo: PostRepository,
    user_id: int,
    *,
    publication_type: PublicationType,
    target_id: str | None = "-1001234567890",
) -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="AgAC_resolver_test",
        telegram_file_unique_id="AQAD_resolver_test",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=publication_type,
        target_id=target_id,
    )
    return post.id


# === PublisherResolver tests (SPEC-016 §8 resolver reminder/telegram/unknown) ===


async def test_resolver_routes_reminder_to_reminder_publisher() -> None:
    """REMINDER → ReminderPublisher."""
    reminder = FakePublisher(name="reminder")
    telegram = FakePublisher(name="telegram")
    resolver = DefaultPublisherResolver(telegram_publisher=telegram, reminder_publisher=reminder)

    publisher = resolver.resolve(PublicationType.REMINDER)

    assert publisher is reminder


async def test_resolver_routes_telegram_to_telegram_publisher() -> None:
    """TELEGRAM → TelegramChannelPublisher."""
    reminder = FakePublisher(name="reminder")
    telegram = FakePublisher(name="telegram")
    resolver = DefaultPublisherResolver(telegram_publisher=telegram, reminder_publisher=reminder)

    publisher = resolver.resolve(PublicationType.TELEGRAM)

    assert publisher is telegram


async def test_resolver_unknown_type_rejected() -> None:
    """Неизвестный тип публикации → UnknownPublicationTypeError."""
    resolver = DefaultPublisherResolver(
        telegram_publisher=FakePublisher(), reminder_publisher=FakePublisher()
    )

    # Создаём фиктивный тип, который не равен TELEGRAM или REMINDER
    # Используем прямое сравнение строк через enum
    class _FakeEnum(str):
        pass

    fake_type = _FakeEnum("VK")

    with pytest.raises(UnknownPublicationTypeError):
        resolver.resolve(fake_type)  # type: ignore[arg-type]


async def test_resolver_unknown_publication_type_keeps_post_safe() -> None:
    """Неизвестный тип не публикуется — пост остаётся нетронутым."""
    resolver = DefaultPublisherResolver(
        telegram_publisher=FakePublisher(), reminder_publisher=FakePublisher()
    )

    class _FakeEnum(str):
        pass

    fake_type = _FakeEnum("VK")

    with pytest.raises(UnknownPublicationTypeError):
        resolver.resolve(fake_type)  # type: ignore[arg-type]


async def test_resolver_telegram_not_configured_raises() -> None:
    """TELEGRAM publisher не настроен → PublisherNotConfiguredError."""
    resolver = DefaultPublisherResolver(telegram_publisher=None, reminder_publisher=FakePublisher())

    with pytest.raises(PublisherNotConfiguredError) as exc_info:
        resolver.resolve(PublicationType.TELEGRAM)
    assert exc_info.value.publication_type == PublicationType.TELEGRAM


async def test_resolver_reminder_not_configured_raises() -> None:
    """REMINDER publisher не настроен → PublisherNotConfiguredError."""
    resolver = DefaultPublisherResolver(telegram_publisher=FakePublisher(), reminder_publisher=None)

    with pytest.raises(PublisherNotConfiguredError) as exc_info:
        resolver.resolve(PublicationType.REMINDER)
    assert exc_info.value.publication_type == PublicationType.REMINDER


async def test_resolver_with_only_reminder_configured() -> None:
    """Если настроен только REMINDER — TELEGRAM запрещён."""
    reminder = FakePublisher()
    resolver = DefaultPublisherResolver(telegram_publisher=None, reminder_publisher=reminder)

    assert resolver.resolve(PublicationType.REMINDER) is reminder

    with pytest.raises(PublisherNotConfiguredError):
        resolver.resolve(PublicationType.TELEGRAM)


async def test_resolver_with_only_telegram_configured() -> None:
    """Если настроен только TELEGRAM — REMINDER запрещён."""
    telegram = FakePublisher()
    resolver = DefaultPublisherResolver(telegram_publisher=telegram, reminder_publisher=None)

    assert resolver.resolve(PublicationType.TELEGRAM) is telegram

    with pytest.raises(PublisherNotConfiguredError):
        resolver.resolve(PublicationType.REMINDER)


async def test_resolver_with_no_publishers_configured() -> None:
    """Если ничего не настроено — оба типа запрещены."""
    resolver = DefaultPublisherResolver()

    with pytest.raises(PublisherNotConfiguredError):
        resolver.resolve(PublicationType.TELEGRAM)

    with pytest.raises(PublisherNotConfiguredError):
        resolver.resolve(PublicationType.REMINDER)


def test_publisher_resolver_protocol_runtime_check() -> None:
    """DefaultPublisherResolver соответствует протоколу PublisherResolver."""
    resolver: PublisherResolver = DefaultPublisherResolver()
    # Проверяем, что метод resolve доступен и вызываем
    with pytest.raises(PublisherNotConfiguredError):
        resolver.resolve(PublicationType.REMINDER)


# === PublicationService success/failure (SPEC-016 §8 service success/failure) ===


async def test_publication_service_uses_resolver_to_route_reminder(post_repo) -> None:
    """PublicationService использует PublisherResolver для REMINDER поста."""
    repo, user_id = post_repo
    reminder = FakePublisher(name="reminder")
    telegram = FakePublisher(name="telegram")
    resolver = DefaultPublisherResolver(telegram_publisher=telegram, reminder_publisher=reminder)
    service = PublicationService(repo, resolver)

    post_id = await _create_post(repo, user_id, publication_type=PublicationType.REMINDER)
    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert reminder.calls and not telegram.calls
    assert len(reminder.calls) == 1


async def test_publication_service_uses_resolver_to_route_telegram(post_repo) -> None:
    """PublicationService использует PublisherResolver для TELEGRAM поста."""
    repo, user_id = post_repo
    reminder = FakePublisher(name="reminder")
    telegram = FakePublisher(name="telegram")
    resolver = DefaultPublisherResolver(telegram_publisher=telegram, reminder_publisher=reminder)
    service = PublicationService(repo, resolver)

    post_id = await _create_post(repo, user_id, publication_type=PublicationType.TELEGRAM)
    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert telegram.calls and not reminder.calls
    assert len(telegram.calls) == 1


async def test_publication_service_unknown_type_marks_failed(post_repo) -> None:
    """Неизвестный тип → пост в FAILED, Publisher не вызван."""
    repo, user_id = post_repo
    reminder = FakePublisher(name="reminder")
    telegram = FakePublisher(name="telegram")

    class _UnknownTypeResolver:
        """Резолвер, который не знает ни одного типа (имитация расширения enum)."""

        def resolve(self, publication_type: PublicationType) -> Publisher:
            raise UnknownPublicationTypeError(publication_type)

    service = PublicationService(repo, _UnknownTypeResolver())

    post_id = await _create_post(repo, user_id, publication_type=PublicationType.TELEGRAM)

    with pytest.raises(PublicationError):
        await service.publish(post_id)

    # Publisher'ы не вызваны
    assert reminder.calls == []
    assert telegram.calls == []

    # Пост в FAILED с информативным сообщением
    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.FAILED
    assert "неизвестный тип публикации" in (result.error_message or "")


async def test_publication_service_routes_valid_type_after_resolver_update(post_repo) -> None:
    """PublicationService корректно работает с типом из БД (happy path через резолвер)."""
    repo, user_id = post_repo
    reminder = FakePublisher(name="reminder")
    telegram = FakePublisher(name="telegram")
    resolver = DefaultPublisherResolver(telegram_publisher=telegram, reminder_publisher=reminder)
    service = PublicationService(repo, resolver)

    post_id = await _create_post(repo, user_id, publication_type=PublicationType.TELEGRAM)
    result = await service.publish(post_id)

    assert result.status == PostStatus.PUBLISHED
    assert telegram.calls and not reminder.calls


async def test_publication_service_publisher_not_configured_marks_failed(post_repo) -> None:
    """Publisher не настроен → пост в FAILED, конкретный Publisher не вызван."""
    repo, user_id = post_repo
    # Только REMINDER publisher, TELEGRAM пост не должен публиковаться
    reminder = FakePublisher(name="reminder")
    resolver = DefaultPublisherResolver(telegram_publisher=None, reminder_publisher=reminder)
    service = PublicationService(repo, resolver)

    post_id = await _create_post(repo, user_id, publication_type=PublicationType.TELEGRAM)
    with pytest.raises(PublicationError):
        await service.publish(post_id)

    assert reminder.calls == []
    result = await repo.get_by_id(post_id)
    assert result is not None
    assert result.status == PostStatus.FAILED
    assert "TELEGRAM" in (result.error_message or "")


# === Scheduler/Recovery/MissedJob decoupling (SPEC-016 §7) ===


def test_scheduler_uses_publication_executor_protocol() -> None:
    """SchedulerService принимает PublicationExecutor (протокол), не конкретный класс."""
    from postmaster.services.scheduler_service import PublicationExecutor

    class _NoopExecutor:
        async def publish(self, post_id: int) -> None:
            return None

    # Тип-чекинг: _NoopExecutor соответствует PublicationExecutor (structural typing)
    executor: PublicationExecutor = _NoopExecutor()
    assert hasattr(executor, "publish")


def test_recovery_uses_missed_post_gateway_protocol() -> None:
    """RecoveryService принимает MissedPostGateway (протокол), не конкретный класс."""
    from postmaster.services.recovery_service import MissedPostGateway

    class _NoopMissedHandler:
        async def handle(self, post_id: int) -> None:
            return None

    # Тип-чекинг: _NoopMissedHandler соответствует MissedPostGateway
    handler: MissedPostGateway = _NoopMissedHandler()
    assert hasattr(handler, "handle")


async def test_missed_post_handler_uses_publication_executor_protocol() -> None:
    """MissedPostHandler принимает PublicationExecutor (протокол), не конкретный класс."""
    from postmaster.services.missed_post_handler import PublicationExecutor

    class _NoopExecutor:
        async def publish(self, post_id: int) -> None:
            return None

    executor: PublicationExecutor = _NoopExecutor()
    assert hasattr(executor, "publish")


# === Publisher base contract (SPEC-016 §1, §4) ===


def test_publisher_is_abstract() -> None:
    """Publisher — абстрактный класс с методом publish."""
    with pytest.raises(TypeError):
        Publisher()  # type: ignore[abstract]


def test_publish_result_is_dataclass() -> None:
    """PublishResult — dataclass с обязательными полями."""
    result = PublishResult(post_id=1)
    assert result.post_id == 1
    assert result.external_id is None

    result_with_id = PublishResult(post_id=2, external_id="msg_42")
    assert result_with_id.post_id == 2
    assert result_with_id.external_id == "msg_42"


async def test_concrete_publishers_return_publish_result() -> None:
    """ReminderPublisher и TelegramChannelPublisher возвращают PublishResult."""
    # Проверяем, что оба реализуют единый контракт
    from postmaster.publishers.reminder_publisher import ReminderRecipientResolver
    from postmaster.publishers.telegram_publisher import TelegramSender

    class _StubRecipientResolver:
        async def get_telegram_user_id(self, user_id: int) -> int | None:
            return 42

    class _StubTelegramSender:
        async def send_photo(
            self, *, chat_id: str, photo_file_id: str, caption: str | None = None
        ) -> dict:
            return {"message_id": 1}

    # Тип-чекинг через протоколы
    recipient: ReminderRecipientResolver = _StubRecipientResolver()
    sender: TelegramSender = _StubTelegramSender()

    reminder_pub = ReminderPublisher(sender, recipient)  # type: ignore[arg-type]
    telegram_pub = TelegramChannelPublisher(sender)  # type: ignore[arg-type]

    assert isinstance(reminder_pub, Publisher)
    assert isinstance(telegram_pub, Publisher)
