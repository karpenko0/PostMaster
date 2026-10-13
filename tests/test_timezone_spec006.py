"""TC-006-01…05, AC-006-01…07, ERR-006-01/02 и обратная конвертация."""

import os
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from postmaster.domain.schedule import (
    DateTimeErrorCode,
    DateTimeValidationError,
    parse_publication_datetime,
)
from postmaster.domain.timezone import (
    convert_local_to_utc,
    convert_utc_to_local,
    resolve_user_timezone,
)

MOSCOW = "Europe/Moscow"
LOCAL = datetime(2026, 10, 7, 15, 0)
EXPECTED_UTC = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def test_local_to_utc_and_roundtrip() -> None:
    assert convert_local_to_utc(LOCAL, MOSCOW) == EXPECTED_UTC
    result = convert_utc_to_local(EXPECTED_UTC, MOSCOW)
    assert result == LOCAL.replace(tzinfo=ZoneInfo(MOSCOW))
    assert result.utcoffset() == timedelta(hours=3)
    assert result.strftime("%d.%m.%Y %H:%M") == "07.10.2026 15:00"


def test_utc_input_does_not_need_to_be_reinterpreted() -> None:
    assert convert_local_to_utc(datetime(2026, 10, 7, 12, tzinfo=UTC), MOSCOW) == EXPECTED_UTC
    assert convert_local_to_utc(datetime(2026, 10, 7, 12), "UTC") == EXPECTED_UTC
    assert convert_utc_to_local(EXPECTED_UTC, "UTC") == EXPECTED_UTC


def test_iana_offsets_vary_by_date_without_using_server_timezone() -> None:
    # Не фиксировать смещение Berlin в +01: летом +02, зимой +01.
    assert convert_local_to_utc(datetime(2026, 7, 7, 15), "Europe/Berlin") == datetime(
        2026, 7, 7, 13, tzinfo=UTC
    )
    assert convert_local_to_utc(datetime(2026, 1, 7, 15), "Europe/Berlin") == datetime(
        2026, 1, 7, 14, tzinfo=UTC
    )


@pytest.mark.parametrize("user_zone", [None, "", "Invalid/Timezone", "../UTC", "Mars/Base"])
def test_missing_or_invalid_user_timezone_falls_back(user_zone: str | None) -> None:
    assert resolve_user_timezone(user_zone, MOSCOW) == MOSCOW


def test_valid_user_timezone_overrides_default() -> None:
    assert resolve_user_timezone("Asia/Tokyo", MOSCOW) == "Asia/Tokyo"
    assert resolve_user_timezone("UTC", MOSCOW) == "UTC"


def test_invalid_default_is_a_configuration_error_not_a_server_timezone() -> None:
    with pytest.raises((ValueError, ZoneInfoNotFoundError)):
        resolve_user_timezone("UTC", "Missing/Default")


@pytest.mark.parametrize(
    "input_utc", [datetime(2026, 10, 7, 12), datetime(2026, 10, 7, 15, tzinfo=ZoneInfo(MOSCOW))]
)
def test_conversion_does_not_treat_naive_or_local_datetime_as_utc(input_utc: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        convert_utc_to_local(input_utc, MOSCOW)


def test_future_check_is_made_in_utc() -> None:
    accepted = parse_publication_datetime(
        "07.10.2026 15:00", MOSCOW, datetime(2026, 10, 7, 11, 30, tzinfo=UTC)
    )
    assert accepted.utc_datetime == EXPECTED_UTC
    with pytest.raises(DateTimeValidationError) as exc:
        parse_publication_datetime(
            "07.10.2026 15:00", MOSCOW, datetime(2026, 10, 7, 12, 30, tzinfo=UTC)
        )
    assert exc.value.code == DateTimeErrorCode.PAST_DATETIME


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="TZ окружения отсутствует на платформе")
def test_server_timezone_does_not_affect_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    before = os.environ.get("TZ")
    try:
        for server_tz in ("UTC", "Pacific/Honolulu", "Asia/Tokyo"):
            monkeypatch.setenv("TZ", server_tz)
            time.tzset()
            assert convert_local_to_utc(LOCAL, MOSCOW) == EXPECTED_UTC
            assert convert_utc_to_local(EXPECTED_UTC, MOSCOW).hour == 15
    finally:
        if before is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", before)
        time.tzset()


async def test_post_service_handoff_requires_utc_and_publication_timezone() -> None:
    from postmaster.services.post_service import PostService

    service = PostService()
    post = await service.create_post(
        owner_telegram_id=42,
        photo_file_id="image",
        scheduled_at=EXPECTED_UTC,
        timezone=MOSCOW,
    )
    assert post.scheduled_at_utc == EXPECTED_UTC
    assert post.timezone == MOSCOW
    for invalid in (LOCAL, LOCAL.replace(tzinfo=ZoneInfo(MOSCOW))):
        with pytest.raises(ValueError, match="UTC"):
            await service.create_post(
                owner_telegram_id=42,
                photo_file_id="image",
                scheduled_at=invalid,
                timezone=MOSCOW,
            )
    with pytest.raises(ZoneInfoNotFoundError):
        await service.create_post(
            owner_telegram_id=42,
            photo_file_id="image",
            scheduled_at=EXPECTED_UTC,
            timezone="Invalid/Timezone",
        )
