"""Обработчики шагов сценария создания публикации (SPEC-003: US-01, AC-04, TC-02).

Обработчик передаёт сообщение в DialogService и отправляет текст, который соответствует исходу.
Тексты ответов находятся здесь, а не в сервисе (решение D12 плана SPEC-003). Обработчики
работают только в личных чатах (решение D14), поэтому в группе бот не отвечает на каждое сообщение.
"""

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message
from telebot.util import content_type_media

from postmaster.domain.dialog import DialogState
from postmaster.domain.schedule import DATETIME_EXAMPLE
from postmaster.services.dialog_service import DialogOutcome, DialogOutcomeKind, DialogService

DATE_PROMPT = (
    f"Укажите дату и время публикации в формате ДД.ММ.ГГГГ ЧЧ:ММ, например {DATETIME_EXAMPLE}."
)

# Подсказка для недопустимого сообщения: что ждёт бот на текущем шаге.
HINT_BY_STATE: dict[DialogState, str] = {
    DialogState.IDLE: "Чтобы создать публикацию, отправьте /start.",
    DialogState.WAITING_PHOTO: "Сейчас ждём фотографию. Отправьте изображение для публикации.",
    DialogState.WAITING_DATETIME: f"Сейчас ждём дату публикации. {DATE_PROMPT}",
}

# Все типы медиа, кроме текста и фото: они обрабатываются отдельно.
OTHER_CONTENT_TYPES = [kind for kind in content_type_media if kind not in ("text", "photo")]


def reply_text(outcome: DialogOutcome) -> str:
    """Текст ответа для исхода шага. Приветствие /start формирует обработчик команды."""
    match outcome.kind:
        case DialogOutcomeKind.PHOTO_ACCEPTED:
            return f"Фото принято. {DATE_PROMPT}"
        case DialogOutcomeKind.PHOTO_REPLACED:
            return f"Фото заменено. {DATE_PROMPT}"
        case DialogOutcomeKind.DATETIME_INVALID:
            return f"Не удалось распознать дату или она уже прошла. {DATE_PROMPT}"
        case DialogOutcomeKind.POST_CREATED:
            when = outcome.scheduled_at.strftime("%d.%m.%Y %H:%M") if outcome.scheduled_at else ""
            return (
                f"Публикация создана на {when} ({outcome.zone_name}). "
                "Чтобы создать новую, отправьте /start."
            )
        case DialogOutcomeKind.INVALID_MESSAGE:
            return HINT_BY_STATE[outcome.state]
        case _:
            raise ValueError(f"для исхода {outcome.kind} нет текста в обработчиках шагов")


def register_dialog_handlers(bot: AsyncTeleBot, dialog_service: DialogService) -> None:
    """Регистрирует обработчики фото, текста и прочих сообщений. Регистрировать после /start."""

    @bot.message_handler(content_types=["photo"], chat_types=["private"])
    async def on_photo(message: Message) -> None:
        sender = message.from_user
        if sender is None or not message.photo:
            return
        # Telegram присылает несколько размеров. Последний — самый большой.
        outcome = await dialog_service.receive_photo(
            sender.id, message.chat.id, message.photo[-1].file_id
        )
        await bot.send_message(message.chat.id, reply_text(outcome))

    @bot.message_handler(content_types=["text"], chat_types=["private"])
    async def on_text(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        outcome = await dialog_service.receive_text(sender.id, message.chat.id, message.text or "")
        await bot.send_message(message.chat.id, reply_text(outcome))

    @bot.message_handler(content_types=OTHER_CONTENT_TYPES, chat_types=["private"])
    async def on_other(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        outcome = await dialog_service.receive_other(sender.id, message.chat.id)
        await bot.send_message(message.chat.id, reply_text(outcome))
