"""SPEC-030: Резервное копирование SQLite.

§8 Обязательные проверки:
- backup create: копия создаётся;
- open/verify: копия открывается и проходит integrity_check;
- failed destination: недоступное назначение → контролируемая ошибка;
- data comparison: содержимое копии совпадает с исходной БД.

§7 Дополнительно:
- backup не меняет активную БД;
- backup не включает .env;
- ошибка backup логируется.
"""

import logging
import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness import db_path
from postmaster.app import backup_main
from postmaster.database.backup import (
    EXPECTED_TABLES,
    BackupError,
    BackupReport,
    create_backup,
)
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.repositories.user_repository import UserRepository

TEST_TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789"
RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


# === Фикстуры ===============================================================


@pytest.fixture
async def source_db(tmp_path: Path) -> AsyncIterator[Path]:
    """Активная БД с пользователем и публикацией."""
    file_path = db_path(tmp_path)
    engine = create_database_engine(f"sqlite+aiosqlite:///{file_path}")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user_repo = UserRepository(factory)
        await user_repo.add(
            telegram_user_id=5001,
            username="backup_user",
            first_name="Backup",
            now=RUN_AT,
        )
        async with factory() as session, session.begin():
            await session.execute(
                _sql(
                    "INSERT INTO posts "
                    "(user_id, telegram_file_id, telegram_file_unique_id, "
                    "scheduled_at_utc, timezone, status, publication_type, "
                    "target_id, error_message, created_at, updated_at, "
                    "published_at, caption) VALUES "
                    "(1, 'file_1', 'unique_1', '2026-10-20 12:00:00', 'UTC', "
                    "'PENDING', 'REMINDER', NULL, NULL, "
                    "'2026-10-20 12:00:00', '2026-10-20 12:00:00', NULL, NULL)"
                )
            )
    finally:
        await engine.dispose()
    yield file_path


def _sql(statement: str):
    import sqlalchemy

    return sqlalchemy.text(statement)


