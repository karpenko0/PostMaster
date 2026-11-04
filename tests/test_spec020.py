"""SPEC-020: Поддержка caption.

Тесты проверяют:
- save/read: caption сохраняется в БД и читается обратно через PostRepository.
- null: пост без caption сохраняется и читается как caption=None.
- Reminder: caption передаётся в ReminderPublisher (send_photo).
- Telegram: caption передаётся в TelegramChannelPublisher (send_photo).
- Publisher не меняет текст caption.
- E2E через Long Polling: caption из Telegram доходит до БД.
- Пустая/пробельная caption нормализуется до None.
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
from postmaster.publishers import DefaultPublisherResolver
from postmaster.publishers.reminder_publisher import (
    ReminderPublisher,
)
from postmaster.publishers.telegram_publisher import TelegramChannelPublisher
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.photo_service import PhotoService
from postmaster.services.publication_service import PublicationService
from postmaster.services.user_service import UserService

RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


class _StubPhotoSize:
    """Заглушка PhotoSizeLike для тестов PhotoService."""

    def __init__(
        self,
        file_id: str,
        width: int = 1280,
        height: int = 960,
        file_size: int | None = 150000,
    ) -> None:
        self.file_id = file_id
        self.file_unique_id = f"{file_id}-uniq"
        self.width = width
        self.height = height
        self.file_size = file_size


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
    caption: str | None = "Initial caption",
    publication_type: PublicationType = PublicationType.TELEGRAM,
    target_id: str | None = "-1001234567890",
) -> Post:
    return await repo.create_post(
        user_id=user_id,
        telegram_file_id=f"file_{user_id}",
        telegram_file_unique_id=f"unique_{user_id}",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=publication_type,
        target_id=target_id,
        caption=caption,
    )


# === PhotoService caption tests (SPEC-020 §3) ===


def test_photo_service_extracts_caption() -> None:
    """PhotoService сохраняет caption из message.caption в PhotoData."""
    service = PhotoService()
    photo = service.handle_photo(
        telegram_user_id=1,
        photo_sizes=[_StubPhotoSize("file-1")],
        caption="Hello, world!",
    )
    assert photo.caption == "Hello, world!"


def test_photo_service_no_caption() -> None:
    """Без caption → PhotoData.caption == None."""
    service = PhotoService()
    photo = service.handle_photo(telegram_user_id=1, photo_sizes=[_StubPhotoSize("file-1")])
    assert photo.caption is None


def test_photo_service_empty_caption_normalized_to_none() -> None:
    """Пустая caption → None (не пустая строка)."""
    service = PhotoService()
    photo = service.handle_photo(
        telegram_user_id=1, photo_sizes=[_StubPhotoSize("file-1")], caption=""
    )
    assert photo.caption is None


def test_photo_service_whitespace_caption_normalized_to_none() -> None:
    """Caption из одних пробелов → None."""
    service = PhotoService()
    photo = service.handle_photo(
        telegram_user_id=1, photo_sizes=[_StubPhotoSize("file-1")], caption="   \t\n"
    )
    assert photo.caption is None


def test_photo_service_strips_caption_whitespace() -> None:
    """Caption с пробелами по краям подрезается."""
    service = PhotoService()
    photo = service.handle_photo(
        telegram_user_id=1,
        photo_sizes=[_StubPhotoSize("file-1")],
        caption="  Hello!  ",
    )
    assert photo.caption == "Hello!"


def test_photo_service_preserves_internal_whitespace() -> None:
    """Внутренние пробелы caption сохраняются."""
    service = PhotoService()
    photo = service.handle_photo(
        telegram_user_id=1,
        photo_sizes=[_StubPhotoSize("file-1")],
        caption="Hello   world",
    )
    assert photo.caption == "Hello   world"


def test_photo_service_preserves_unicode() -> None:
    """Unicode caption сохраняется без изменений."""
    service = PhotoService()
    text = "Привет, мир! 🎉"
    photo = service.handle_photo(
        telegram_user_id=1, photo_sizes=[_StubPhotoSize("file-1")], caption=text
    )
    assert photo.caption == text


# === Repository caption tests (SPEC-020 §8 save/read, null) ===


async def test_caption_save_and_read(post_repo) -> None:
    """Caption сохраняется и читается из БД."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=100)
    post = await _create_post(repo, user_id, caption="My caption")

    fetched = await repo.get_by_id(post.id)

    assert fetched is not None
    assert fetched.caption == "My caption"


