"""Таблица переходов сценария и разбор даты: AC-01, AC-02, TC-01, BR-01…BR-03 (SPEC-003).

Проверка не требует Telegram и базы данных. Каждая пара «состояние — событие» проверяется явно.
"""

import itertools
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from postmaster.domain.dialog import TRANSITIONS, DialogEvent, DialogState, next_state
from postmaster.domain.schedule import parse_local_datetime

IDLE = DialogState.IDLE
WAITING_PHOTO = DialogState.WAITING_PHOTO
WAITING_DATETIME = DialogState.WAITING_DATETIME
START = DialogEvent.START
PHOTO = DialogEvent.PHOTO
VALID_DATETIME = DialogEvent.VALID_DATETIME

# Утверждённый сценарий из FSM-контракта §5 и правил BR-01…BR-04. Всё остальное запрещено.
SCENARIO = {
    (IDLE, START): WAITING_PHOTO,
    (WAITING_PHOTO, START): WAITING_PHOTO,
    (WAITING_DATETIME, START): WAITING_PHOTO,
    (WAITING_PHOTO, PHOTO): WAITING_DATETIME,
    (WAITING_DATETIME, PHOTO): WAITING_DATETIME,
    (WAITING_DATETIME, VALID_DATETIME): IDLE,
}

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
MOSCOW = ZoneInfo("Europe/Moscow")


def test_dialog_has_exactly_three_states() -> None:
    # AC-01: все три состояния реализованы.
    assert {state.value for state in DialogState} == {"IDLE", "WAITING_PHOTO", "WAITING_DATETIME"}


@pytest.mark.parametrize(
    ("state", "event"),
    list(itertools.product(DialogState, DialogEvent)),
)
def test_only_scenario_transitions_are_allowed(state: DialogState, event: DialogEvent) -> None:
    # AC-02 и TC-01: разрешён только переход из сценария, для остальных пар состояние не меняется.
    assert next_state(state, event) == SCENARIO.get((state, event))


def test_transition_table_matches_scenario_exactly() -> None:
    assert TRANSITIONS == SCENARIO


@pytest.mark.parametrize(
    ("state", "expected"),
    [(IDLE, WAITING_PHOTO), (WAITING_PHOTO, WAITING_PHOTO), (WAITING_DATETIME, WAITING_PHOTO)],
)
def test_start_leads_to_waiting_photo_from_every_state(
    state: DialogState, expected: DialogState
) -> None:
    # BR-01: /start переводит в WAITING_PHOTO независимо от текущего шага.
    assert next_state(state, START) == expected


def test_valid_local_date_is_converted_to_utc() -> None:
    # BR-03: дата из пояса пользователя переводится в UTC. Москва = UTC+3.
    result = parse_local_datetime("15.10.2026 18:30", MOSCOW, NOW)

    assert result == datetime(2026, 10, 15, 15, 30, tzinfo=UTC)


def test_surrounding_spaces_are_ignored() -> None:
    assert parse_local_datetime("  15.10.2026 18:30  ", UTC, NOW) == datetime(
        2026, 10, 15, 18, 30, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "text",
    ["31.02.2026 10:00", "2026-10-15 18:30", "вчера", "", "15.10.2026", "15.10.2026 25:00"],
)
def test_malformed_date_is_rejected(text: str) -> None:
    assert parse_local_datetime(text, UTC, NOW) is None


def test_past_date_is_rejected() -> None:
    assert parse_local_datetime("01.10.2026 10:00", UTC, NOW) is None


def test_current_moment_is_rejected() -> None:
    # Публикация должна быть строго в будущем: время, равное текущему, не подходит.
    assert parse_local_datetime("08.10.2026 12:00", UTC, NOW) is None