def _rows(path: Path, table: str) -> list[tuple]:
    """Читает все строки таблицы из файла SQLite."""
    with sqlite3.connect(str(path)) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def _count(path: Path, table: str) -> int:
    with sqlite3.connect(str(path)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# === backup create (SPEC-030 §8) ===========================================


def test_backup_is_created(source_db: Path, tmp_path: Path) -> None:
    """Backup создаётся по указанному пути (SPEC-030 §8)."""
    destination = tmp_path / "backups" / "postmaster-backup.db"

    report = create_backup(source_db, destination)

    assert destination.is_file()
    assert isinstance(report, BackupReport)
    assert report.destination == destination
    assert report.source == source_db
    assert report.duration_seconds >= 0


def test_backup_creates_missing_destination_directory(source_db: Path, tmp_path: Path) -> None:
    """Каталог для backup создаётся автоматически."""
    destination = tmp_path / "deep" / "nested" / "path" / "backup.db"

    create_backup(source_db, destination)

    assert destination.is_file()


def test_backup_leaves_no_temp_file(source_db: Path, tmp_path: Path) -> None:
    """После backup нет ``.tmp`` артефактов в каталоге назначения."""
    destination = tmp_path / "backup.db"

    create_backup(source_db, destination)

    assert not (tmp_path / "backup.db.tmp").exists()
    # Все WAL/SHM артефакты, если они есть, принадлежат фикстуре, не backup.
    leftover = list(tmp_path.glob("backup.db.*"))
    assert leftover == []


def test_backup_overwrites_existing_destination(source_db: Path, tmp_path: Path) -> None:
    """Повторный backup перезаписывает предыдущий файл."""
    destination = tmp_path / "backup.db"
    destination.write_bytes(b"stale garbage")

    report = create_backup(source_db, destination)

    assert report.ok is True
    assert destination.read_bytes()[:16] == b"SQLite format 3\x00"


# === open/verify (SPEC-030 §8) =============================================


def test_backup_passes_integrity_check(source_db: Path, tmp_path: Path) -> None:
    """Копия проходит PRAGMA integrity_check (SPEC-030 §1)."""
    destination = tmp_path / "backup.db"

    report = create_backup(source_db, destination)

    assert report.integrity == "ok"
    assert report.ok is True


def test_backup_is_openable_and_has_tables(source_db: Path, tmp_path: Path) -> None:
    """Копия открывается обычным sqlite3 и содержит users/posts (SPEC-030 §8)."""
    destination = tmp_path / "backup.db"

    create_backup(source_db, destination)

    with sqlite3.connect(str(destination)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {"users", "posts"} <= tables


def test_backup_report_contains_row_counts(source_db: Path, tmp_path: Path) -> None:
    """Отчёт содержит число строк в users и posts."""
    destination = tmp_path / "backup.db"

    report = create_backup(source_db, destination)

    assert set(report.row_counts) == set(EXPECTED_TABLES)
    assert report.row_counts["users"] == 1
    assert report.row_counts["posts"] == 1


def test_backup_of_empty_database_is_created(tmp_path: Path) -> None:
    """Backup пустой (но валидной) БД тоже создаётся."""
    empty = tmp_path / "empty.db"
    with sqlite3.connect(str(empty)) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY)")
    destination = tmp_path / "backup.db"

    report = create_backup(empty, destination)

    assert report.ok is True
    assert report.row_counts == {"users": 0, "posts": 0}


# === data comparison (SPEC-030 §8) =========================================


def test_backup_data_matches_source(source_db: Path, tmp_path: Path) -> None:
    """Данные в backup совпадают с исходной БД (SPEC-030 §8)."""
    destination = tmp_path / "backup.db"

    create_backup(source_db, destination)

    assert _rows(destination, "users") == _rows(source_db, "users")
    assert _rows(destination, "posts") == _rows(source_db, "posts")


def test_backup_preserves_user_and_post_contents(source_db: Path, tmp_path: Path) -> None:
    """Конкретные значения полей сохраняются без искажений."""
    destination = tmp_path / "backup.db"

    create_backup(source_db, destination)

    with sqlite3.connect(str(destination)) as conn:
        user = conn.execute("SELECT telegram_user_id, username, first_name FROM users").fetchone()
        post = conn.execute(
            "SELECT telegram_file_id, status, publication_type FROM posts"
        ).fetchone()

    assert user == (5001, "backup_user", "Backup")
    assert post == ("file_1", "PENDING", "REMINDER")


def test_backup_captures_changes_made_after_first_backup(source_db: Path, tmp_path: Path) -> None:
    """Второй backup отражает изменения, сделанные после первого."""
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    create_backup(source_db, first)

    with sqlite3.connect(str(source_db)) as conn:
        conn.execute(
            "INSERT INTO users "
            "(id, telegram_user_id, username, first_name, timezone, created_at, updated_at) "
            "VALUES (2, 5002, 'second_user', 'Second', NULL, "
            "'2026-10-21 12:00:00', '2026-10-21 12:00:00')"
        )

    create_backup(source_db, second)

    assert _count(first, "users") == 1
    assert _count(second, "users") == 2


# === Backup не меняет активную БД (SPEC-030 §7) ============================


def test_backup_does_not_change_source_database(source_db: Path, tmp_path: Path) -> None:
    """Активная БД не меняется после backup (SPEC-030 §7)."""
    destination = tmp_path / "backup.db"
    users_before = _rows(source_db, "users")
    posts_before = _rows(source_db, "posts")

    create_backup(source_db, destination)

    assert _rows(source_db, "users") == users_before
    assert _rows(source_db, "posts") == posts_before
    assert _count(source_db, "users") == 1
    assert _count(source_db, "posts") == 1


def test_backup_does_not_modify_source_mtime(source_db: Path, tmp_path: Path) -> None:
    """Backup только читает источник: время модификации файла не меняется."""
    destination = tmp_path / "backup.db"
    before = source_db.stat().st_mtime_ns

    create_backup(source_db, destination)

    assert source_db.stat().st_mtime_ns == before


def test_backup_works_with_wal_database(tmp_path: Path) -> None:
    """Backup работает с БД в режиме WAL (активная база приложения)."""
    import asyncio as _asyncio

    async def _setup() -> None:
        file_path = db_path(tmp_path)
        engine = create_database_engine(f"sqlite+aiosqlite:///{file_path}")
        try:
            await create_schema(engine)
            factory = create_session_factory(engine)
            user_repo = UserRepository(factory)
            await user_repo.add(telegram_user_id=5101, username="wal", first_name="Wal", now=RUN_AT)
        finally:
            await engine.dispose()

    _asyncio.run(_setup())
    file_path = db_path(tmp_path)
    destination = tmp_path / "backup.db"

    report = create_backup(file_path, destination)

    assert report.ok is True
    assert report.row_counts["users"] == 1


# === Backup не включает .env (SPEC-030 §7) =================================


def test_backup_does_not_include_env_file(source_db: Path, tmp_path: Path) -> None:
    """Backup — это файл SQLite; .env в него не попадает (SPEC-030 §7)."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"BOT_TOKEN={TEST_TOKEN}\n", encoding="utf-8")
    destination = tmp_path / "backup.db"

    create_backup(source_db, destination)

    assert destination.read_bytes()[:16] == b"SQLite format 3\x00"
    assert TEST_TOKEN.encode() not in destination.read_bytes()
    # В backup нет таблицы/данных из .env — только users и posts.
    with sqlite3.connect(str(destination)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert tables == {"users", "posts"}


# === failed destination (SPEC-030 §8) ======================================


def test_backup_fails_when_source_missing(tmp_path: Path) -> None:
    """Отсутствующий источник → BackupError."""
    with pytest.raises(BackupError, match="источник не найден"):
        create_backup(tmp_path / "absent.db", tmp_path / "backup.db")


def test_backup_fails_when_destination_directory_is_a_file(source_db: Path, tmp_path: Path) -> None:
    """Недоступное назначение → BackupError, а не ложный успех (SPEC-030 §8)."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    destination = blocker / "backup.db"

    with pytest.raises(BackupError):
        create_backup(source_db, destination)


def test_backup_fails_when_source_is_not_sqlite(tmp_path: Path) -> None:
    """Не-SQLite источник → BackupError."""
    source = tmp_path / "garbage.db"
    source.write_bytes(b"this is not a sqlite database at all")

    with pytest.raises(BackupError):
        create_backup(source, tmp_path / "backup.db")


def test_backup_failure_is_logged(
    source_db: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Ошибка backup логируется (SPEC-030 §7)."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="postmaster.database.backup"):
        with pytest.raises(BackupError):
            create_backup(source_db, blocker / "backup.db")

    assert any("Backup failed" in record.getMessage() for record in caplog.records)


def test_backup_failure_leaves_no_partial_file(source_db: Path, tmp_path: Path) -> None:
    """После неудачного backup не остаётся частичного файла.

    Делаем destination дочерним путём файла-blocker: mkdir/connect падают
    с FileExistsError до создания файла.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    destination = blocker / "backup.db"

    with pytest.raises(BackupError):
        create_backup(source_db, destination)

    assert not destination.exists()


# === CLI: python -m postmaster backup ======================================


def _run_backup_cli(
    env: dict[str, str], tmp_path: Path, args: list[str] | None = None
) -> subprocess.CompletedProcess:
    full_env = {
        "PATH": "/usr/bin:/bin",
        "SystemRoot": "C:\\Windows",
        "TEMP": str(tmp_path),
        "HOME": str(tmp_path),
        "PYTHONIOENCODING": "utf-8",
        **env,
    }
    return subprocess.run(
        [sys.executable, "-m", "postmaster", "backup", *(args or [])],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


def test_backup_cli_creates_backup(source_db: Path, tmp_path: Path) -> None:
    """CLI создаёт backup и печатает результат проверки."""
    destination = tmp_path / "cli-backup.db"
    env = {
        "BOT_TOKEN": TEST_TOKEN,
        "DATABASE_URL": f"sqlite+aiosqlite:///{source_db}",
        "BACKUP_PATH": str(destination),
    }

    result = _run_backup_cli(env, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert destination.is_file()
    assert "backup создан" in result.stdout
    assert "ok" in result.stdout


def test_backup_cli_reports_failure(source_db: Path, tmp_path: Path) -> None:
    """CLI возвращает 1 при недоступном назначении."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    env = {
        "BOT_TOKEN": TEST_TOKEN,
        "DATABASE_URL": f"sqlite+aiosqlite:///{source_db}",
        "BACKUP_PATH": str(blocker / "backup.db"),
    }

    result = _run_backup_cli(env, tmp_path)

    assert result.returncode == 1
    assert "Ошибка backup" in (result.stdout + result.stderr)


def test_backup_cli_rejects_non_sqlite_url(tmp_path: Path) -> None:
    """Для не-SQLite DATABASE_URL CLI сообщает, что backup неприменим."""
    env = {
        "BOT_TOKEN": TEST_TOKEN,
        "DATABASE_URL": "postgresql+asyncpg://user:pass@host/db",
        "BACKUP_PATH": str(tmp_path / "backup.db"),
    }

    result = _run_backup_cli(env, tmp_path)

    assert result.returncode == 1
    assert "только для SQLite" in (result.stdout + result.stderr)


def test_backup_cli_returns_2_on_missing_token(tmp_path: Path) -> None:
    """Без BOT_TOKEN → код 2."""
    env = {"BACKUP_PATH": str(tmp_path / "backup.db")}

    result = _run_backup_cli(env, tmp_path)

    assert result.returncode == 2
    assert "BOT_TOKEN" in (result.stdout + result.stderr)


def test_backup_cli_does_not_print_token(tmp_path: Path, source_db: Path) -> None:
    """Токен не попадает в вывод CLI."""
    env = {
        "BOT_TOKEN": TEST_TOKEN,
        "DATABASE_URL": f"sqlite+aiosqlite:///{source_db}",
        "BACKUP_PATH": str(tmp_path / "backup.db"),
    }

    result = _run_backup_cli(env, tmp_path)

    assert TEST_TOKEN not in (result.stdout + result.stderr)


def test_backup_main_in_process_returns_0(
    source_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """backup_main() в процессе возвращает 0 при успешном backup."""
    destination = tmp_path / "in-process.db"
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{source_db}")
    monkeypatch.setenv("BACKUP_PATH", str(destination))

    code = backup_main([])

    assert code == 0
    assert destination.is_file()
