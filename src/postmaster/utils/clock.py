"""Текущее время для дат записей (SPEC-002, §6)."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Возвращает текущее время в UTC с часовым поясом."""
    return datetime.now(UTC)
