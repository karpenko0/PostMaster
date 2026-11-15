"""SPEC-033: E2E-тесты.

Полные сценарии от Telegram-ввода до DB/result через running_app с
локальным fake Telegram API (test double, §7). Проверки §8:

- Reminder E2E;
- Telegram E2E;
- /cancel;
- invalid datetime.

Каждый сценарий идёт через реальный ``Application`` (Long Polling
через fake API, настоящие services + repositories + SQLite + Scheduler).
Результаты проверяются по DB (``Post.status``) и по outgoing-сообщениям
fake Telegram API (sent_messages).
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fake_telegram import TEST_TOKEN
from harness import WAIT_SECONDS, db_path
from postmaster.app import Application
from postmaster.config import Settings
from postmaster.database.engine import create_session_factory
from postmaster.domain.dialog import DialogState
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.dialog_service import REMINDER_BUTTON, TELEGRAM_BUTTON

USER_TG = 99001
CHAT_ID = 99001
PHOTO_ID = "e2e_photo_id"


async def _wait_for_sent(fake_telegram, *, marker: str, timeout: float = 5.0) -> list[dict]:
    """Ждёт появления сообщения в fake_telegram.sent_messages, содержащего ``marker``."""
    elapsed = 0.0
    interval = 0.05
    while elapsed < timeout:
        for message in fake_telegram.sent_messages:
            text = message.get("text", "")
            if marker in text:
                return list(fake_telegram.sent_messages)
        await asyncio.sleep(interval)
        elapsed += interval
    raise AssertionError(
        f"сообщение с маркером {marker!r} не появилось за {timeout}s: "
        f"{[m.get('text') for m in fake_telegram.sent_messages]}"
    )


def _build_app(
    tmp_path: Path,
    *,
    post_mode: str = "REMINDER",
    target_id: str | None = None,
) -> Application:
    kwargs = {
        "bot_token": TEST_TOKEN,
        "database_url": f"sqlite+aiosqlite:///{db_path(tmp_path)}",
        "log_level": "INFO",
        "post_mode": post_mode,
    }
    if target_id is not None:
        kwargs["telegram_target_id"] = target_id
    settings = Settings(**kwargs)
    return Application.from_settings(settings)


async def _start_app(app: Application, fake_telegram) -> None:
    """Запускает app.run() как фоновую задачу и ждёт первого опроса fake Telegram."""
    fake_telegram.methods.clear()
    fake_telegram.update_delivered.clear()
    fake_telegram.get_updates_called.clear()
    run_task = asyncio.create_task(app.run())
    try:
        await asyncio.wait_for(fake_telegram.get_updates_called.wait(), timeout=WAIT_SECONDS)
    finally:
        app._e2e_run_task = run_task  # type: ignore[attr-defined]
    fake_telegram.update_delivered.clear()


async def _stop_app(app: Application, fake_telegram) -> None:
    await app.stop()
    run_task = getattr(app, "_e2e_run_task", None)
    if run_task is not None:
        await asyncio.wait_for(run_task, timeout=WAIT_SECONDS)
        delattr(app, "_e2e_run_task")


async def _send_and_await_update(fake_telegram, *, timeout: float = 3.0) -> None:
    """Ждёт доставки следующего update боту через Long Polling."""
    fake_telegram.update_delivered.clear()
    await asyncio.wait_for(fake_telegram.update_delivered.wait(), timeout=timeout)


@pytest.fixture
async def reminder_app(tmp_path: Path, fake_telegram) -> AsyncIterator[Application]:
    """Running app в режиме REMINDER (POST_MODE по умолчанию)."""
    app = _build_app(tmp_path)
    await _start_app(app, fake_telegram)
    try:
        yield app
    finally:
        await _stop_app(app, fake_telegram)


@pytest.fixture
async def telegram_app(tmp_path: Path, fake_telegram) -> AsyncIterator[Application]:
    """Running app в режиме TELEGRAM с заданным target_id."""
    app = _build_app(tmp_path, post_mode="TELEGRAM", target_id="-1001234567890")
    await _start_app(app, fake_telegram)
    try:
        yield app
    finally:
        await _stop_app(app, fake_telegram)


async def _choose_type_and_confirm(
    fake_telegram,
    app: Application,
    *,
    type_button: str,
    date_text: str | None = None,
    ask_target: bool = True,
    choose_type: bool = True,
) -> None:
    """Проходит шаги PM-03 (тип публикации) и PM-05 (превью → подтверждение).

    Без этих шагов диалог остаётся в WAITING_PUBLICATION_TYPE/WAITING_CONFIRMATION:
    публикация создаётся только после явного подтверждения. ``choose_type=False``
    нужен, когда тип уже выбран ранее (например, после ошибки формата даты).
    """
    if choose_type:
        await _wait_for_state(app, DialogState.WAITING_PUBLICATION_TYPE)
        await _await_reply(
            fake_telegram,
            action=lambda: fake_telegram.add_text_message(
                type_button, chat_id=CHAT_ID, user_id=USER_TG
            ),
        )
    if type_button == TELEGRAM_BUTTON and ask_target:
        await _wait_for_state(app, DialogState.WAITING_TARGET)
        await _await_reply(
            fake_telegram,
            action=lambda: fake_telegram.add_text_message(
                "-1001234567890", chat_id=CHAT_ID, user_id=USER_TG
            ),
        )
    if date_text is not None:
        await _wait_for_state(app, DialogState.WAITING_DATETIME)
        await _await_reply(
            fake_telegram,
            action=lambda: fake_telegram.add_text_message(
                date_text, chat_id=CHAT_ID, user_id=USER_TG
            ),
        )
    # Ждём превью (фото с подписью) и подтверждаем его inline-кнопкой.
    await _wait_for_preview(fake_telegram)
    await _wait_for_state(app, DialogState.WAITING_CONFIRMATION)
    await _await_reply(
        fake_telegram,
        action=lambda: fake_telegram.add_callback_query(
            "pm05:confirm", chat_id=CHAT_ID, user_id=USER_TG
        ),
    )


async def _wait_for_state(app: Application, state: DialogState, *, timeout: float = 5.0) -> None:
    """Ждёт, пока FSM пользователя перейдёт в ожидаемое состояние.

    Между отправкой сообщения и записью состояния в БД проходит время: без этой
    синхронизации следующий апдейт мог быть обработан ещё в старом состоянии.
    """
    elapsed = 0.0
    while elapsed < timeout:
        if await _current_state(app) == state:
            return
        await asyncio.sleep(0.02)
        elapsed += 0.02
    raise AssertionError(f"FSM не перешёл в {state}")


async def _current_state(app: Application) -> str | None:
    return await app._state_storage.get_state(CHAT_ID, CHAT_ID)


async def _wait_for_preview(fake_telegram, *, timeout: float = 5.0) -> dict:
    """Ждёт отправки превью (sendPhoto с подписью) в fake Telegram."""
    elapsed = 0.0
    while elapsed < timeout:
        if fake_telegram.sent_photos:
            return fake_telegram.sent_photos[-1]
        await asyncio.sleep(0.05)
        elapsed += 0.05
    raise AssertionError(
        "превью перед сохранением не появилось; "
        f"messages={[m.get('text') for m in fake_telegram.sent_messages]}; "
        f"photos={fake_telegram.sent_photos}"
    )


def _moscow_future(hours: int = 2) -> str:
    """Будущее время в формате приложения (DEFAULT_TIMEZONE=Europe/Moscow).

    Раньше здесь форматировался UTC-момент, и при поясе приложения Europe/Moscow
    дата оказывалась в прошлом — сценарий падал на валидации.
    """
    moscow = ZoneInfo("Europe/Moscow")
    return (
        (datetime.now(UTC) + timedelta(hours=hours)).astimezone(moscow).strftime("%d.%m.%Y %H:%M")
    )


def _texts_of(messages: list[dict]) -> list[str]:
    return [m.get("text", "") for m in messages]


# === Reminder E2E (§8) =====================================================


async def test_reminder_e2e_happy_path_creates_pending_post(reminder_app, fake_telegram) -> None:
    """/start → photo → datetime → пользователь зарегистрирован, пост в PENDING,
    задание в Scheduler; outgoing сообщения содержат подтверждение."""
    app = reminder_app

    # /start — пользователь регистрируется, бот присылает приветствие.
    fake_telegram.add_text_message("/start", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_sent(fake_telegram, marker="PostMaster")
    await _wait_for_state(app, DialogState.WAITING_PHOTO)

    # Фото — переход к выбору типа публикации (PM-03).
    fake_telegram.add_photo_message(file_id=PHOTO_ID, chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_sent(fake_telegram, marker="Как опубликовать этот пост?")

    # Будущая дата — превью (PM-05), затем подтверждение создаёт пост в Scheduler.
    # Используем Europe/Moscow (DEFAULT_TIMEZONE), поэтому формируем локальное время.
    import zoneinfo

    moscow = zoneinfo.ZoneInfo("Europe/Moscow")
    future_local = datetime(2099, 1, 1, 12, 0, tzinfo=moscow)
    await _choose_type_and_confirm(
        fake_telegram,
        app,
        type_button=REMINDER_BUTTON,
        date_text=future_local.strftime("%d.%m.%Y %H:%M"),
    )
    await _wait_for_sent(fake_telegram, marker="Принято")

    # Состояние пользователя и публикации в БД.
    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    posts = await PostRepository(factory).list_pending_by_user(user.id)
    assert len(posts) == 1
    post = posts[0]
    assert post.status == PostStatus.PENDING
    assert post.publication_type == PublicationType.REMINDER
    assert post.telegram_file_id == PHOTO_ID

    # Scheduler получил задание на publish.
    # Проверяем через _scheduler.running, так как APScheduler встроен в SchedulerService
    assert app._scheduler.running, "Scheduler не запущен"

    # В REMINDER режиме подтверждение содержит «напомню».
    confirmation_texts = [
        text for text in _texts_of(fake_telegram.sent_messages) if "Принято" in text
    ]
    assert confirmation_texts
    assert "напомню" in confirmation_texts[0]


# === Telegram E2E (§8) ====================================================


async def test_telegram_e2e_happy_path_saves_post_with_target(telegram_app, fake_telegram) -> None:
    """POST_MODE=TELEGRAM: /start → photo → datetime → пост создан с target_id,
    publication_type=TELEGRAM."""
    app = telegram_app

    fake_telegram.add_text_message("/start", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_state(app, DialogState.WAITING_PHOTO)

    fake_telegram.add_photo_message(file_id=PHOTO_ID, chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)

    # При явном выборе TELEGRAM бот запрашивает канал (POST_MODE влияет на превью).
    await _choose_type_and_confirm(
        fake_telegram,
        app,
        type_button=TELEGRAM_BUTTON,
        date_text=_moscow_future(3),
    )
    await _wait_for_sent(fake_telegram, marker="Запланировано")

    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    posts = await PostRepository(factory).list_pending_by_user(user.id)
    assert len(posts) == 1
    post = posts[0]
    assert post.publication_type == PublicationType.TELEGRAM
    assert post.target_id == "-1001234567890"
    assert post.status == PostStatus.PENDING


# === /cancel (§8) ========================================================


async def test_cancel_e2e_marks_post_cancelled_and_removes_scheduler_job(
    reminder_app, fake_telegram
) -> None:
    """Полный happy-path → /cancel → пост CANCELLED, задание удалено из Scheduler."""
    app = reminder_app

    fake_telegram.add_text_message("/start", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_state(app, DialogState.WAITING_PHOTO)

    fake_telegram.add_photo_message(file_id=PHOTO_ID, chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)

    await _choose_type_and_confirm(
        fake_telegram,
        app,
        type_button=REMINDER_BUTTON,
        date_text=_moscow_future(2),
    )
    await _wait_for_sent(fake_telegram, marker="Принято")

    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    post = (await PostRepository(factory).list_pending_by_user(user.id))[0]
    assert post.status == PostStatus.PENDING
    assert any(job.id == f"post:{post.id}" for job in app._scheduler._scheduler.get_jobs())

    fake_telegram.add_text_message(f"/cancel {post.id}", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_sent(fake_telegram, marker="отменена")

    persisted = await PostRepository(factory).get_by_id(post.id)
    assert persisted is not None
    assert persisted.status == PostStatus.CANCELLED
    assert not any(job.id == f"post:{post.id}" for job in app._scheduler._scheduler.get_jobs())


async def test_cancel_e2e_nonexistent_post_returns_error(reminder_app, fake_telegram) -> None:
    """/cancel с несуществующим post_id: сообщение об ошибке, ничего не падает."""
    app = reminder_app

    fake_telegram.add_text_message("/start", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)

    fake_telegram.add_text_message("/cancel 999999", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    # Пользователь существует, но пост не найден — нормальная ситуация.
    await _wait_for_sent(fake_telegram, marker="не найдена")

    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None


# === invalid datetime (§8) ===============================================


async def test_invalid_datetime_e2e_preserves_draft_and_returns_error(
    reminder_app, fake_telegram
) -> None:
    """Невалидная дата: ошибка пользователю, draft сохранён, пост НЕ создан."""
    app = reminder_app

    fake_telegram.add_text_message("/start", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_state(app, DialogState.WAITING_PHOTO)

    fake_telegram.add_photo_message(file_id=PHOTO_ID, chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    # PM-03: сначала тип публикации (ждём состояние до отправки следующего шага).
    await _wait_for_state(app, DialogState.WAITING_PUBLICATION_TYPE)
    await _await_reply(
        fake_telegram,
        action=lambda: fake_telegram.add_text_message(
            REMINDER_BUTTON, chat_id=CHAT_ID, user_id=USER_TG
        ),
    )
    await _wait_for_state(app, DialogState.WAITING_DATETIME)

    # Полностью невалидный формат: бот отвечает ошибкой, черновик сохраняется.
    await _await_reply(
        fake_telegram,
        action=lambda: fake_telegram.add_text_message(
            "вчера в обед", chat_id=CHAT_ID, user_id=USER_TG
        ),
    )
    await _wait_for_sent(fake_telegram, marker="не понимаю такой формат")

    # Пост НЕ создан.
    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    posts = await PostRepository(factory).list_pending_by_user(user.id)
    assert posts == []

    # Пользователь снова может прислать корректную дату: превью → подтверждение.
    # Тип уже выбран на предыдущем шаге, повторять его не нужно.
    await _choose_type_and_confirm(
        fake_telegram,
        app,
        type_button=REMINDER_BUTTON,
        date_text=_moscow_future(2),
        choose_type=False,
    )
    await _wait_for_sent(fake_telegram, marker="Принято")

    posts_after = await PostRepository(factory).list_pending_by_user(user.id)
    assert len(posts_after) == 1


async def test_past_datetime_e2e_rejected_and_post_not_created(reminder_app, fake_telegram) -> None:
    """Прошедшая дата: rejection, пост не создан (SPEC-005)."""
    app = reminder_app

    await _await_reply(
        fake_telegram,
        action=lambda: fake_telegram.add_text_message("/start", chat_id=CHAT_ID, user_id=USER_TG),
    )
    await _wait_for_state(app, DialogState.WAITING_PHOTO)
    await _await_reply(
        fake_telegram,
        action=lambda: fake_telegram.add_photo_message(
            file_id=PHOTO_ID, chat_id=CHAT_ID, user_id=USER_TG
        ),
    )
    # PM-03: тип публикации обязателен перед шагом даты.
    await _wait_for_state(app, DialogState.WAITING_PUBLICATION_TYPE)
    await _await_reply(
        fake_telegram,
        action=lambda: fake_telegram.add_text_message(
            REMINDER_BUTTON, chat_id=CHAT_ID, user_id=USER_TG
        ),
    )
    await _wait_for_state(app, DialogState.WAITING_DATETIME)

    moscow = ZoneInfo("Europe/Moscow")
    past = (datetime.now(UTC) - timedelta(days=1)).astimezone(moscow)
    await _await_reply(
        fake_telegram,
        action=lambda: fake_telegram.add_text_message(
            past.strftime("%d.%m.%Y %H:%M"), chat_id=CHAT_ID, user_id=USER_TG
        ),
    )
    await _wait_for_sent(fake_telegram, marker="уже прошло")

    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    posts = await PostRepository(factory).list_pending_by_user(user.id)
    assert posts == []


# === /my_posts (бонус) ===================================================


async def test_my_posts_e2e_empty_then_shows_post(reminder_app, fake_telegram) -> None:
    """/my_posts пуст у нового пользователя, потом показывает запланированный пост."""
    app = reminder_app

    fake_telegram.add_text_message("/start", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    fake_telegram.add_text_message("/my_posts", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_sent(fake_telegram, marker="нет запланированных")

    fake_telegram.add_photo_message(file_id=PHOTO_ID, chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)

    await _choose_type_and_confirm(
        fake_telegram,
        app,
        type_button=REMINDER_BUTTON,
        date_text=_moscow_future(2),
    )
    await _wait_for_sent(fake_telegram, marker="Принято")

    factory = create_session_factory(app._engine)
    user = await UserRepository(factory).find_by_telegram_id(USER_TG)
    assert user is not None
    expected_post = (await PostRepository(factory).list_pending_by_user(user.id))[0]

    fake_telegram.add_text_message("/my_posts", chat_id=CHAT_ID, user_id=USER_TG)
    await _send_and_await_update(fake_telegram)
    await _wait_for_sent(fake_telegram, marker=f"ID {expected_post.id}")


async def _await_reply(
    fake_telegram, *, action, timeout: float = 5.0, tag: str = "message"
) -> None:
    """Отправляет действие и ждёт ПОЛНОЙ обработки: появления нового исходящего события.

    ``update_delivered`` поднимается при доставке апдейта, то есть до выполнения
    хэндлера. Если синхронизироваться по нему, следующий шаг уходит раньше, чем бот
    записал состояние и ответил — и обрабатывается в старом состоянии. Поэтому
    отправляем и ждём именно ответ.
    """
    before = len(fake_telegram.outbound_events)
    action()
    await _await_outbound(fake_telegram, before=before, timeout=timeout)


async def _await_outbound(fake_telegram, *, before: int, timeout: float = 5.0) -> None:
    """Ждёт, пока бот пришлёт новое исходящее сообщение (текст или фото).

    ``update_delivered`` поднимается при доставке update, то есть ДО обработки
    хэндлером: следующий шаг мог уйти раньше, чем бот ответит на предыдущий.
    Поэтому синхронизируемся по числу исходящих событий.
    """
    elapsed = 0.0
    while elapsed < timeout:
        if len(fake_telegram.outbound_events) > before:
            return
        await asyncio.sleep(0.02)
        elapsed += 0.02
    raise AssertionError("бот не прислал ответ на сообщение")
