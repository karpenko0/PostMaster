"""Health check: проверка процесса, БД и Scheduler (SPEC-029).

``HealthCheck`` — асинхронный сервис, выполняющий три проверки:

- ``process`` — процесс жив (сам факт вызова ``check()``);
- ``database`` — через внедрённый ``database_probe`` (не импортирует
  sqlalchemy напрямую, чтобы остаться в слое services);
- ``scheduler`` — через внедрённый ``scheduler_status`` (port-объект).

Все проверки read-only. Результат — ``HealthReport`` из ``utils.health``
без секретов и пользовательских данных.

``HealthReporter`` — фоновый цикл, который вызывает ``HealthCheck.check()``
с заданным интервалом и атомарно пишет отчёт в файл. Используется
``Application.startup`` для запуска и ``Application.shutdown`` для остановки.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from postmaster.utils.health import (
    CHECK_DATABASE,
    CHECK_PROCESS,
    CHECK_SCHEDULER,
    CheckResult,
    HealthReport,
    write_report,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30.0

DatabaseProbe = Callable[[], Awaitable[bool]]


class SchedulerStatusProvider(Protocol):
    """Порт для проверки статуса Scheduler (SPEC-029 §1)."""

    @property
    def running(self) -> bool: ...


class HealthCheck:
    """Выполняет проверки process/database/scheduler (SPEC-029 §1)."""

    def __init__(
        self,
        *,
        database_probe: DatabaseProbe,
        scheduler_status: SchedulerStatusProvider,
    ) -> None:
        self._database_probe = database_probe
        self._scheduler_status = scheduler_status

    async def check(self) -> HealthReport:
        """Возвращает отчёт; все проверки read-only."""
        database_ok = await self._safe_probe_database()
        scheduler_running = bool(self._scheduler_status.running)
        checks: tuple[CheckResult, ...] = (
            CheckResult(name=CHECK_PROCESS, ok=True),
            CheckResult(name=CHECK_DATABASE, ok=database_ok),
            CheckResult(
                name=CHECK_SCHEDULER,
                ok=scheduler_running,
                detail=None if scheduler_running else "not_running",
            ),
        )
        return HealthReport(
            healthy=all(c.ok for c in checks),
            checks=checks,
            generated_at=datetime.now(UTC),
        )

    async def _safe_probe_database(self) -> bool:
        try:
            return bool(await self._database_probe())
        except Exception:  # noqa: BLE001 — health probe не должен бросать
            return False


class HealthReporter:
    """Фоновый цикл: проверяет здоровье и пишет отчёт в файл (SPEC-029 §1)."""

    def __init__(
        self,
        *,
        health_check: HealthCheck,
        target: Path,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._health_check = health_check
        self._target = target
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._last_report: HealthReport | None = None

    @property
    def last_report(self) -> HealthReport | None:
        """Последний отчёт (для тестов)."""
        return self._last_report

    async def start(self) -> None:
        """Создаёт фоновую задачу, если её ещё нет."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="health-reporter")

    async def stop(self) -> None:
        """Останавливает фоновую задачу."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def report_now(self) -> HealthReport:
        """Выполняет проверку и пишет отчёт в файл. Возвращает отчёт."""
        report = await self._health_check.check()
        self._last_report = report
        write_report(self._target, report)
        return report

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self.report_now()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Health report write failed")
                await asyncio.sleep(self._interval_seconds)
        except asyncio.CancelledError:
            pass


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DatabaseProbe",
    "HealthCheck",
    "HealthReporter",
    "SchedulerStatusProvider",
]
