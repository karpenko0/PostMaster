"""Обработчики шагов сценария (SPEC-003: US-01, AC-04, TC-02; SPEC-004, SPEC-005, PM-04, PM-05).

Обработчик передаёт сообщение в сервисы и отправляет текст, который соответствует исходу.
Тексты ответов находятся здесь, а не в сервисе (решение D12 плана SPEC-003). Фото обрабатывается
по SPEC-004: метаданные извлекает PhotoService, черновик ведёт DialogService (§21). Обработчики
работают только в личных чатах (решение D14), поэтому в группе бот не отвечает на каждое сообщение.
PM-05 добавляет шаг предварительного просмотра: после валидной даты бот показывает preview
с inline-кнопками «✅ Подтвердить» / «❌ Отменить».
"""

import logging

from telebot.async_telebot import AsyncTeleBot
from telebot.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telebot.util import content_type_media

from postmaster.domain.dialog import DialogState
from postmaster.domain.photo import PhotoProcessingError
from postmaster.domain.post import PublicationType
from postmaster.domain.schedule import DATETIME_EXAMPLE, DateTimeErrorCode
from postmaster.domain.timezone import convert_utc_to_local
from postmaster.handlers.inline_keyboards import (
    CANCEL_BUTTON_TEXT,
    CONFIRM_BUTTON_TEXT,
)
from postmaster.services.dialog_service import (
    CAPTION_KEY,
    PHOTO_FILE_ID_KEY,
    PUBLICATION_TYPE_KEY,
    REMINDER_BUTTON,
    TARGET_ID_KEY,
    TELEGRAM_BUTTON,
    DialogOutcome,
    DialogOutcomeKind,
    DialogService,
)
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

# PM-03: приглашение выбрать тип публикации. Используется и как подсказка состояния.
PUBLICATION_TYPE_PROMPT_TEXT = "Как опубликовать этот пост?"

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
    DialogState.WAITING_PUBLICATION_TYPE: PUBLICATION_TYPE_PROMPT_TEXT,
    DialogState.WAITING_TARGET: "Укажите Telegram-канал для публикации.",
    DialogState.WAITING_DATETIME: f"Сейчас ждём дату публикации. {DATE_PROMPT}",
    DialogState.WAITING_CONFIRMATION: ("Подтвердите параметры публикации кнопками под превью."),
}

# Префиксы callback data для PM-05 (PM05 §5.1, §6.1).
CONFIRM_CALLBACK_DATA = "pm05:confirm"
CANCEL_CALLBACK_DATA = "pm05:cancel"

# Максимальная длина подписи в превью; полный текст сохраняется в draft и Post.
PREVIEW_CAPTION_MAX = 80

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


def _build_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Inline-клавиатура PM-05: «✅ Подтвердить» / «❌ Отменить»."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(CONFIRM_BUTTON_TEXT, callback_data=CONFIRM_CALLBACK_DATA),
        InlineKeyboardButton(CANCEL_BUTTON_TEXT, callback_data=CANCEL_CALLBACK_DATA),
    )
    return markup


def _truncate_caption(text: str, limit: int = PREVIEW_CAPTION_MAX) -> str:
    """Сокращает подпись с многоточием для превью. Полный текст остаётся в draft."""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _format_preview_text(
    *,
    publication_type: str,
    target_id: str | None,
    local_date: str,
    local_time: str,
    timezone: str,
    caption: str | None,
) -> str:
    """Текст превью публикации для PM-05 §4.

    Канал отображается только для TELEGRAM. Подпись отображается, если задана
    (сокращается для удобства, но полностью сохраняется в черновике).
    """
    if publication_type == PublicationType.REMINDER.value:
        type_line = "Тип: 🔔 Напоминание"
    else:
        type_line = "Тип: 📢 Публикация в Telegram"

    lines = [
        "**Проверь публикацию перед сохранением**",
        f"- {type_line}",
    ]
    if publication_type == PublicationType.TELEGRAM.value and target_id:
        lines.append(f"- Канал: {target_id}")
    lines.append(f"- Дата: {local_date}")
    lines.append(f"- Время: {local_time}")
    lines.append(f"- Часовой пояс: {timezone}")
    if caption:
        lines.append(f"- Подпись: {_truncate_caption(caption)}")
    return "\n".join(lines)


