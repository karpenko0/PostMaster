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

CANCEL_CALLBACK_PREFIX = "cancel:"
RESCHEDULE_CALLBACK_PREFIX = "resched:"

CANCEL_BUTTON_TEXT = "❌ Отменить"
RESCHEDULE_BUTTON_TEXT = "🕒 Перенести"

MAX_CALLBACK_LENGTH = 64


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
