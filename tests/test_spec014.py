"""SPEC-014: Reminder Publisher.

Тесты проверяют:
- success: отправляется текст «Время публиковать!» и фото по telegram_file_id.
- no caption: caption отсутствует — отправляется только фото.
- caption present: caption добавляется к фото.
- missing user: пользователь не найден — UserNotFoundError.
- missing file: telegram_file_id пустой — PhotoDataMissingError.
- blocked bot: Telegram вернул 403 — TelegramBlockedError.
- send errors: ошибка Telegram API пробрасывается.
- Return value: возвращается PublishResult с external_id.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.publishers.base import PublishResult
from postmaster.publishers.reminder_publisher import (
    REMINDER_TEXT_TEMPLATE,
    PhotoDataMissingError,
    ReminderPublisher,
    TelegramBlockedError,
    UserNotFoundError,
    _is_bot_blocked,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.recipient_resolver import (
    UserServiceReminderRecipientResolver,
)
from postmaster.services.user_service import UserService

RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


class FakeTelegramException(Exception):
    """Эмуляция ApiTelegramException с error_code."""

    def __init__(self, error_code: int, description: str = "") -> None:
        self.error_code = error_code
        super().__init__(description)


class RecordingReminderSender:
    """Заглушка TelegramReminderSender с записью всех вызовов."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_message_with: Exception | None = None
        self.fail_photo_with: Exception | None = None
        self.next_message_id: int = 1000

    async def send_message(self, *, chat_id: int, text: str) -> dict | None:
        self.calls.append({"method": "send_message", "chat_id": chat_id, "text": text})
        if self.fail_message_with is not None:
            raise self.fail_message_with
        return {"message_id": self.next_message_id}

    async def send_photo(
        self, *, chat_id: int, photo_file_id: str, caption: str | None = None
    ) -> dict | None:
        self.calls.append(
            {
                "method": "send_photo",
                "chat_id": chat_id,
                "photo_file_id": photo_file_id,
                "caption": caption,
            }
        )
        if self.fail_photo_with is not None:
            raise self.fail_photo_with
        return {"message_id": self.next_message_id + 1}


class StaticRecipientResolver:
    """Заглушка ReminderRecipientResolver с фиксированным Telegram ID."""

    def __init__(self, telegram_user_id: int | None) -> None:
        self._telegram_user_id = telegram_user_id

    async def get_telegram_user_id(self, user_id: int) -> int | None:
        return self._telegram_user_id


