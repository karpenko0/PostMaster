"""Создание таблиц при запуске приложения.

Миграции Alembic появятся вместе с SPEC-027 (решение D4 плана SPEC-002). До этого
create_all создаёт только отсутствующие таблицы и ничего не меняет в существующих.
"""

from sqlalchemy.ext.asyncio import AsyncEngine

from postmaster.database.base import Base
from postmaster.database.models import UserModel

# Все таблицы приложения. Новая модель добавляется сюда, иначе create_all её не создаст.
TABLES = (UserModel.__table__,)


async def create_schema(engine: AsyncEngine) -> None:
    """Создаёт таблицы из TABLES, если их ещё нет."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=list(TABLES))
