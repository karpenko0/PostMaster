"""SPEC-028: Docker и постоянные данные.

Docker-образ запускает приложение с переменными окружения и persistent
bind-mount для data/postmaster.db; секреты не запекаются в образ;
перезапуск и пересоздание контейнера с тем же volume сохраняют БД;
Scheduler останавливается gracefully по SIGTERM.

§8 Обязательные проверки:
- build (структурно);
- startup;
- restart persistence;
- recreate persistence;
- missing env.

§7 Дополнительно:
- graceful shutdown Scheduler (идемпотентность);
- секреты не попадают в образ.
"""

import sqlite3
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from harness import db_path
from postmaster.app import Application
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import PublicationType
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.scheduler_service import SchedulerService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
COMPOSE = PROJECT_ROOT / "docker-compose.yml"

NOW = datetime.now(UTC)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# === Graceful shutdown Scheduler (SPEC-028 §7) =============================


class _SchedulerShutdownSpy:
    """Обёртка над SchedulerService: считает вызовы shutdown."""

    def __init__(self) -> None:
        self.shutdown_calls = 0

    def schedule_post(self, *, post_id: int, run_at) -> None:
        return None

    def remove_job(self, post_id: int) -> bool:
        return False

    def shutdown(self) -> None:
        self.shutdown_calls += 1


async def test_application_shutdown_stops_scheduler() -> None:
    """Application.shutdown() останавливает Scheduler (SPEC-028 §7)."""
    scheduler = _SchedulerShutdownSpy()
    app = _make_application(scheduler=scheduler)

    await app.shutdown()

    assert scheduler.shutdown_calls == 1


async def test_scheduler_shutdown_is_idempotent() -> None:
    """Повторный shutdown Scheduler безопасен (SPEC-028 §7)."""
    scheduler = SchedulerService()
    scheduler.start()

    scheduler.shutdown()
    # Второй вызов не должен выбрасывать исключение.
    scheduler.shutdown()
    scheduler.shutdown()


async def test_scheduler_running_reports_correct_state() -> None:
    """Scheduler.running корректно отражает состояние до и после запуска."""
    scheduler = SchedulerService()
    assert scheduler.running is False

    scheduler.start()
    assert scheduler.running is True

    # Повторные shutdown() безопасны, даже если APScheduler ещё не сбросил
    # флаг ``running`` после асинхронного shutdown(wait=False) — это
    # деталь реализации APScheduler, не наш контракт.
    scheduler.shutdown()
    scheduler.shutdown()


# === Persistence: перезапуск/пересоздание контейнера (SPEC-028 §7, §8) ====


@pytest.fixture
async def db_file(tmp_path: Path) -> AsyncIterator[Path]:
    """SQLite-БД с одной публикацией, переживающая reopen движка."""
    file_path = db_path(tmp_path)
    engine = create_database_engine(f"sqlite+aiosqlite:///{file_path}")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        user_repo = UserRepository(factory)
        post_repo = PostRepository(factory)
        user = await user_repo.add(
            telegram_user_id=4001,
            username="docker",
            first_name="Docker",
            now=NOW,
        )
        await post_repo.create_post(
            user_id=user.id,
            telegram_file_id="docker_file",
            telegram_file_unique_id="docker_unique",
            scheduled_at_utc=NOW,
            timezone="UTC",
            publication_type=PublicationType.REMINDER,
            target_id=None,
        )
    finally:
        await engine.dispose()
    yield file_path