async def test_caption_null_save_and_read(post_repo) -> None:
    """Пост без caption читается с caption=None."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=101)
    post = await _create_post(repo, user_id, caption=None)

    fetched = await repo.get_by_id(post.id)

    assert fetched is not None
    assert fetched.caption is None


async def test_caption_unicode_in_db(post_repo) -> None:
    """Unicode caption сохраняется в БД без потерь."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=102)
    text = "Тестовая подпись 🎉 с эмодзи"
    post = await _create_post(repo, user_id, caption=text)

    fetched = await repo.get_by_id(post.id)

    assert fetched is not None
    assert fetched.caption == text


async def test_caption_long_text(post_repo) -> None:
    """Длинный caption (1024 символа, лимит Telegram) сохраняется."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=103)
    text = "A" * 1024
    post = await _create_post(repo, user_id, caption=text)

    fetched = await repo.get_by_id(post.id)

    assert fetched is not None
    assert fetched.caption == text


# === Telegram publisher caption (SPEC-020 §8 Telegram) ===


class _RecordingTelegramSender:
    """Заглушка TelegramSender для проверки caption."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> dict | None:
        self.calls.append({"chat_id": chat_id, "photo_file_id": photo_file_id, "caption": caption})
        return {"message_id": 1}


async def test_telegram_publisher_passes_caption(post_repo) -> None:
    """TelegramChannelPublisher передаёт caption в send_photo."""
    repo, _ = post_repo
    sender = _RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    post = _make_full_post(caption="Caption for Telegram")

    await publisher.publish(post)

    assert len(sender.calls) == 1
    assert sender.calls[0]["caption"] == "Caption for Telegram"


async def test_telegram_publisher_caption_none(post_repo) -> None:
    """TelegramChannelPublisher передаёт caption=None без подписи."""
    repo, _ = post_repo
    sender = _RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    post = _make_full_post(caption=None)

    await publisher.publish(post)

    assert sender.calls[0]["caption"] is None


async def test_telegram_publisher_does_not_modify_caption(post_repo) -> None:
    """TelegramChannelPublisher НЕ меняет текст caption (SPEC-020 §7)."""
    repo, _ = post_repo
    sender = _RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    original = "  Caption with spaces  "
    post = _make_full_post(caption=original)

    await publisher.publish(post)

    # Publisher не подрезает пробелы — это сделал PhotoService на входе
    assert sender.calls[0]["caption"] == original


# === Reminder publisher caption (SPEC-020 §8 Reminder) ===


class _RecordingReminderSender:
    """Заглушка TelegramReminderSender."""

    def __init__(self) -> None:
        self.photo_calls: list[dict] = []
        self.text_calls: list[dict] = []

    async def send_message(self, *, chat_id: int, text: str) -> dict | None:
        self.text_calls.append({"chat_id": chat_id, "text": text})
        return {"message_id": 1}

    async def send_photo(
        self, *, chat_id: int, photo_file_id: str, caption: str | None = None
    ) -> dict | None:
        self.photo_calls.append(
            {"chat_id": chat_id, "photo_file_id": photo_file_id, "caption": caption}
        )
        return {"message_id": 2}


class _StaticRecipientResolver:
    def __init__(self, telegram_user_id: int | None) -> None:
        self._telegram_user_id = telegram_user_id

    async def get_telegram_user_id(self, user_id: int) -> int | None:
        return self._telegram_user_id


async def test_reminder_publisher_passes_caption_to_photo(post_repo) -> None:
    """ReminderPublisher передаёт caption в send_photo."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=200)
    sender = _RecordingReminderSender()
    publisher = ReminderPublisher(sender, _StaticRecipientResolver(777))
    post = _make_full_post(user_id=user_id, caption="Reminder caption")

    await publisher.publish(post)

    assert len(sender.photo_calls) == 1
    assert sender.photo_calls[0]["caption"] == "Reminder caption"
    # Caption идёт в send_photo, не в send_message
    assert not any("Reminder caption" in c.get("text", "") for c in sender.text_calls)


async def test_reminder_publisher_caption_none(post_repo) -> None:
    """ReminderPublisher с caption=None — фото без подписи."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=201)
    sender = _RecordingReminderSender()
    publisher = ReminderPublisher(sender, _StaticRecipientResolver(778))
    post = _make_full_post(user_id=user_id, caption=None)

    await publisher.publish(post)

    assert sender.photo_calls[0]["caption"] is None


