"""Создание и безопасная миграция таблиц при запуске приложения.

PM-06-R2 добавляет ``posts.source_draft_id``, ``posts.schedule_version`` и таблицу
``dialog_states``. ``create_all`` создаёт только отсутствующие таблицы, поэтому для
уже существующей БД отдельно выполняется идемпотентная миграция:

* недостающие колонки добавляются ``ALTER TABLE ... ADD COLUMN`` с DEFAULT;
* уникальный индекс создаётся через ``IF NOT EXISTS``;
* существующие строки не изменяются, ``source_draft_id`` остаётся NULL,
  ``schedule_version`` получает 0;
* повторный запуск не меняет схему и не удаляет данные.

Перед применением миграции на production рекомендуется резервная копия:
``python -m postmaster backup`` (см. docs/reviews/PM-06-R2-review.md).
"""

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from postmaster.database.base import Base
from postmaster.database.models import DialogStateModel, PostModel, UserModel

# Все таблицы приложения. Новая модель добавляется сюда, иначе create_all её не создаст.
TABLES = (UserModel.__table__, PostModel.__table__, DialogStateModel.__table__)


async def create_schema(engine: AsyncEngine) -> None:
    """Создаёт таблицы из TABLES и безопасно мигрирует существующую БД."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=list(TABLES))
        await connection.run_sync(_migrate_posts)


def _migrate_posts(sync_connection) -> None:
    """Добавляет колонки/индекс PM-06-R2, если БД создана до этой ревизии."""
    inspector = inspect(sync_connection)
    if "posts" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("posts")}
    if "source_draft_id" not in columns:
        sync_connection.execute(text("ALTER TABLE posts ADD COLUMN source_draft_id VARCHAR(64)"))
    if "schedule_version" not in columns:
        sync_connection.execute(
            text("ALTER TABLE posts ADD COLUMN schedule_version INTEGER NOT NULL DEFAULT 0")
        )
    # NULL-значения старых записей не конфликтуют: UNIX/PostgreSQL UNIQUE это допускает.
    sync_connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_posts_source_draft_id ON posts (source_draft_id)"
        )
    )
