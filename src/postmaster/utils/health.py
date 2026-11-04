"""Отчёт health check: структуры данных и чтение/запись файла (SPEC-029).

Модуль не зависит ни от БД, ни от Telegram — только от стандартной библиотеки.
Отчёт содержит имена проверок, их статус и безопасные краткие детали.
Секреты и данные пользователей в отчёт не попадают (SPEC-029 §1).

Запись атомарная: временный файл + ``os.replace``. Читатель видит либо
полный валидный отчёт, либо ничего.
"""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHECK_PROCESS = "process"
CHECK_DATABASE = "database"
CHECK_SCHEDULER = "scheduler"

DEFAULT_MAX_AGE_SECONDS = 90.0


class HealthReportError(RuntimeError):
    """Базовая ошибка чтения отчёта health check."""


class HealthReportStale(HealthReportError):
    """Отчёт существует, но слишком старый — приложение, вероятно, зависло."""


class HealthReportCorrupted(HealthReportError):
    """Отчёт существует, но повреждён."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Результат одной проверки (SPEC-029 §1)."""

    name: str
    ok: bool
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"name": self.name, "ok": self.ok}
        if self.detail is not None:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Полный отчёт health check (SPEC-029 §1, §7)."""

    healthy: bool
    checks: tuple[CheckResult, ...]
    generated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "generated_at": self.generated_at.isoformat(),
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def summary(self) -> str:
        """Краткий текст без секретов и данных пользователей."""
        markers = " ".join(f"{c.name}={'ok' if c.ok else 'fail'}" for c in self.checks)
        state = "healthy" if self.healthy else "unhealthy"
        return f"{state}: {markers}"


def write_report(target: Path, report: HealthReport) -> None:
    """Атомарно записывает отчёт в файл (temp + replace)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(report.to_json(), encoding="utf-8")
    os.replace(tmp, target)


def read_report(
    target: Path, *, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS
) -> HealthReport | None:
    """Читает отчёт из файла. Возвращает ``None``, если файла нет.

    Raises:
        HealthReportCorrupted: файл не парсится как валидный JSON-отчёт.
        HealthReportStale: отчёт старше ``max_age_seconds``.
    """
    if not target.is_file():
        return None
    raw = target.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HealthReportCorrupted(f"файл не валиден: {exc}") from exc

    try:
        generated_at = datetime.fromisoformat(payload["generated_at"])
        raw_checks = payload.get("checks", [])
        healthy = bool(payload["healthy"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HealthReportCorrupted(f"отчёт повреждён: {exc}") from exc

    if not generated_at.tzinfo:
        generated_at = generated_at.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - generated_at).total_seconds()
    if age > max_age_seconds:
        raise HealthReportStale(f"отчёт устарел на {age:.0f}s (лимит {max_age_seconds:.0f}s)")

    checks = tuple(
        CheckResult(
            name=item["name"],
            ok=bool(item["ok"]),
            detail=item.get("detail"),
        )
        for item in raw_checks
    )
    return HealthReport(healthy=healthy, checks=checks, generated_at=generated_at)


__all__ = [
    "CHECK_DATABASE",
    "CHECK_PROCESS",
    "CHECK_SCHEDULER",
    "DEFAULT_MAX_AGE_SECONDS",
    "CheckResult",
    "HealthReport",
    "HealthReportCorrupted",
    "HealthReportError",
    "HealthReportStale",
    "read_report",
    "write_report",
]
