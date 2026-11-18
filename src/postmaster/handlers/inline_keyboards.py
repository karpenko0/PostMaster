"""Построитель inline-клавиатур для действий с публикациями (PM-04).

Callback data имеет формат ``<action>:<post_id>``, где:
- ``cancel:<post_id>`` — отмена PENDING-публикации (PM-04 §4);
- ``resched:<post_id>`` — запрос на перенос времени (PM-04 §5).

Длина callback data в Telegram ограничена 64 байтами, поэтому ID хранится в
компактной форме (без префиксов). handler обязателен повторно проверять
владельца и статус в сервисном слое — callback data не доверяется.
"""

from __future__ import annotations

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from postmaster.domain.dialog import DRAFT_ID_LENGTH, is_valid_draft_id, new_draft_id

__all__ = [
    "CANCEL_BUTTON_TEXT",
    "CANCEL_CALLBACK_PREFIX",
    "CANCEL_DRAFT_CALLBACK_PREFIX",
    "CONFIRM_BUTTON_TEXT",
    "CONFIRM_DRAFT_CALLBACK_PREFIX",
    "DRAFT_ID_LENGTH",
    "MAX_CALLBACK_LENGTH",
    "RESCHEDULE_BUTTON_TEXT",
    "RESCHEDULE_CALLBACK_PREFIX",
    "build_confirmation_keyboard",
    "build_post_actions_keyboard",
    "is_valid_draft_id",
    "new_draft_id",
    "parse_cancel_callback",
    "parse_confirmation_callback",
    "parse_reschedule_callback",
]

CANCEL_CALLBACK_PREFIX = "cancel:"
RESCHEDULE_CALLBACK_PREFIX = "resched:"

# PM06-R2-001: callback привязан к конкретному черновику. Короткий префикс и
# 16 hex-символов дают 22 байта — запас до ограничения Telegram в 64 байта.
CONFIRM_DRAFT_CALLBACK_PREFIX = "pm05c:"
CANCEL_DRAFT_CALLBACK_PREFIX = "pm05x:"

CANCEL_BUTTON_TEXT = "❌ Отменить"
CONFIRM_BUTTON_TEXT = "✅ Подтвердить"
RESCHEDULE_BUTTON_TEXT = "🕒 Перенести"

MAX_CALLBACK_LENGTH = 64


def parse_confirmation_callback(data: str | None) -> tuple[str, str] | None:
    """Разбирает ``pm05c:<draft_id>`` / ``pm05x:<draft_id>``.

    Возвращает ``(action, draft_id)`` или None для устаревших/некорректных данных.
    Длина проверяется в байтах: Telegram ограничивает callback_data 64 байтами.
    """
    if not data or len(data.encode("utf-8")) > MAX_CALLBACK_LENGTH:
        return None
    for prefix, action in (
        (CONFIRM_DRAFT_CALLBACK_PREFIX, "confirm"),
        (CANCEL_DRAFT_CALLBACK_PREFIX, "cancel"),
    ):
        if data.startswith(prefix):
            draft_id = data[len(prefix) :]
            if is_valid_draft_id(draft_id):
                return action, draft_id
            return None
    return None


def build_confirmation_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    """Клавиатура превью, привязанная к одному черновику (PM06-R2-001)."""
    if not is_valid_draft_id(draft_id):
        raise ValueError("некорректный идентификатор черновика")
    confirm = f"{CONFIRM_DRAFT_CALLBACK_PREFIX}{draft_id}"
    cancel = f"{CANCEL_DRAFT_CALLBACK_PREFIX}{draft_id}"
    assert len(confirm.encode("utf-8")) <= MAX_CALLBACK_LENGTH
    assert len(cancel.encode("utf-8")) <= MAX_CALLBACK_LENGTH
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(CONFIRM_BUTTON_TEXT, callback_data=confirm),
        InlineKeyboardButton(CANCEL_BUTTON_TEXT, callback_data=cancel),
    )
    return markup


def parse_cancel_callback(data: str) -> int | None:
    """Извлекает post_id из callback data ``cancel:<id>``. None при неверном формате."""
    if not data.startswith(CANCEL_CALLBACK_PREFIX):
        return None
    raw = data[len(CANCEL_CALLBACK_PREFIX) :]
    if not raw.isdigit() or int(raw) <= 0:
        return None
    return int(raw)


def parse_reschedule_callback(data: str) -> int | None:
    """Извлекает post_id из callback data ``resched:<id>``. None при неверном формате."""
    if not data.startswith(RESCHEDULE_CALLBACK_PREFIX):
        return None
    raw = data[len(RESCHEDULE_CALLBACK_PREFIX) :]
    if not raw.isdigit() or int(raw) <= 0:
        return None
    return int(raw)


def build_post_actions_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Inline-клавиатура с кнопками «Отменить» и «Перенести» для одного поста (PM-04 §3.1)."""
    cancel_data = f"{CANCEL_CALLBACK_PREFIX}{post_id}"
    resched_data = f"{RESCHEDULE_CALLBACK_PREFIX}{post_id}"
    assert len(cancel_data) <= MAX_CALLBACK_LENGTH
    assert len(resched_data) <= MAX_CALLBACK_LENGTH
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(CANCEL_BUTTON_TEXT, callback_data=cancel_data),
        InlineKeyboardButton(RESCHEDULE_BUTTON_TEXT, callback_data=resched_data),
    )
    return markup
