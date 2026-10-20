"""SPEC-017: пользовательский список ожидающих публикаций."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fake_telegram import FakeTelegram
from harness import db_path, wait_until
from postmaster.app import Application
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.domain.user import User
from postmaster.handlers.my_posts import (
    NO_PENDING_POSTS_TEXT,
    POSTS_LOAD_FAILED_TEXT,
    USER_NOT_FOUND_TEXT,
    format_post,
    format_post_messages,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.post_query_service import PostQueryService

NOW = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def _post(
    post_id: int,
    scheduled_at: datetime,
    *,
    timezone: str = "Europe/Moscow",
    publication_type: PublicationType = PublicationType.REMINDER,
) -> Post:
    return Post(
        id=post_id,
        user_id=1,
        telegram_file_id=f"file-{post_id}",
        telegram_file_unique_id=f"unique-{post_id}",
        scheduled_at_utc=scheduled_at,
        timezone=timezone,
        publication_type=publication_type,
        created_at=NOW,
        updated_at=NOW,
        status=PostStatus.PENDING,
        target_id="-100123" if publication_type == PublicationType.TELEGRAM else None,
    )


def test_tc_017_04_05_format_converts_timezone_and_maps_publication_types() -> None:
    reminder = _post(42, datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    telegram = _post(
        43,
        datetime(2026, 10, 21, 7, 30, tzinfo=UTC),
        publication_type=PublicationType.TELEGRAM,
    )

    assert format_post(reminder) == "#42\n📅 20.10.2026 15:00\n📌 Тип: Напоминание"
    assert format_post(telegram) == "#43\n📅 21.10.2026 10:30\n📌 Тип: Telegram"


def test_empty_list_and_large_list_formatting() -> None:
    assert format_post_messages([]) == [NO_PENDING_POSTS_TEXT]
    posts = [_post(i, NOW) for i in range(1, 100)]
    messages = format_post_messages(posts)
    assert len(messages) > 1
    assert all(len(message) <= 3500 for message in messages)
    ids = [
        int(line[1:])
        for message in messages
        for line in message.splitlines()
        if line.startswith("#")
    ]
    assert ids == list(range(1, 100))


async def test_post_query_service_returns_none_for_unknown_user() -> None:
    class MissingUserService:
        async def get_by_telegram_id(self, telegram_user_id: int) -> None:
            return None

    class UnusedPostRepository:
        async def list_pending_by_user(self, user_id: int) -> list[Post]:
            raise AssertionError("repository must not be called without a user")

    service = PostQueryService(MissingUserService(), UnusedPostRepository())  # type: ignore[arg-type]
    assert await service.get_pending_posts_for_user(999) is None


async def test_repository_filters_by_owner_and_pending_and_orders_by_time(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/spec017.db")
    try:
        from postmaster.database.schema import create_schema

        await create_schema(engine)
        factory = create_session_factory(engine)
        users = UserRepository(factory)
        posts = PostRepository(factory)
        owner = await users.add(telegram_user_id=1701, username=None, first_name="Owner", now=NOW)
        other = await users.add(telegram_user_id=1702, username=None, first_name="Other", now=NOW)
        first = await posts.create_post(
            user_id=owner.id,
            telegram_file_id="a",
            telegram_file_unique_id="a-unique",
            scheduled_at_utc=datetime(2026, 10, 22, 18, 0, tzinfo=UTC),
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
            now=NOW,
        )
        second = await posts.create_post(
            user_id=owner.id,
            telegram_file_id="b",
            telegram_file_unique_id="b-unique",
            scheduled_at_utc=datetime(2026, 10, 22, 15, 0, tzinfo=UTC),
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
            now=NOW,
        )
        published = await posts.create_post(
            user_id=owner.id,
            telegram_file_id="c",
            telegram_file_unique_id="c-unique",
            scheduled_at_utc=datetime(2026, 10, 22, 14, 0, tzinfo=UTC),
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
            now=NOW,
        )
        other_pending = await posts.create_post(
            user_id=other.id,
            telegram_file_id="d",
            telegram_file_unique_id="d-unique",
            scheduled_at_utc=datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
            now=NOW,
        )
        processing = await posts.create_post(
            user_id=owner.id,
            telegram_file_id="processing",
            telegram_file_unique_id="processing-unique",
            scheduled_at_utc=datetime(2026, 10, 22, 12, 0, tzinfo=UTC),
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
            now=NOW,
        )
        failed = await posts.create_post(
            user_id=owner.id,
            telegram_file_id="failed",
            telegram_file_unique_id="failed-unique",
            scheduled_at_utc=datetime(2026, 10, 22, 11, 0, tzinfo=UTC),
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
            now=NOW,
        )
        cancelled = await posts.create_post(
            user_id=owner.id,
            telegram_file_id="cancelled",
            telegram_file_unique_id="cancelled-unique",
            scheduled_at_utc=datetime(2026, 10, 22, 10, 0, tzinfo=UTC),
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
            now=NOW,
        )
        assert await posts.claim_post_for_processing(published.id, now=NOW)
        await posts.mark_published(published.id, published_at=NOW, now=NOW)
        assert await posts.claim_post_for_processing(processing.id, now=NOW)
        assert await posts.claim_post_for_processing(failed.id, now=NOW)
        await posts.mark_failed(failed.id, error_message="test failure", now=NOW)
        assert await posts.cancel_post(cancelled.id, owner.id, now=NOW)

        result = await posts.list_pending_by_user(owner.id)
        assert [post.id for post in result] == [second.id, first.id]
        assert all(
            post.user_id == owner.id and post.status == PostStatus.PENDING for post in result
        )
        returned_ids = [post.id for post in result]
        assert other_pending.id not in returned_ids
        assert {published.id, processing.id, failed.id, cancelled.id}.isdisjoint(returned_ids)
    finally:
        await engine.dispose()


async def test_my_posts_end_to_end_shows_only_current_users_pending_posts(
    running_app: Application, fake_telegram: FakeTelegram, tmp_path: Path
) -> None:
    """IT-017-01…03 / AC-017-01…12: реальный handler, сервисы и SQLite."""
    path = db_path(tmp_path)
    engine = create_database_engine(f"sqlite+aiosqlite:///{path}")
    try:
        factory = create_session_factory(engine)
        users = UserRepository(factory)
        posts = PostRepository(factory)
        owner = await users.add(
            telegram_user_id=17001, username="owner", first_name="Owner", now=NOW
        )
        other = await users.add(
            telegram_user_id=17002, username="other", first_name="Other", now=NOW
        )
        empty_user = await users.add(
            telegram_user_id=17003, username="empty", first_name="Empty", now=NOW
        )
        near = await posts.create_post(
            user_id=owner.id,
            telegram_file_id="near-photo",
            telegram_file_unique_id="near-photo-uid",
            scheduled_at_utc=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
            now=NOW,
        )
        far = await posts.create_post(
            user_id=owner.id,
            telegram_file_id="far-photo",
            telegram_file_unique_id="far-photo-uid",
            scheduled_at_utc=datetime(2026, 10, 21, 7, 30, tzinfo=UTC),
            timezone="Europe/Moscow",
            publication_type=PublicationType.TELEGRAM,
            target_id="-1001234567890",
            now=NOW,
        )
        foreign = await posts.create_post(
            user_id=other.id,
            telegram_file_id="foreign-photo",
            telegram_file_unique_id="foreign-photo-uid",
            scheduled_at_utc=datetime(2026, 10, 20, 11, 0, tzinfo=UTC),
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
            now=NOW,
        )
        processing = await posts.create_post(
            user_id=owner.id,
            telegram_file_id="processing-photo",
            telegram_file_unique_id="processing-photo-uid",
            scheduled_at_utc=datetime(2026, 10, 20, 10, 0, tzinfo=UTC),
            timezone="Europe/Moscow",
            publication_type=PublicationType.REMINDER,
            now=NOW,
        )
        await posts.claim_post_for_processing(processing.id, now=NOW)

        fake_telegram.add_text_message("/my_posts", chat_id=17001, user_id=17001)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
        response = fake_telegram.sent_messages[0]["text"]

        assert response.startswith("Ваши запланированные публикации:")
        assert response.index(f"#{near.id}") < response.index(f"#{far.id}")
        assert "20.10.2026 15:00" in response
        assert "21.10.2026 10:30" in response
        assert "Тип: Напоминание" in response and "Тип: Telegram" in response
        assert f"#{foreign.id}" not in response
        assert f"#{processing.id}" not in response

        fake_telegram.add_text_message("/my_posts", chat_id=empty_user.telegram_user_id)
        await wait_until(lambda: len(fake_telegram.sent_messages) == 2)
        assert fake_telegram.sent_messages[1]["text"] == NO_PENDING_POSTS_TEXT
    finally:
        await engine.dispose()


async def test_my_posts_handles_missing_user_and_repository_failure(
    running_app: Application, fake_telegram: FakeTelegram
) -> None:
    fake_telegram.add_text_message("/my_posts", chat_id=17999, user_id=17999)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    assert fake_telegram.sent_messages[0]["text"] == USER_NOT_FOUND_TEXT

    class BrokenPostRepository:
        async def list_pending_by_user(self, user_id: int) -> list[Post]:
            raise RuntimeError("database detail must not reach user")

    class ExistingUserService:
        async def get_by_telegram_id(self, telegram_user_id: int) -> User:
            return User(
                id=1,
                telegram_user_id=telegram_user_id,
                username=None,
                first_name="Tester",
                timezone=None,
                created_at=NOW,
                updated_at=NOW,
            )

    service = PostQueryService(
        ExistingUserService(),
        BrokenPostRepository(),  # type: ignore[arg-type]
    )
    # The production handler's error branch is exercised through a minimal fake bot adapter.
    captured: list[str] = []

    class FakeBot:
        def message_handler(self, **_kwargs: Any):
            def decorate(handler):
                self.handler = handler
                return handler

            return decorate

        async def send_message(self, chat_id: int, text: str) -> None:
            captured.append(text)

    from postmaster.handlers.my_posts import register_my_posts_handler

    bot = FakeBot()
    register_my_posts_handler(bot, service)  # type: ignore[arg-type]
    await bot.handler(
        SimpleNamespace(
            from_user=SimpleNamespace(id=17001),
            chat=SimpleNamespace(id=17001),
        )
    )
    assert captured == [POSTS_LOAD_FAILED_TEXT]
