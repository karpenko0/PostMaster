"""Обработчики шагов сценария (SPEC-003: US-01, AC-04, TC-02; SPEC-004, SPEC-005).

Обработчик передаёт сообщение в сервисы и отправляет текст, который соответствует исходу.
Тексты ответов находятся здесь, а не в сервисе (решение D12 плана SPEC-003). Фото обрабатывается
по SPEC-004: метаданные извлекает PhotoService, черновик ведёт DialogService (§21). Обработчики
работают только в личных чатах (решение D14), поэтому в группе бот не отвечает на каждое сообщение.
"""

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message
from telebot.util import content_type_media

from postmaster.domain.dialog import DialogState
from postmaster.domain.photo import PhotoProcessingError
from postmaster.domain.post import PublicationType
from postmaster.domain.schedule import DATETIME_EXAMPLE, DateTimeErrorCode
from postmaster.domain.timezone import convert_utc_to_local
from postmaster.services.dialog_service import DialogOutcome, DialogOutcomeKind, DialogService
from postmaster.services.photo_service import PhotoService

logger = logging.getLogger(__name__)

DATE_PROMPT = (
    f"Укажите дату и время публикации в формате ДД.ММ.ГГГГ ЧЧ:ММ, например {DATETIME_EXAMPLE}."
)

# Ответ после принятия фото — точный текст §25 SPEC-004 (US-004-01, AC-004-07).
PHOTO_ACCEPTED_TEXT = (
    "📸 Фото получил!\n\n"
    "Теперь укажи дату и время публикации.\n\n"
    "Формат:\n\n"
    "ДД.ММ.ГГГГ ЧЧ:ММ\n\n"
    "Например:\n"
    "07.10.2026 15:00"
)

# Точные подсказки §26 и §27 SPEC-005: формат и прошедший момент.
INVALID_FORMAT_TEXT = (
    "⚠️ Я пока не понимаю такой формат.\n\nНапиши дату и время так:\n\n07.10.2026 17:00"
)
PAST_DATETIME_TEXT = (
    "⚠️ Это время уже прошло.\n\nУкажи дату и время в будущем.\n\nНапример:\n08.10.2026 15:00"
)

# Ответ на сообщение вместо фото и на ошибку обработки фото — §26 и ERR-004-01 SPEC-004.
PHOTO_REQUEST = "Отправь фотографию, которую нужно опубликовать."

# Подсказка для недопустимого сообщения: что ждёт бот на текущем шаге.
HINT_BY_STATE: dict[DialogState, str] = {
    DialogState.IDLE: "Чтобы создать публикацию, отправьте /start.",
    DialogState.WAITING_PHOTO: PHOTO_REQUEST,
    DialogState.WAITING_DATETIME: f"Сейчас ждём дату публикации. {DATE_PROMPT}",
}

_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

# Все типы медиа, кроме текста и фото: они обрабатываются отдельно.
OTHER_CONTENT_TYPES = [kind for kind in content_type_media if kind not in ("text", "photo")]


def reply_text(outcome: DialogOutcome) -> str:
    """Текст ответа для исхода шага. Приветствие /start формирует обработчик команды."""
    match outcome.kind:
        case DialogOutcomeKind.PHOTO_ACCEPTED:
            return PHOTO_ACCEPTED_TEXT
        case DialogOutcomeKind.PHOTO_REPLACED:
            return f"Фото заменено. {DATE_PROMPT}"
        case DialogOutcomeKind.DATETIME_INVALID:
            return (
                PAST_DATETIME_TEXT
                if outcome.error_code == DateTimeErrorCode.PAST_DATETIME
                else INVALID_FORMAT_TEXT
            )
        case DialogOutcomeKind.POST_CREATED:
            if outcome.post is None:
                raise ValueError("подтверждение без сохранённого Post")
            local = convert_utc_to_local(outcome.post.scheduled_at_utc, outcome.post.timezone)
            date = f"{local.day} {_MONTHS[local.month - 1]} {local.year}"
            clock = local.strftime("%H:%M")
            if outcome.post.publication_type == PublicationType.REMINDER:
                return (
                    "✅ Принято!\n\nФото запланировано:\n"
                    f"📅 {date}\n⏰ {clock}\n\nЯ напомню тебе точно в это время."
                )
            return (
                "✅ Запланировано!\n\n"
                f"📅 {date}\n⏰ {clock}\n\n"
                "Фото будет автоматически опубликовано в канале."
            )
        case DialogOutcomeKind.POST_SAVED_UNSCHEDULED:
            return (
                "⚠️ Публикация сохранена, но пока не поставлена в расписание. "
                "Повторно создавать её не нужно; задача будет восстановлена позже."
            )
        case DialogOutcomeKind.POST_FAILED:
            return "⚠️ Не удалось сохранить публикацию. Повтори дату позже; фото сохранено."
        case DialogOutcomeKind.INVALID_MESSAGE:
            return HINT_BY_STATE[outcome.state]
        case _:
            raise ValueError(f"для исхода {outcome.kind} нет текста в обработчиках шагов")


def register_dialog_handlers(
    bot: AsyncTeleBot,
    dialog_service: DialogService,
    photo_service: PhotoService,
) -> None:
    """Регистрирует обработчики фото, текста и прочих сообщений. Регистрировать после /start."""

    @bot.message_handler(content_types=["photo"], chat_types=["private"])
    async def on_photo(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        # message.photo — массив PhotoSize; максимальный размер и метаданные выбирает сервис
        # (BR-004-04, §22). Фото скачивать не нужно (BR-004-03). Подпись передаётся отдельным
        # полем message.caption (SPEC-020).
        try:
            photo = photo_service.handle_photo(
                sender.id, list(message.photo or []), caption=message.caption
            )
        except PhotoProcessingError:
            # ERR-004-01…ERR-004-03: FSM не меняется, пользователь получает запрос фотографии.
            await bot.send_message(message.chat.id, PHOTO_REQUEST)
            return
        outcome = await dialog_service.receive_photo(sender.id, message.chat.id, photo)
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
