"""TC-005-01…11 и AC-005-01…12: строгий парсер, коды ошибок, UTC."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from postmaster.domain.schedule import (
    DateTimeErrorCode,
    DateTimeValidationError,
    parse_publication_datetime,
)

NOW = datetime(2026, 10, 7, 11, 59, tzinfo=UTC)  # Москва: 14:59


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("07.10.2026 15:00", datetime(2026, 10, 7, 12, tzinfo=UTC)),
        ("01.12.2026 09:30", datetime(2026, 12, 1, 6, 30, tzinfo=UTC)),
        ("31.12.2026 23:59", datetime(2026, 12, 31, 20, 59, tzinfo=UTC)),
        ("29.02.2028 00:01", datetime(2028, 2, 28, 21, 1, tzinfo=UTC)),
    ],
)
def test_future_dates_are_normalized(text: str, expected: datetime) -> None:
    result = parse_publication_datetime(text, "Europe/Moscow", NOW)
    assert result.utc_datetime == expected
    assert result.timezone == "Europe/Moscow"
    assert result.local_datetime.tzinfo == ZoneInfo("Europe/Moscow")
    assert result.local_datetime.astimezone(UTC) == expected
    assert result.local_datetime.utcoffset() == timedelta(hours=3)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("завтра", DateTimeErrorCode.INVALID_FORMAT),
        ("завтра в пять", DateTimeErrorCode.INVALID_FORMAT),
        ("07/10/2026 15:00", DateTimeErrorCode.INVALID_FORMAT),
        ("2026-10-07 15:00", DateTimeErrorCode.INVALID_FORMAT),
        ("07.10.26 15:00", DateTimeErrorCode.INVALID_FORMAT),
        ("7.10.2026 15:00", DateTimeErrorCode.INVALID_FORMAT),
        ("07.10.2026 5:00", DateTimeErrorCode.INVALID_FORMAT),
        ("07.10.2026 15:0", DateTimeErrorCode.INVALID_FORMAT),
        (" 07.10.2026 15:00", DateTimeErrorCode.INVALID_FORMAT),
        ("07.10.2026 15:00 ", DateTimeErrorCode.INVALID_FORMAT),
        ("07.10.2026\t15:00", DateTimeErrorCode.INVALID_FORMAT),
        ("07.10.2026 15:00\n", DateTimeErrorCode.INVALID_FORMAT),
        ("07.10.2026", DateTimeErrorCode.INVALID_FORMAT),
        ("07.10.2026 в 15:00", DateTimeErrorCode.INVALID_FORMAT),
        ("32.10.2026 15:00", DateTimeErrorCode.INVALID_DATE),
        ("00.10.2026 15:00", DateTimeErrorCode.INVALID_DATE),
        ("31.11.2026 15:00", DateTimeErrorCode.INVALID_DATE),
        ("31.02.2026 15:00", DateTimeErrorCode.INVALID_DATE),
        ("29.02.2026 15:00", DateTimeErrorCode.INVALID_DATE),
        ("15.13.2026 15:00", DateTimeErrorCode.INVALID_DATE),
        ("07.10.2026 24:30", DateTimeErrorCode.INVALID_TIME),
        ("07.10.2026 25:00", DateTimeErrorCode.INVALID_TIME),
        ("07.10.2026 15:60", DateTimeErrorCode.INVALID_TIME),
        ("07.10.2026 15:99", DateTimeErrorCode.INVALID_TIME),
        ("07.10.2026 15:00\x00", DateTimeErrorCode.INVALID_FORMAT),
    ],
)
def test_invalid_values_have_precise_code(text: str, code: DateTimeErrorCode) -> None:
    with pytest.raises(DateTimeValidationError) as exc:
        parse_publication_datetime(text, "Europe/Moscow", NOW)
    assert exc.value.code == code


@pytest.mark.parametrize(
    "now", [datetime(2026, 10, 7, 12, tzinfo=UTC), datetime(2026, 10, 7, 13, tzinfo=UTC)]
)
def test_equal_and_past_are_rejected(now: datetime) -> None:
    with pytest.raises(DateTimeValidationError) as exc:
        parse_publication_datetime("07.10.2026 15:00", "Europe/Moscow", now)
    assert exc.value.code == DateTimeErrorCode.PAST_DATETIME


def test_comparison_uses_instant_not_naive_clock() -> None:
    # Локально 15:00 ещё впереди 14:59, а на границе 15:00 уже не впереди.
    assert parse_publication_datetime(
        "07.10.2026 15:00", "Europe/Moscow", NOW
    ).utc_datetime == datetime(2026, 10, 7, 12, tzinfo=UTC)
    with pytest.raises(DateTimeValidationError, match="PAST_DATETIME"):
        parse_publication_datetime(
            "07.10.2026 15:00",
            "Europe/Moscow",
            datetime(2026, 10, 7, 15, tzinfo=ZoneInfo("Europe/Moscow")),
        )


def test_profile_timezone_changes_utc_instant() -> None:
    result = parse_publication_datetime(
        "07.10.2026 15:00", "Asia/Tokyo", datetime(2026, 10, 7, 5, tzinfo=UTC)
    )
    assert result.utc_datetime == datetime(2026, 10, 7, 6, tzinfo=UTC)
    assert result.local_datetime.utcoffset() == timedelta(hours=9)


def test_naive_now_is_not_interpreted_as_machine_timezone() -> None:
    with pytest.raises(ValueError, match="now должен"):
        parse_publication_datetime("07.10.2026 15:00", "Europe/Moscow", datetime(2026, 10, 7))
