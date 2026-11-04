"""SPEC-016: Абстракция Publisher, PublisherResolver, интеграция с PublicationService и E2E."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fake_telegram import TEST_TOKEN, FakeTelegram
from harness import db_path, future_date_text, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.config import Settings
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.dialog import DialogState
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.domain.user import User
from postmaster.publishers import (
    REMINDER_TEXT,
    InvalidPublicationTypeError,
    Publisher,
    PublisherNotFoundError,
    PublisherResolver,
    PublisherResolverErrorCode,
    PublishResult,
    ReminderPublisher,
    TelegramPublisher,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.publication_service import PublicationService
from postmaster.services.scheduler_service import SchedulerService

CREATED_AT = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 10, 7, 17, 0, tzinfo=UTC)
CLAIM_AT = datetime(2026, 10, 7, 17, 0, 1, tzinfo=UTC)
PUBLISHED_AT = datetime(2026, 10, 7, 17, 0, 2, tzinfo=UTC)
CHANNEL_ID = "-1001234567890"


class RecordingTelegramClient:
    """Тестовый TelegramClient для проверки вызовов обоих Publisher."""

    def __init__(self) -> None:
        self.messages: list[tuple[str | int, str]] = []
        self.photos: list[tuple[str | int, str, str | None]] = []

    async def send_message(self, chat_id: str | int, text: str) -> None:
        self.messages.append((chat_id, text))

    async def send_photo(
        self,
        chat_id: str | int,
        file_id: str,
        caption: str | None = None,
    ) -> None:
        self.photos.append((chat_id, file_id, caption))


class StubUserRepository:
    def __init__(self, user: User | None) -> None:
        self._user = user

    async def get_by_id(self, user_id: int) -> User | None:
        return self._user


@pytest.fixture
async def spec016_db(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], UserRepository, PostRepository, User]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/spec016.db")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user_repo = UserRepository(factory)
        post_repo = PostRepository(factory)
        user = await user_repo.add(
            telegram_user_id=700222333,
            username="spec016_user",
            first_name="Ирина",
            now=CREATED_AT,
        )
        yield factory, user_repo, post_repo, user
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# §24 Unit Tests (TC-016-01 … TC-016-04)
# ---------------------------------------------------------------------------


def test_tc_016_01_02_resolver_returns_registered_reminder_and_telegram_publishers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TC-016-01, TC-016-02, AC-016-02, AC-016-03, §10–11, §16, §22."""
    client = RecordingTelegramClient()
    user_repo = StubUserRepository(None)
    reminder_pub = ReminderPublisher(client, user_repo)
    telegram_pub = TelegramPublisher(client)

    resolver = PublisherResolver(
        reminder_publisher=reminder_pub,
        telegram_publisher=telegram_pub,
    )

    with caplog.at_level("INFO", logger="postmaster.publishers.resolver"):
        resolved_reminder = resolver.resolve(PublicationType.REMINDER, post_id=10)
        resolved_telegram = resolver.resolve(PublicationType.TELEGRAM, post_id=20)

    assert resolved_reminder is reminder_pub
    assert resolved_telegram is telegram_pub
    # §16: Resolver не пересоздаёт Publisher при каждом вызове.
    assert resolver.resolve(PublicationType.REMINDER) is reminder_pub
    assert resolver.resolve("TELEGRAM") is telegram_pub
    assert "Publisher resolved post_id=10 type=REMINDER" in caplog.text
    assert "Publisher resolved post_id=20 type=TELEGRAM" in caplog.text


