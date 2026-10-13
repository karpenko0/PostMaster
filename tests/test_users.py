"""Регистрация пользователя в БД: TC-01…TC-04, AC-01, AC-02, BR-01, BR-02, BR-04 (SPEC-002)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.models import UserModel
from postmaster.database.schema import create_schema
from postmaster.domain.user import DuplicateTelegramUserError, User
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.user_service import UserService

SessionFactory = async_sessionmaker[AsyncSession]
MOMENT = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[SessionFactory]:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/users.db")
    await create_schema(engine)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def repository(session_factory: SessionFactory) -> UserRepository:
    return UserRepository(session_factory)


@pytest.fixture
def service(repository: UserRepository) -> UserService:
    return UserService(repository, default_timezone="UTC")


async def _count_rows(session_factory: SessionFactory, telegram_user_id: int) -> int:
    statement = (
        select(func.count())
        .select_from(UserModel)
        .where(UserModel.telegram_user_id == telegram_user_id)
    )
    async with session_factory() as session:
        return int(await session.scalar(statement) or 0)


def _user(timezone: str | None) -> User:
    return User(
        id=1,
        telegram_user_id=42,
        username=None,
        first_name="Иван",
        timezone=timezone,
        created_at=MOMENT,
        updated_at=MOMENT,
    )


async def test_new_user_is_created_once(
    service: UserService, session_factory: SessionFactory
) -> None:
    before = datetime.now(UTC)

    user = await service.get_or_create(42, "ivan", "Иван")

    after = datetime.now(UTC)
    assert user.telegram_user_id == 42
    assert user.username == "ivan"
    assert user.first_name == "Иван"
    assert user.timezone is None
    assert user.created_at.utcoffset() == timedelta(0)
    assert before <= user.created_at <= after
    assert user.updated_at == user.created_at
    assert await _count_rows(session_factory, 42) == 1


async def test_repeated_start_returns_same_user_without_duplicate(
    service: UserService, session_factory: SessionFactory
) -> None:
    first = await service.get_or_create(42, "ivan", "Иван")

    second = await service.get_or_create(42, "ivan_new", "Иван Петров")

    assert second.id == first.id
    assert await _count_rows(session_factory, 42) == 1
    # Дубликата нет, но изменившиеся имя и username записываются (решение D3, пересмотрено).
    assert second.username == "ivan_new"
    assert second.first_name == "Иван Петров"


async def test_repeated_start_with_new_name_updates_profile_only(
    service: UserService, session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Решение D3 (пересмотрено): новые username и first_name записываются вместе с updated_at.
    # Дата создания и часовой пояс не меняются. Если данные совпали, запись не пишется.
    monkeypatch.setattr("postmaster.services.user_service.utc_now", lambda: MOMENT)
    created = await service.get_or_create(42, "old_name", "Иван")
    async with session_factory() as session, session.begin():
        await session.execute(
            update(UserModel)
            .where(UserModel.telegram_user_id == 42)
            .values(timezone="Europe/Moscow")
        )

    later = MOMENT + timedelta(days=1)
    monkeypatch.setattr("postmaster.services.user_service.utc_now", lambda: later)
    updated = await service.get_or_create(42, "new_name", "Иван Петров")

    assert (updated.username, updated.first_name) == ("new_name", "Иван Петров")
    assert updated.created_at == created.created_at
    assert updated.updated_at == later
    assert updated.timezone == "Europe/Moscow"

    unchanged_moment = later + timedelta(hours=1)
    monkeypatch.setattr("postmaster.services.user_service.utc_now", lambda: unchanged_moment)
    same = await service.get_or_create(42, "new_name", "Иван Петров")

    assert same.updated_at == later
    assert await _count_rows(session_factory, 42) == 1


async def test_user_without_username_is_stored_with_empty_username(
    service: UserService, session_factory: SessionFactory
) -> None:
    created = await service.get_or_create(43, None, "Пётр")

    again = await service.get_or_create(43, None, "Пётр")

    assert created.username is None
    assert again.id == created.id
    assert await _count_rows(session_factory, 43) == 1


async def test_database_rejects_second_row_with_same_telegram_user_id(
    session_factory: SessionFactory,
) -> None:
    def row() -> UserModel:
        stamp = MOMENT.replace(tzinfo=None)
        return UserModel(
            telegram_user_id=42,
            username=None,
            first_name="Иван",
            timezone=None,
            created_at=stamp,
            updated_at=stamp,
        )

    async with session_factory() as session, session.begin():
        session.add(row())

    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            session.add(row())


async def test_repository_reports_duplicate_as_domain_error(repository: UserRepository) -> None:
    await repository.add(telegram_user_id=42, username=None, first_name="Иван", now=MOMENT)

    with pytest.raises(DuplicateTelegramUserError):
        await repository.add(telegram_user_id=42, username=None, first_name="Иван", now=MOMENT)


async def test_service_reuses_row_when_registration_races(
    service: UserService,
    repository: UserRepository,
    session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = await service.get_or_create(42, "ivan", "Иван")
    real_find = repository.find_by_telegram_id
    lookups = 0

    async def find_missing_once(telegram_user_id: int) -> User | None:
        nonlocal lookups
        lookups += 1
        # Первый поиск не видит строку, будто параллельный /start записал её позже.
        if lookups == 1:
            return None
        return await real_find(telegram_user_id)

    monkeypatch.setattr(repository, "find_by_telegram_id", find_missing_once)

    user = await service.get_or_create(42, "ivan", "Иван")

    assert user.id == existing.id
    assert await _count_rows(session_factory, 42) == 1


async def test_parallel_registrations_create_one_row(
    service: UserService, session_factory: SessionFactory
) -> None:
    users = await asyncio.gather(*(service.get_or_create(77, "anna", "Анна") for _ in range(5)))

    assert {user.id for user in users} == {users[0].id}
    assert await _count_rows(session_factory, 77) == 1


async def test_service_applies_default_timezone_to_user_without_timezone(
    repository: UserRepository,
) -> None:
    service = UserService(repository, default_timezone="Europe/Moscow")

    user = await service.get_or_create(50, None, "Ольга")

    assert user.timezone is None
    assert service.effective_timezone(user) == "Europe/Moscow"


def test_effective_timezone_falls_back_to_default_when_user_has_none() -> None:
    assert _user(timezone=None).effective_timezone("Europe/Moscow") == "Europe/Moscow"


def test_effective_timezone_prefers_timezone_set_by_user() -> None:
    assert _user(timezone="Asia/Tokyo").effective_timezone("Europe/Moscow") == "Asia/Tokyo"


async def test_timezone_of_returns_default_without_record_or_own_timezone(
    session_factory: SessionFactory, repository: UserRepository
) -> None:
    # BR-04: без записи и без собственного пояса действует DEFAULT_TIMEZONE.
    service = UserService(repository, default_timezone="Europe/Moscow")
    await service.get_or_create(50, None, "Ольга")

    assert await service.timezone_of(999) == "Europe/Moscow"
    assert await service.timezone_of(50) == "Europe/Moscow"


async def test_timezone_of_prefers_timezone_stored_for_user(
    session_factory: SessionFactory, repository: UserRepository
) -> None:
    # BR-04: если пояс пользователя задан, он важнее DEFAULT_TIMEZONE. В SPEC-003 пояс
    # не задаётся пользователем, поэтому запись обновляется напрямую.
    service = UserService(repository, default_timezone="Europe/Moscow")
    await service.get_or_create(51, None, "Мария")
    async with session_factory() as session, session.begin():
        await session.execute(
            update(UserModel).where(UserModel.telegram_user_id == 51).values(timezone="Asia/Tokyo")
        )

    assert await service.timezone_of(51) == "Asia/Tokyo"