def _render_preview(
    draft: dict[str, str],
    scheduled_at,
    zone_name: str,
) -> str:
    """Собирает текст превью из черновика и распарсенного времени.

    scheduled_at — локальное время пользователя (уже в его поясе), поэтому
    конвертация из UTC не требуется: форматируем как есть.
    """
    local_date = f"{scheduled_at.day:02d}.{scheduled_at.month:02d}.{scheduled_at.year:04d}"
    local_time = f"{scheduled_at.hour:02d}:{scheduled_at.minute:02d}"
    return _format_preview_text(
        publication_type=draft.get(PUBLICATION_TYPE_KEY, PublicationType.REMINDER.value),
        target_id=draft.get(TARGET_ID_KEY) or None,
        local_date=local_date,
        local_time=local_time,
        timezone=zone_name,
        caption=draft.get(CAPTION_KEY),
    )


def reply_text(outcome: DialogOutcome) -> str:
    """Текст ответа для исхода шага. Приветствие /start формирует обработчик команды."""
    match outcome.kind:
        case DialogOutcomeKind.PHOTO_ACCEPTED:
            return PUBLICATION_TYPE_PROMPT_TEXT
        case DialogOutcomeKind.PHOTO_REPLACED:
            return f"Фото заменено. {HINT_BY_STATE[outcome.state]}"
        case DialogOutcomeKind.TYPE_SELECTED:
            return DATE_PROMPT
        case DialogOutcomeKind.TARGET_REQUESTED:
            return "Укажите Telegram-канал для публикации."
        case DialogOutcomeKind.TARGET_INVALID:
            return "Некорректный идентификатор. Укажите @channel_name или числовой ID канала."
        case DialogOutcomeKind.TARGET_FORBIDDEN:
            return "Этот канал не входит в список разрешённых. Укажите другой канал."
        case DialogOutcomeKind.DATETIME_INVALID:
            return (
                PAST_DATETIME_TEXT
                if outcome.error_code == DateTimeErrorCode.PAST_DATETIME
                else INVALID_FORMAT_TEXT
            )
        case DialogOutcomeKind.PREVIEW_SHOWN:
            # Текст превью собирается отдельно в on_text; здесь используется как fallback.
            return "Проверьте параметры публикации и нажмите «Подтвердить» или «Отменить»."
        case DialogOutcomeKind.POST_CREATED:
            post = outcome.post
            if post is None:
                # Повторный confirm: post не возвращается, есть только локальное время.
                if outcome.scheduled_at is None:
                    raise ValueError("POST_CREATED без post и без scheduled_at")
                local = outcome.scheduled_at
                date = f"{local.day} {_MONTHS[local.month - 1]} {local.year}"
                clock = local.strftime("%H:%M")
                return f"✅ Публикация уже запланирована!\n\n📅 {date}\n⏰ {clock}"
            local = convert_utc_to_local(post.scheduled_at_utc, post.timezone)
            date = f"{local.day} {_MONTHS[local.month - 1]} {local.year}"
            clock = local.strftime("%H:%M")
            if post.publication_type == PublicationType.REMINDER:
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
            return "⚠️ Не удалось сохранить публикацию. Попробуйте подтвердить ещё раз."
        case DialogOutcomeKind.CONFIRMATION_CANCELLED:
            return "Создание публикации отменено. Пост не был сохранён."
        case DialogOutcomeKind.INVALID_MESSAGE:
            return HINT_BY_STATE[outcome.state]
        case DialogOutcomeKind.RESCHEDULE_PROMPT:
            return DATE_PROMPT
        case DialogOutcomeKind.RESCHEDULE_DATETIME_INVALID:
            return (
                PAST_DATETIME_TEXT
                if outcome.error_code == DateTimeErrorCode.PAST_DATETIME
                else INVALID_FORMAT_TEXT
            )
        case DialogOutcomeKind.RESCHEDULE_OK:
            if outcome.scheduled_at is None:
                raise ValueError("подтверждение переноса без нового времени")
            local = outcome.scheduled_at
            date = f"{local.day:02d}.{local.month:02d}.{local.year:04d}"
            clock = f"{local.hour:02d}:{local.minute:02d}"
            post_id = outcome.post.id if outcome.post else 0
            return f"Публикация #{post_id} перенесена на {date} в {clock}."
        case DialogOutcomeKind.RESCHEDULE_REJECTED:
            return "Не удалось перенести публикацию. Она больше недоступна."
        case _:
            raise ValueError(f"для исхода {outcome.kind} нет текста в обработчиках шагов")


