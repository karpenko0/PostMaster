"""SPEC-029: Health Check.

§8 Обязательные проверки:
- healthy: процесс жив, БД доступна, Scheduler запущен;
- DB down: БД недоступна → unhealthy;
- scheduler down: Scheduler остановлен → unhealthy;
- no secrets: ответ не содержит токена, паролей и пользовательских данных.

§7 Приёмка:
- DB или Scheduler failure → unhealthy;
- check read-only (не изменяет данные в БД).
"""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness import db_path
from postmaster.app import health_main
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.health import probe_database
from postmaster.database.schema import create_schema
from postmaster.services.health_service import HealthCheck, HealthReporter
from postmaster.services.scheduler_service import SchedulerService
from postmaster.utils.health import (
    CHECK_DATABASE,
    CHECK_PROCESS,
    CHECK_SCHEDULER,
    CheckResult,
    HealthReport,
    HealthReportCorrupted,
    HealthReportStale,
    read_report,
    write_report,
)

TEST_TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789"
SAMPLE_DB_PASSWORD = "postgresql+asyncpg://alice:Sup3rSecret@db.local/x"


# === Чистые структуры данных (utils.health) ===============================


def test_check_result_to_dict_omits_detail_when_none() -> None:
    """CheckResult.to_dict не добавляет detail, если поле None."""
    result = CheckResult(name="process", ok=True)
    assert result.to_dict() == {"name": "process", "ok": True}


def test_check_result_to_dict_includes_detail_when_set() -> None:
    result = CheckResult(name="database", ok=False, detail="OperationalError")
    assert result.to_dict() == {"name": "database", "ok": False, "detail": "OperationalError"}


def test_health_report_healthy_requires_all_checks_ok() -> None:
    """healthy=True только если все CheckResult.ok=True."""
    all_ok = HealthReport(
        healthy=True,
        checks=(
            CheckResult(name="process", ok=True),
            CheckResult(name="database", ok=True),
            CheckResult(name="scheduler", ok=True),
        ),
        generated_at=datetime.now(UTC),
    )
    assert all_ok.healthy is True

    one_fail = HealthReport(
        healthy=False,
        checks=(
            CheckResult(name="process", ok=True),
            CheckResult(name="database", ok=False),
            CheckResult(name="scheduler", ok=True),
        ),
        generated_at=datetime.now(UTC),
    )
    assert one_fail.healthy is False


def test_health_report_to_json_is_deterministic() -> None:
    """JSON-сериализация детерминирована (sort_keys) — удобно для диффов."""
    report = HealthReport(
        healthy=False,
        checks=(CheckResult(name="scheduler", ok=False, detail="not_running"),),
        generated_at=datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC),
    )
    text = report.to_json()
    assert json.loads(text)["healthy"] is False
    # Порядок ключей в JSON отсортирован.
    assert text.find('"checks"') < text.find('"generated_at"') < text.find('"healthy"')


def test_health_report_summary_does_not_contain_secrets() -> None:
    """summary() не содержит значений секретов (SPEC-029 §1)."""
    report = HealthReport(
        healthy=False,
        checks=(
            CheckResult(name="process", ok=True),
            CheckResult(name="database", ok=False, detail="OperationalError"),
        ),
        generated_at=datetime.now(UTC),
    )
    summary = report.summary()
    assert TEST_TOKEN not in summary
    assert "Sup3rSecret" not in summary


# === write_report / read_report ===========================================


def test_write_report_is_atomic(tmp_path: Path) -> None:
    """write_report создаёт файл через temp + os.replace."""
    target = tmp_path / "nested" / "health.json"
    report = HealthReport(
        healthy=True,
        checks=(CheckResult(name="process", ok=True),),
        generated_at=datetime.now(UTC),
    )

    write_report(target, report)

    assert target.is_file()
    # Временный файл не остаётся.
    assert not (target.with_suffix(target.suffix + ".tmp")).exists()


def test_read_report_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_report(tmp_path / "absent.json") is None


