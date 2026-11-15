"""Создание таблиц и миграции при запуске приложения (SPEC-002, PM-06).

Исторически схема создавалась только ``create_all``: новые таблицы появлялись, но новые
колонки в уже существующих таблицах — нет. PM-06 добавляет версионированные миграции
(см. ``migrations.py``): ``create_all`` создаёт отсутствующие таблицы, после чего миграции
доводят существующие до актуальной версии. Оба шага идемпотентны.
"""

from sqlalchemy.ext.asyncio import AsyncEngine

from postmaster.database.base import Base
from postmaster.database.migrations import apply_migrations
from postmaster.database.models import DialogStateModel, PostModel, UserModel

# Все таблицы приложения. Новая модель добавляется сюда, иначе create_all её не создаст.
TABLES = (UserModel.__table__, PostModel.__table__, DialogStateModel.__table__)


async def create_schema(engine: AsyncEngine) -> None:
    """Создаёт отсутствующие таблицы и применяет миграции к существующим."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=list(TABLES))
    await apply_migrations(engine)
