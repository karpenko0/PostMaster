"""SPEC-008 TC-01…05: доменная сущность, статусы, типы и инварианты."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from postmaster.domain.post import Post, PostStatus, PublicationType

STAMP = datetime(2026, 10, 7, 10, tzinfo=UTC)


def reminder(**changes: object) -> Post:
    fields = {
        "id": 42,
        "user_id": 15,
        "telegram_file_id": "AgAC...",
        "telegram_file_unique_id": "AQAD...",
        "scheduled_at_utc": datetime(2026, 10, 7, 12, tzinfo=UTC),
        "timezone": "Europe/Moscow",
        "publication_type": PublicationType.REMINDER,
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    fields.update(changes)
    return Post(**fields)


def test_new_post_defaults_and_optional_fields() -> None:
    post = reminder()
    assert post.status == PostStatus.PENDING
    assert post.error_message is post.published_at is post.caption is post.target_id is None
    assert post.user_id == 15
    assert post.scheduled_at_utc.tzinfo is UTC


def test_exact_enum_values() -> None:
    assert {status.value for status in PostStatus} == {
        "PENDING",
        "PROCESSING",
        "PUBLISHED",
        "FAILED",
        "CANCELLED",
    }
    assert {kind.value for kind in PublicationType} == {"REMINDER", "TELEGRAM"}


def test_published_requires_timestamp_and_others_cannot_have_one() -> None:
    with pytest.raises(ValueError, match="published_at"):
        reminder(status=PostStatus.PUBLISHED)
    published = reminder(status=PostStatus.PUBLISHED, published_at=STAMP)
    assert published.published_at == STAMP
    with pytest.raises(ValueError, match="published_at"):
        replace(published, status=PostStatus.FAILED)


@pytest.mark.parametrize("field", ["scheduled_at_utc", "created_at", "updated_at", "published_at"])
def test_system_timestamps_must_be_aware_utc(field: str) -> None:
    changes = {field: STAMP.replace(tzinfo=None)}
    if field == "published_at":
        changes["status"] = PostStatus.PUBLISHED
    with pytest.raises(ValueError, match="aware UTC"):
        reminder(**changes)


@pytest.mark.parametrize("field", ["scheduled_at_utc", "created_at", "updated_at"])
def test_required_system_dates_rejected_when_missing(field: str) -> None:
    with pytest.raises(ValueError, match="обязателен"):
        reminder(**{field: None})


@pytest.mark.parametrize("field", ["telegram_file_id", "telegram_file_unique_id"])
def test_file_ids_cannot_be_blank(field: str) -> None:
    with pytest.raises(ValueError, match="обязателен"):
        reminder(**{field: "  "})


def test_telegram_target_and_iana_timezone() -> None:
    with pytest.raises(ValueError, match="target_id"):
        reminder(publication_type=PublicationType.TELEGRAM)
    assert reminder(publication_type=PublicationType.TELEGRAM, target_id="@example").target_id
    with pytest.raises(ValueError, match="IANA"):
        reminder(timezone="Invalid/Timezone")


def test_unknown_status_and_type_rejected_in_domain() -> None:
    with pytest.raises(ValueError, match="статус или тип"):
        reminder(status="UNKNOWN")
    with pytest.raises(ValueError, match="статус или тип"):
        reminder(publication_type="VK")
