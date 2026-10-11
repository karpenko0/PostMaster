"""Строгий разбор локальной даты публикации и преобразование в UTC (SPEC-005).

Значение now вводится извне: проверка не зависит от системных часов. Правила для
неоднозначного/несуществующего локального времени при переходе DST определит SPEC-006.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from enum import StrEnum
from zoneinfo import ZoneInfo

DATETIME_FORMAT = "%d.%m.%Y %H:%M"
DATETIME_EXAMPLE = "15.10.2026 18:30"
# strptime сам по себе принимает однозначные цифры; здесь ширина и разделители обязательны.
_DATETIME_PATTERN = re.compile(r"([0-9]{2})\.([0-9]{2})\.([0-9]{4}) ([0-9]{2}):([0-9]{2})\Z")


class DateTimeErrorCode(StrEnum):
    INVALID_FORMAT = "INVALID_FORMAT"
    INVALID_DATE = "INVALID_DATE"
    INVALID_TIME = "INVALID_TIME"
    PAST_DATETIME = "PAST_DATETIME"


class DateTimeValidationError(ValueError):
    """Ошибка ввода с кодом для пользовательского ответа; текст ввода не сохраняется."""

    def __init__(self, code: DateTimeErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ParsedPublicationDateTime:
    local_datetime: datetime
    timezone: str
    utc_datetime: datetime


def parse_publication_datetime(
    value: str, timezone: str, now: datetime
) -> ParsedPublicationDateTime:
    """Разбирает строго DD.MM.YYYY HH:MM, различает ошибки даты/времени и проверяет будущее.

    now обязан иметь tzinfo. Сравнение ведётся в UTC (не по naive локальным часам).
    timezone — проверенный IANA-идентификатор из профиля или DEFAULT_TIMEZONE.
    """
    match = _DATETIME_PATTERN.fullmatch(value)
    if match is None:
        raise DateTimeValidationError(DateTimeErrorCode.INVALID_FORMAT)
    day, month, year = map(int, match.groups()[:3])
    try:
        # Календарь проверяем отдельно, чтобы код ошибки не зависел от текста ValueError.
        datetime(year, month, day)
    except ValueError:
        raise DateTimeValidationError(DateTimeErrorCode.INVALID_DATE) from None
    try:
        naive = datetime.strptime(value, DATETIME_FORMAT)
    except ValueError:
        raise DateTimeValidationError(DateTimeErrorCode.INVALID_TIME) from None
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now должен содержать часовой пояс")
    zone = ZoneInfo(timezone)
    local = naive.replace(tzinfo=zone)
    utc = local.astimezone(UTC)
    if utc <= now.astimezone(UTC):
        raise DateTimeValidationError(DateTimeErrorCode.PAST_DATETIME)
    return ParsedPublicationDateTime(local_datetime=local, timezone=timezone, utc_datetime=utc)


def parse_local_datetime(text: str, zone: tzinfo, now: datetime) -> datetime | None:
    """Совместимый интерфейс SPEC-003 (уступает место строгому API SPEC-005).

    Старый контракт принимает tzinfo и пробелы вокруг строки, возвращает None при ошибке.
    Новые обработчики используют только parse_publication_datetime.
    """
    try:
        naive = datetime.strptime(text.strip(), DATETIME_FORMAT)
    except ValueError:
        return None
    scheduled = naive.replace(tzinfo=zone).astimezone(UTC)
    return scheduled if scheduled > now else None
