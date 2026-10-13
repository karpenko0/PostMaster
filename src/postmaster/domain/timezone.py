"""Часовые зоны пользователя и преобразование моментов (SPEC-006).

Здесь нет зависимости от timezone сервера и не используется datetime.now(). Правила
специальной обработки переходов DST (двусмысленного/пропущенного часа) ТЗ не задаёт:
для naive локального времени применяется стандартное поведение zoneinfo (fold=0).
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def resolve_user_timezone(user_timezone: str | None, default_timezone: str) -> str:
    """Возвращает IANA-пояс пользователя; при отсутствии/ошибке — конфигурационный пояс.

    Значение DEFAULT_TIMEZONE валидируется при загрузке настроек. Если функция вызвана
    отдельно с невалидным default, ошибка не маскируется: иначе конвертация невозможна.
    """
    ZoneInfo(default_timezone)
    if user_timezone:
        try:
            ZoneInfo(user_timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            pass
        else:
            return user_timezone
    return default_timezone


def convert_local_to_utc(local_datetime: datetime, timezone: str) -> datetime:
    """Считает naive дату локальными часами пользователя и переводит в aware UTC.

    Если передано aware datetime, сохраняет его абсолютный момент: сначала переводит в
    заданный пояс, не подменяя tzinfo (это могло бы сдвинуть фактическое время).
    """
    zone = ZoneInfo(timezone)
    if local_datetime.tzinfo is None or local_datetime.utcoffset() is None:
        local_datetime = local_datetime.replace(tzinfo=zone)
    else:
        local_datetime = local_datetime.astimezone(zone)
    return local_datetime.astimezone(UTC)


def convert_utc_to_local(utc_datetime: datetime, timezone: str) -> datetime:
    """Переводит aware UTC из хранилища в локальное время пользователя/публикации.

    Naive datetime не трактуется как UTC или время сервера: репозиторий должен явно
    восстановить tzinfo=UTC при чтении из SQLite.
    """
    if utc_datetime.tzinfo is None or utc_datetime.utcoffset() != timedelta(0):
        raise ValueError("utc_datetime должен содержать часовой пояс UTC")
    return utc_datetime.astimezone(ZoneInfo(timezone))