@pytest.mark.parametrize("unknown_type", ["UNKNOWN", "VK", "INSTAGRAM"])
def test_tc_016_03_unknown_type_raises_publisher_not_found_and_logs_error(
    unknown_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    """TC-016-03, ERR-016-01, §12, §21, §22: неизвестный тип → PUBLISHER_NOT_FOUND."""
    client = RecordingTelegramClient()
    resolver = PublisherResolver(
        reminder_publisher=ReminderPublisher(client, StubUserRepository(None)),
        telegram_publisher=TelegramPublisher(client),
    )

    with caplog.at_level("ERROR", logger="postmaster.publishers.resolver"):
        with pytest.raises(PublisherNotFoundError) as exc_info:
            resolver.resolve(unknown_type, post_id=42)

    assert exc_info.value.error_code == PublisherResolverErrorCode.PUBLISHER_NOT_FOUND.value
    assert f"Publisher not found post_id=42 publication_type={unknown_type}" in caplog.text


@pytest.mark.parametrize("invalid_type", [None, "", "   ", 123, True])
def test_err_016_02_invalid_publication_type_raises_invalid_publication_type(
    invalid_type: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """ERR-016-02, §12, §21: некорректный тип публикации → INVALID_PUBLICATION_TYPE."""
    resolver = PublisherResolver()

    with caplog.at_level("ERROR", logger="postmaster.publishers.resolver"):
        with pytest.raises(InvalidPublicationTypeError) as exc_info:
            resolver.resolve(invalid_type, post_id=99)

    assert exc_info.value.error_code == PublisherResolverErrorCode.INVALID_PUBLICATION_TYPE.value
    assert "Publisher not found post_id=99" in caplog.text


async def test_tc_016_04_both_publishers_implement_unified_publish_contract() -> None:
    """TC-016-04, AC-016-01, AC-016-07, BR-016-01…03, INV-016-01."""
    user = User(
        id=7,
        telegram_user_id=500600,
        username="unified",
        first_name="Олег",
        timezone="Europe/Moscow",
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )
    client = RecordingTelegramClient()
    reminder_pub = ReminderPublisher(client, StubUserRepository(user))
    telegram_pub = TelegramPublisher(client)

    assert isinstance(reminder_pub, Publisher)
    assert isinstance(telegram_pub, Publisher)

    reminder_post = Post(
        id=1,
        user_id=user.id,
        telegram_file_id="AgAC_rem_1",
        telegram_file_unique_id="AQAD_rem_1",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        status=PostStatus.PROCESSING,
        publication_type=PublicationType.REMINDER,
        created_at=CREATED_AT,
        updated_at=CLAIM_AT,
    )
    telegram_post = Post(
        id=2,
        user_id=user.id,
        telegram_file_id="AgAC_tg_2",
        telegram_file_unique_id="AQAD_tg_2",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        status=PostStatus.PROCESSING,
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        created_at=CREATED_AT,
        updated_at=CLAIM_AT,
    )

    res_rem = await reminder_pub.publish(reminder_post)
    res_tg = await telegram_pub.publish(telegram_post)

    assert isinstance(res_rem, PublishResult) and res_rem == PublishResult(
        success=True, error_code=None, error_message=None
    )
    assert isinstance(res_tg, PublishResult) and res_tg == PublishResult(
        success=True, error_code=None, error_message=None
    )


# ---------------------------------------------------------------------------
# §25 PublicationService Tests & §26 Integration Tests (IT-016-01 … IT-016-03)
# ---------------------------------------------------------------------------


async def test_publication_service_calls_mark_published_on_success_and_error_flow_on_failure(
    spec016_db,
) -> None:
    """§25 PublicationService Tests, AC-016-04, AC-016-08, BR-016-07…08, INV-016-04…05."""
    _, _, post_repo, user = spec016_db

    class StubResultPublisher(Publisher):
        def __init__(self, result: PublishResult) -> None:
            self._result = result
            self.calls: list[int] = []

        async def publish(self, post: Any) -> PublishResult:
            self.calls.append(post.id)
            return self._result

    ok_pub = StubResultPublisher(PublishResult(success=True))
    fail_pub = StubResultPublisher(
        PublishResult(
            success=False,
            error_code="TELEGRAM_PERMISSION_DENIED",
            error_message="Forbidden",
        )
    )
    resolver = PublisherResolver(
        reminder_publisher=ok_pub,
        telegram_publisher=fail_pub,
    )
    service = PublicationService(
        post_repo,
        publisher_resolver=resolver,
        now_fn=lambda: PUBLISHED_AT,
    )
    assert service.publisher_resolver is resolver

    # 1) success=True → mark_published()
    post_ok = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_ok",
        telegram_file_unique_id="AQAD_ok",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        now=CREATED_AT,
    )
    assert await service.execute(post_ok.id) is True
    saved_ok = await post_repo.get_by_id(post_ok.id)
    assert saved_ok is not None and saved_ok.status == PostStatus.PUBLISHED
    assert saved_ok.published_at == PUBLISHED_AT

    # 2) success=False → error flow (mark_failed)
    post_fail = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_fail",
        telegram_file_unique_id="AQAD_fail",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        now=CREATED_AT,
    )
    assert await service.execute(post_fail.id) is False
    saved_fail = await post_repo.get_by_id(post_fail.id)
    assert saved_fail is not None and saved_fail.status == PostStatus.FAILED
    assert saved_fail.error_message == "Forbidden"

    # 3) AC-016-04, BR-016-08, ERR-016-01: незарегистрированный тип не вызывает Publisher
    only_reminder_resolver = PublisherResolver(reminder_publisher=ok_pub)
    service_unregistered = PublicationService(
        post_repo,
        publisher_resolver=only_reminder_resolver,
        now_fn=lambda: PUBLISHED_AT,
    )
    post_unreg = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_unreg",
        telegram_file_unique_id="AQAD_unreg",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        now=CREATED_AT,
    )
    calls_before = list(ok_pub.calls)
    assert await service_unregistered.execute(post_unreg.id) is False
    assert ok_pub.calls == calls_before
    saved_unreg = await post_repo.get_by_id(post_unreg.id)
    assert saved_unreg is not None and saved_unreg.status == PostStatus.FAILED
    assert saved_unreg.error_message is not None
    assert "PUBLISHER_NOT_FOUND" in saved_unreg.error_message


