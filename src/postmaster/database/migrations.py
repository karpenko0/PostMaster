"""Версионированные миграции схемы БД (PM-06).

До PM-06 схема создавалась только ``create_all``: новые таблицы появлялись, но новые
колонки в существующих таблицах не добавлялись, поэтому развёрнутая база после обновления
приложения становилась несовместимой. Модуль добавляет минимальный, но проверяемый механизм:

1. Таблица ``schema_version`` хранит применённые версии.
2. Каждая миграция идемпотентна: повторный запуск не меняет схему и данные.
3. Для каждой миграции есть откат (``rollback``) — это требование PM-06 §2.

Ограничения решения: DDL выполняется через SQLAlchemy-инспекцию и текстовый ALTER TABLE.
SQLite умеет ``ALTER TABLE … ADD COLUMN`` без пересоздания таблицы, поэтому миграция
``0001`` безопасна для существующих данных. ``DROP COLUMN`` поддерживается SQLite 3.35+;
для более старых версий откат выполняет пересоздание таблицы через ``CREATE TABLE … AS``.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

SCHEMA_VERSION_TABLE = "schema_version"


@dataclass(frozen=True, slots=True)
class Migration:
    """Одна миграция схемы: версия, имя и обратимая пара действий."""

    version: int
    name: str


MIGRATION_POSTS_DRAFT_ID = Migration(version=1, name="posts_draft_id")


async def _current_version(connection: Connection) -> int:
    await connection.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
            "version INTEGER PRIMARY KEY, name VARCHAR NOT NULL, applied_at DATETIME NOT NULL)"
        )
    )
    result = await connection.execute(
        text(f"SELECT COALESCE(MAX(version), 0) FROM {SCHEMA_VERSION_TABLE}")
    )
    return int(result.scalar_one())


def _columns(connection: Connection, table: str) -> set[str]:
    inspector = inspect(connection)
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _table_exists(connection: Connection, table: str) -> bool:
    return table in inspect(connection).get_table_names()


def _apply_posts_draft_id(connection: Connection) -> bool:
    """Добавляет posts.draft_id и уникальный индекс. True — схема изменена."""
    if not _table_exists(connection, "posts"):
        # Таблицы ещё нет: её создаст create_schema вместе с нужной колонкой.
        return False
    changed = False
    if "draft_id" not in _columns(connection, "posts"):
        connection.execute(text("ALTER TABLE posts ADD COLUMN draft_id VARCHAR(64)"))
        changed = True
    existing_indexes = {index["name"] for index in inspect(connection).get_indexes("posts")}
    if "ux_posts_draft_id" not in existing_indexes:
        # Индекс уникален, но NULL-значения в SQLite не считаются равными,
        # поэтому старые записи без draft_id совместимы.
        connection.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS ux_posts_draft_id ON posts (draft_id)")
        )
        changed = True
    return changed


def _rollback_posts_draft_id(connection: Connection) -> bool:
    """Удаляет индекс и колонку posts.draft_id. True — схема изменена."""
    if not _table_exists(connection, "posts"):
        return False
    changed = False
    existing_indexes = {index["name"] for index in inspect(connection).get_indexes("posts")}
    if "ux_posts_draft_id" in existing_indexes:
        connection.execute(text("DROP INDEX IF EXISTS ux_posts_draft_id"))
        changed = True
    if "draft_id" in _columns(connection, "posts"):
        try:
            connection.execute(text("ALTER TABLE posts DROP COLUMN draft_id"))
        except Exception:  # noqa: BLE001 — старый SQLite без DROP COLUMN
            # Пересоздание таблицы без DATA-потерь: draft_id — служебная колонка,
            # созданная этой миграцией, в старых данных её быть не может.
            logger.warning("ALTER TABLE DROP COLUMN недоступен, пересоздаём posts без draft_id")
            connection.execute(
                text(
                    "CREATE TABLE posts_rollback AS SELECT id, user_id, telegram_file_id, "
                    "telegram_file_unique_id, scheduled_at_utc, timezone, status, "
                    "publication_type, target_id, error_message, created_at, updated_at, "
                    "published_at, caption FROM posts"
                )
            )
            connection.execute(text("DROP TABLE posts"))
            connection.execute(text("ALTER TABLE posts_rollback RENAME TO posts"))
        changed = True
    return changed


async def apply_migrations(engine: AsyncEngine) -> int:
    """Применяет все миграции, отсутствующие в БД. Возвращает новую версию схемы."""
    async with engine.begin() as connection:
        version = await _current_version(connection)
        if version < MIGRATION_POSTS_DRAFT_ID.version:
            changed = await connection.run_sync(lambda sync: _apply_posts_draft_id(sync))
            await connection.execute(
                text(
                    f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, name, applied_at) "
                    "VALUES (:version, :name, :applied_at)"
                ),
                {
                    "version": MIGRATION_POSTS_DRAFT_ID.version,
                    "name": MIGRATION_POSTS_DRAFT_ID.name,
                    "applied_at": datetime.now(UTC).replace(tzinfo=None),
                },
            )
            version = MIGRATION_POSTS_DRAFT_ID.version
            logger.info(
                "Migration applied version=%s name=%s schema_changed=%s",
                MIGRATION_POSTS_DRAFT_ID.version,
                MIGRATION_POSTS_DRAFT_ID.name,
                changed,
            )
    return version


async def rollback_migrations(engine: AsyncEngine, *, target_version: int = 0) -> int:
    """Откатывает миграции до target_version. Возвращает итоговую версию схемы."""
    async with engine.begin() as connection:
        version = await _current_version(connection)
        if version >= MIGRATION_POSTS_DRAFT_ID.version > target_version:
            await connection.run_sync(lambda sync: _rollback_posts_draft_id(sync))
            await connection.execute(
                text(f"DELETE FROM {SCHEMA_VERSION_TABLE} WHERE version = :version"),
                {"version": MIGRATION_POSTS_DRAFT_ID.version},
            )
            version = target_version
            logger.info("Migration rolled back version=%s", MIGRATION_POSTS_DRAFT_ID.version)
    return version


async def schema_version(engine: AsyncEngine) -> int:
    """Текущая версия схемы без изменения БД."""
    async with engine.connect() as connection:
        return await _current_version(connection)
