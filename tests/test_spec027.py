"""SPEC-027: SQLite и Repository layer.

SQLAlchemy + SQLite как постоянное хранилище; UserRepository/PostRepository;
транзакции, ограничения (constraints) и атомарные обновления статуса.

§8 Обязательные проверки:
- CRUD;
- persistence reopen (данные переживают перезапуск приложения);
- unique user (уникальность telegram_user_id);
- atomic claim/cancel.

§7 Дополнительно:
- handlers не выполняют SQL;
- Post коммитится в БД до регистрации задания в Scheduler;
- ограничения и внешние ключи работают на уровне БД, а не только репозитория.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.models import PostModel, UserModel
from postmaster.database.schema import create_schema
from postmaster.domain.post import (
    InvalidPostTransitionError,
    PostNotFoundError,
    PostStatus,
    PublicationType,
    UserNotFoundError,
)
from postmaster.domain.user import DuplicateTelegramUserError
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src" / "postmaster"
HANDLERS_ROOT = SRC_ROOT / "handlers"

RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], int, Path]]:
    """Фабрика сессий, id пользователя и путь к файлу SQLite."""
    db_file = db_path(tmp_path)
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_file}")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user_repo = UserRepository(factory)
        user = await user_repo.add(
            telegram_user_id=1001, username="alice", first_name="Alice", now=RUN_AT
        )
        yield factory, user.id, db_file
    finally:
        await engine.dispose()


@pytest.fixture
def repos(db) -> tuple[UserRepository, PostRepository]:
    factory, _user_id, _ = db
    return UserRepository(factory), PostRepository(factory)


async def _create_post(repo: PostRepository, user_id: int, **kwargs) -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id=kwargs.get("telegram_file_id", "file_1"),
        telegram_file_unique_id=kwargs.get("telegram_file_unique_id", "unique_1"),
        scheduled_at_utc=kwargs.get("scheduled_at_utc", RUN_AT),
        timezone=kwargs.get("timezone", "UTC"),
        publication_type=kwargs.get("publication_type", PublicationType.REMINDER),
        target_id=kwargs.get("target_id"),
        caption=kwargs.get("caption"),
    )
    return post.id


# === CRUD =================================================================


async def test_user_crud(db) -> None:
    """CRUD пользователя: создание, чтение, обновление (SPEC-027 §8)."""
    factory, _, _ = db
    repo = UserRepository(factory)

    created = await repo.add(telegram_user_id=2001, username="bob", first_name="Bob", now=RUN_AT)
    assert created.id > 0
    assert created.telegram_user_id == 2001
    assert created.username == "bob"

    fetched = await repo.find_by_telegram_id(2001)
    assert fetched is not None
    assert fetched.id == created.id

    by_id = await repo.find_by_id(created.id)
    assert by_id is not None
    assert by_id.telegram_user_id == 2001

    updated = await repo.update_profile(
        telegram_user_id=2001, username="bob2", first_name="Bob Jr", now=RUN_AT
    )
    assert updated.username == "bob2"
    assert updated.first_name == "Bob Jr"


async def test_post_crud(db) -> None:
    """CRUD публикации: создание, чтение, смена статуса (SPEC-027 §8)."""
    factory, user_id, _ = db
    repo = PostRepository(factory)

    post_id = await _create_post(repo, user_id)
    created = await repo.get_by_id(post_id)
    assert created is not None
    assert created.user_id == user_id
    assert created.status == PostStatus.PENDING
    assert created.telegram_file_id == "file_1"

    claimed = await repo.claim_for_processing(post_id)
    assert claimed is not None
    assert claimed.status == PostStatus.PROCESSING

    published = await repo.update_status(post_id, PostStatus.PUBLISHED, published_at=RUN_AT)
    assert published.status == PostStatus.PUBLISHED
    assert published.published_at == RUN_AT


async def test_update_status_on_missing_post_raises(db) -> None:
    """Смена статуса несуществующей публикации — PostNotFoundError."""
    factory, _, _ = db
    repo = PostRepository(factory)

    with pytest.raises(PostNotFoundError):
        await repo.update_status(999_999, PostStatus.PUBLISHED, published_at=RUN_AT)


async def test_invalid_transition_is_rejected(db) -> None:
    """Недопустимый переход статуса отклоняется и не меняет запись."""
    factory, user_id, _ = db
    repo = PostRepository(factory)
    post_id = await _create_post(repo, user_id)

    with pytest.raises(InvalidPostTransitionError):
        await repo.update_status(post_id, PostStatus.PUBLISHED, published_at=RUN_AT)

    unchanged = await repo.get_by_id(post_id)
    assert unchanged is not None
    assert unchanged.status == PostStatus.PENDING


# === persistence reopen ====================================================


async def test_data_survives_engine_reopen(tmp_path: Path) -> None:
    """Данные переживают закрытие и открытие БД (SPEC-027 §7, §8)."""
    url = f"sqlite+aiosqlite:///{db_path(tmp_path)}"

    engine = create_database_engine(url)
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user_repo = UserRepository(factory)
        post_repo = PostRepository(factory)
        user = await user_repo.add(
            telegram_user_id=3001, username="carol", first_name="Carol", now=RUN_AT
        )
        post_id = await _create_post(post_repo, user.id)
        await post_repo.claim_for_processing(post_id)
    finally:
        await engine.dispose()

    # «Перезапуск приложения»: новое соединение к тому же файлу.
    engine = create_database_engine(url)
    try:
        factory = create_session_factory(engine)
        user_repo = UserRepository(factory)
        post_repo = PostRepository(factory)

        user = await user_repo.find_by_telegram_id(3001)
        assert user is not None
        assert user.username == "carol"

        post = await post_repo.get_by_id(post_id)
        assert post is not None
        assert post.status == PostStatus.PROCESSING
        assert post.user_id == user.id
    finally:
        await engine.dispose()


async def test_schema_is_not_dropped_on_reopen(tmp_path: Path) -> None:
    """Повторный create_schema не удаляет существующие таблицы и данные."""
    url = f"sqlite+aiosqlite:///{db_path(tmp_path)}"

    engine = create_database_engine(url)
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user_repo = UserRepository(factory)
        await user_repo.add(telegram_user_id=3101, username="dave", first_name="Dave", now=RUN_AT)
    finally:
        await engine.dispose()

    engine = create_database_engine(url)
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user = await UserRepository(factory).find_by_telegram_id(3101)
        assert user is not None
        assert user.username == "dave"
    finally:
        await engine.dispose()


# === unique user ==========================================================


async def test_duplicate_telegram_user_id_is_rejected(db) -> None:
    """Второй пользователь с тем же telegram_user_id не создаётся (SPEC-027 §8)."""
    factory, _, _ = db
    repo = UserRepository(factory)

    with pytest.raises(DuplicateTelegramUserError):
        await repo.add(
            telegram_user_id=1001, username="impostor", first_name="Impostor", now=RUN_AT
        )


async def test_database_enforces_unique_telegram_user_id(db) -> None:
    """Уникальность обеспечивается самой БД, а не только репозиторием."""
    factory, _, _ = db

    async with factory() as session, session.begin():
        session.add(
            UserModel(
                telegram_user_id=4001,
                username="raw",
                first_name="Raw",
                timezone=None,
                created_at=RUN_AT.replace(tzinfo=None),
                updated_at=RUN_AT.replace(tzinfo=None),
            )
        )

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(
                UserModel(
                    telegram_user_id=4001,
                    username="raw2",
                    first_name="Raw2",
                    timezone=None,
                    created_at=RUN_AT.replace(tzinfo=None),
                    updated_at=RUN_AT.replace(tzinfo=None),
                )
            )


async def test_duplicate_detection_against_raw_constraint(db) -> None:
    """Репозиторий отличает дубль telegram_user_id от других нарушений."""
    factory, _, _ = db
    repo = UserRepository(factory)

    with pytest.raises(DuplicateTelegramUserError):
        await repo.add(telegram_user_id=1001, username="second", first_name="Second", now=RUN_AT)

    async with factory() as session:
        count = (
            await session.execute(
                text("SELECT COUNT(*) FROM users WHERE telegram_user_id = :tid"),
                {"tid": 1001},
            )
        ).scalar()

    assert count == 1


# === atomic claim / cancel =================================================


async def test_claim_is_atomic_and_single_winner(db) -> None:
    """Только один исполнитель получает claim (SPEC-027 §8)."""
    factory, user_id, _ = db
    repo = PostRepository(factory)
    post_id = await _create_post(repo, user_id)

    first = await repo.claim_for_processing(post_id)
    second = await repo.claim_for_processing(post_id)

    assert first is not None
    assert second is None

    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.PROCESSING


async def test_concurrent_claim_has_single_winner(db) -> None:
    """Параллельные claim: ровно один успешный."""
    factory, user_id, _ = db
    repo = PostRepository(factory)
    post_id = await _create_post(repo, user_id)

    results = await asyncio.gather(*(repo.claim_for_processing(post_id) for _ in range(5)))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1


async def test_cancel_is_atomic_and_single_winner(db) -> None:
    """Параллельные отмены: ровно одна успешна (SPEC-027 §8)."""
    factory, user_id, _ = db
    repo = PostRepository(factory)
    post_id = await _create_post(repo, user_id)

    results = await asyncio.gather(*(repo.cancel_post(post_id, user_id) for _ in range(5)))

    assert sum(1 for result in results if result) == 1
    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.CANCELLED


async def test_cancel_after_claim_does_not_succeed(db) -> None:
    """Отмена уже взятого в работу поста не проходит."""
    factory, user_id, _ = db
    repo = PostRepository(factory)
    post_id = await _create_post(repo, user_id)
    await repo.claim_for_processing(post_id)

    assert await repo.cancel_post(post_id, user_id) is False

    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.PROCESSING


async def test_claim_after_cancel_does_not_succeed(db) -> None:
    """Claim отменённого поста не проходит."""
    factory, user_id, _ = db
    repo = PostRepository(factory)
    post_id = await _create_post(repo, user_id)
    assert await repo.cancel_post(post_id, user_id) is True

    assert await repo.claim_for_processing(post_id) is None


# === Constraints ===========================================================


async def test_database_rejects_invalid_status(db) -> None:
    """CheckConstraint на status работает на уровне БД."""
    factory, user_id, _ = db

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(
                PostModel(
                    user_id=user_id,
                    telegram_file_id="f",
                    telegram_file_unique_id="u",
                    scheduled_at_utc=RUN_AT.replace(tzinfo=None),
                    timezone="UTC",
                    status="NOT_A_STATUS",
                    publication_type="REMINDER",
                    target_id=None,
                    error_message=None,
                    created_at=RUN_AT.replace(tzinfo=None),
                    updated_at=RUN_AT.replace(tzinfo=None),
                    published_at=None,
                    caption=None,
                )
            )


async def test_database_rejects_published_without_timestamp(db) -> None:
    """PUBLISHED без published_at отклоняется БД."""
    factory, user_id, _ = db

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(
                PostModel(
                    user_id=user_id,
                    telegram_file_id="f",
                    telegram_file_unique_id="u",
                    scheduled_at_utc=RUN_AT.replace(tzinfo=None),
                    timezone="UTC",
                    status="PUBLISHED",
                    publication_type="REMINDER",
                    target_id=None,
                    error_message=None,
                    created_at=RUN_AT.replace(tzinfo=None),
                    updated_at=RUN_AT.replace(tzinfo=None),
                    published_at=None,
                    caption=None,
                )
            )


async def test_database_rejects_telegram_post_without_target(db) -> None:
    """TELEGRAM без target_id отклоняется БД."""
    factory, user_id, _ = db

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(
                PostModel(
                    user_id=user_id,
                    telegram_file_id="f",
                    telegram_file_unique_id="u",
                    scheduled_at_utc=RUN_AT.replace(tzinfo=None),
                    timezone="UTC",
                    status="PENDING",
                    publication_type="TELEGRAM",
                    target_id=None,
                    error_message=None,
                    created_at=RUN_AT.replace(tzinfo=None),
                    updated_at=RUN_AT.replace(tzinfo=None),
                    published_at=None,
                    caption=None,
                )
            )


async def test_foreign_key_is_enforced(db) -> None:
    """posts.user_id ссылается на users.id (SPEC-027 §4)."""
    factory, _, _ = db

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(
                PostModel(
                    user_id=999_999,
                    telegram_file_id="f",
                    telegram_file_unique_id="u",
                    scheduled_at_utc=RUN_AT.replace(tzinfo=None),
                    timezone="UTC",
                    status="PENDING",
                    publication_type="REMINDER",
                    target_id=None,
                    error_message=None,
                    created_at=RUN_AT.replace(tzinfo=None),
                    updated_at=RUN_AT.replace(tzinfo=None),
                    published_at=None,
                    caption=None,
                )
            )


async def test_repository_rejects_post_for_unknown_user(db) -> None:
    """Репозиторий не создаёт пост для несуществующего пользователя."""
    factory, _, _ = db
    repo = PostRepository(factory)

    with pytest.raises(UserNotFoundError):
        await _create_post(repo, 999_999)


async def test_indexes_exist_for_queue_and_user_listing(db) -> None:
    """Индексы для очереди и выборки пользователя созданы (SPEC-027 §4)."""
    factory, _, _ = db

    async with factory() as session:
        rows = (await session.execute(text("PRAGMA index_list(posts)"))).all()
        names = {row[1] for row in rows}

    assert "ix_posts_user_status_scheduled" in names
    assert "ix_posts_status_scheduled" in names


# === Транзакции ============================================================


async def test_failed_write_leaves_no_partial_data(db) -> None:
    """Ошибка внутри транзакции не оставляет частичных записей (SPEC-027 §6)."""
    factory, user_id, _ = db

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            session.add(
                PostModel(
                    user_id=user_id,
                    telegram_file_id="f",
                    telegram_file_unique_id="u",
                    scheduled_at_utc=RUN_AT.replace(tzinfo=None),
                    timezone="UTC",
                    status="PENDING",
                    publication_type="REMINDER",
                    target_id=None,
                    error_message=None,
                    created_at=RUN_AT.replace(tzinfo=None),
                    updated_at=RUN_AT.replace(tzinfo=None),
                    published_at=None,
                    caption=None,
                )
            )
            # Нарушение constraints откатывает всю транзакцию.
            session.add(
                PostModel(
                    user_id=user_id,
                    telegram_file_id="f2",
                    telegram_file_unique_id="u2",
                    scheduled_at_utc=RUN_AT.replace(tzinfo=None),
                    timezone="UTC",
                    status="NOT_A_STATUS",
                    publication_type="REMINDER",
                    target_id=None,
                    error_message=None,
                    created_at=RUN_AT.replace(tzinfo=None),
                    updated_at=RUN_AT.replace(tzinfo=None),
                    published_at=None,
                    caption=None,
                )
            )

    async with factory() as session:
        count = (await session.execute(text("SELECT COUNT(*) FROM posts"))).scalar()

    assert count == 0


async def test_status_update_is_a_single_atomic_statement(db) -> None:
    """Переход статуса — один условный UPDATE по прежнему статусу."""
    factory, user_id, _ = db
    repo = PostRepository(factory)
    post_id = await _create_post(repo, user_id)
    await repo.claim_for_processing(post_id)

    # Повторная попытка сменить статус на тот же — отклоняется переходом.
    with pytest.raises(InvalidPostTransitionError):
        await repo.update_status(post_id, PostStatus.PROCESSING)

    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.PROCESSING


# === Handlers не выполняют SQL (SPEC-027 §7) =============================


def test_handlers_do_not_import_sqlalchemy() -> None:
    """Handlers не работают с SQLAlchemy — только через сервисы (SPEC-027 §7)."""
    offenders: list[str] = []
    for path in HANDLERS_ROOT.rglob("*.py"):
        text_content = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text_content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "sqlalchemy" in line or "AsyncSession" in line or "session_factory" in line:
                offenders.append(f"{path.name}:{line_no}")

    assert offenders == [], "Handlers не должны импортировать SQLAlchemy: " + ", ".join(offenders)


def test_handlers_do_not_contain_sql_statements() -> None:
    """Handlers не содержат SQL-запросов (SPEC-027 §7)."""
    sql_markers = ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", ".execute(")
    offenders: list[str] = []
    for path in HANDLERS_ROOT.rglob("*.py"):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if any(marker in line for marker in sql_markers):
                offenders.append(f"{path.name}:{line_no}")

    assert offenders == [], "Handlers не должны содержать SQL: " + ", ".join(offenders)


def test_handlers_depend_only_on_services() -> None:
    """Handlers обращаются к сервисам, а не к репозиториям (SPEC-027 §7)."""
    offenders: list[str] = []
    for path in HANDLERS_ROOT.rglob("*.py"):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith(("import ", "from ")) and "repositories" in stripped:
                offenders.append(f"{path.name}:{line_no}")

    assert offenders == [], "Handlers не должны импортировать repositories: " + ", ".join(offenders)


# === Commit Post до Scheduler (SPEC-027 §7) ==============================


async def test_post_is_committed_before_scheduler_registration(db) -> None:
    """Post сохраняется в БД до вызова Scheduler.schedule_post (SPEC-027 §7).

    Проверяется через отдельное синхронное подключение к файлу SQLite:
    в момент вызова ``schedule_post`` строка уже зафиксирована.
    """
    import sqlite3

    from postmaster.services.post_service import PostService

    factory, user_id, sqlite_file = db

    observed: dict[str, object] = {}

    class _Scheduler:
        def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
            with sqlite3.connect(str(sqlite_file)) as connection:
                row = connection.execute(
                    "SELECT status FROM posts WHERE id = ?", (post_id,)
                ).fetchone()
            observed["status_in_db"] = None if row is None else row[0]

    repo = PostRepository(factory)
    service = PostService(repository=repo, scheduler=_Scheduler())

    post = await service.create_scheduled_post(
        user_id=user_id,
        telegram_file_id="file_1",
        telegram_file_unique_id="unique_1",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )

    assert observed["status_in_db"] == PostStatus.PENDING.value
    assert post.status == PostStatus.PENDING


async def test_scheduler_failure_keeps_committed_post(db) -> None:
    """Ошибка Scheduler не откатывает уже зафиксированный Post (SPEC-027 §6)."""
    from postmaster.services.post_service import (
        PostService,
        SchedulerRegistrationError,
    )

    factory, user_id, _ = db
    repo = PostRepository(factory)

    class _FailingScheduler:
        def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
            raise RuntimeError("scheduler недоступен")

    service = PostService(repository=repo, scheduler=_FailingScheduler())

    with pytest.raises(SchedulerRegistrationError) as info:
        await service.create_scheduled_post(
            user_id=user_id,
            telegram_file_id="file_1",
            telegram_file_unique_id="unique_1",
            scheduled_at_utc=RUN_AT,
            timezone="UTC",
            publication_type=PublicationType.REMINDER,
        )

    # Запись осталась в БД — её можно восстановить через Recovery (SPEC-011).
    saved = await repo.get_by_id(info.value.post.id)
    assert saved is not None
    assert saved.status == PostStatus.PENDING