def test_read_report_raises_corrupted_on_invalid_json(tmp_path: Path) -> None:
    target = tmp_path / "health.json"
    target.write_text("not json {", encoding="utf-8")
    with pytest.raises(HealthReportCorrupted):
        read_report(target)


def test_read_report_raises_stale_when_too_old(tmp_path: Path) -> None:
    target = tmp_path / "health.json"
    report = HealthReport(
        healthy=True,
        checks=(CheckResult(name="process", ok=True),),
        generated_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    write_report(target, report)

    with pytest.raises(HealthReportStale):
        read_report(target, max_age_seconds=1.0)


def test_read_report_roundtrip(tmp_path: Path) -> None:
    """write_report → read_report возвращает эквивалентный отчёт."""
    target = tmp_path / "health.json"
    original = HealthReport(
        healthy=False,
        checks=(
            CheckResult(name="process", ok=True),
            CheckResult(name="database", ok=False, detail="OperationalError"),
            CheckResult(name="scheduler", ok=False, detail="not_running"),
        ),
        generated_at=datetime.now(UTC),
    )
    write_report(target, original)

    loaded = read_report(target)

    assert loaded is not None
    assert loaded.healthy is False
    assert loaded.checks == original.checks
    assert loaded.generated_at == original.generated_at


# === probe_database (database.health) =====================================


async def test_probe_database_returns_true_for_accessible_db(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        assert await probe_database(engine) is True
    finally:
        await engine.dispose()


async def test_probe_database_returns_false_when_unavailable(tmp_path: Path) -> None:
    """БД недоступна → False (SPEC-029 §7 DB down)."""
    # Путь к каталогу нельзя открыть как файл базы данных.
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}")
    try:
        assert await probe_database(engine) is False
    finally:
        await engine.dispose()


# === HealthCheck (services.health_service) ================================


class _FakeScheduler:
    def __init__(self, *, running: bool) -> None:
        self._running = running

    @property
    def running(self) -> bool:
        return self._running


async def test_health_check_is_healthy_when_all_ok() -> None:
    """Healthy: process + database + scheduler (SPEC-029 §8)."""
    check = HealthCheck(
        database_probe=lambda: _async_return(True),
        scheduler_status=_FakeScheduler(running=True),
    )

    report = await check.check()

    assert report.healthy is True
    names = [c.name for c in report.checks]
    assert names == [CHECK_PROCESS, CHECK_DATABASE, CHECK_SCHEDULER]


async def test_health_check_is_unhealthy_when_database_down() -> None:
    """DB down → unhealthy (SPEC-029 §7)."""
    check = HealthCheck(
        database_probe=lambda: _async_return(False),
        scheduler_status=_FakeScheduler(running=True),
    )

    report = await check.check()

    assert report.healthy is False
    by_name = {c.name: c for c in report.checks}
    assert by_name[CHECK_DATABASE].ok is False
    assert by_name[CHECK_SCHEDULER].ok is True
    assert by_name[CHECK_PROCESS].ok is True


async def test_health_check_is_unhealthy_when_scheduler_down() -> None:
    """Scheduler down → unhealthy (SPEC-029 §7)."""
    check = HealthCheck(
        database_probe=lambda: _async_return(True),
        scheduler_status=_FakeScheduler(running=False),
    )

    report = await check.check()

    assert report.healthy is False
    by_name = {c.name: c for c in report.checks}
    assert by_name[CHECK_SCHEDULER].ok is False
    assert by_name[CHECK_SCHEDULER].detail == "not_running"


async def test_health_check_is_unhealthy_when_both_fail() -> None:
    """Оба отказа → unhealthy, в отчёте обе ошибки."""
    check = HealthCheck(
        database_probe=lambda: _async_return(False),
        scheduler_status=_FakeScheduler(running=False),
    )

    report = await check.check()

    assert report.healthy is False
    assert sum(1 for c in report.checks if not c.ok) == 2


async def test_health_check_swallows_probe_exceptions() -> None:
    """Исключение из database_probe не разрушает health check."""

    async def _boom() -> bool:
        raise RuntimeError("boom")

    check = HealthCheck(
        database_probe=_boom,
        scheduler_status=_FakeScheduler(running=True),
    )

    report = await check.check()

    assert report.healthy is False
    by_name = {c.name: c for c in report.checks}
    assert by_name[CHECK_DATABASE].ok is False


async def test_health_check_report_contains_no_secrets() -> None:
    """В отчёте нет токена, пароля БД, user_id/telegram_id (SPEC-029 §1)."""

    async def _leaking_probe() -> bool:
        raise RuntimeError(f"{TEST_TOKEN} {SAMPLE_DB_PASSWORD}")

    check = HealthCheck(
        database_probe=_leaking_probe,
        scheduler_status=_FakeScheduler(running=True),
    )

    report = await check.check()
    serialized = report.to_json()

    assert TEST_TOKEN not in serialized
    assert "Sup3rSecret" not in serialized
    assert "alice:Sup3rSecret" not in serialized


# === HealthReporter — read-only (SPEC-029 §7) ============================


async def test_reporter_writes_file_immediately(tmp_path: Path) -> None:
    """HealthReporter при start пишет отчёт в файл."""
    target = tmp_path / "health.json"
    check = HealthCheck(
        database_probe=lambda: _async_return(True),
        scheduler_status=_FakeScheduler(running=True),
    )
    reporter = HealthReporter(health_check=check, target=target, interval_seconds=3600)

    await reporter.start()
    try:
        # Даём фоновой задаче шанс выполнить первую запись.
        await _wait_for_file(target, timeout=2.0)
        assert target.is_file()

        loaded = read_report(target)
        assert loaded is not None
        assert loaded.healthy is True
    finally:
        await reporter.stop()


async def test_reporter_check_is_read_only(tmp_path: Path) -> None:
    """Health check не изменяет данные в БД (SPEC-029 §7)."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)
        import sqlalchemy as _sa

        async with factory() as session:
            await session.execute(
                _sa.text(
                    "INSERT INTO users "
                    "(id, telegram_user_id, username, first_name, timezone, "
                    "created_at, updated_at) VALUES "
                    "(1, 1, 'alice', 'Alice', NULL, "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )

        async with factory() as session:
            count_before = (await session.execute(_sa.text("SELECT COUNT(*) FROM users"))).scalar()

        check = HealthCheck(
            database_probe=lambda: probe_database(engine),
            scheduler_status=_FakeScheduler(running=True),
        )
        reporter = HealthReporter(
            health_check=check,
            target=tmp_path / "health.json",
            interval_seconds=3600,
        )
        await reporter.start()
        try:
            for _ in range(5):
                await reporter.report_now()
        finally:
            await reporter.stop()

        async with factory() as session:
            count_after = (await session.execute(_sa.text("SELECT COUNT(*) FROM users"))).scalar()

        assert count_after == count_before
    finally:
        await engine.dispose()


async def test_reporter_stop_is_idempotent() -> None:
    """Повторные start/stop не падают (контракт на жизненный цикл)."""
    check = HealthCheck(
        database_probe=lambda: _async_return(True),
        scheduler_status=_FakeScheduler(running=True),
    )
    reporter = HealthReporter(
        health_check=check,
        target=Path("/tmp/health-spec029.json"),
        interval_seconds=3600,
    )

    await reporter.start()
    await reporter.stop()
    await reporter.stop()  # повторный stop безопасен
    await reporter.start()  # повторный start работает


# === HealthReporter + реальная SchedulerService ============================


async def test_reporter_with_real_scheduler(tmp_path: Path) -> None:
    """HealthReporter интегрируется с реальным SchedulerService."""
    target = tmp_path / "health.json"
    scheduler = SchedulerService()
    check = HealthCheck(
        database_probe=lambda: _async_return(True),
        scheduler_status=scheduler,
    )
    reporter = HealthReporter(health_check=check, target=target, interval_seconds=3600)

    # Scheduler ещё не запущен → unhealthy.
    before = await reporter.report_now()
    assert before.healthy is False
    scheduler_check = next(c for c in before.checks if c.name == CHECK_SCHEDULER)
    assert scheduler_check.ok is False

    # После запуска Scheduler → healthy.
    scheduler.start()
    try:
        after = await reporter.report_now()
        assert after.healthy is True
    finally:
        scheduler.shutdown()


# === CLI: python -m postmaster health =====================================


def _write_env(tmp_path: Path, **values) -> Path:
    env_file = tmp_path / ".env"
    lines = [f"{key}={value}" for key, value in values.items()]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_file


def _run_health_cli(
    env: dict[str, str], tmp_path: Path, args: list[str] | None = None
) -> subprocess.CompletedProcess:
    """Запускает ``python -m postmaster health`` в подпроцессе."""
    full_env = {
        "PATH": "/usr/bin:/bin",
        "SystemRoot": "C:\\Windows",
        "TEMP": str(tmp_path),
        "HOME": str(tmp_path),
        "PYTHONIOENCODING": "utf-8",
        **env,
    }
    return subprocess.run(
        [sys.executable, "-m", "postmaster", "health", *(args or [])],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


def test_health_cli_reports_missing_file_as_unhealthy(tmp_path: Path) -> None:
    """Файл отчёта отсутствует → exit code 1 (приложение не запущено)."""
    _write_env(tmp_path, BOT_TOKEN=TEST_TOKEN)
    env = {"BOT_TOKEN": TEST_TOKEN, "HEALTH_FILE": str(tmp_path / "absent.json")}

    result = _run_health_cli(env, tmp_path)

    assert result.returncode == 1
    assert "не найден" in (result.stdout + result.stderr)


def test_health_cli_reports_stale_as_unhealthy(tmp_path: Path) -> None:
    """Устаревший отчёт → exit code 1."""
    target = tmp_path / "health.json"
    write_report(
        target,
        HealthReport(
            healthy=True,
            checks=(CheckResult(name="process", ok=True),),
            generated_at=datetime(2020, 1, 1, tzinfo=UTC),
        ),
    )
    env = {
        "BOT_TOKEN": TEST_TOKEN,
        "HEALTH_FILE": str(target),
        "HEALTH_MAX_AGE_SECONDS": "1",
    }

    result = _run_health_cli(env, tmp_path)

    assert result.returncode == 1
    assert "устарел" in (result.stdout + result.stderr)


def test_health_cli_reports_healthy(tmp_path: Path) -> None:
    """Актуальный отчёт → exit code 0."""
    target = tmp_path / "health.json"
    write_report(
        target,
        HealthReport(
            healthy=True,
            checks=(
                CheckResult(name="process", ok=True),
                CheckResult(name="database", ok=True),
                CheckResult(name="scheduler", ok=True),
            ),
            generated_at=datetime.now(UTC),
        ),
    )
    env = {"BOT_TOKEN": TEST_TOKEN, "HEALTH_FILE": str(target)}

    result = _run_health_cli(env, tmp_path)

    assert result.returncode == 0
    assert "healthy" in (result.stdout + result.stderr)


def test_health_cli_reports_unhealthy_when_scheduler_down(tmp_path: Path) -> None:
    """Scheduler down в отчёте → exit code 1."""
    target = tmp_path / "health.json"
    write_report(
        target,
        HealthReport(
            healthy=False,
            checks=(
                CheckResult(name="process", ok=True),
                CheckResult(name="database", ok=True),
                CheckResult(name="scheduler", ok=False, detail="not_running"),
            ),
            generated_at=datetime.now(UTC),
        ),
    )
    env = {"BOT_TOKEN": TEST_TOKEN, "HEALTH_FILE": str(target)}

    result = _run_health_cli(env, tmp_path)

    assert result.returncode == 1


def test_health_cli_reports_corrupted_as_unhealthy(tmp_path: Path) -> None:
    """Повреждённый JSON → exit code 1."""
    target = tmp_path / "health.json"
    target.write_text("not a json", encoding="utf-8")
    env = {"BOT_TOKEN": TEST_TOKEN, "HEALTH_FILE": str(target)}

    result = _run_health_cli(env, tmp_path)

    assert result.returncode == 1
    assert "corrupted" in (result.stdout + result.stderr)


def test_health_cli_does_not_echo_token(tmp_path: Path) -> None:
    """Токен не попадает в вывод CLI даже при ошибках."""
    env = {
        "BOT_TOKEN": TEST_TOKEN,
        "HEALTH_FILE": str(tmp_path / "absent.json"),
    }

    result = _run_health_cli(env, tmp_path)

    assert TEST_TOKEN not in (result.stdout + result.stderr)


def test_health_cli_uses_dotenv_for_token(tmp_path: Path) -> None:
    """BOT_TOKEN читается из .env, если не задан в окружении."""
    (tmp_path / ".env").write_text(
        f"BOT_TOKEN={TEST_TOKEN}\nHEALTH_FILE=/missing.json\n", encoding="utf-8"
    )
    env = {"HOME": str(tmp_path), "TEMP": str(tmp_path)}

    result = _run_health_cli(env, tmp_path)

    assert result.returncode == 1
    # Значение токена не попадает в вывод; имя поля может встречаться
    # только в сообщениях об ошибках конфигурации, которые здесь не выводятся.
    assert TEST_TOKEN not in (result.stdout + result.stderr)
    assert "BOT_TOKEN=" not in (result.stdout + result.stderr)


def test_health_cli_returns_2_on_missing_token(tmp_path: Path) -> None:
    """Без BOT_TOKEN → exit code 2 (конфигурационная ошибка)."""
    env = {"HEALTH_FILE": str(tmp_path / "absent.json")}

    result = _run_health_cli(env, tmp_path)

    assert result.returncode == 2
    assert "BOT_TOKEN" in (result.stdout + result.stderr)


# === health_main в процессе (тот же модуль, что и __main__) ================


def test_health_main_in_process_returns_1_when_missing(tmp_path: Path, monkeypatch) -> None:
    """health_main() возвращает 1 при отсутствии отчёта."""
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("HEALTH_FILE", str(tmp_path / "absent.json"))
    code = health_main([])
    assert code == 1


def test_health_main_in_process_returns_0_when_healthy(tmp_path: Path, monkeypatch) -> None:
    """health_main() возвращает 0 при healthy отчёте."""
    target = tmp_path / "health.json"
    write_report(
        target,
        HealthReport(
            healthy=True,
            checks=(
                CheckResult(name="process", ok=True),
                CheckResult(name="database", ok=True),
                CheckResult(name="scheduler", ok=True),
            ),
            generated_at=datetime.now(UTC),
        ),
    )
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("HEALTH_FILE", str(target))
    code = health_main([])
    assert code == 0


# === Защита секретов в транспортном формате ================================


def test_report_json_does_not_leak_token_or_credentials() -> None:
    """JSON-отчёт не содержит ни токена, ни пароля БД, ни user_id (SPEC-029 §1)."""

    async def _probe() -> bool:
        raise RuntimeError(f"connect failed token={TEST_TOKEN} url={SAMPLE_DB_PASSWORD}")

    check = HealthCheck(
        database_probe=_probe,
        scheduler_status=_FakeScheduler(running=True),
    )

    serialized = _run_check_sync(check)

    assert TEST_TOKEN not in serialized
    assert "Sup3rSecret" not in serialized
    assert "alice:Sup3rSecret" not in serialized
    assert "user_id" not in serialized
    assert "telegram_user_id" not in serialized


# === Вспомогательное =======================================================


def _async_return(value: bool):
    """Возвращает корутину с готовым значением."""

    async def _coroutine() -> bool:
        return value

    return _coroutine()


async def _wait_for_file(target: Path, *, timeout: float) -> None:
    """Ждёт появления файла до ``timeout`` секунд."""
    import asyncio
    import time

    start = time.monotonic()
    while not target.is_file():
        if time.monotonic() - start > timeout:
            raise AssertionError(f"файл {target} не появился за {timeout}s")
        await asyncio.sleep(0.02)


def _run_check_sync(check: HealthCheck) -> str:
    """Запускает async ``check.check()`` и возвращает JSON отчёта."""
    import asyncio

    report = asyncio.run(check.check())
    return report.to_json()
