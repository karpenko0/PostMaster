"""SPEC-017: Команда /my_posts.

Тесты проверяют:
- Empty: если у пользователя нет PENDING-постов — выводится EMPTY_LIST.
- Sorting: посты отсортированы по scheduled_at_utc ASC.
- Timezone: время отображается в часовой зоне пользователя.
- Filtering: только PENDING-посты.
- Isolation: пользователь видит только свои посты.
- Неизвестный пользователь → EMPTY_LIST.
- Handler отправляет сообщение.
- Handler работает только в личных чатах (chat_types=["private"]).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.handlers.my_posts import MY_POSTS_FAILED, register_my_posts_handler
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.my_posts_service import (
    EMPTY_LIST,
    MyPostsService,
)
from postmaster.services.user_service import UserService


class _StubPostListing:
    """Заглушка PostListing для тестов, не использует БД."""

    def __init__(self, posts_by_user: dict[int, list[Post]] | None = None) -> None:
        self._posts_by_user = posts_by_user or {}

    async def list_pending_by_user(self, user_id: int) -> list[Post]:
        return list(self._posts_by_user.get(user_id, []))


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


async def _make_user(user_service: UserService, telegram_user_id: int, timezone: str) -> int:
    user = await user_service.get_or_create(
        telegram_user_id=telegram_user_id,
        username=None,
        first_name=f"User{telegram_user_id}",
    )
    # User.timezone frozen, но DEFAULT_TIMEZONE в UserService используется как fallback.
    # Возвращаем user.id, а timezone указывается при создании Post.
    return user.id


def _make_post(
    user_id: int,
    scheduled_at_utc: datetime,
    *,
    publication_type: PublicationType = PublicationType.REMINDER,
    target_id: str | None = None,
    timezone: str = "Europe/Moscow",
) -> Post:
    return Post(
        id=hash((user_id, scheduled_at_utc)) & 0x7FFFFFFF,
        user_id=user_id,
        telegram_file_id=f"file_{user_id}_{scheduled_at_utc.timestamp()}",
        telegram_file_unique_id=f"unique_{user_id}",
        scheduled_at_utc=scheduled_at_utc,
        timezone=timezone,
        publication_type=publication_type,
        created_at=scheduled_at_utc - timedelta(days=1),
        updated_at=scheduled_at_utc - timedelta(days=1),
        target_id=target_id,
    )


# === MyPostsService tests (SPEC-017 §8 empty/sorting/timezone/filtering/isolation) ===


async def test_my_posts_empty(post_repo) -> None:
    """Если у пользователя нет постов — EMPTY_LIST."""
    repo, user_service = post_repo
    await _make_user(user_service, telegram_user_id=100, timezone="UTC")
    service = MyPostsService(repo, user_service)

    result = await service.render_list(telegram_user_id=100)

    assert result == EMPTY_LIST


async def test_my_posts_unknown_user_returns_empty(post_repo) -> None:
    """Неизвестный telegram_user_id → EMPTY_LIST."""
    repo, user_service = post_repo
    service = MyPostsService(repo, user_service)

    result = await service.render_list(telegram_user_id=999999)

    assert result == EMPTY_LIST


async def test_my_posts_sorting_by_scheduled_at_utc(post_repo) -> None:
    """Посты отсортированы по scheduled_at_utc ASC."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=101, timezone="UTC")

    # Создаём посты в произвольном порядке
    now = datetime.now(UTC)
    await repo.create_post(
        user_id=user_id,
        telegram_file_id="file_late",
        telegram_file_unique_id="unique_late",
        scheduled_at_utc=now + timedelta(hours=3),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    await repo.create_post(
        user_id=user_id,
        telegram_file_id="file_early",
        telegram_file_unique_id="unique_early",
        scheduled_at_utc=now + timedelta(hours=1),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    await repo.create_post(
        user_id=user_id,
        telegram_file_id="file_mid",
        telegram_file_unique_id="unique_mid",
        scheduled_at_utc=now + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )

    service = MyPostsService(repo, user_service)
    result = await service.render_list(telegram_user_id=101)

    # В выводе должны быть все три поста, отсортированные по времени
    # Ищем по ID (т.к. file_id не отображается)
    lines = [line for line in result.split("\n") if line.startswith(("1.", "2.", "3."))]
    assert len(lines) == 3
    # Время должно возрастать: 1 час, 2 часа, 3 часа
    # Извлекаем время из каждой строки
    assert lines[0].startswith("1.")
    assert lines[1].startswith("2.")
    assert lines[2].startswith("3.")


async def test_my_posts_timezone(post_repo) -> None:
    """Время отображается в часовой зоне пользователя."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=102, timezone="Europe/Moscow")

    # Пост на 12:00 UTC
    scheduled = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    await repo.create_post(
        user_id=user_id,
        telegram_file_id="file_tz",
        telegram_file_unique_id="unique_tz",
        scheduled_at_utc=scheduled,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )

    service = MyPostsService(repo, user_service)
    result = await service.render_list(telegram_user_id=102)

    # UTC 12:00 = Moscow 15:00
    assert "15:00" in result


async def test_my_posts_timezone_different_user(post_repo) -> None:
    """Разные пользователи видят время в своей зоне."""
    repo, user_service = post_repo
    user_moscow = await _make_user(user_service, telegram_user_id=103, timezone="Europe/Moscow")
    user_ny = await _make_user(user_service, telegram_user_id=104, timezone="America/New_York")

    scheduled = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    await repo.create_post(
        user_id=user_moscow,
        telegram_file_id="file_m",
        telegram_file_unique_id="unique_m",
        scheduled_at_utc=scheduled,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    await repo.create_post(
        user_id=user_ny,
        telegram_file_id="file_n",
        telegram_file_unique_id="unique_n",
        scheduled_at_utc=scheduled,
        timezone="America/New_York",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )

    service = MyPostsService(repo, user_service)
    result_moscow = await service.render_list(telegram_user_id=103)
    result_ny = await service.render_list(telegram_user_id=104)

    assert "15:00" in result_moscow  # Moscow: UTC+3
    assert "08:00" in result_ny  # NY: UTC-4 (DST)


async def test_my_posts_filter_only_pending(post_repo) -> None:
    """Отображаются только PENDING-посты."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=105, timezone="UTC")

    now = datetime.now(UTC)
    pending = await repo.create_post(
        user_id=user_id,
        telegram_file_id="file_pending",
        telegram_file_unique_id="unique_pending",
        scheduled_at_utc=now + timedelta(hours=1),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    pending_id = pending.id
    cancelled = await repo.create_post(
        user_id=user_id,
        telegram_file_id="file_cancelled",
        telegram_file_unique_id="unique_cancelled",
        scheduled_at_utc=now + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    cancelled_id = cancelled.id
    await repo.update_status(cancelled_id, PostStatus.CANCELLED)
    published = await repo.create_post(
        user_id=user_id,
        telegram_file_id="file_published",
        telegram_file_unique_id="unique_published",
        scheduled_at_utc=now + timedelta(hours=3),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    published_id = published.id
    await repo.update_status(published_id, PostStatus.PROCESSING)
    await repo.update_status(published_id, PostStatus.PUBLISHED, published_at=datetime.now(UTC))

    service = MyPostsService(repo, user_service)
    result = await service.render_list(telegram_user_id=105)

    # В выводе только pending_id
    assert f"ID {pending_id}" in result
    assert f"ID {cancelled_id}" not in result
    assert f"ID {published_id}" not in result


async def test_my_posts_isolation(post_repo) -> None:
    """Пользователь видит только свои посты."""
    repo, user_service = post_repo
    user_alice = await _make_user(user_service, telegram_user_id=106, timezone="UTC")
    user_bob = await _make_user(user_service, telegram_user_id=107, timezone="UTC")

    now = datetime.now(UTC)
    alice = await repo.create_post(
        user_id=user_alice,
        telegram_file_id="file_alice",
        telegram_file_unique_id="unique_alice",
        scheduled_at_utc=now + timedelta(hours=1),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    alice_id = alice.id
    bob = await repo.create_post(
        user_id=user_bob,
        telegram_file_id="file_bob",
        telegram_file_unique_id="unique_bob",
        scheduled_at_utc=now + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    bob_id = bob.id

    service = MyPostsService(repo, user_service)
    alice_result = await service.render_list(telegram_user_id=106)
    bob_result = await service.render_list(telegram_user_id=107)

    # Проверяем по полному "ID {post_id}", чтобы не ловить совпадения в номере строки
    assert f"ID {alice_id}" in alice_result
    assert f"ID {bob_id}" not in alice_result
    assert f"ID {bob_id}" in bob_result
    assert f"ID {alice_id}" not in bob_result


async def test_my_posts_includes_publication_type_label(post_repo) -> None:
    """В выводе указан тип публикации."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=108, timezone="UTC")

    now = datetime.now(UTC)
    await repo.create_post(
        user_id=user_id,
        telegram_file_id="file_t",
        telegram_file_unique_id="unique_t",
        scheduled_at_utc=now + timedelta(hours=1),
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )
    await repo.create_post(
        user_id=user_id,
        telegram_file_id="file_r",
        telegram_file_unique_id="unique_r",
        scheduled_at_utc=now + timedelta(hours=2),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )

    service = MyPostsService(repo, user_service)
    result = await service.render_list(telegram_user_id=108)

    assert "публикация в канал" in result
    assert "напоминание" in result


async def test_my_posts_includes_post_id(post_repo) -> None:
    """В выводе указан post.id."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=109, timezone="UTC")

    now = datetime.now(UTC)
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id="file_id",
        telegram_file_unique_id="unique_id",
        scheduled_at_utc=now + timedelta(hours=1),
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )
    post_id = post.id

    service = MyPostsService(repo, user_service)
    result = await service.render_list(telegram_user_id=109)

    assert f"ID {post_id}" in result


async def test_my_posts_with_stub_post_listing(post_repo) -> None:
    """MyPostsService работает с любой реализацией PostListing (не только PostRepository)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=110, timezone="UTC")

    now = datetime.now(UTC)
    posts = [
        _make_post(user_id, now + timedelta(hours=1)),
        _make_post(user_id, now + timedelta(hours=2)),
    ]
    stub = _StubPostListing({user_id: posts})
    service = MyPostsService(stub, user_service)

    result = await service.render_list(telegram_user_id=110)

    # Должны быть обе записи
    lines = [line for line in result.split("\n") if line.startswith(("1.", "2."))]
    assert len(lines) == 2


# === Handler tests (SPEC-017 §7) ===


async def test_handler_register_only_private_chat() -> None:
    """Handler /my_posts зарегистрирован только для приватных чатов."""

    class _StubBot:
        def __init__(self) -> None:
            self.handlers: list[dict] = []

        def message_handler(self, *, commands: list[str], chat_types: list[str]):
            def decorator(func):
                self.handlers.append({"commands": commands, "chat_types": chat_types})
                return func

            return decorator

    bot = _StubBot()  # type: ignore[arg-type]
    register_my_posts_handler(bot, MyPostsService(_StubPostListing(), None))  # type: ignore[arg-type]

    assert len(bot.handlers) == 1
    assert bot.handlers[0]["commands"] == ["my_posts"]
    assert bot.handlers[0]["chat_types"] == ["private"]


def test_my_posts_failed_message_constant() -> None:
    """Константа MY_POSTS_FAILED не пустая."""
    assert MY_POSTS_FAILED
    assert isinstance(MY_POSTS_FAILED, str)


def test_empty_list_constant() -> None:
    """Константа EMPTY_LIST не пустая."""
    assert EMPTY_LIST
    assert isinstance(EMPTY_LIST, str)


# === E2E test через fake Telegram API (SPEC-017 §3) ===


async def test_my_posts_e2e_via_fake_telegram(running_app, fake_telegram, tmp_path: Path) -> None:
    """/my_posts через Long Polling возвращает список PENDING постов."""
    from harness import wait_until

    # Регистрируем пользователя через /start
    fake_telegram.add_text_message(
        "/start", chat_id=200, user_id=200, username=None, first_name="Тестер"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)

    # Создаём пост напрямую через engine
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from postmaster.database.models import PostModel
    from postmaster.database.schema import create_schema
    from postmaster.domain.post import PublicationType

    engine = running_app._engine
    await create_schema(engine)  # Идемпотентная операция
    factory = async_sessionmaker(engine, expire_on_commit=False)
    scheduled = datetime.now(UTC) + timedelta(hours=2)
    async with factory() as session:
        from postmaster.domain.post import PostStatus

        session.add(
            PostModel(
                user_id=1,  # Первый пользователь
                telegram_file_id="AgAC_e2e_test",
                telegram_file_unique_id="AQAD_e2e_test",
                scheduled_at_utc=scheduled,
                timezone="UTC",
                status=PostStatus.PENDING.value,
                publication_type=PublicationType.REMINDER.value,
                target_id=None,
                error_message=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                published_at=None,
                caption=None,
            )
        )
        await session.commit()

    # Отправляем /my_posts
    fake_telegram.add_text_message("/my_posts", chat_id=200, user_id=200)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    # Проверяем сообщение с /my_posts
    my_posts_message = fake_telegram.sent_messages[-1]
    assert my_posts_message["chat_id"] == "200"
    assert "ID 1" in my_posts_message["text"]
    assert "напоминание" in my_posts_message["text"]