async def test_it_016_01_02_03_and_vk_extensibility_without_scheduler_changes(
    spec016_db,
) -> None:
    """§20, §26 IT-016-01…03, AC-016-05…06, AC-016-09: REMINDER, TELEGRAM и расширение VK."""
    _, user_repo, post_repo, user = spec016_db
    client = RecordingTelegramClient()
    reminder_pub = ReminderPublisher(client, user_repo)
    telegram_pub = TelegramPublisher(client)
    resolver = PublisherResolver(
        reminder_publisher=reminder_pub,
        telegram_publisher=telegram_pub,
    )
    pub_service = PublicationService(
        post_repo,
        publisher_resolver=resolver,
        now_fn=lambda: PUBLISHED_AT,
    )
    scheduler = SchedulerService(post_repo, pub_service)

    # IT-016-01 & IT-016-03 (REMINDER): PENDING → PROCESSING → Resolver → PUBLISHED
    rem_post = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_it16_rem",
        telegram_file_unique_id="AQAD_it16_rem",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        caption="Подпись напоминания",
        now=CREATED_AT,
    )
    await scheduler._on_post_due(rem_post.id)

    rem_saved = await post_repo.get_by_id(rem_post.id)
    assert rem_saved is not None and rem_saved.status == PostStatus.PUBLISHED
    assert client.messages == [(user.telegram_user_id, REMINDER_TEXT)]
    assert client.photos[0] == (
        user.telegram_user_id,
        "AgAC_it16_rem",
        "Подпись напоминания",
    )

    # IT-016-02 & IT-016-03 (TELEGRAM): PENDING → PROCESSING → Resolver → PUBLISHED
    tg_post = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_it16_tg",
        telegram_file_unique_id="AQAD_it16_tg",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        target_id=CHANNEL_ID,
        caption="Подпись канала",
        now=CREATED_AT,
    )
    await scheduler._on_post_due(tg_post.id)

    tg_saved = await post_repo.get_by_id(tg_post.id)
    assert tg_saved is not None and tg_saved.status == PostStatus.PUBLISHED
    assert client.photos[1] == (CHANNEL_ID, "AgAC_it16_tg", "Подпись канала")

    # §20, US-016-03, AC-016-09: регистрация VKPublisher без правок Scheduler
    class VKPublisher(Publisher):
        def __init__(self) -> None:
            self.published_ids: list[int] = []

        async def publish(self, post: Any) -> PublishResult:
            self.published_ids.append(post.id)
            return PublishResult(success=True)

    vk_pub = VKPublisher()
    resolver.register("VK", vk_pub)
    assert resolver.resolve("VK") is vk_pub
    vk_res = await resolver.resolve("VK").publish(SimpleNamespace(id=777))
    assert vk_res.success is True
    assert vk_pub.published_ids == [777]


# ---------------------------------------------------------------------------
# §27 E2E Scenarios (REMINDER & TELEGRAM через Application.from_settings)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("post_mode", "target_id"),
    [
        ("REMINDER", None),
        ("TELEGRAM", CHANNEL_ID),
    ],
)
async def test_e2e_27_scheduler_to_resolver_to_publisher_for_reminder_and_telegram(
    post_mode: str,
    target_id: str | None,
    fake_telegram: FakeTelegram,
    tmp_path: Path,
) -> None:
    """§27 E2E: Scheduler → PublicationService → Claim → Resolver → Publisher → PUBLISHED."""
    settings = Settings(
        bot_token=TEST_TOKEN,
        database_url=f"sqlite+aiosqlite:///{db_path(tmp_path)}",
        log_level="INFO",
        post_mode=post_mode,
        telegram_target_id=target_id,
    )
    app = Application.from_settings(settings)
    task = asyncio.create_task(app.run())
    try:
        await asyncio.wait_for(fake_telegram.get_updates_called.wait(), timeout=5.0)
        fake_telegram.add_text_message("/start", chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 1)

        fake_telegram.add_photo_message(f"photo-{post_mode}", chat_id=42, user_id=42)
        await wait_for_state(app, 42, DialogState.WAITING_DATETIME)

        fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 3)
        await wait_for_state(app, 42, DialogState.IDLE)

        with sqlite3.connect(db_path(tmp_path)) as conn:
            post_id, saved_type, status = conn.execute(
                "SELECT id, publication_type, status FROM posts"
            ).fetchone()
        assert (saved_type, status) == (post_mode, "PENDING")
        before_idx = len(fake_telegram.outbound_events)

        await app._scheduler._on_post_due(post_id)

        events = fake_telegram.outbound_events[before_idx:]
        if post_mode == "REMINDER":
            assert [e["method"] for e in events] == ["sendMessage", "sendPhoto"]
            assert events[0]["chat_id"] == "42"
            assert events[0]["text"] == REMINDER_TEXT
            assert events[1]["chat_id"] == "42"
            assert events[1]["photo"] == "photo-REMINDER"
        else:
            assert [e["method"] for e in events] == ["sendPhoto"]
            assert events[0]["chat_id"] == CHANNEL_ID
            assert events[0]["photo"] == "photo-TELEGRAM"

        with sqlite3.connect(db_path(tmp_path)) as conn:
            final_status, published_at = conn.execute(
                "SELECT status, published_at FROM posts WHERE id=?",
                (post_id,),
            ).fetchone()
        assert final_status == "PUBLISHED"
        assert published_at is not None
    finally:
        await app.stop()
        await asyncio.wait_for(task, timeout=5.0)
