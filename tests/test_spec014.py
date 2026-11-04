"""SPEC-014: ReminderPublisher, контракты, unit, integration и сквозные E2E-сценарии."""

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telebot.asyncio_helper import ApiTelegramException

from fake_telegram import FakeTelegram
from harness import db_path, future_date_text, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.bot.factory import create_bot
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.dialog import DialogState
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.domain.user import User
from postmaster.publishers.base import (
    BotTelegramClient,
    Publisher,
    PublishResult,
    TelegramClient,
)
from postmaster.publishers.reminder import (
    REMINDER_TEXT,
    ReminderErrorCode,
    ReminderPublisher,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.publication_service import PublicationService

SPEC_014_PATH = Path(__file__).resolve().parent.parent / "docs" / "specs" / "SPEC-014.md"
CREATED_AT = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
CLAIM_AT = datetime(2026, 10, 7, 12, 0, 1, tzinfo=UTC)
PUBLISHED_AT = datetime(2026, 10, 7, 12, 0, 2, tzinfo=UTC)


class FakeTelegramClient:
    """Тестовая реализация TelegramClient (§45–46) с записью порядка вызовов."""

    def __init__(
        self,
        *,
        message_error: BaseException | None = None,
        photo_error: BaseException | None = None,
    ) -> None:
        self.messages: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str, str | None]] = []
        self.events: list[str] = []
        self._message_error = message_error
        self._photo_error = photo_error

    async def send_message(self, chat_id: int, text: str) -> None:
        self.events.append("send_message")
        if self._message_error is not None:
            raise self._message_error
        self.messages.append((chat_id, text))

    async def send_photo(
        self,
        chat_id: int,
        file_id: str,
        caption: str | None = None,
    ) -> None:
        self.events.append("send_photo")
        if self._photo_error is not None:
            raise self._photo_error
        self.photos.append((chat_id, file_id, caption))


class StubUserRepository:
    """Подменный репозиторий пользователей для изолированных unit-тестов."""

    def __init__(self, user: Any = None) -> None:
        self.user = user
        self.requested_ids: list[int] = []

    async def get_by_id(self, user_id: int) -> Any:
        self.requested_ids.append(user_id)
        return self.user


def _sample_user(
    *,
    user_id: int = 15,
    telegram_user_id: int = 777001,
) -> User:
    return User(
        id=user_id,
        telegram_user_id=telegram_user_id,
        username="smm_user",
        first_name="Ольга",
        timezone="Europe/Moscow",
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def _sample_post(
    *,
    post_id: int = 42,
    user_id: int = 15,
    file_id: str = "AgACAgIAAxkBAAIB_reminder_photo",
    publication_type: PublicationType = PublicationType.REMINDER,
    target_id: str | None = None,
    caption: str | None = None,
    status: PostStatus = PostStatus.PROCESSING,
) -> Post:
    return Post(
        id=post_id,
        user_id=user_id,
        telegram_file_id=file_id,
        telegram_file_unique_id="AQAD_unique",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        status=status,
        publication_type=publication_type,
        target_id=target_id,
        caption=caption,
        created_at=CREATED_AT,
        updated_at=CLAIM_AT,
    )


@pytest.fixture
async def spec014_db(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], UserRepository, PostRepository, User]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/spec014.db")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user_repo = UserRepository(factory)
        post_repo = PostRepository(factory)
        user = await user_repo.add(
            telegram_user_id=900123456,
            username="reminder_owner",
            first_name="Мария",
            now=CREATED_AT,
        )
        yield factory, user_repo, post_repo, user
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Контракты и точное совпадение текста со спецификацией (§13, §14, §16, §45)
# ---------------------------------------------------------------------------


def test_reminder_text_matches_spec014_verbatim() -> None:
    """§16, US-014-01, AC-014-02: текст напоминания совпадает со спецификацией дословно."""
    spec_text = SPEC_014_PATH.read_text(encoding="utf-8")
    section = spec_text.split("# 16. Reminder text", 1)[1]
    fenced = section.split("```", 2)[1].removeprefix("text").strip()
    assert REMINDER_TEXT == fenced