async def test_reminder_publisher_does_not_modify_caption(post_repo) -> None:
    """ReminderPublisher НЕ меняет текст caption."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=202)
    sender = _RecordingReminderSender()
    publisher = ReminderPublisher(sender, _StaticRecipientResolver(779))
    original = "  Caption with spaces  "
    post = _make_full_post(user_id=user_id, caption=original)

    await publisher.publish(post)

    assert sender.photo_calls[0]["caption"] == original


# === PublicationService end-to-end (SPEC-020 §3 caption → Post → Publisher) ===


async def test_publication_service_passes_caption_to_telegram(post_repo) -> None:
    """PublicationService передаёт caption в Telegram Publisher при публикации."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=300)
    sender = _RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)

    post = await _create_post(
        repo, user_id, publication_type=PublicationType.TELEGRAM, caption="Pub caption"
    )
    result = await service.publish(post.id)

    assert result.status == PostStatus.PUBLISHED
    assert sender.calls[0]["caption"] == "Pub caption"


async def test_publication_service_passes_caption_to_reminder(post_repo) -> None:
    """PublicationService передаёт caption в Reminder Publisher при публикации."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=301)
    sender = _RecordingReminderSender()
    publisher = ReminderPublisher(sender, _StaticRecipientResolver(880))
    resolver = DefaultPublisherResolver(telegram_publisher=None, reminder_publisher=publisher)
    service = PublicationService(repo, resolver)

    post = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.REMINDER,
        target_id=None,
        caption="Reminder caption",
    )
    result = await service.publish(post.id)

    assert result.status == PostStatus.PUBLISHED
    assert sender.photo_calls[0]["caption"] == "Reminder caption"


async def test_publication_no_caption_does_not_block(post_repo) -> None:
    """Отсутствие caption не блокирует публикацию (SPEC-020 §7)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=302)
    sender = _RecordingTelegramSender()
    publisher = TelegramChannelPublisher(sender)
    resolver = DefaultPublisherResolver(telegram_publisher=publisher, reminder_publisher=None)
    service = PublicationService(repo, resolver)

    post = await _create_post(
        repo, user_id, publication_type=PublicationType.TELEGRAM, caption=None
    )
    result = await service.publish(post.id)

    assert result.status == PostStatus.PUBLISHED
    assert sender.calls[0]["caption"] is None


# === E2E через Long Polling (SPEC-020 §3) ===


async def test_caption_e2e_via_fake_telegram(running_app, fake_telegram, tmp_path: Path) -> None:
    """Caption из Telegram доходит до БД и обратно через /my_posts."""
    from harness import future_date_text, wait_until

    # Регистрируем пользователя
    fake_telegram.add_text_message(
        "/start", chat_id=500, user_id=500, username=None, first_name="Тестер"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)

    # Отправляем фото с caption
    fake_telegram.add_photo_message(
        file_id="caption-photo", chat_id=500, user_id=500, caption="E2E caption text"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    # Отправляем дату
    fake_telegram.add_text_message(
        f"/skip_date\n{future_date_text(days=2)}", chat_id=500, user_id=500
    )

    # Ждём завершения публикации
    async def post_with_caption_exists() -> bool:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from postmaster.database.models import PostModel

        engine = running_app._engine
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            from sqlalchemy import select

            result = await session.execute(select(PostModel).where(PostModel.user_id == 1))
            posts = list(result.scalars())
            return bool(posts) and posts[0].caption == "E2E caption text"

    await wait_until(post_with_caption_exists)


# === Helpers ===


def _make_full_post(
    *,
    user_id: int = 1,
    caption: str | None = "Test caption",
    publication_type: PublicationType = PublicationType.TELEGRAM,
    target_id: str | None = "-1001234567890",
) -> Post:
    """Создаёт Post со всеми обязательными полями."""
    return Post(
        id=1,
        user_id=user_id,
        telegram_file_id="file_caption_test",
        telegram_file_unique_id="unique_caption_test",
        scheduled_at_utc=RUN_AT,
        timezone="Europe/Moscow",
        publication_type=publication_type,
        created_at=RUN_AT,
        updated_at=RUN_AT,
        status=PostStatus.PROCESSING,
        target_id=target_id,
        caption=caption,
    )
