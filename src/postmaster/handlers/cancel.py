"""Обработчик команды /cancel (SPEC-018)."""

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from postmaster.services.post_cancellation_service import CancelOutcome, PostCancellationService

logger = logging.getLogger(__name__)

POST_ID_REQUIRED_TEXT = "Укажите ID публикации.\n\nПример:\n/cancel 42"
INVALID_POST_ID_TEXT = "Некорректный ID публикации.\nПример: /cancel 42"
USER_NOT_FOUND_TEXT = "Не удалось найти данные пользователя. Используйте /start."


def parse_post_id(text: str | None) -> tuple[int | None, str | None]:
    """Извлекает один положительный целочисленный ID из команды."""
    parts = (text or "").split()
    if len(parts) < 2:
        return None, POST_ID_REQUIRED_TEXT
    if len(parts) != 2:
        return None, INVALID_POST_ID_TEXT
    raw_id = parts[1]
    if not raw_id.isascii() or not raw_id.isdecimal():
        return None, INVALID_POST_ID_TEXT
    post_id = int(raw_id)
    if post_id <= 0:
        return None, INVALID_POST_ID_TEXT
    return post_id, None


def _response_for_outcome(outcome: CancelOutcome, post_id: int) -> str:
    match outcome:
        case CancelOutcome.CANCELLED:
            return f"✅ Публикация #{post_id} отменена."
        case CancelOutcome.USER_NOT_FOUND:
            return USER_NOT_FOUND_TEXT
        case CancelOutcome.PROCESSING:
            return "Эта публикация уже выполняется и не может быть отменена."
        case CancelOutcome.PUBLISHED:
            return "Эта публикация уже выполнена."
        case CancelOutcome.FAILED:
            return "Эта публикация завершилась с ошибкой и не может быть отменена."
        case CancelOutcome.ALREADY_CANCELLED:
            return "Эта публикация уже отменена."
        case _:
            return (
                f"Не удалось отменить публикацию #{post_id}.\n"
                "Проверьте ID и убедитесь, что публикация ещё ожидает выполнения."
            )


def register_cancel_handler(
    bot: AsyncTeleBot,
    cancellation_service: PostCancellationService,
) -> None:
    """Регистрирует /cancel в личном чате."""

    @bot.message_handler(commands=["cancel"], chat_types=["private"])
    async def handle_cancel(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        post_id, parse_error = parse_post_id(message.text)
        if parse_error is not None:
            await bot.send_message(message.chat.id, parse_error)
            return
        assert post_id is not None
        try:
            outcome = await cancellation_service.cancel(sender.id, post_id)
        except Exception as exc:
            logger.error(
                "Post cancel failed telegram_user_id=%s post_id=%s error_type=%s",
                sender.id,
                post_id,
                type(exc).__name__,
            )
            outcome = CancelOutcome.NOT_CANCELLABLE
        await bot.send_message(message.chat.id, _response_for_outcome(outcome, post_id))
