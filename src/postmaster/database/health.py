"""Проба доступности БД для health check (SPEC-029).

``probe_database`` выполняет ``SELECT 1`` через переданный движок и
возвращает ``True`` при успехе, ``False`` при ошибке. Метод read-only:
никаких изменений в данных.

Импорт sqlalchemy разрешён в слое ``database`` (test_architecture).
"""

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine


async def probe_database(engine: AsyncEngine) -> bool:
    """Проверяет доступность БД запросом ``SELECT 1``.

    Возвращает ``True`` при успехе, ``False`` при любой ошибке
    (SQLAlchemyError, OSError, таймаут и т. д.). Текст ошибки НЕ возвращается,
    чтобы health-отчёт не утекал внутренние сообщения SQLAlchemy.
    """
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        return False
    except Exception:  # noqa: BLE001 — health probe должен быть устойчив ко всему
        return False
    return True