def test_reminder_publisher_and_telegram_client_contracts(
    fake_telegram: FakeTelegram,
) -> None:
    """§13, §14, §45: ReminderPublisher наследует Publisher, BotTelegramClient — TelegramClient."""
    assert issubclass(ReminderPublisher, Publisher)
    client = FakeTelegramClient()
    assert isinstance(client, TelegramClient)
    bot_client = BotTelegramClient(create_bot(fake_telegram.token))
    assert isinstance(bot_client, TelegramClient)
    result = PublishResult(success=True, error_code=None, error_message=None)
    assert (result.success, result.error_code, result.error_message) == (True, None, None)
    # Поддержка обоих порядков позиционных аргументов: (client, repo) и (repo, client)
    repo = StubUserRepository()
    pub_reversed = ReminderPublisher(repo, client)
    assert pub_reversed._client is client
    assert pub_reversed._users is repo


# ---------------------------------------------------------------------------
# §48–50 Unit Tests (TC-014-01 … TC-014-06, message error, photo error)
# ---------------------------------------------------------------------------


async def test_tc_014_01_successful_reminder_sends_text_then_photo_without_creating_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """TC-014-01, AC-014-01…04, AC-014-07, AC-014-10, §19, §38, §40."""
    monkeypatch.chdir(tmp_path)
    user = _sample_user(user_id=15, telegram_user_id=555777)
    post = _sample_post(post_id=42, user_id=15, file_id="AgAC_photo_42")
    client = FakeTelegramClient()
    user_repo = StubUserRepository(user)
    publisher = ReminderPublisher(client, user_repo)

    with caplog.at_level("INFO", logger="postmaster.publishers.reminder"):
        result = await publisher.publish(post)

    assert result == PublishResult(success=True, error_code=None, error_message=None)
    assert user_repo.requested_ids == [15]
    assert client.events == ["send_message", "send_photo"]
    assert client.messages == [(555777, REMINDER_TEXT)]
    assert client.photos == [(555777, "AgAC_photo_42", None)]
    assert list(tmp_path.iterdir()) == []
    assert "Reminder publication started post_id=42 user_id=15" in caplog.text
    assert "Reminder publication completed post_id=42" in caplog.text
    assert "AgAC_photo_42" not in caplog.text


