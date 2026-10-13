"""SPEC-007: nullable Telegram-поля, идентичность, схема и параллельная регистрация."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.models import UserModel
from postmaster.database.schema import create_schema
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.user_service import UserService

STAMP = datetime(2026, 10, 7, 12, tzinfo=UTC)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/spec007.db")
    await create_schema(engine)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def test_users_schema_matches_spec_and_unique_index_exists(factory) -> None:
    async with factory() as session:
        cols = (await session.execute(text("PRAGMA table_info(users)"))).fetchall()
        indexes = (await session.execute(text("PRAGMA index_list(users)"))).fetchall()
    fields = {row[1]: row for row in cols}
    assert set(fields) == {
        "id",
        "telegram_user_id",
        "username",
        "first_name",
        "timezone",
        "created_at",
        "updated_at",
    }
    assert fields["id"][2].upper() == "INTEGER" and fields["id"][5] == 1
    assert fields["telegram_user_id"][2].upper() == "BIGINT"
    assert fields["telegram_user_id"][3] == 1
    for optional in ("username", "first_name", "timezone"):
        assert fields[optional][2].upper() == "VARCHAR" and fields[optional][3] == 0
    for stamp in ("created_at", "updated_at"):
        assert fields[stamp][2].upper() == "DATETIME" and fields[stamp][3] == 1
    async with factory() as session:
        unique_indexes = [row for row in indexes if row[2] == 1]
        indexed_columns = [
            [
                info[2]
                for info in (await session.execute(text(f"PRAGMA index_info('{row[1]}')"))).all()
            ]
            for row in unique_indexes
        ]
    assert ["telegram_user_id"] in indexed_columns
    assert UserModel.__table__.c.telegram_user_id.unique


async def test_none_name_and_username_are_stored_and_found_by_telegram_id(factory) -> None:
    repository = UserRepository(factory)
    service = UserService(repository, default_timezone="Europe/Moscow")
    assert await service.get_by_telegram_id(900) is None

    user = await service.get_or_create(900, None, None)
    assert user.id > 0 and user.telegram_user_id == 900
    assert user.username is None and user.first_name is None and user.timezone is None
    assert service.effective_timezone(user) == "Europe/Moscow"
    assert await service.get_by_telegram_id(900) == user
    assert await repository.find_by_telegram_id(900) == user
    assert await repository.get_by_telegram_id(900) == user
    assert await repository.get_by_telegram_id(901) is None
    assert await service.get_by_telegram_id(901) is None


async def test_profile_updates_nulls_and_preserves_identity_timezone_and_created_at(
    factory, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repository = UserRepository(factory)
    service = UserService(repository, default_timezone="Europe/Moscow")
    monkeypatch.setattr("postmaster.services.user_service.utc_now", lambda: STAMP)
    with caplog.at_level("INFO", logger="postmaster.services.user_service"):
        first = await service.get_or_create(42, "old_name", "Иван")
    assert "User created telegram_user_id=42" in caplog.text
    async with factory() as session, session.begin():
        await session.execute(
            update(UserModel).where(UserModel.telegram_user_id == 42).values(timezone="Asia/Tokyo")
        )

    later = STAMP + timedelta(hours=1)
    monkeypatch.setattr("postmaster.services.user_service.utc_now", lambda: later)
    with caplog.at_level("INFO", logger="postmaster.services.user_service"):
        without_names = await service.get_or_create(42, None, None)
    assert without_names.id == first.id
    assert without_names.telegram_user_id == 42
    assert without_names.username is None and without_names.first_name is None
    assert without_names.timezone == "Asia/Tokyo"
    assert without_names.created_at == STAMP and without_names.updated_at == later
    assert "Existing user resolved telegram_user_id=42" in caplog.text
    assert f"User profile updated user_id={first.id}" in caplog.text
    assert "old_name" not in caplog.text and "Иван" not in caplog.text

    monkeypatch.setattr(
        "postmaster.services.user_service.utc_now", lambda: later + timedelta(hours=1)
    )
    unchanged = await service.get_or_create(42, None, None)
    assert unchanged.updated_at == later  # неизменённые данные не пишутся
    restored = await service.get_or_create(42, "new_name", "Новое имя")
    assert restored.id == first.id and restored.timezone == "Asia/Tokyo"
    assert restored.created_at == first.created_at


async def test_same_username_does_not_merge_different_telegram_users(factory) -> None:
    service = UserService(UserRepository(factory), default_timezone="Europe/Moscow")
    a = await service.get_or_create(111, "same", None)
    b = await service.get_or_create(222, "same", None)
    assert a.id != b.id and a.telegram_user_id != b.telegram_user_id
    assert await service.get_by_telegram_id(111) == a
    assert await service.get_by_telegram_id(222) == b


async def test_concurrent_nullable_profile_creation_is_one_row(factory) -> None:
    # CT-007-01: реальные конкурирующие транзакции, а не последовательные вызовы.
    service = UserService(UserRepository(factory), default_timezone="Europe/Moscow")
    users = await asyncio.gather(*(service.get_or_create(123456789, None, None) for _ in range(8)))
    assert len({user.id for user in users}) == 1
    async with factory() as session:
        count = (await session.execute(text("SELECT COUNT(*) FROM users"))).scalar_one()
    assert count == 1


async def test_existing_sqlite_schema_needs_spec027_migration(tmp_path: Path) -> None:
    """create_all не меняет старое NOT NULL и не подменяет миграцию скрытой правкой данных."""
    from sqlalchemy.exc import IntegrityError

    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/legacy.db")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    telegram_user_id BIGINT NOT NULL UNIQUE,
                    username VARCHAR,
                    first_name VARCHAR NOT NULL,
                    timezone VARCHAR,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
            """)
            )
            await conn.execute(
                text("""
                INSERT INTO users (telegram_user_id, username, first_name, created_at, updated_at)
                VALUES (1, 'old', 'Имя', '2026-10-07 12:00:00', '2026-10-07 12:00:00')
            """)
            )
        await create_schema(engine)
        factory = create_session_factory(engine)
        async with factory() as session:
            columns = (await session.execute(text("PRAGMA table_info(users)"))).fetchall()
            old_row = (
                await session.execute(text("SELECT first_name FROM users WHERE id=1"))
            ).scalar()
        assert next(row for row in columns if row[1] == "first_name")[3] == 1
        assert old_row == "Имя"
        with pytest.raises(IntegrityError):
            await UserService(
                UserRepository(factory), default_timezone="Europe/Moscow"
            ).get_or_create(2, None, None)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("username", "first_name"),
    [
        ("new", "Анна"),  # только username
        ("old", "Новое имя"),  # только first_name
        (None, "Анна"),  # удаление username
        ("old", None),  # исчезновение first_name
    ],
)
async def test_each_telegram_field_can_change_independently(
    factory, monkeypatch: pytest.MonkeyPatch, username: str | None, first_name: str | None
) -> None:
    service = UserService(UserRepository(factory), default_timezone="Europe/Moscow")
    monkeypatch.setattr("postmaster.services.user_service.utc_now", lambda: STAMP)
    before = await service.get_or_create(999, "old", "Анна")
    later = STAMP + timedelta(days=1)
    monkeypatch.setattr("postmaster.services.user_service.utc_now", lambda: later)

    after = await service.get_or_create(999, username, first_name)

    assert after.id == before.id and after.telegram_user_id == 999
    assert (after.username, after.first_name) == (username, first_name)
    assert after.created_at == before.created_at
    assert after.updated_at == later
