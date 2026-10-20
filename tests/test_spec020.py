"""SPEC-020: подпись фото сохраняется, читается и передаётся Publisher без изменений."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fake_telegram import FakeTelegram
from harness import db_path, future_date_text, wait_for_state, wait_until
from postmaster.app import Application
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.models import PostModel
from postmaster.database.schema import create_schema
from postmaster.domain.dialog import DialogState
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.domain.user import User
from postmaster.publishers.reminder import ReminderPublisher
from postmaster.publishers.telegram import TelegramPublisher
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
CAPTION = "Пользовательский текст\n  без изменений — #PostMaster"


@pytest.fixture
async def spec020_db(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], User, PostRepository]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/spec020.db")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user = await UserRepository(factory).add(
            telegram_user_id=20020,
            username="caption_user",
            first_name="Caption",
            now=NOW,
        )
        yield factory, user, PostRepository(factory)
    finally:
        await engine.dispose()


async def _create(
    repository: PostRepository,
    user_id: int,
    *,
    caption: str | None,
    post_id_offset: int = 0,
) -> Post:
    return await repository.create_post(
        user_id=user_id,
        telegram_file_id=f"caption-photo-{post_id_offset}",
        telegram_file_unique_id=f"caption-unique-{post_id_offset}",
        scheduled_at_utc=NOW + timedelta(days=post_id_offset + 1),
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        caption=caption,
        now=NOW,
    )


async def test_tc_020_01_02_rt_020_01_02_caption_round_trips_and_empty_is_null(spec020_db) -> None:
    factory, user, repository = spec020_db
    saved = await _create(repository, user.id, caption=CAPTION)
    no_caption = await _create(repository, user.id, caption=None, post_id_offset=1)
    blank_caption = await _create(repository, user.id, caption="   ", post_id_offset=2)

    loaded = await repository.get_by_id(saved.id)
    assert loaded is not None and loaded.caption == CAPTION
    assert (await repository.get_by_id(no_caption.id)).caption is None
    assert (await repository.get_by_id(blank_caption.id)).caption is None

    async with factory() as session:
        persisted = dict(
            (row.id, row.caption)
            for row in (await session.scalars(select(PostModel).order_by(PostModel.id))).all()
        )
    assert persisted == {saved.id: CAPTION, no_caption.id: None, blank_caption.id: None}


class RecordingTelegramClient:
    def __init__(self, user: User | None = None) -> None:
        self.user = user
        self.photo_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def get_by_id(self, _user_id: int) -> User | None:
        return self.user

    async def send_message(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def send_photo(self, *args: Any, **kwargs: Any) -> None:
        self.photo_calls.append((args, kwargs))


def _publisher_post(kind: PublicationType, caption: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=20,
        user_id=1,
        telegram_file_id="telegram-photo-id",
        publication_type=kind,
        target_id="-1001234567890" if kind == PublicationType.TELEGRAM else None,
        status=PostStatus.PROCESSING,
        caption=caption,
    )


@pytest.mark.parametrize("caption", [CAPTION, "  spaced text  "])
async def test_tc_020_03_04_publishers_forward_caption_exactly(caption: str) -> None:
    user = User(
        id=1,
        telegram_user_id=70001,
        username=None,
        first_name="Test",
        timezone=None,
        created_at=NOW,
        updated_at=NOW,
    )
    reminder_client = RecordingTelegramClient(user)
    reminder = ReminderPublisher(reminder_client, reminder_client)
    result = await reminder.publish(_publisher_post(PublicationType.REMINDER, caption))
    assert result.success
    assert reminder_client.photo_calls == [
        ((user.telegram_user_id, "telegram-photo-id"), {"caption": caption})
    ]

    telegram_client = RecordingTelegramClient()
    telegram = TelegramPublisher(telegram_client)
    result = await telegram.publish(_publisher_post(PublicationType.TELEGRAM, caption))
    assert result.success
    assert telegram_client.photo_calls == [
        (("-1001234567890", "telegram-photo-id"), {"caption": caption})
    ]


@pytest.mark.parametrize("caption", [None, "", "   "])
async def test_tc_020_05_both_publishers_send_photo_without_empty_caption(
    caption: str | None,
) -> None:
    user = User(
        id=1,
        telegram_user_id=70002,
        username=None,
        first_name="Test",
        timezone=None,
        created_at=NOW,
        updated_at=NOW,
    )
    reminder_client = RecordingTelegramClient(user)
    reminder_post = Post(
        id=21,
        user_id=1,
        telegram_file_id="photo-without-caption",
        telegram_file_unique_id="unique-21",
        scheduled_at_utc=NOW + timedelta(days=1),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        status=PostStatus.PROCESSING,
        created_at=NOW,
        updated_at=NOW,
        caption=caption,
    )
    assert reminder_post.caption is None
    assert (
        await ReminderPublisher(reminder_client, reminder_client).publish(reminder_post)
    ).success
    assert reminder_client.photo_calls == [((user.telegram_user_id, "photo-without-caption"), {})]

    telegram_client = RecordingTelegramClient()
    telegram_post = Post(
        id=22,
        user_id=1,
        telegram_file_id="photo-without-caption-tg",
        telegram_file_unique_id="unique-22",
        scheduled_at_utc=NOW + timedelta(days=1),
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        status=PostStatus.PROCESSING,
        target_id="-1001234567890",
        created_at=NOW,
        updated_at=NOW,
        caption=caption,
    )
    assert telegram_post.caption is None
    assert (await TelegramPublisher(telegram_client).publish(telegram_post)).success
    assert telegram_client.photo_calls == [(("-1001234567890", "photo-without-caption-tg"), {})]


async def test_photo_caption_is_saved_from_telegram_update_through_publication(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    user_id = 20021
    fake_telegram.add_text_message("/start", chat_id=user_id, user_id=user_id)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    await wait_for_state(running_app, user_id, DialogState.WAITING_PHOTO)

    fake_telegram.add_photo_message(
        "caption-e2e-photo", chat_id=user_id, user_id=user_id, caption=CAPTION
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
    await wait_for_state(running_app, user_id, DialogState.WAITING_DATETIME)
    draft = await running_app.bot.current_states.get_data(
        user_id, user_id, bot_id=running_app.bot.bot_id
    )
    assert draft["caption"] == CAPTION

    fake_telegram.add_text_message(future_date_text(), chat_id=user_id, user_id=user_id)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 3)

    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        factory = create_session_factory(engine)
        owner = await UserRepository(factory).get_by_telegram_id(user_id)
        posts = PostRepository(factory)
        assert owner is not None
        saved = await posts.list_pending_by_user(owner.id)
        assert len(saved) == 1 and saved[0].caption == CAPTION
    finally:
        await engine.dispose()


async def test_blank_photo_caption_is_stored_as_null_and_post_still_created(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    user_id = 20022
    fake_telegram.add_text_message("/start", chat_id=user_id, user_id=user_id)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    await wait_for_state(running_app, user_id, DialogState.WAITING_PHOTO)

    fake_telegram.add_photo_message(
        "blank-caption-photo", chat_id=user_id, user_id=user_id, caption="   "
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
    await wait_for_state(running_app, user_id, DialogState.WAITING_DATETIME)
    draft = await running_app.bot.current_states.get_data(
        user_id, user_id, bot_id=running_app.bot.bot_id
    )
    assert "caption" not in draft

    fake_telegram.add_text_message(future_date_text(), chat_id=user_id, user_id=user_id)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 3)

    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        factory = create_session_factory(engine)
        owner = await UserRepository(factory).get_by_telegram_id(user_id)
        saved = await PostRepository(factory).list_pending_by_user(owner.id)
        assert len(saved) == 1 and saved[0].caption is None
    finally:
        await engine.dispose()