@pytest.fixture
async def post_repo(tmp_path: Path) -> AsyncIterator[tuple[PostRepository, int]]:
    """Создаёт репозиторий постов и тестового пользователя."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        user = await user_repo.add(
            telegram_user_id=555,
            first_name="Test",
            username="tester",
            now=datetime(2026, 10, 7, tzinfo=UTC),
        )
        yield PostRepository(factory), user.id
    finally:
        await engine.dispose()


def _make_post(
    user_id: int,
    *,
    file_id: str = "AgAC_reminder_file",
    caption: str | None = "Caption text",
) -> Post:
    return Post(
        id=1,
        user_id=user_id,
        telegram_file_id=file_id,
        telegram_file_unique_id="AQAD_reminder_unique",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        created_at=RUN_AT,
        updated_at=RUN_AT,
        status=PostStatus.PROCESSING,
        caption=caption,
    )


async def test_reminder_success_sends_text_and_photo(post_repo) -> None:
    """Успешная отправка: текст «Время публиковать!» + фото по telegram_file_id."""
    repo, user_id = post_repo
    sender = RecordingReminderSender()
    publisher = ReminderPublisher(sender, StaticRecipientResolver(telegram_user_id=555))
    post = _make_post(user_id)

    result = await publisher.publish(post)

    assert isinstance(result, PublishResult)
    assert result.post_id == post.id
    assert result.external_id is not None
    assert len(sender.calls) == 2

    text_call = sender.calls[0]
    assert text_call["method"] == "send_message"
    assert text_call["chat_id"] == 555
    assert "Время публиковать" in text_call["text"]
    assert "20.10.2026 15:00" in text_call["text"]

    photo_call = sender.calls[1]
    assert photo_call["method"] == "send_photo"
    assert photo_call["chat_id"] == 555
    assert photo_call["photo_file_id"] == "AgAC_reminder_file"
    assert photo_call["caption"] == "Caption text"


async def test_reminder_success_without_caption(post_repo) -> None:
    """Без caption: фото отправляется без подписи."""
    repo, user_id = post_repo
    sender = RecordingReminderSender()
    publisher = ReminderPublisher(sender, StaticRecipientResolver(telegram_user_id=555))
    post = _make_post(user_id, caption=None)

    await publisher.publish(post)

    photo_call = sender.calls[1]
    assert photo_call["method"] == "send_photo"
    assert photo_call["caption"] is None


async def test_reminder_uses_correct_text_template() -> None:
    """Текст напоминания соответствует шаблону SPEC-014."""
    local_time = "20.10.2026 15:00"
    expected = REMINDER_TEXT_TEMPLATE.format(local_time=local_time)
    assert "Время публиковать" in expected
    assert local_time in expected


async def test_reminder_missing_user_raises_error(post_repo) -> None:
    """Если владелец поста не найден — UserNotFoundError."""
    repo, user_id = post_repo
    sender = RecordingReminderSender()
    publisher = ReminderPublisher(sender, StaticRecipientResolver(telegram_user_id=None))
    post = _make_post(user_id)

    with pytest.raises(UserNotFoundError):
        await publisher.publish(post)

    assert sender.calls == []


async def test_reminder_missing_file_id_raises_error(post_repo) -> None:
    """Если telegram_file_id пустой — PhotoDataMissingError."""
    repo, user_id = post_repo
    sender = RecordingReminderSender()
    publisher = ReminderPublisher(sender, StaticRecipientResolver(telegram_user_id=555))
    post = _make_post(user_id, file_id="placeholder")
    object.__setattr__(post, "telegram_file_id", "")

    with pytest.raises(PhotoDataMissingError):
        await publisher.publish(post)

    assert sender.calls == []


async def test_reminder_missing_file_id_whitespace_raises_error(post_repo) -> None:
    """Если telegram_file_id — только пробелы — PhotoDataMissingError."""
    repo, user_id = post_repo
    sender = RecordingReminderSender()
    publisher = ReminderPublisher(sender, StaticRecipientResolver(telegram_user_id=555))
    post = _make_post(user_id, file_id="placeholder")
    object.__setattr__(post, "telegram_file_id", "   ")

    with pytest.raises(PhotoDataMissingError):
        await publisher.publish(post)


async def test_reminder_blocked_bot_on_send_message(post_repo) -> None:
    """Если Telegram вернул 403 на send_message — TelegramBlockedError."""
    repo, user_id = post_repo
    sender = RecordingReminderSender()
    sender.fail_message_with = FakeTelegramException(403, "Forbidden: bot was blocked by the user")
    publisher = ReminderPublisher(sender, StaticRecipientResolver(telegram_user_id=555))
    post = _make_post(user_id)

    with pytest.raises(TelegramBlockedError):
        await publisher.publish(post)


async def test_reminder_blocked_bot_on_send_photo(post_repo) -> None:
    """Если Telegram вернул 403 на send_photo — TelegramBlockedError."""
    repo, user_id = post_repo
    sender = RecordingReminderSender()
    sender.fail_photo_with = FakeTelegramException(403, "Forbidden: bot was blocked by the user")
    publisher = ReminderPublisher(sender, StaticRecipientResolver(telegram_user_id=555))
    post = _make_post(user_id)

    with pytest.raises(TelegramBlockedError):
        await publisher.publish(post)


async def test_reminder_non_blocked_error_propagates(post_repo) -> None:
    """Если Telegram вернул не-403 ошибку — исключение пробрасывается."""
    repo, user_id = post_repo
    sender = RecordingReminderSender()
    sender.fail_message_with = RuntimeError("network timeout")
    publisher = ReminderPublisher(sender, StaticRecipientResolver(telegram_user_id=555))
    post = _make_post(user_id)

    with pytest.raises(RuntimeError, match="network timeout"):
        await publisher.publish(post)


async def test_reminder_photo_error_after_text_succeeds(post_repo) -> None:
    """Если текст отправлен, но фото не отправлено — ошибка пробрасывается."""
    repo, user_id = post_repo
    sender = RecordingReminderSender()
    sender.fail_photo_with = RuntimeError("photo upload failed")
    publisher = ReminderPublisher(sender, StaticRecipientResolver(telegram_user_id=555))
    post = _make_post(user_id)

    with pytest.raises(RuntimeError, match="photo upload failed"):
        await publisher.publish(post)

    # Текст был отправлен, плюс попытка отправки фото (которая упала)
    assert len(sender.calls) == 2
    assert sender.calls[0]["method"] == "send_message"
    assert sender.calls[1]["method"] == "send_photo"


async def test_reminder_returns_publish_result(post_repo) -> None:
    """Publisher возвращает PublishResult с external_id из Telegram."""
    repo, user_id = post_repo
    sender = RecordingReminderSender()
    publisher = ReminderPublisher(sender, StaticRecipientResolver(telegram_user_id=555))
    post = _make_post(user_id)

    result = await publisher.publish(post)

    assert result.post_id == post.id
    assert result.external_id is not None


async def test_reminder_with_user_service_resolver(tmp_path: Path) -> None:
    """ReminderPublisher работает с UserServiceReminderRecipientResolver."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        user = await user_repo.add(
            telegram_user_id=777,
            first_name="Reminder",
            username=None,
            now=datetime(2026, 10, 7, tzinfo=UTC),
        )
        user_service = UserService(user_repo, default_timezone="Europe/Moscow")
        recipient_resolver = UserServiceReminderRecipientResolver(user_service)

        sender = RecordingReminderSender()
        publisher = ReminderPublisher(sender, recipient_resolver)
        post = _make_post(user.id)

        result = await publisher.publish(post)

        assert result.post_id == post.id
        assert sender.calls[0]["chat_id"] == 777
    finally:
        await engine.dispose()


def test_is_bot_blocked_detects_403_error_code() -> None:
    """_is_bot_blocked детектирует исключения с error_code=403."""
    exc = FakeTelegramException(403, "Forbidden")
    assert _is_bot_blocked(exc) is True


def test_is_bot_blocked_detects_blocked_text() -> None:
    """_is_bot_blocked детектирует по тексту ошибки."""
    exc = RuntimeError("Forbidden: bot was blocked by the user")
    assert _is_bot_blocked(exc) is True


def test_is_bot_blocked_detects_deactivated_text() -> None:
    """_is_bot_blocked детектирует деактивированного пользователя."""
    exc = RuntimeError("User is deactivated")
    assert _is_bot_blocked(exc) is True


def test_is_bot_blocked_returns_false_for_other_errors() -> None:
    """_is_bot_blocked возвращает False для других ошибок."""
    assert _is_bot_blocked(RuntimeError("network timeout")) is False
    assert _is_bot_blocked(FakeTelegramException(400, "Bad Request")) is False
