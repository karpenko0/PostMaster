"""SPEC-008: таблица, FK, индексы, CRUD, фильтрация, статусы, recovery."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.models import PostModel
from postmaster.database.schema import create_schema
from postmaster.domain.post import (
    InvalidPostTransitionError,
    PostNotFoundError,
    PostStatus,
    PublicationType,
    UserNotFoundError,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository

CREATED = datetime(2026, 10, 7, 10, tzinfo=UTC)
SCHEDULED = datetime(2026, 10, 7, 12, tzinfo=UTC)
LATER = datetime(2026, 10, 7, 11, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], int]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/posts.db")
    await create_schema(engine)
    factory = create_session_factory(engine)
    user = await UserRepository(factory).add(
        telegram_user_id=123456789, username="anna", first_name="Анна", now=CREATED
    )
    try:
        yield factory, user.id
    finally:
        await engine.dispose()


async def create(repo: PostRepository, user_id: int, **changes):
    params = dict(
        user_id=user_id,
        telegram_file_id="AgAC...",
        telegram_file_unique_id="AQAD...",
        scheduled_at_utc=SCHEDULED,
        timezone="Europe/Moscow",
        publication_type=PublicationType.REMINDER,
        target_id=None,
        caption=None,
        now=CREATED,
    )
    params.update(changes)
    return await repo.create_post(**params)


async def test_schema_has_all_fields_constraints_fk_and_indexes(db) -> None:
    factory, _ = db
    async with factory() as session:
        columns = (await session.execute(text("PRAGMA table_info(posts)"))).all()
        fks = (await session.execute(text("PRAGMA foreign_key_list(posts)"))).all()
        indexes = (await session.execute(text("PRAGMA index_list(posts)"))).all()
        foreign_keys_on = (await session.execute(text("PRAGMA foreign_keys"))).scalar()
    fields = {column[1]: column for column in columns}
    assert set(fields) == {
        "id",
        "user_id",
        "telegram_file_id",
        "telegram_file_unique_id",
        "scheduled_at_utc",
        "timezone",
        "status",
        "publication_type",
        "target_id",
        "error_message",
        "created_at",
        "updated_at",
        "published_at",
        "caption",
    }
    for name, expected_type in {
        "id": "INTEGER",
        "user_id": "INTEGER",
        "telegram_file_id": "TEXT",
        "telegram_file_unique_id": "TEXT",
        "scheduled_at_utc": "DATETIME",
        "timezone": "VARCHAR",
        "status": "VARCHAR",
        "publication_type": "VARCHAR",
        "target_id": "VARCHAR",
        "error_message": "TEXT",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
        "published_at": "DATETIME",
        "caption": "TEXT",
    }.items():
        assert fields[name][2].upper() == expected_type
        assert fields[name][3] == (
            0 if name in {"target_id", "error_message", "published_at", "caption"} else 1
        )
    assert fields["id"][5] == 1
    assert fields["status"][4] == "'PENDING'"
    assert foreign_keys_on == 1
    assert [(fk[2], fk[3], fk[4]) for fk in fks] == [("users", "user_id", "id")]
    assert {index[1] for index in indexes} >= {
        "ix_posts_user_status_scheduled",
        "ix_posts_status_scheduled",
    }


async def test_create_get_by_id_and_utc_roundtrip(db, caplog: pytest.LogCaptureFixture) -> None:
    factory, user_id = db
    repo = PostRepository(factory)
    with caplog.at_level("INFO", logger="postmaster.repositories.post_repository"):
        post = await create(repo, user_id, caption=None)
    assert post.id > 0 and post.user_id == user_id
    assert post.telegram_file_id == "AgAC..."
    assert post.telegram_file_unique_id == "AQAD..."
    assert post.scheduled_at_utc == SCHEDULED
    assert post.scheduled_at_utc.tzinfo is UTC
    assert post.timezone == "Europe/Moscow"
    assert post.status == PostStatus.PENDING
    assert post.publication_type == PublicationType.REMINDER
    assert post.target_id is post.error_message is post.published_at is post.caption is None
    assert post.created_at == post.updated_at == CREATED
    # SPEC-025: структурированное событие post_created с полями post_id/user_id/status.
    assert "event=post_created" in caplog.text
    assert f"post_id={post.id}" in caplog.text
    assert f"user_id={user_id}" in caplog.text
    assert "status=PENDING" in caplog.text
    assert "AgAC..." not in caplog.text
    assert await repo.get_by_id(post.id) == post
    assert await repo.get_by_id(post.id + 100) is None


async def test_telegram_post_requires_target_and_optional_caption(db) -> None:
    factory, user_id = db
    repo = PostRepository(factory)
    post = await create(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
        caption="Подпись",
    )
    assert (post.publication_type, post.target_id, post.caption) == (
        PublicationType.TELEGRAM,
        "-1001234567890",
        "Подпись",
    )
    with pytest.raises(ValueError, match="target_id"):
        await create(repo, user_id, publication_type=PublicationType.TELEGRAM, target_id=None)


async def test_user_fk_is_enforced_by_repository_and_database(db) -> None:
    factory, user_id = db
    repo = PostRepository(factory)
    with pytest.raises(UserNotFoundError):
        await create(repo, user_id + 999)
    assert await repo.list_pending() == []
    # Финальная защита — FK базы, даже при прямом вызове ORM в другой транзакции.
    async with factory() as session:
        model = PostModel(
            user_id=user_id + 999,
            telegram_file_id="photo",
            telegram_file_unique_id="unique",
            scheduled_at_utc=SCHEDULED.replace(tzinfo=None),
            timezone="Europe/Moscow",
            status="PENDING",
            publication_type="REMINDER",
            created_at=CREATED.replace(tzinfo=None),
            updated_at=CREATED.replace(tzinfo=None),
        )
        with pytest.raises(IntegrityError):
            async with session.begin():
                session.add(model)
                await session.flush()


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"telegram_file_id": ""}, "telegram_file_id"),
        ({"telegram_file_unique_id": ""}, "telegram_file_unique_id"),
        ({"scheduled_at_utc": None}, "scheduled_at_utc"),
        ({"scheduled_at_utc": SCHEDULED.replace(tzinfo=None)}, "scheduled_at_utc"),
        ({"scheduled_at_utc": SCHEDULED.astimezone(ZoneInfo("Europe/Moscow"))}, "scheduled_at_utc"),
        ({"publication_type": "VK"}, "VK"),
    ],
)
async def test_invalid_input_does_not_create_post(db, change, error) -> None:
    factory, user_id = db
    repo = PostRepository(factory)
    with pytest.raises(ValueError, match=error):
        await create(repo, user_id, **change)
    assert await repo.list_pending() == []


async def test_database_rejects_unknown_status_type_and_published_without_timestamp(db) -> None:
    factory, user_id = db
    repo = PostRepository(factory)
    post = await create(repo, user_id)
    for column, value in (
        ("status", "UNKNOWN"),
        ("publication_type", "VK"),
        ("status", "PUBLISHED"),
        ("telegram_file_id", ""),
    ):
        with pytest.raises(IntegrityError):
            async with factory() as session, session.begin():
                await session.execute(
                    text(f"UPDATE posts SET {column}=:value WHERE id=:id"),
                    {"value": value, "id": post.id},
                )
    assert await repo.get_by_id(post.id) == post


async def test_database_defaults_and_check_constraints_even_without_repository(db) -> None:
    factory, user_id = db
    # SQL напрямую: ограничения и default должны работать без Python-репозитория.
    async with factory() as session, session.begin():
        row = await session.execute(
            text(
                "INSERT INTO posts (user_id, telegram_file_id, telegram_file_unique_id, "
                "scheduled_at_utc, timezone, publication_type, created_at, updated_at) "
                "VALUES (:user_id, 'photo', 'unique', '2026-10-07 12:00:00', "
                "'Europe/Moscow', 'REMINDER', '2026-10-07 10:00:00', "
                "'2026-10-07 10:00:00') RETURNING id, status, error_message, published_at"
            ),
            {"user_id": user_id},
        )
        row = row.one()
    assert row[1:] == ("PENDING", None, None)
    for changes in (
        "telegram_file_unique_id=''",
        "timezone=''",
        "publication_type='TELEGRAM'",
        "status='PROCESSING', published_at='2026-10-07 11:00:00'",
    ):
        with pytest.raises(IntegrityError):
            async with factory() as session, session.begin():
                await session.execute(
                    text(f"UPDATE posts SET {changes} WHERE id=:id"), {"id": row[0]}
                )
    assert (await PostRepository(factory).get_by_id(row[0])).status == PostStatus.PENDING


async def test_pending_lists_filter_owner_status_and_order(db) -> None:
    factory, a_id = db
    b = await UserRepository(factory).add(
        telegram_user_id=222, username=None, first_name=None, now=CREATED
    )
    repo = PostRepository(factory)
    a_late = await create(repo, a_id, scheduled_at_utc=SCHEDULED + timedelta(hours=3))
    a_early = await create(repo, a_id, scheduled_at_utc=SCHEDULED)
    a_done = await create(repo, a_id, scheduled_at_utc=SCHEDULED + timedelta(hours=1))
    b_pending = await create(repo, b.id, scheduled_at_utc=SCHEDULED + timedelta(hours=2))
    await repo.update_status(a_done.id, PostStatus.PROCESSING, now=LATER)
    await repo.update_status(a_done.id, PostStatus.PUBLISHED, now=LATER + timedelta(minutes=1))

    assert [p.id for p in await repo.list_pending_by_user(a_id)] == [a_early.id, a_late.id]
    assert [p.id for p in await repo.list_pending_by_user(b.id)] == [b_pending.id]
    assert [p.id for p in await repo.list_pending()] == [a_early.id, b_pending.id, a_late.id]


async def test_cancel_requires_owner_and_pending_status(db) -> None:
    factory, user_id = db
    repo = PostRepository(factory)
    post = await create(repo, user_id)
    assert await repo.cancel_post(post.id, user_id + 1, now=LATER) is False
    assert await repo.get_by_id(post.id) == post
    assert await repo.cancel_post(post.id, user_id, now=LATER) is True
    cancelled = await repo.get_by_id(post.id)
    assert cancelled.status == PostStatus.CANCELLED
    assert cancelled.created_at == CREATED and cancelled.updated_at == LATER
    assert cancelled.published_at is None
    assert await repo.cancel_post(post.id, user_id) is False
    assert await repo.cancel_post(post.id + 99, user_id) is False
    assert await repo.list_pending() == []


async def test_concurrent_cancellation_only_succeeds_once(db) -> None:
    factory, user_id = db
    repo = PostRepository(factory)
    post = await create(repo, user_id)
    results = await asyncio.gather(
        repo.cancel_post(post.id, user_id, now=LATER),
        repo.cancel_post(post.id, user_id, now=LATER),
    )
    assert sorted(results) == [False, True]
    assert (await repo.get_by_id(post.id)).status == PostStatus.CANCELLED
    with pytest.raises(InvalidPostTransitionError):
        await repo.update_status(post.id, PostStatus.PROCESSING)


async def test_cancel_does_not_interrupt_processing_or_finished_posts(db) -> None:
    factory, user_id = db
    repo = PostRepository(factory)
    processing = await create(repo, user_id)
    await repo.update_status(processing.id, PostStatus.PROCESSING, now=LATER)
    assert await repo.cancel_post(processing.id, user_id) is False
    assert (await repo.get_by_id(processing.id)).status == PostStatus.PROCESSING
    await repo.update_status(processing.id, PostStatus.PUBLISHED, now=LATER)
    assert await repo.cancel_post(processing.id, user_id) is False
    assert (await repo.get_by_id(processing.id)).published_at == LATER

    failed = await create(repo, user_id)
    await repo.update_status(failed.id, PostStatus.PROCESSING, now=LATER)
    await repo.update_status(failed.id, PostStatus.FAILED, now=LATER)
    assert await repo.cancel_post(failed.id, user_id) is False
    assert (await repo.get_by_id(failed.id)).status == PostStatus.FAILED


async def test_status_transitions_published_failed_retry_and_timestamps(db) -> None:
    factory, user_id = db
    repo = PostRepository(factory)
    post = await create(repo, user_id)
    processing = await repo.update_status(post.id, PostStatus.PROCESSING, now=LATER)
    assert processing.status == PostStatus.PROCESSING and processing.updated_at == LATER
    published_time = LATER + timedelta(minutes=1)
    published = await repo.update_status(
        post.id, PostStatus.PUBLISHED, published_at=published_time, now=published_time
    )
    assert published.status == PostStatus.PUBLISHED
    assert published.published_at == published.updated_at == published_time
    assert published.created_at == post.created_at
    with pytest.raises(InvalidPostTransitionError):
        await repo.update_status(post.id, PostStatus.PROCESSING)

    failed_post = await create(repo, user_id)
    await repo.update_status(failed_post.id, PostStatus.PROCESSING, now=LATER)
    failed = await repo.update_status(
        failed_post.id, PostStatus.FAILED, "Telegram API timeout", now=published_time
    )
    assert failed.error_message == "Telegram API timeout"
    assert failed.published_at is None
    assert failed.status == PostStatus.FAILED
    assert await repo.list_pending() == []
    with pytest.raises(InvalidPostTransitionError):
        await repo.update_status(failed_post.id, PostStatus.PENDING)

    retry = await create(repo, user_id)
    await repo.update_status(retry.id, PostStatus.PROCESSING, now=LATER)
    reset = await repo.update_status(retry.id, PostStatus.PENDING, now=published_time)
    assert reset.status == PostStatus.PENDING
    assert reset.published_at is reset.error_message is None


async def test_invalid_transitions_do_not_modify_post(db) -> None:
    factory, user_id = db
    repo = PostRepository(factory)
    post = await create(repo, user_id)
    for status in (PostStatus.PUBLISHED, PostStatus.FAILED, PostStatus.PENDING):
        with pytest.raises(InvalidPostTransitionError):
            await repo.update_status(post.id, status)
    with pytest.raises(ValueError):
        await repo.update_status(post.id, "UNKNOWN")
    with pytest.raises(ValueError, match="error_message"):
        await repo.update_status(post.id, PostStatus.PROCESSING, error_message="oops")
    with pytest.raises(ValueError, match="published_at"):
        await repo.update_status(post.id, PostStatus.PROCESSING, published_at=LATER)
    with pytest.raises(PostNotFoundError):
        await repo.update_status(post.id + 99, PostStatus.PROCESSING)
    assert await repo.get_by_id(post.id) == post


async def test_pending_post_survives_new_engine_and_session(tmp_path: Path) -> None:
    path = tmp_path / "recovery.db"
    engine = create_database_engine(f"sqlite+aiosqlite:///{path}")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user = await UserRepository(factory).add(
            telegram_user_id=123456789, username=None, first_name=None, now=CREATED
        )
        post = await create(PostRepository(factory), user.id)
    finally:
        await engine.dispose()

    # Новый процесс открывает тот же файл: ни один объект/соединение первого запуска не нужен.
    engine2 = create_database_engine(f"sqlite+aiosqlite:///{path}")
    try:
        await create_schema(engine2)
        repo = PostRepository(create_session_factory(engine2))
        assert await repo.get_by_id(post.id) == post
        assert await repo.list_pending() == [post]
        assert [p.id for p in await repo.list_pending_by_user(user.id)] == [post.id]
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "SELECT scheduled_at_utc, timezone FROM posts WHERE id=?", (post.id,)
            ).fetchone()
        assert row[0].startswith("2026-10-07 12:00") and row[1] == "Europe/Moscow"
    finally:
        await engine2.dispose()
