"""Обработчики шагов сценария (SPEC-003: US-01, AC-04, TC-02; SPEC-004, SPEC-005, PM-04, PM-05).

Обработчик передаёт сообщение в сервисы и отправляет текст, который соответствует исходу.
Тексты ответов находятся здесь, а не в сервисе (решение D12 плана SPEC-003). Фото обрабатывается
по SPEC-004: метаданные извлекает PhotoService, черновик ведёт DialogService (§21). Обработчики
работают только в личных чатах (решение D14), поэтому в группе бот не отвечает на каждое сообщение.
PM-05 добавляет шаг предварительного просмотра: после валидной даты бот показывает preview
с inline-кнопками «✅ Подтвердить» / «❌ Отменить».
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

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

from postmaster.domain.dialog import DialogState, draft_token, token_matches
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
    DRAFT_ID_KEY,
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

# PM-06 / BUG-01: callback привязан к конкретному черновику.
# Формат: "pm05:<confirm|cancel>:<draft_token>".
# Telegram ограничивает callback_data 64 байтами, поэтому вместо UUID (36 символов)
# используется короткий токен из draft_id (первые CALLBACK_TOKEN_LENGTH символов).
CALLBACK_DATA_MAX = 64
CALLBACK_ACTION_CONFIRM = "confirm"
CALLBACK_ACTION_CANCEL = "cancel"
_CALLBACK_PATTERN = re.compile(
    r"^pm05:(?P<action>confirm|cancel)(?::(?P<token>[A-Za-z0-9_-]{1,32}))?$"
)

# Максимальная длина подписи в превью; полный текст сохраняется в draft и Post.
PREVIEW_CAPTION_MAX = 80

# Ограничение Telegram на caption сообщения с фотографией и на текст сообщения.
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_TEXT_LIMIT = 4096

# Сколько раз пробуем отправить превью. Без бесконечных повторов (PM-06 / BUG-03).
PREVIEW_SEND_ATTEMPTS = 2
PREVIEW_RETRY_DELAY = 0.2

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


# Символы разметки Telegram legacy Markdown, которые обязательно экранируются.
# legacy Markdown поддерживает только *_`[ — остальные символы не являются разметкой
# и экранировать их нельзя, иначе в тексте появятся лишние обратные слэши.
MARKDOWN_ESCAPED_CHARS = frozenset({"_", "*", "`", "["})


def escape_markdown(text: str) -> str:
    """Экранирует пользовательский текст для parse_mode="Markdown" (PM-06 / BUG-03).

    Telegram legacy Markdown использует ``_ * ` [`` как разметку. Без экранирования
    подпись вида ``__bold__`` или ``[ссылка]`` ломает разбор и Telegram отклоняет
    сообщение целиком (``can't parse entities``). Экранирование затрагивает только
    управляющие символы: Unicode и эмодзи остаются без изменений.
    """
    if not text:
        return ""
    result = []
    for char in text:
        if char in MARKDOWN_ESCAPED_CHARS:
            result.append("\\")
        result.append(char)
    return "".join(result)


def build_confirmation_callback_data(action: str, draft_id: str | None) -> str:
    """Собирает callback_data ``pm05:<action>:<token>`` и проверяет лимит 64 байта."""
    token = draft_token(draft_id)
    data = f"pm05:{action}" if not token else f"pm05:{action}:{token}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_MAX:  # pragma: no cover — защита от регресса
        raise ValueError("callback_data превышает лимит Telegram в 64 байта")
    return data


def parse_confirmation_callback(data: str | None) -> tuple[str, str | None] | None:
    """Разбирает callback_data кнопок превью.

    Возвращает ``(action, token)`` или None для неизвестного/испорченного значения.
    Отсутствующий токен допустим для совместимости со старыми сообщениями: такой
    callback считается устаревшим при обработке.
    """
    if not data:
        return None
    match = _CALLBACK_PATTERN.match(data)
    if match is None:
        return None
    return match.group("action"), match.group("token")


def _build_confirmation_keyboard(draft_id: str | None = None) -> InlineKeyboardMarkup:
    """Inline-клавиатура PM-05: «✅ Подтвердить» / «❌ Отменить».

    PM-06 / BUG-01: callback_data содержит токен черновика, поэтому кнопки старого
    превью не могут подтвердить или отменить новый черновик.
    """
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(
            CONFIRM_BUTTON_TEXT,
            callback_data=build_confirmation_callback_data(CALLBACK_ACTION_CONFIRM, draft_id),
        ),
        InlineKeyboardButton(
            CANCEL_BUTTON_TEXT,
            callback_data=build_confirmation_callback_data(CALLBACK_ACTION_CANCEL, draft_id),
        ),
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
    escape: bool = True,
) -> str:
    """Текст превью публикации для PM-05 §4 (PM-06 / BUG-03).

    Канал отображается только для TELEGRAM. Подпись отображается, если задана
    (сокращается для удобства, но полностью сохраняется в черновике).

    Пользовательские значения (подпись, канал) экранируются, если ``escape`` истинно:
    иначе символы ``_ * ` [`` ломают parse_mode="Markdown", и Telegram отклоняет
    сообщение целиком. Строки, которые формирует бот, разметку содержат осознанно.
    """
    if publication_type == PublicationType.REMINDER.value:
        type_line = "Тип: 🔔 Напоминание"
    else:
        type_line = "Тип: 📢 Публикация в Telegram"

    def user_text(value: str | None) -> str:
        if not value:
            return ""
        return escape_markdown(value) if escape else value

    lines = [
        "*Проверь публикацию перед сохранением*",
        f"• {type_line}",
    ]
    if publication_type == PublicationType.TELEGRAM.value and target_id:
        lines.append(f"• Канал: {user_text(target_id)}")
    lines.append(f"• Дата: {local_date}")
    lines.append(f"• Время: {local_time}")
    lines.append(f"• Часовой пояс: {timezone}")
    if caption:
        lines.append(f"• Подпись: {user_text(_truncate_caption(caption))}")
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


def _fit_telegram_limit(text: str, limit: int) -> str:
    """Обрезает текст без разрыва суррогатных пар: Telegram отклоняет битый UTF-8."""
    if len(text) <= limit:
        return text
    cut = text[: max(1, limit - 1)]
    # Не оставляем «половину» суррогатной пары (возможно при нестандартных строках).
    if cut and 0xD800 <= ord(cut[-1]) <= 0xDBFF:
        cut = cut[:-1]
    return cut.rstrip() + "…"


def _render_preview_for_caption(
    draft: dict[str, str],
    scheduled_at,
    zone_name: str,
) -> str:
    """Превью, гарантированно помещающееся в caption фото (1024 символа)."""
    return _fit_telegram_limit(
        _render_preview(draft, scheduled_at, zone_name), TELEGRAM_CAPTION_LIMIT
    )


def _render_preview_for_message(
    draft: dict[str, str],
    scheduled_at,
    zone_name: str,
) -> str:
    """Превью, помещающееся в обычное сообщение (4096 символов)."""
    return _fit_telegram_limit(_render_preview(draft, scheduled_at, zone_name), TELEGRAM_TEXT_LIMIT)


def _render_preview_plain(
    draft: dict[str, str],
    scheduled_at,
    zone_name: str,
) -> str:
    """Превью без экранирования Markdown — fallback, если разметка не разобралась."""
    return _format_preview_text(
        publication_type=draft.get(PUBLICATION_TYPE_KEY, PublicationType.REMINDER.value),
        target_id=draft.get(TARGET_ID_KEY) or None,
        local_date=f"{scheduled_at.day:02d}.{scheduled_at.month:02d}.{scheduled_at.year:04d}",
        local_time=f"{scheduled_at.hour:02d}:{scheduled_at.minute:02d}",
        timezone=zone_name,
        caption=draft.get(CAPTION_KEY),
        escape=False,
    )


PREVIEW_FAILED_TEXT = (
    "⚠️ Не удалось показать превью: Telegram временно недоступен.\n\n"
    "Черновик сохранён — отправь дату и время ещё раз, чтобы повторить."
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
        case DialogOutcomeKind.STALE_CALLBACK:
            # PM-06 / BUG-01, BUG-06: кнопка от предыдущего превью.
            return (
                "⏳ Эта кнопка устарела: она относится к предыдущему черновику.\n\n"
                "Текущий сценарий не изменён. Используй кнопки под последним превью "
                "или отправь /start."
            )
        case DialogOutcomeKind.ALREADY_CONFIRMED:
            return (
                "✅ Эта публикация уже создана.\n\n"
                "Отменить или перенести её можно командой /my_posts."
            )
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
        case DialogOutcomeKind.RESCHEDULE_CONFLICT:
            return (
                "⚠️ Сейчас идёт другой сценарий, поэтому перенос не запущен.\n\n"
                "Данные не потеряны: заверши текущий шаг и нажми «Перенести» ещё раз."
            )
        case _:
            raise ValueError(f"для исхода {outcome.kind} нет текста в обработчиках шагов")


async def _send_preview(
    bot: AsyncTeleBot,
    *,
    chat_id: int,
    draft: dict[str, str],
    scheduled_at,
    zone_name: str,
    draft_id: str | None,
) -> bool:
    """Отправляет превью с inline-кнопками. True — превью доставлено (PM-06 / BUG-03).

    Порядок попыток (каждая ограничена PREVIEW_SEND_ATTEMPTS, без бесконечных повторов):

    1. фото + caption с Markdown;
    2. фото + caption без parse_mode — если Telegram не разобрал разметку;
    3. то же самое ещё раз при временной сетевой ошибке;
    4. текстовое сообщение с Markdown, затем без Markdown.

    Черновик при неудаче не изменяется: пользователь получает PREVIEW_FAILED_TEXT и может
    повторить ввод даты. Исключения не пробрасываются в обработчик.
    """
    if scheduled_at is None:
        logger.error("Preview without scheduled_at chat_id=%s", chat_id)
        return False
    file_id = draft.get(PHOTO_FILE_ID_KEY)
    keyboard = _build_confirmation_keyboard(draft_id)
    markdown_text = (
        _render_preview_for_caption(draft, scheduled_at, zone_name)
        if file_id
        else _render_preview_for_message(draft, scheduled_at, zone_name)
    )
    plain_text = _render_preview_plain(draft, scheduled_at, zone_name)

    async def attempt(send, **kwargs) -> bool:
        for number in range(1, PREVIEW_SEND_ATTEMPTS + 1):
            try:
                await send(**kwargs)
                return True
            except Exception:
                logger.warning(
                    "Preview send attempt failed chat_id=%s attempt=%s/%s",
                    chat_id,
                    number,
                    PREVIEW_SEND_ATTEMPTS,
                    exc_info=number == PREVIEW_SEND_ATTEMPTS,
                )
                if number < PREVIEW_SEND_ATTEMPTS:
                    await asyncio.sleep(PREVIEW_RETRY_DELAY)
        return False

    if file_id:
        if await attempt(
            bot.send_photo,
            chat_id=chat_id,
            photo=file_id,
            caption=markdown_text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        ):
            return True
        # file_id мог устареть или разметка не разобралась: повторяем без parse_mode.
        if await attempt(
            bot.send_photo,
            chat_id=chat_id,
            photo=file_id,
            caption=plain_text,
            reply_markup=keyboard,
            parse_mode=None,
        ):
            return True
        logger.warning("Preview photo failed, falling back to text chat_id=%s", chat_id)

    if await attempt(
        bot.send_message,
        chat_id=chat_id,
        text=markdown_text,
        reply_markup=keyboard,
        parse_mode="Markdown",
    ):
        return True
    return await attempt(
        bot.send_message,
        chat_id=chat_id,
        text=plain_text,
        reply_markup=keyboard,
        parse_mode=None,
    )


# Исходы, после которых черновик завершён и кнопки превью больше не нужны.
_TERMINAL_CALLBACK_OUTCOMES = frozenset(
    {
        DialogOutcomeKind.CONFIRMATION_CANCELLED,
        DialogOutcomeKind.POST_SAVED_UNSCHEDULED,
    }
)

CALLBACK_ERROR_TEXT = (
    "⚠️ Не удалось обработать действие из-за временной ошибки. "
    "Кнопки остались активными — нажми ещё раз."
)
CALLBACK_RETRY_TEXT = "Временная ошибка, попробуй ещё раз"


def _callback_toast(outcome: DialogOutcome) -> str:
    """Короткий ответ на callback (снимает «часики» и объясняет результат)."""
    match outcome.kind:
        case DialogOutcomeKind.POST_CREATED:
            return "Запланировано"
        case DialogOutcomeKind.POST_SAVED_UNSCHEDULED:
            return "Сохранено, восстановим позже"
        case DialogOutcomeKind.STALE_CALLBACK:
            return "Кнопка устарела"
        case DialogOutcomeKind.ALREADY_CONFIRMED:
            return "Публикация уже создана"
        case DialogOutcomeKind.CONFIRMATION_CANCELLED:
            return "Отменено"
        case _:
            return "Не удалось выполнить действие"


async def _safe_answer(bot: AsyncTeleBot, callback_id: str, text: str | None = None) -> None:
    """answerCallbackQuery, который не роняет обработчик при ошибке Telegram."""
    try:
        if text:
            await bot.answer_callback_query(callback_id, text=text)
        else:
            await bot.answer_callback_query(callback_id)
    except Exception:
        logger.debug("answer_callback_query failed", exc_info=True)


async def _remove_keyboard(bot: AsyncTeleBot, chat_id: int, message_id: int) -> None:
    """Убирает inline-клавиатуру. Ошибка не влияет на результат операции."""
    try:
        await bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
    except Exception:
        logger.debug("edit_message_reply_markup failed", exc_info=True)


async def _safe_send(bot: AsyncTeleBot, chat_id: int, text: str) -> None:
    """Сообщение пользователю с ограниченным числом попыток и без разметки."""
    for number in range(1, PREVIEW_SEND_ATTEMPTS + 1):
        try:
            await bot.send_message(chat_id, text)
            return
        except Exception:
            logger.warning(
                "send_message failed chat_id=%s attempt=%s",
                chat_id,
                number,
                exc_info=number == PREVIEW_SEND_ATTEMPTS,
            )
            if number < PREVIEW_SEND_ATTEMPTS:
                await asyncio.sleep(PREVIEW_RETRY_DELAY)


def register_dialog_handlers(
    bot: AsyncTeleBot,
    dialog_service: DialogService,
    photo_service: PhotoService,
    *,
    on_post_created: Callable[[str], Awaitable[None]] | None = None,
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
            # PM-05: показать превью с фото и inline-кнопками, привязанными к черновику.
            draft = outcome.draft or await dialog_service._store.get_draft(
                sender.id, message.chat.id
            )
            sent = await _send_preview(
                bot,
                chat_id=message.chat.id,
                draft=draft,
                scheduled_at=outcome.scheduled_at,
                zone_name=outcome.zone_name or "",
                draft_id=outcome.draft_id or draft.get(DRAFT_ID_KEY),
            )
            if not sent:
                # PM-06 / BUG-03: черновик не потерян, пользователю дан понятный ответ.
                await bot.send_message(message.chat.id, PREVIEW_FAILED_TEXT)
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

    @bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("pm05:"))
    async def on_confirmation_callback(call: CallbackQuery) -> None:
        """PM-05 §5, §6, PM-06 / BUG-01, BUG-06: кнопки «Подтвердить» и «Отменить».

        Порядок обработки:

        1. callback_data разбирается на действие и токен черновика; неизвестный формат
           получает информативный ответ без изменения состояния;
        2. сервис сверяет токен с текущим черновиком: устаревшая кнопка не может
           подтвердить/отменить новый черновик и не удаляет активный черновик;
        3. клавиатура удаляется только после успешного завершения операции — при
           восстановимой ошибке пользователь может нажать кнопку ещё раз.
        """
        parsed = parse_confirmation_callback(call.data)
        if parsed is None or call.from_user is None or call.message is None:
            await _safe_answer(bot, call.id)
            return
        action, token = parsed
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        message_id = call.message.message_id

        draft = await dialog_service._store.get_draft(user_id, chat_id)
        current_draft_id = draft.get(DRAFT_ID_KEY)
        # Полный draft_id для сервиса: токен однозначно определяет черновик.
        callback_draft_id = (
            current_draft_id
            if token_matches(current_draft_id, token)
            else (f"{token}" if token else None)
        )

        try:
            if action == CALLBACK_ACTION_CONFIRM:
                outcome = await dialog_service.confirm_post(
                    user_id, chat_id, draft_id=callback_draft_id
                )
            else:
                outcome = await dialog_service.cancel_creation(
                    user_id, chat_id, draft_id=callback_draft_id
                )
        except Exception:
            # PM-06 / BUG-06: восстановимая ошибка не должна оставлять пользователя
            # без кнопок — клавиатуру сохраняем, повторное нажатие безопасно.
            logger.exception("Confirmation callback failed user_id=%s action=%s", user_id, action)
            await _safe_answer(bot, call.id, CALLBACK_RETRY_TEXT)
            await _safe_send(bot, chat_id, CALLBACK_ERROR_TEXT)
            return

        text = reply_text(outcome)

        if outcome.kind in _TERMINAL_CALLBACK_OUTCOMES:
            await _remove_keyboard(bot, chat_id, message_id)
        elif outcome.kind == DialogOutcomeKind.POST_CREATED:
            # Повторное нажатие: пост уже создан, кнопки больше не нужны.
            await _remove_keyboard(bot, chat_id, message_id)

        await _safe_send(bot, chat_id, text)
        await _safe_answer(bot, call.id, _callback_toast(outcome))
