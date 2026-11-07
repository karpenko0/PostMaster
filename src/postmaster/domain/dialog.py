"""Состояния диалога и переходы сценария создания публикации (SPEC-003).

Переходы заданы явной таблицей. Событие, которого нет в таблице для текущего состояния,
не меняет состояние (AC-02). Модуль не зависит от Telegram и базы данных.
"""

from enum import StrEnum


class DialogState(StrEnum):
    """Шаг пользователя в сценарии (AC-01). Состояние IDLE означает, что сценарий не идёт."""

    IDLE = "IDLE"
    WAITING_PHOTO = "WAITING_PHOTO"
    WAITING_PUBLICATION_TYPE = "WAITING_PUBLICATION_TYPE"
    WAITING_TARGET = "WAITING_TARGET"
    WAITING_DATETIME = "WAITING_DATETIME"


class DialogEvent(StrEnum):
    """События, которые меняют состояние. Названия совпадают с FSM-контрактом §5 SPEC-003."""

    START = "/start"
    PHOTO = "PHOTO"
    REMINDER = "REMINDER"
    TELEGRAM = "TELEGRAM"
    VALID_TARGET = "VALID_TARGET"
    VALID_DATETIME = "VALID_DATETIME"


class InvalidTransitionError(RuntimeError):
    """Переход не описан в таблице. Возникает из-за ошибки в коде, а не из-за ввода пользователя."""


TRANSITIONS: dict[tuple[DialogState, DialogEvent], DialogState] = {
    # BR-01: /start работает из любого состояния и начинает новый сценарий.
    (DialogState.IDLE, DialogEvent.START): DialogState.WAITING_PHOTO,
    (DialogState.WAITING_PHOTO, DialogEvent.START): DialogState.WAITING_PHOTO,
    (DialogState.WAITING_DATETIME, DialogEvent.START): DialogState.WAITING_PHOTO,
    (DialogState.WAITING_PUBLICATION_TYPE, DialogEvent.START): DialogState.WAITING_PHOTO,
    (DialogState.WAITING_TARGET, DialogEvent.START): DialogState.WAITING_PHOTO,
    (DialogState.WAITING_PUBLICATION_TYPE, DialogEvent.REMINDER): DialogState.WAITING_DATETIME,
    (DialogState.WAITING_PUBLICATION_TYPE, DialogEvent.TELEGRAM): DialogState.WAITING_TARGET,
    (DialogState.WAITING_TARGET, DialogEvent.VALID_TARGET): DialogState.WAITING_DATETIME,
    (DialogState.WAITING_PUBLICATION_TYPE, DialogEvent.PHOTO): DialogState.WAITING_PUBLICATION_TYPE,
    (DialogState.WAITING_TARGET, DialogEvent.PHOTO): DialogState.WAITING_TARGET,
    # SPEC-009 §32: после завершения новая фотография начинает новый сценарий.
    (DialogState.IDLE, DialogEvent.PHOTO): DialogState.WAITING_PUBLICATION_TYPE,
    # BR-02: фото принимается на шаге ожидания фотографии.
    (DialogState.WAITING_PHOTO, DialogEvent.PHOTO): DialogState.WAITING_PUBLICATION_TYPE,
    # BR-04: новое фото на шаге даты заменяет предыдущее, состояние остаётся прежним.
    (DialogState.WAITING_DATETIME, DialogEvent.PHOTO): DialogState.WAITING_DATETIME,
    # BR-03: корректная дата создаёт пост и возвращает в IDLE.
    (DialogState.WAITING_DATETIME, DialogEvent.VALID_DATETIME): DialogState.IDLE,
}


def next_state(state: DialogState, event: DialogEvent) -> DialogState | None:
    """Возвращает состояние после события или None, если такой переход не разрешён."""
    return TRANSITIONS.get((state, event))
