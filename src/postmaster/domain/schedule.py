"""Разбор даты публикации, которую вводит пользователь (SPEC-003, решение D8).

Формат ДД.ММ.ГГГГ ЧЧ:ММ временный, до отдельной спецификации дат. Текущее время передаётся
аргументом, поэтому разбор проверяется без реальных часов. Модуль не зависит от Telegram и БД.
"""

from datetime import UTC, datetime, tzinfo

DATETIME_FORMAT = "%d.%m.%Y %H:%M"
DATETIME_EXAMPLE = "15.10.2026 18:30"


def parse_local_datetime(text: str, zone: tzinfo, now: datetime) -> datetime | None:
    """Переводит дату, указанную в поясе пользователя, в UTC.

    Возвращает None, если формат неверен или время не позже now. Аргумент now передаётся
    с часовым поясом.
    """
    try:
        naive = datetime.strptime(text.strip(), DATETIME_FORMAT)
    except ValueError:
        return None
    scheduled = naive.replace(tzinfo=zone).astimezone(UTC)
    return scheduled if scheduled > now else None
