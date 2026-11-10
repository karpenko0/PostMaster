"""Inline-callback handlers для отмены и переноса публикаций (PM-04).

Callback data:
- ``cancel:<post_id>`` — отмена PENDING-публикации через CancelService.
- ``resched:<post_id>`` — запрос новой даты/времени; FSM переводится в
  WAITING_RESCHEDULE_DATETIME, ID поста сохраняется в черновике.

Все проверки владельца и статуса выполняются в сервисном слое (CancelService,
RescheduleService). Handler НЕ доверяет callback data — он только передаёт ID.
Это защищает от подмены callback data (PM-04 §4.3, §5.3, §7).
"""

from __future__ import annotations

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import CallbackQuery

from postmaster.handlers.inline_keyboards import (
    parse_cancel_callback,
    parse_reschedule_callback,
)
from postmaster.services.cancel_service import (
    CancelService,
    PostNotFoundForUserError,
)
from postmaster.services.dialog_service import DialogService
from postmaster.services.user_service import UserService
from postmaster.utils.logging_config import (
    EVENT_COMMAND_FAILED,
    EVENT_POST_CANCEL_BUTTON,
    EVENT_POST_RESCHEDULE_BUTTON,
    log_event,
)

logger = logging.getLogger(__name__)

CANCEL_OK = "Публикация #{post_id} отменена."
CANCEL_NOT_FOUND = "Публикация недоступна для отмены."
CANCEL_FAILED = "Не удалось отменить публикацию. Попробуйте позже."
RESCHEDULE_PROMPT = "Укажи новую дату и время публикации в формате ДД.ММ.ГГГГ ЧЧ:ММ"
RESCHEDULE_NOT_FOUND = "Публикация недоступна для переноса."
RESCHEDULE_FAILED = "Не удалось начать перенос публикации. Попробуйте позже."


def register_post_actions_callbacks(
    bot: AsyncTeleBot,
    *,
    user_service: UserService,
    cancel_service: CancelService,
    dialog_service: DialogService,
) -> None:
    """Регистрирует inline-callback handlers (PM-04 §4, §5)."""

    @bot.callback_query_handler(
        func=lambda c: bool(c.data) and c.data.startswith("cancel:")
    )
    async def on_cancel(call: CallbackQuery) -> None:
        post_id = parse_cancel_callback(call.data or "")
        sender = call.from_user
        if sender is None or post_id is None or call.message is None:
            await bot.answer_callback_query(call.id)
            return
        try:
            user = await user_service.get_by_telegram_id(sender.id)
            if user is None:
                await _answer_and_notify(call, bot, CANCEL_NOT_FOUND)
                log_event(
                    logger,
                    EVENT_POST_CANCEL_BUTTON,
                    telegram_user_id=sender.id,
                    post_id=post_id,
                    result="not_found",
                )
                return
            await cancel_service.cancel(post_id=post_id, user_id=user.id)
        except PostNotFoundForUserError:
            await _answer_and_notify(call, bot, CANCEL_NOT_FOUND)
            log_event(
                logger,
                EVENT_POST_CANCEL_BUTTON,
                telegram_user_id=sender.id,
                post_id=post_id,
                result="not_found",
            )
            return
        except Exception:
            log_event(
                logger,
                EVENT_COMMAND_FAILED,
                level=logging.ERROR,
                command="cancel_button",
                post_id=post_id,
                telegram_user_id=sender.id,
            )
            logger.exception(
                "Cancel button failed post_id=%s telegram_user_id=%s",
                post_id,
                sender.id,
            )
            await _answer_and_notify(call, bot, CANCEL_FAILED)
            return

        log_event(
            logger,
            EVENT_POST_CANCEL_BUTTON,
            telegram_user_id=sender.id,
            post_id=post_id,
            result="ok",
        )
        await _answer_and_notify(call, bot, CANCEL_OK.format(post_id=post_id))

    @bot.callback_query_handler(
        func=lambda c: bool(c.data) and c.data.startswith("resched:")
    )
    async def on_reschedule(call: CallbackQuery) -> None:
        post_id = parse_reschedule_callback(call.data or "")
        sender = call.from_user
        if sender is None or post_id is None or call.message is None:
            await bot.answer_callback_query(call.id)
            return
        user = await user_service.get_by_telegram_id(sender.id)
        if user is None:
            await _answer_and_notify(call, bot, RESCHEDULE_NOT_FOUND)
            log_event(
                logger,
                EVENT_POST_RESCHEDULE_BUTTON,
                telegram_user_id=sender.id,
                post_id=post_id,
                result="not_found",
            )
            return

        try:
            await dialog_service.start_reschedule(
                sender.id, call.message.chat.id, post_id
            )
        except Exception:
            log_event(
                logger,
                EVENT_COMMAND_FAILED,
                level=logging.ERROR,
                command="reschedule_button",
                post_id=post_id,
                telegram_user_id=sender.id,
            )
            logger.exception(
                "Reschedule button failed post_id=%s telegram_user_id=%s",
                post_id,
                sender.id,
            )
            await _answer_and_notify(call, bot, RESCHEDULE_FAILED)
            return

        log_event(
            logger,
            EVENT_POST_RESCHEDULE_BUTTON,
            telegram_user_id=sender.id,
            post_id=post_id,
            result="prompted",
        )
        await _answer_and_notify(call, bot, RESCHEDULE_PROMPT)


async def _answer_and_notify(
    call: CallbackQuery, bot: AsyncTeleBot, text: str
) -> None:
    """Отправляет новое сообщение и отвечает на callback (снимает «часики»)."""
    await bot.send_message(call.message.chat.id, text)
    await bot.answer_callback_query(call.id)