async def test_tc_014_02_wrong_publication_type_does_not_call_telegram(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TC-014-02, BR-014-01, ERR-014-01, INV-014-01: для TELEGRAM Telegram API не вызывается."""
    post = _sample_post(
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )
    client = FakeTelegramClient()
    user_repo = StubUserRepository(_sample_user())
    publisher = ReminderPublisher(client, user_repo)

    with caplog.at_level("ERROR", logger="postmaster.publishers.reminder"):
        result = await publisher.publish(post)

    assert result.success is False
    assert result.error_code == ReminderErrorCode.INVALID_PUBLICATION_TYPE.value
    assert client.messages == []
    assert client.photos == []
    assert user_repo.requested_ids == []
    assert (
        f"Reminder publication failed post_id={post.id} error_code=INVALID_PUBLICATION_TYPE"
        in caplog.text
    )


async def test_tc_014_03_user_missing_and_target_user_missing_do_not_call_telegram(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TC-014-03, AC-014-08, ERR-014-02, ERR-014-03, §23–24, §28."""
    post = _sample_post(post_id=42, user_id=99)
    client = FakeTelegramClient()
    publisher_missing_user = ReminderPublisher(client, StubUserRepository(None))

    with caplog.at_level("ERROR", logger="postmaster.publishers.reminder"):
        res_missing = await publisher_missing_user.publish(post)

    assert res_missing.success is False
    assert res_missing.error_code == ReminderErrorCode.USER_NOT_FOUND.value
    assert client.messages == [] and client.photos == []
    assert "error_code=USER_NOT_FOUND" in caplog.text

    # §24, ERR-014-03: у найденного объекта пользователя отсутствует telegram_user_id.
    broken_user = SimpleNamespace(id=99, telegram_user_id=None)
    publisher_missing_target = ReminderPublisher(client, StubUserRepository(broken_user))
    res_target = await publisher_missing_target.publish(post)

    assert res_target.success is False
    assert res_target.error_code == ReminderErrorCode.TARGET_USER_MISSING.value
    assert client.messages == [] and client.photos == []


@pytest.mark.parametrize("bad_file_id", [None, "", "   "])
async def test_tc_014_04_file_missing_fails_before_telegram_calls(
    bad_file_id: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    """TC-014-04, AC-014-09, ERR-014-04, §25: при пустом telegram_file_id фото не отправляется."""
    broken_post = SimpleNamespace(
        id=42,
        user_id=15,
        telegram_file_id=bad_file_id,
        publication_type=PublicationType.REMINDER,
        caption=None,
        status=PostStatus.PROCESSING,
    )
    client = FakeTelegramClient()
    publisher = ReminderPublisher(client, StubUserRepository(_sample_user()))

    with caplog.at_level("ERROR", logger="postmaster.publishers.reminder"):
        result = await publisher.publish(broken_post)

    assert result.success is False
    assert result.error_code == ReminderErrorCode.FILE_ID_MISSING.value
    assert client.messages == []
    assert client.photos == []
    assert "error_code=FILE_ID_MISSING" in caplog.text


async def test_tc_014_05_caption_present_is_forwarded_to_send_photo_and_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TC-014-05, AC-014-05, BR-014-08, §18, §39: caption передаётся с фото и не пишется в лог."""
    secret_caption = "Эксклюзивная подпись для поста #42"
    post = _sample_post(caption=secret_caption)
    client = FakeTelegramClient()
    publisher = ReminderPublisher(client, StubUserRepository(_sample_user()))

    with caplog.at_level("INFO", logger="postmaster.publishers.reminder"):
        result = await publisher.publish(post)

    assert result.success is True
    assert client.photos == [(777001, post.telegram_file_id, secret_caption)]
    assert secret_caption not in caplog.text


async def test_tc_014_06_caption_absent_sends_photo_without_caption() -> None:
    """TC-014-06, AC-014-06, §18: при caption=None фотография отправляется без подписи."""
    post = _sample_post(caption=None)
    client = FakeTelegramClient()
    publisher = ReminderPublisher(client, StubUserRepository(_sample_user()))

    result = await publisher.publish(post)

    assert result.success is True
    assert client.photos == [(777001, post.telegram_file_id, None)]


async def test_unit_49_message_error_returns_failure_and_skips_photo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§49, AC-014-11, ERR-014-05, §43: ошибка send_message → success=False, фото не шлётся."""
    post = _sample_post()
    client = FakeTelegramClient(message_error=RuntimeError("Telegram send_message failed"))
    publisher = ReminderPublisher(client, StubUserRepository(_sample_user()))

    with caplog.at_level("ERROR", logger="postmaster.publishers.reminder"):
        result = await publisher.publish(post)

    assert result.success is False
    assert result.error_code == ReminderErrorCode.MESSAGE_SEND_FAILED.value
    assert client.events == ["send_message"]
    assert client.photos == []
    assert (
        f"Reminder publication failed post_id={post.id} error_code=MESSAGE_SEND_FAILED"
        in caplog.text
    )


async def test_unit_50_photo_error_and_invalid_file_id_return_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§50, §32–33, §36, AC-014-11, ERR-014-06: ошибка send_photo — неуспех публикации."""
    post = _sample_post()
    client = FakeTelegramClient(photo_error=RuntimeError("Telegram send_photo failed"))
    publisher = ReminderPublisher(client, StubUserRepository(_sample_user()))

    with caplog.at_level("ERROR", logger="postmaster.publishers.reminder"):
        result = await publisher.publish(post)

    assert result.success is False
    assert result.error_code == ReminderErrorCode.PHOTO_SEND_FAILED.value
    assert client.events == ["send_message", "send_photo"]
    assert "error_code=PHOTO_SEND_FAILED" in caplog.text

    # §36: если Telegram отклоняет сохранённый file_id, возвращается INVALID_FILE_ID.
    invalid_file_exc = ApiTelegramException(
        "sendPhoto",
        None,
        {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: wrong file identifier/HTTP URL specified",
        },
    )
    client_invalid = FakeTelegramClient(photo_error=invalid_file_exc)
    pub_invalid = ReminderPublisher(client_invalid, StubUserRepository(_sample_user()))
    res_invalid = await pub_invalid.publish(post)

    assert res_invalid.success is False
    assert res_invalid.error_code == ReminderErrorCode.INVALID_FILE_ID.value
    assert res_invalid.retryable is False


# ---------------------------------------------------------------------------
# §51–53 Integration Tests (UserRepository, PublicationService, Error/Retry)
# ---------------------------------------------------------------------------


async def test_it_014_01_02_03_user_repository_resolution_caption_and_status_ownership(
    spec014_db,
) -> None:
    """IT-014-01…03, AC-014-12, §30: Post.user_id → UserRepository.get_by_id → telegram_user_id."""
    _, user_repo, post_repo, user = spec014_db
    created = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_it_photo",
        telegram_file_unique_id="AQAD_it_unique",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        caption="Подпись напоминания",
        now=CREATED_AT,
    )
    assert await post_repo.claim_post_for_processing(created.id, now=CLAIM_AT) is True
    processing_post = await post_repo.get_by_id(created.id)
    assert processing_post is not None and processing_post.status == PostStatus.PROCESSING

    client = FakeTelegramClient()
    publisher = ReminderPublisher(client, user_repo)
    result = await publisher.publish(processing_post)

    assert result.success is True
    assert client.messages == [(user.telegram_user_id, REMINDER_TEXT)]
    assert client.photos == [(user.telegram_user_id, "AgAC_it_photo", "Подпись напоминания")]
    # AC-014-12, §30, INV-014-07: сам ReminderPublisher не меняет статус в БД на PUBLISHED.
    still_processing = await post_repo.get_by_id(created.id)
    assert still_processing is not None
    assert still_processing.status == PostStatus.PROCESSING


async def test_it_014_52_publication_service_claims_publishes_reminder_and_marks_published(
    spec014_db,
) -> None:
    """§52 Integration Test: PENDING → PROCESSING → ReminderPublisher → PUBLISHED."""
    _, user_repo, post_repo, user = spec014_db
    post = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_pub_service",
        telegram_file_unique_id="AQAD_pub_service",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        now=CREATED_AT,
    )
    client = FakeTelegramClient()
    reminder_publisher = ReminderPublisher(client, user_repo)
    service = PublicationService(
        post_repo,
        publishers={PublicationType.REMINDER: reminder_publisher},
        now_fn=lambda: PUBLISHED_AT,
    )

    executed = await service.execute(post.id)

    assert executed is True
    assert client.messages == [(user.telegram_user_id, REMINDER_TEXT)]
    assert client.photos == [(user.telegram_user_id, "AgAC_pub_service", None)]
    published = await post_repo.get_by_id(post.id)
    assert published is not None
    assert published.status == PostStatus.PUBLISHED
    assert published.published_at == PUBLISHED_AT
    assert published.error_message is None


async def test_it_014_53_publication_service_handles_permanent_and_temporary_reminder_errors(
    spec014_db,
) -> None:
    """§53, §35, §37: ошибки ReminderPublisher переводят пост в FAILED либо в PENDING для retry."""
    _, user_repo, post_repo, user = spec014_db

    # 1) Окончательная ошибка (например, фото не отправилось из-за невалидного file_id).
    post_perm = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_bad_file",
        telegram_file_unique_id="AQAD_bad_file",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        now=CREATED_AT,
    )
    perm_client = FakeTelegramClient(photo_error=RuntimeError("Bad Request: wrong file identifier"))
    perm_service = PublicationService(
        post_repo,
        publishers={PublicationType.REMINDER: ReminderPublisher(perm_client, user_repo)},
        now_fn=lambda: PUBLISHED_AT,
    )
    assert await perm_service.execute(post_perm.id) is False
    failed = await post_repo.get_by_id(post_perm.id)
    assert failed is not None
    assert failed.status == PostStatus.FAILED
    assert failed.error_message is not None

    # 2) Временная сетевая ошибка (TimeoutError) возвращает пост в PENDING (§37).
    post_temp = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_temp_net",
        telegram_file_unique_id="AQAD_temp_net",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        now=CREATED_AT,
    )
    temp_client = FakeTelegramClient(message_error=TimeoutError("Telegram API request timed out"))
    temp_service = PublicationService(
        post_repo,
        publishers={PublicationType.REMINDER: ReminderPublisher(temp_client, user_repo)},
        now_fn=lambda: PUBLISHED_AT,
    )
    assert await temp_service.execute(post_temp.id) is False
    requeued = await post_repo.get_by_id(post_temp.id)
    assert requeued is not None
    assert requeued.status == PostStatus.PENDING
    assert requeued.error_message == "Telegram API request timed out"


# ---------------------------------------------------------------------------
# §54–56 E2E Scenarios (настоящий Application + FakeTelegram HTTP сервер)
# ---------------------------------------------------------------------------


async def test_e2e_54_normal_reminder_flow_delivers_message_and_photo_and_marks_published(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    """§54 E2E-сценарий — обычное напоминание через диалог, Scheduler и FakeTelegram."""
    fake_telegram.add_text_message("/start", chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)

    fake_telegram.add_photo_message("reminder-e2e-photo", chat_id=42, user_id=42)
    await wait_for_state(running_app, 42, DialogState.WAITING_DATETIME)

    fake_telegram.add_text_message(future_date_text(), chat_id=42, user_id=42)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 3)
    await wait_for_state(running_app, 42, DialogState.IDLE)

    with sqlite3.connect(db_path(tmp_path)) as conn:
        post_id, status = conn.execute("SELECT id, status FROM posts").fetchone()
    assert status == "PENDING"
    events_before = len(fake_telegram.outbound_events)

    # Наступает запланированное время публикации.
    await running_app._scheduler._on_post_due(post_id)

    new_events = fake_telegram.outbound_events[events_before:]
    assert [e["method"] for e in new_events] == ["sendMessage", "sendPhoto"]
    assert new_events[0] == {
        "method": "sendMessage",
        "chat_id": "42",
        "text": REMINDER_TEXT,
    }
    assert new_events[1] == {
        "method": "sendPhoto",
        "chat_id": "42",
        "photo": "reminder-e2e-photo",
        "caption": None,
    }

    with sqlite3.connect(db_path(tmp_path)) as conn:
        final_status, published_at, error_msg = conn.execute(
            "SELECT status, published_at, error_message FROM posts WHERE id=?",
            (post_id,),
        ).fetchone()
    assert final_status == "PUBLISHED"
    assert published_at is not None
    assert error_msg is None


async def test_e2e_55_missed_reminder_executes_after_downtime_and_marks_published(
    spec014_db, fake_telegram: FakeTelegram
) -> None:
    """§55 E2E-сценарий — missed reminder: пропущенный пост доставляется один раз."""
    _, user_repo, post_repo, user = spec014_db
    missed_post = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_missed_reminder",
        telegram_file_unique_id="AQAD_missed_reminder",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        caption="Не забудь опубликовать",
        now=CREATED_AT,
    )
    bot = create_bot(fake_telegram.token)
    reminder_publisher = ReminderPublisher(BotTelegramClient(bot), user_repo)
    pub_service = PublicationService(
        post_repo,
        publishers={PublicationType.REMINDER: reminder_publisher},
        now_fn=lambda: PUBLISHED_AT,
    )

    # Имитируем обнаружение пропущенного поста после перезапуска (SPEC-011/012).
    pending_posts = await post_repo.list_pending()
    assert [p.id for p in pending_posts] == [missed_post.id]
    for post in pending_posts:
        assert await pub_service.execute(post.id) is True

    assert fake_telegram.outbound_events == [
        {
            "method": "sendMessage",
            "chat_id": str(user.telegram_user_id),
            "text": REMINDER_TEXT,
        },
        {
            "method": "sendPhoto",
            "chat_id": str(user.telegram_user_id),
            "photo": "AgAC_missed_reminder",
            "caption": "Не забудь опубликовать",
        },
    ]
    updated = await post_repo.get_by_id(missed_post.id)
    assert updated is not None
    assert updated.status == PostStatus.PUBLISHED
    assert updated.published_at == PUBLISHED_AT


async def test_e2e_56_blocked_bot_returns_failure_and_marks_post_failed(
    spec014_db, fake_telegram: FakeTelegram
) -> None:
    """§56 E2E-сценарий — blocked bot: Telegram возвращает 403, пост переводится в FAILED."""
    _, user_repo, post_repo, user = spec014_db
    post = await post_repo.create_post(
        user_id=user.id,
        telegram_file_id="AgAC_blocked_user",
        telegram_file_unique_id="AQAD_blocked_user",
        scheduled_at_utc=SCHEDULED_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        now=CREATED_AT,
    )
    fake_telegram.fail_send_message = (
        403,
        "Forbidden: bot was blocked by the user",
    )
    bot = create_bot(fake_telegram.token)
    reminder_publisher = ReminderPublisher(bot, user_repo)
    pub_service = PublicationService(
        post_repo,
        publishers={PublicationType.REMINDER: reminder_publisher},
        now_fn=lambda: PUBLISHED_AT,
    )

    assert await pub_service.execute(post.id) is False
    assert fake_telegram.sent_photos == []
    failed = await post_repo.get_by_id(post.id)
    assert failed is not None
    assert failed.status == PostStatus.FAILED
    assert failed.error_message is not None
    assert "blocked by the user" in failed.error_message
    assert replace(failed, status=PostStatus.FAILED) == failed
