"""Вспомогательные функции сквозных тестов: ожидания, состояние диалога и строки SQLite.

Используются тестами, которые запускают приложение через Long Polling и fake Telegram API.
"""

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from postmaster.app import Application

WAIT_SECONDS = 10


def db_path(tmp_path: Path) -> Path:
    """Файл SQLite, который использует приложение в тесте."""
    return tmp_path / "data" / "app.db"


async def wait_until(predicate: Callable[[], bool]) -> None:
    """Ждёт, пока условие станет истинным. Иначе выбрасывает TimeoutError."""

    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), WAIT_SECONDS)


async def current_state(app: Application, user_id: int, chat_id: int | None = None) -> str | None:
    """Состояние диалога. В личном чате chat_id совпадает с user_id."""
    return await app.bot.get_state(user_id, user_id if chat_id is None else chat_id)


async def wait_for_state(
    app: Application, user_id: int, expected: str | None, chat_id: int | None = None
) -> None:
    """Ждёт нужное состояние диалога. Иначе выбрасывает TimeoutError."""

    async def poll() -> None:
        while await current_state(app, user_id, chat_id) != expected:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), WAIT_SECONDS)


def users_rows(path: Path, telegram_user_id: int) -> list[tuple[int, str | None, str]]:
    """Строки users с этим telegram_user_id: (id, username, first_name)."""
    query = "SELECT id, username, first_name FROM users WHERE telegram_user_id = ?"
    with closing(sqlite3.connect(path)) as connection:
        return list(connection.execute(query, (telegram_user_id,)).fetchall())


def future_date_text(days: int = 2) -> str:
    """Дата в формате ДД.ММ.ГГГГ ЧЧ:ММ по UTC, на days дней вперёд.

    Приложение в тестах использует DEFAULT_TIMEZONE=UTC, поэтому такая дата всегда в будущем.
    """
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%d.%m.%Y %H:%M")