def register_dialog_handlers(
    bot: AsyncTeleBot,
    dialog_service: DialogService,
    photo_service: PhotoService,
) -> None:
    """Регистрирует обработчики фото, текста, прочих сообщений и callback PM-05."""

    @bot.message_handler(content_types=["photo"], chat_types=["private"])
    async def on_photo(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        try:
            photo = photo_service.handle_photo(
                sender.id, list(message.photo or []), caption=message.caption
            )
        except PhotoProcessingError:
            await bot.send_message(message.chat.id, PHOTO_REQUEST)
            return
        outcome = await dialog_service.receive_photo(sender.id, message.chat.id, photo)
        if outcome.state == DialogState.WAITING_PUBLICATION_TYPE:
            keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            keyboard.add(KeyboardButton(REMINDER_BUTTON), KeyboardButton(TELEGRAM_BUTTON))
            await bot.send_message(message.chat.id, reply_text(outcome), reply_markup=keyboard)
        else:
            await bot.send_message(message.chat.id, reply_text(outcome))

    @bot.message_handler(content_types=["text"], chat_types=["private"])
    async def on_text(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        outcome = await dialog_service.receive_text(sender.id, message.chat.id, message.text or "")
        if outcome.kind == DialogOutcomeKind.PREVIEW_SHOWN:
            # PM-05: показать превью с фото и inline-кнопками.
            draft = await dialog_service._store.get_draft(sender.id, message.chat.id)
            preview_text = _render_preview(
                draft,
                outcome.scheduled_at,
                outcome.zone_name or "",
            )
            file_id = draft.get(PHOTO_FILE_ID_KEY)
            keyboard = _build_confirmation_keyboard()
            try:
                if file_id:
                    await bot.send_photo(
                        message.chat.id,
                        file_id,
                        caption=preview_text,
                        reply_markup=keyboard,
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(
                        message.chat.id,
                        preview_text,
                        reply_markup=keyboard,
                        parse_mode="Markdown",
                    )
            except Exception:
                logger.exception("Failed to send preview chat_id=%s", message.chat.id)
                # Если file_id недоступен, fallback на текстовое сообщение.
                await bot.send_message(
                    message.chat.id,
                    preview_text,
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                )
            return
        if outcome.kind in (
            DialogOutcomeKind.TYPE_SELECTED,
            DialogOutcomeKind.TARGET_REQUESTED,
        ):
            await bot.send_message(
                message.chat.id,
                reply_text(outcome),
                reply_markup=ReplyKeyboardRemove(),
            )
        else:
            await bot.send_message(message.chat.id, reply_text(outcome))

    @bot.message_handler(content_types=OTHER_CONTENT_TYPES, chat_types=["private"])
    async def on_other(message: Message) -> None:
        sender = message.from_user
        if sender is None:
            return
        outcome = await dialog_service.receive_other(sender.id, message.chat.id)
        await bot.send_message(message.chat.id, reply_text(outcome))

    @bot.callback_query_handler(
        func=lambda c: (
            bool(c.data) and (c.data == CONFIRM_CALLBACK_DATA or c.data == CANCEL_CALLBACK_DATA)
        )
    )
    async def on_confirmation_callback(call: CallbackQuery) -> None:
        """PM-05 §5, §6: обработка кнопок «Подтвердить» и «Отменить»."""
        if call.from_user is None or call.message is None:
            await bot.answer_callback_query(call.id)
            return
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        if call.data == CONFIRM_CALLBACK_DATA:
            outcome = await dialog_service.confirm_post(user_id, chat_id)
            text = reply_text(outcome)
            try:
                await bot.edit_message_reply_markup(
                    chat_id, call.message.message_id, reply_markup=None
                )
            except Exception:
                logger.debug("edit_message_reply_markup failed", exc_info=True)
            await bot.send_message(chat_id, text)
            if outcome.kind == DialogOutcomeKind.POST_CREATED:
                await bot.answer_callback_query(call.id, text="Запланировано")
            elif outcome.kind == DialogOutcomeKind.POST_SAVED_UNSCHEDULED:
                await bot.answer_callback_query(call.id, text="Сохранено, восстановим позже")
            else:
                await bot.answer_callback_query(call.id, text="Ошибка")
            return

        # CANCEL_CALLBACK_DATA
        outcome = await dialog_service.cancel_creation(user_id, chat_id)
        text = reply_text(outcome)
        try:
            await bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except Exception:
            logger.debug("edit_message_reply_markup failed", exc_info=True)
        await bot.send_message(chat_id, text)
        await bot.answer_callback_query(call.id, text="Отменено")
