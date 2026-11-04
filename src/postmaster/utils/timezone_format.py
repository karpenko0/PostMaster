"""Форматирование даты и времени с учётом часового пояса (SPEC-006, SPEC-010)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def format_local_datetime(utc_datetime: datetime, timezone: str) -> str:
    """Преобразует aware UTC datetime в строку формата ДД.ММ.ГГГГ ЧЧ:ММ в указанном поясе.

    Raises ValueError если utc_datetime не aware UTC или timezone невалиден.
    """
    if utc_datetime.tzinfo is None or utc_datetime.utcoffset() != timedelta(0):
        raise ValueError("utc_datetime должен быть aware UTC")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"неизвестный часовой пояс: {timezone}") from exc
    local = utc_datetime.astimezone(zone)
    return local.strftime("%d.%m.%Y %H:%M")
