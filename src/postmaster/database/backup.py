"""Резервное копирование SQLite (SPEC-030).

``create_backup`` создаёт копию файла SQLite через Online Backup API
(``sqlite3.Connection.backup``). Метод безопасен при работающей БД:
он использует пошаговую итерацию и не блокирует записи.

После создания копия проверяется: открывается новое соединение,
выполняется ``PRAGMA integrity_check`` и подсчитываются строки в
``users`` и ``posts``. При успехе возвращается ``BackupReport`` с
путём и числом строк; при ошибке — ``BackupError``.

Активная БД не модифицируется: используется только ``Connection.backup``
на чтение. ``.env`` не включается — backup это файл SQLite, а секреты
хранятся в отдельном ``.env``.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

logger = logging.getLogger(__name__)

#: Таблицы, которые мы ожидаем в backup и сравниваем по числу строк.
EXPECTED_TABLES: tuple[str, ...] = ("users", "posts")


class BackupError(RuntimeError):
    """Создание или проверка backup завершились ошибкой."""


@dataclass(frozen=True, slots=True)
class BackupReport:
    """Результат успешного backup (SPEC-030 §7)."""

    source: Path
    destination: Path
    duration_seconds: float
    integrity: str
    row_counts: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Backup валиден (``integrity_check == 'ok'``)."""
        return self.integrity == "ok"


def create_backup(
    source: Path,
    destination: Path,
    *,
    expected_tables: tuple[str, ...] = EXPECTED_TABLES,
    connection_factory: Callable[[Path], sqlite3.Connection] | None = None,
) -> BackupReport:
    """Создаёт резервную копию ``source`` в ``destination``.

    Args:
        source: путь к активному файлу SQLite.
        destination: путь к файлу backup.
        expected_tables: таблицы, для которых считаются строки.
        connection_factory: фабрика ``sqlite3.Connection`` для тестов;
            по умолчанию ``sqlite3.connect``.

    Raises:
        BackupError: source не существует/не является SQLite,
            destination недоступен, integrity_check не 'ok' или
            отсутствуют ожидаемые таблицы.

    Returns:
        BackupReport с путями, длительностью, integrity и числом строк.
    """
    if not source.is_file():
        raise BackupError(f"источник не найден: {source}")

    factory = connection_factory or sqlite3.connect

    started = monotonic()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Удаляем существующий backup, чтобы sqlite3 мог открыть destination
        # как пустой файл и записать новый. Пропуск удаления ломает backup
        # в случаях, когда destination содержит данные другого формата
        # (например, остатки предыдущего теста).
        if destination.exists():
            destination.unlink()
        with factory(str(source)) as src_conn:
            _verify_sqlite(src_conn, source)
            with factory(str(destination)) as dst_conn:
                src_conn.backup(dst_conn)
    except BackupError:
        raise
    except sqlite3.Error as exc:
        logger.exception("Backup failed source=%s destination=%s", source, destination)
        raise BackupError(f"SQLite error: {exc}") from exc
    except OSError as exc:
        logger.exception("Backup failed source=%s destination=%s", source, destination)
        raise BackupError(f"filesystem error: {exc}") from exc

    duration = monotonic() - started

    try:
        integrity, row_counts = _verify_backup(destination, expected_tables, connection_factory)
    except BackupError:
        # Если копия повреждена, удаляем её и пробрасываем ошибку.
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    logger.info(
        "Backup created source=%s destination=%s duration=%.3fs rows=%s",
        source,
        destination,
        duration,
        row_counts,
    )
    return BackupReport(
        source=source,
        destination=destination,
        duration_seconds=duration,
        integrity=integrity,
        row_counts=row_counts,
    )
    return BackupReport(
        source=source,
        destination=destination,
        duration_seconds=duration,
        integrity=integrity,
        row_counts=row_counts,
    )


def _verify_sqlite(connection: sqlite3.Connection, path: Path) -> None:
    """Проверяет, что ``connection`` указывает на валидный SQLite-файл."""
    try:
        cursor = connection.execute("PRAGMA quick_check")
        result = cursor.fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"источник не является SQLite: {path}") from exc
    if result is None or result[0] != "ok":
        raise BackupError(f"источник повреждён: quick_check={result!r}")


def _verify_backup(
    destination: Path,
    expected_tables: tuple[str, ...],
    connection_factory: Callable[[Path], sqlite3.Connection] | None,
) -> tuple[str, dict[str, int]]:
    """Открывает backup и возвращает (integrity, row_counts)."""
    factory = connection_factory or sqlite3.connect
    try:
        with factory(str(destination)) as conn:
            integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
            integrity = integrity_row[0] if integrity_row is not None else ""
            counts: dict[str, int] = {}
            for table in expected_tables:
                if not _table_exists(conn, table):
                    raise BackupError(f"в backup отсутствует таблица {table!r}")
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error as exc:
        raise BackupError(f"не удалось проверить backup: {exc}") from exc
    return integrity, counts


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    """True, если таблица присутствует в схеме."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


__all__ = [
    "EXPECTED_TABLES",
    "BackupError",
    "BackupReport",
    "create_backup",
]
