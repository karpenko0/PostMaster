"""Подключение к SQLite: каталог, WAL, busy_timeout, внешние ключи, доступность (SPEC-027)."""

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from postmaster.database.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    check_database,
    create_database_engine,
    create_session_factory,
)


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def test_sqlite_directory_is_created_and_pragmas_are_applied(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "postmaster.db"
    engine = create_database_engine(_sqlite_url(db_path))
    try:
        assert db_path.parent.is_dir()

        await check_database(engine)
        async with engine.connect() as connection:
            journal_mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar()
            busy_timeout = (await connection.execute(text("PRAGMA busy_timeout"))).scalar()
            foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar()

        assert str(journal_mode).lower() == "wal"
        assert busy_timeout == SQLITE_BUSY_TIMEOUT_MS
        assert foreign_keys == 1
    finally:
        await engine.dispose()


async def test_pragmas_are_applied_to_every_pooled_connection(tmp_path: Path) -> None:
    engine = create_database_engine(_sqlite_url(tmp_path / "pool.db"))
    try:
        connections = [await engine.connect() for _ in range(2)]
        try:
            for connection in connections:
                foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar()
                assert foreign_keys == 1
        finally:
            for connection in connections:
                await connection.close()
    finally:
        await engine.dispose()


async def test_check_database_raises_when_database_is_unavailable(tmp_path: Path) -> None:
    # Путь к существующему каталогу нельзя открыть как файл базы данных.
    engine = create_database_engine(_sqlite_url(tmp_path))
    try:
        with pytest.raises(OperationalError):
            await check_database(engine)
    finally:
        await engine.dispose()


async def test_session_factory_keeps_objects_readable_after_commit(tmp_path: Path) -> None:
    engine = create_database_engine(_sqlite_url(tmp_path / "session.db"))
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            assert session.sync_session.expire_on_commit is False
    finally:
        await engine.dispose()
