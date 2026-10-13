"""Движок и сессии SQLAlchemy (SPEC-027).

MVP работает с SQLite через aiosqlite. Строка подключения задаётся в DATABASE_URL,
поэтому переход на PostgreSQL не требует изменений в repositories и services.
"""

from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Время ожидания блокировки SQLite, мс. Нужно при параллельной работе polling и Scheduler.
SQLITE_BUSY_TIMEOUT_MS = 5000


def create_database_engine(database_url: str) -> AsyncEngine:
    """Создаёт асинхронный движок. Для SQLite создаёт каталог файла при необходимости."""
    url = make_url(database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    if is_sqlite:
        _ensure_sqlite_directory(url)

    engine = create_async_engine(url, pool_pre_ping=True)
    if is_sqlite:
        _configure_sqlite(engine)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий. expire_on_commit выключен: объекты читаемы после commit."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def check_database(engine: AsyncEngine) -> None:
    """Проверяет доступность БД запросом SELECT 1. При недоступности выбрасывает исключение."""
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


def _ensure_sqlite_directory(url: URL) -> None:
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return
    Path(database).parent.mkdir(parents=True, exist_ok=True)


def _configure_sqlite(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            # WAL позволяет читать БД во время записи. Нужно для параллельных задач.
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()
