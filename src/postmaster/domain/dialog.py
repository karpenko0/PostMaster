"""Состояния диалога и переходы сценария создания публикации (SPEC-003).

Переходы заданы явной таблицей. Событие, которого нет в таблице для текущего состояния,
не меняет состояние (AC-02). Модуль не зависит от Telegram и базы данных.
"""

import re
from enum import StrEnum


class DialogState(StrEnum):
    """Шаг пользователя в сценарии (AC-01). Состояние IDLE означает, что сценарий не идёт."""

    IDLE = "IDLE"
    WAITING_PHOTO = "WAITING_PHOTO"
    WAITING_PUBLICATION_TYPE = "WAITING_PUBLICATION_TYPE"
    WAITING_TARGET = "WAITING_TARGET"
    WAITING_DATETIME = "WAITING_DATETIME"
    # PM-05: предварительный просмотр перед сохранением публикации.
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    # PM-04: перенос публикации на новое время через inline-кнопку.
    WAITING_RESCHEDULE_DATETIME = "WAITING_RESCHEDULE_DATETIME"


class DialogEvent(StrEnum):
    """События, которые меняют состояние. Названия совпадают с FSM-контрактом §5 SPEC-003."""

    START = "/start"
    PHOTO = "PHOTO"
    REMINDER = "REMINDER"
    TELEGRAM = "TELEGRAM"
    VALID_TARGET = "VALID_TARGET"
    VALID_DATETIME = "VALID_DATETIME"
    # PM-05: подтверждение публикации на этапе предпросмотра.
    CONFIRM_DATETIME = "CONFIRM_DATETIME"
    # PM-04: перенос публикации на новое время.
    RESCHEDULE_VALID = "RESCHEDULE_VALID"


class InvalidTransitionError(RuntimeError):
    """Переход не описан в таблице. Возникает из-за ошибки в коде, а не из-за ввода пользователя."""


TRANSITIONS: dict[tuple[DialogState, DialogEvent], DialogState] = {
    # BR-01: /start работает из любого состояния и начинает новый сценарий.
    (DialogState.IDLE, DialogEvent.START): DialogState.WAITING_PHOTO,
    (DialogState.WAITING_PHOTO, DialogEvent.START): DialogState.WAITING_PHOTO,
    (DialogState.WAITING_PUBLICATION_TYPE, DialogEvent.START): DialogState.WAITING_PHOTO,
    (DialogState.WAITING_TARGET, DialogEvent.START): DialogState.WAITING_PHOTO,
    (DialogState.WAITING_DATETIME, DialogEvent.START): DialogState.WAITING_PHOTO,
    (DialogState.WAITING_CONFIRMATION, DialogEvent.START): DialogState.WAITING_PHOTO,
    (DialogState.WAITING_RESCHEDULE_DATETIME, DialogEvent.START): DialogState.WAITING_PHOTO,
    # PM-03: выбор типа публикации.
    (DialogState.WAITING_PUBLICATION_TYPE, DialogEvent.REMINDER): DialogState.WAITING_DATETIME,
    (DialogState.WAITING_PUBLICATION_TYPE, DialogEvent.TELEGRAM): DialogState.WAITING_TARGET,
    # PM-03: ввод корректного target_id переводит к выбору даты.
    (DialogState.WAITING_TARGET, DialogEvent.VALID_TARGET): DialogState.WAITING_DATETIME,
    # PM-03: новое фото на шаге выбора типа публикации заменяет предыдущее.
    (DialogState.WAITING_PUBLICATION_TYPE, DialogEvent.PHOTO): DialogState.WAITING_PUBLICATION_TYPE,
    (DialogState.WAITING_TARGET, DialogEvent.PHOTO): DialogState.WAITING_TARGET,
    (DialogState.WAITING_DATETIME, DialogEvent.PHOTO): DialogState.WAITING_DATETIME,
    # PM-05: валидная дата переводит на предварительный просмотр, а не создаёт пост.
    (DialogState.WAITING_DATETIME, DialogEvent.VALID_DATETIME): DialogState.WAITING_CONFIRMATION,
    # PM-05: подтверждение публикации в WAITING_CONFIRMATION возвращает в IDLE.
    (DialogState.WAITING_CONFIRMATION, DialogEvent.CONFIRM_DATETIME): DialogState.IDLE,
    # SPEC-009 §32: после завершения новая фотография начинает новый сценарий.
    (DialogState.IDLE, DialogEvent.PHOTO): DialogState.WAITING_PUBLICATION_TYPE,
    # BR-02: фото принимается на шаге ожидания фотографии.
    (DialogState.WAITING_PHOTO, DialogEvent.PHOTO): DialogState.WAITING_PUBLICATION_TYPE,
    # PM-04: корректная новая дата применяется и возвращает в IDLE.
    (DialogState.WAITING_RESCHEDULE_DATETIME, DialogEvent.RESCHEDULE_VALID): DialogState.IDLE,
}


def next_state(state: DialogState, event: DialogEvent) -> DialogState | None:
    """Возвращает состояние после события или None, если такой переход не разрешён."""
    return TRANSITIONS.get((state, event))


# PM-06 / BUG-01: callback_data Telegram ограничено 64 байтами, а draft_id — UUID (36).
# Поэтому в callback попадает короткий токен черновика — однозначный префикс draft_id.
CALLBACK_TOKEN_LENGTH = 16
_TOKEN_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def draft_token(draft_id: str | None) -> str:
    """Короткий токен черновика для callback_data. Пустая строка, если draft_id нет."""
    if not draft_id:
        return ""
    return _TOKEN_SAFE.sub("", str(draft_id))[:CALLBACK_TOKEN_LENGTH]


def token_matches(draft_id: str | None, token: str | None) -> bool:
    """Совпадает ли токен из callback с черновиком. Пустой токен совпадением не считается."""
    if not token:
        return False
    expected = draft_token(draft_id)
    return bool(expected) and expected == token