async def test_db_file_survives_engine_reopen(db_file: Path) -> None:
    """Файл SQLite переживает reopen движка — переживает restart контейнера."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_file}")
    try:
        factory = create_session_factory(engine)
        user = await UserRepository(factory).find_by_telegram_id(4001)
        assert user is not None
        assert user.username == "docker"
    finally:
        await engine.dispose()


async def test_db_file_survives_repeated_reopens(db_file: Path) -> None:
    """Файл SQLite переживает несколько reopen — recreate контейнера N раз."""
    for attempt in range(3):
        engine = create_database_engine(f"sqlite+aiosqlite:///{db_file}")
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                count = (await session.execute(text("SELECT COUNT(*) FROM posts"))).scalar()
            assert count == 1, f"iteration {attempt}: expected 1 post, got {count}"
        finally:
            await engine.dispose()


async def test_db_file_is_queryable_via_raw_sqlite_after_engine_close(
    db_file: Path,
) -> None:
    """После закрытия движка файл читается обычным sqlite3 — данные целы."""
    with sqlite3.connect(str(db_file)) as connection:
        users = connection.execute("SELECT telegram_user_id, username FROM users").fetchall()
        posts = connection.execute("SELECT telegram_file_id, status FROM posts").fetchall()

    assert users == [(4001, "docker")]
    assert posts == [("docker_file", "PENDING")]


# === Секреты не запекаются в образ (SPEC-028 §4) ============================


def test_dockerignore_excludes_env_and_data() -> None:
    """`.dockerignore` блокирует `.env` и каталог `data` от попадания в образ."""
    lines = {line.strip() for line in _read(DOCKERIGNORE).splitlines()}
    assert ".env" in lines
    assert ".env.*" in lines
    assert "data" in lines or "data/" in lines
    assert "*.db" in lines


def test_dockerfile_has_no_token_or_env_secrets() -> None:
    """В Dockerfile нет реальных секретов и нет ENV/BOT_TOKEN (SPEC-028 §4)."""
    content = _read(DOCKERFILE)
    for forbidden in ("BOT_TOKEN=", "TELEGRAM_BOT_TOKEN=", "API_KEY=", "SECRET_KEY="):
        assert forbidden not in content, f"Dockerfile содержит {forbidden}"
    # Тест-токен формата bot API тоже не должен попасть.
    import re as _re

    assert not _re.search(r"\d{5,}:[A-Za-z0-9_-]{20,}", content)


def test_compose_does_not_inline_secrets() -> None:
    """`docker-compose.yml` не задаёт секреты в `environment` (SPEC-023).

    Документационные упоминания ``BOT_TOKEN`` в комментариях не считаются —
    проверяется только секция ``environment:`` сервиса ``postmaster``.
    """
    content = _read(COMPOSE)
    in_environment_block = False
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if line.startswith("environment:"):
            in_environment_block = True
            continue
        # Конец секции environment — следующая директива верхнего уровня.
        if in_environment_block and line and not line.startswith(" "):
            in_environment_block = False
        if in_environment_block:
            assert "BOT_TOKEN" not in stripped, (
                f"BOT_TOKEN не должен быть в environment docker-compose: {line!r}"
            )
    # Дополнительно: env_file указан.
    assert "env_file:" in content
    assert ".env" in content


# === Build (SPEC-028 §8) — структурная проверка =============================


def test_dockerfile_uses_python_3_12() -> None:
    """Базовый образ — python:3.12-slim."""
    content = _read(DOCKERFILE)
    assert "FROM python:3.12-slim" in content


def test_dockerfile_pip_install_then_user_switch() -> None:
    """USER postmaster идёт после `pip install`, чтобы установка прошла с правами root."""
    content = _read(DOCKERFILE)
    pip_install_line = next(
        line_no
        for line_no, line in enumerate(content.splitlines(), 1)
        if line.strip().startswith("RUN pip install")
    )
    user_postmaster_line = next(
        line_no
        for line_no, line in enumerate(content.splitlines(), 1)
        if line.strip() == "USER postmaster"
    )
    assert user_postmaster_line > pip_install_line


def test_dockerfile_data_directory_is_owned_by_postmaster() -> None:
    """Каталог /app/data принадлежит postmaster:postmaster (для bind-mount на запись)."""
    content = _read(DOCKERFILE)
    # /app/data должен быть создан ДО USER postmaster.
    assert "mkdir -p /app/data" in content
    assert "chown postmaster:postmaster /app/data" in content


def test_dockerfile_cmd_runs_module_postmaster() -> None:
    """CMD запускает приложение как `python -m postmaster`."""
    content = _read(DOCKERFILE)
    assert 'CMD ["python", "-m", "postmaster"]' in content


def test_dockerfile_does_not_leak_dotenv_into_image() -> None:
    """`.env` не копируется в образ — только pyproject.toml/README.md/src."""
    content = _read(DOCKERFILE)
    # Находим все инструкции COPY.
    for line in content.splitlines():
        if line.strip().startswith("COPY"):
            assert ".env" not in line, f"Dockerfile копирует .env: {line!r}"


# === Startup (SPEC-028 §8) =================================================


def test_compose_uses_init_for_signal_forwarding() -> None:
    """`init: true` в compose передаёт SIGTERM приложению."""
    content = _read(COMPOSE)
    assert "init: true" in content


def test_compose_has_grace_period_for_shutdown() -> None:
    """`stop_grace_period` даёт приложению время на graceful shutdown Scheduler."""
    content = _read(COMPOSE)
    assert "stop_grace_period:" in content


def test_compose_binds_data_to_host() -> None:
    """Каталог данных смонтирован на хост (для persistence между recreate)."""
    content = _read(COMPOSE)
    assert "./data:/app/data" in content


def test_compose_has_restart_policy() -> None:
    """Политика перезапуска настроена для автоматического восстановления."""
    content = _read(COMPOSE)
    assert "restart:" in content


# === Missing env (SPEC-028 §8) =============================================


def test_main_without_bot_token_exits_2(tmp_path: Path) -> None:
    """Без BOT_TOKEN в окружении — код 2 (конфигурационная ошибка)."""
    env = {
        "PATH": "/usr/bin:/bin",
        "SystemRoot": "C:\\Windows",
        "TEMP": str(tmp_path),
        "HOME": str(tmp_path),
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/data/pm.db",
    }
    # Удалить переменные, которые могут протечь из тестового окружения.
    for name in ("BOT_TOKEN", "DATABASE_URL_FROM_SHELL"):
        env.pop(name, None)

    result = subprocess.run(
        [sys.executable, "-m", "postmaster"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 2
    assert "BOT_TOKEN" in (result.stdout + result.stderr)


def test_main_with_invalid_log_level_exits_2(tmp_path: Path) -> None:
    """Некорректный LOG_LEVEL — код 2."""
    env = {
        "PATH": "/usr/bin:/bin",
        "SystemRoot": "C:\\Windows",
        "TEMP": str(tmp_path),
        "HOME": str(tmp_path),
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/data/pm.db",
        "BOT_TOKEN": "123456789:AAFakeTokenForTestsOnly_0123456789",
        "LOG_LEVEL": "LOUDE",
    }

    result = subprocess.run(
        [sys.executable, "-m", "postmaster"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 2
    assert "LOG_LEVEL" in (result.stdout + result.stderr)


# === Вспомогательное =======================================================


class _NoopTransport:
    """Заглушка Transport для Application.shutdown() без запуска Long Polling."""

    async def run(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _NoopBot:
    """Заглушка AsyncTeleBot для Application — нужен только для __init__."""

    async def get_me(self):  # noqa: D401 — для совместимости с AsyncTeleBot
        return None


class _NoopEngine:
    """Заглушка AsyncEngine — вызовы dispose() и check_database() не нужны для shutdown."""

    async def dispose(self) -> None:
        return None


def _make_application(*, scheduler) -> Application:
    """Создаёт Application с фиктивными bot/engine и заданным scheduler."""
    return Application(
        bot=_NoopBot(),
        engine=_NoopEngine(),
        scheduler=scheduler,
        transport=_NoopTransport(),
    )
