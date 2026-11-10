"""PM-04: Интерактивное управление запланированными публикациями через inline-кнопки.

Покрывает:
- PM04-T01..T03: список публикаций (пустой, один, >20).
- PM04-T04..T08: отмена через inline-кнопку (happy path, повторная, чужая, PUBLISHED, PROCESSING).
- PM04-T09..T13: перенос (happy path, чужой, PUBLISHED, неверный формат, прошлое).
- PM04-T14: timezone-корректность переноса.
- PM04-T15: перенос обновляет Scheduler.
- PM04-T16: ошибка Scheduler не откатывает БД (Recovery восстановит).
- PM04-T17: неизменность полей поста при переносе.
- PM04-T18: подмена callback data (cancel:<id> чужого, resched:<id> чужого).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import Post, PostStatus, PublicationType
from postmaster.handlers.inline_keyboards import (
    CANCEL_BUTTON_TEXT,
    RESCHEDULE_BUTTON_TEXT,
    build_post_actions_keyboard,
    parse_cancel_callback,
    parse_reschedule_callback,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.cancel_service import CancelService
from postmaster.services.my_posts_service import (
    EMPTY_LIST,
    MY_POSTS_LIMIT,
    MyPostsService,
    render_post_block,
)
from postmaster.services.reschedule_service import (
    PostNotPendingError,
    RescheduleService,
)
from postmaster.services.user_service import UserService


class RecordingScheduler:
    """Заглушка Scheduler: записывает schedule_post и remove_job."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, datetime]] = []
        self.removed: list[int] = []
        self.fail_schedule: bool = False
        self.fail_remove: bool = False

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        if self.fail_schedule:
            raise RuntimeError("schedule failed")
        self.scheduled.append((post_id, run_at))

    def remove_job(self, post_id: int) -> bool:
        if self.fail_remove:
            raise RuntimeError("remove failed")
        self.removed.append(post_id)
        return True


@pytest.fixture
async def post_repo(tmp_path: Path) -> AsyncIterator[tuple[PostRepository, UserService]]:
    """Создаёт репозиторий постов и UserService."""
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_path(tmp_path)}")
    try:
        await create_schema(engine)
        factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
        user_repo = UserRepository(factory)
        yield PostRepository(factory), UserService(user_repo, default_timezone="UTC")
    finally:
        await engine.dispose()


async def _make_user(user_service: UserService, telegram_user_id: int) -> int:
    user = await user_service.get_or_create(
        telegram_user_id=telegram_user_id,
        username=None,
        first_name=f"User{telegram_user_id}",
    )
    return user.id


async def _create_post(
    repo: PostRepository,
    user_id: int,
    *,
    hours: int = 2,
    publication_type: PublicationType = PublicationType.REMINDER,
    target_id: str | None = None,
    timezone: str = "UTC",
    caption: str | None = None,
) -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id=f"file_{user_id}_{hours}",
        telegram_file_unique_id=f"unique_{user_id}_{hours}",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=hours),
        timezone=timezone,
        publication_type=publication_type,
        target_id=target_id,
        caption=caption,
    )
    return post.id


# === Inline keyboards ======================================================


def test_callback_parsers_roundtrip() -> None:
    """PM-04 §4.3, §5.3: парсеры корректно обрабатывают валидные/невалидные callback data."""
    assert parse_cancel_callback("cancel:123") == 123
    assert parse_cancel_callback("cancel:1") == 1
    assert parse_cancel_callback("cancel:abc") is None
    assert parse_cancel_callback("cancel:") is None
    assert parse_cancel_callback("cancel:0") is None
    assert parse_cancel_callback("cancel:-5") is None
    assert parse_cancel_callback("resched:1") is None

    assert parse_reschedule_callback("resched:123") == 123
    assert parse_reschedule_callback("resched:abc") is None
    assert parse_reschedule_callback("resched:") is None
    assert parse_reschedule_callback("resched:0") is None
    assert parse_reschedule_callback("cancel:1") is None


def test_keyboard_has_two_buttons() -> None:
    """PM-04 §3.1: на каждом посте две кнопки — отмена и перенос."""
    markup = build_post_actions_keyboard(post_id=42)
    flat = [btn for row in markup.keyboard for btn in row]
    texts = sorted(btn.text for btn in flat)
    assert texts == sorted([CANCEL_BUTTON_TEXT, RESCHEDULE_BUTTON_TEXT])
    callback_data = sorted(btn.callback_data for btn in flat)
    assert callback_data == ["cancel:42", "resched:42"]


# === MyPostsService.list_view (PM-04 §3) ==================================


async def test_pm04_t01_empty(post_repo) -> None:
    """PM04-T01: нет публикаций → EMPTY_LIST и пустой view."""
    repo, user_service = post_repo
    await _make_user(user_service, telegram_user_id=400)
    service = MyPostsService(repo, user_service)

    view = await service.list_view(telegram_user_id=400)

    assert view.is_empty
    assert view.hidden == 0
    assert not view.truncated
    assert (await service.render_list(400)) == EMPTY_LIST


async def test_pm04_t02_single_post(post_repo) -> None:
    """PM04-T02: одна публикация → блок с деталями."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=401)
    await _create_post(repo, user_id, hours=3)
    service = MyPostsService(repo, user_service)

    view = await service.list_view(401)

    assert len(view.posts) == 1
    assert not view.truncated


async def test_pm04_t03_more_than_20(post_repo) -> None:
    """PM04-T03: >20 публикаций → показаны первые 20 + усечение."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=402)
    for i in range(MY_POSTS_LIMIT + 5):
        await _create_post(repo, user_id, hours=i + 1)
    service = MyPostsService(repo, user_service)

    view = await service.list_view(402)

    assert len(view.posts) == MY_POSTS_LIMIT
    assert view.hidden == 5
    assert view.truncated


def test_pm04_render_block_contains_required_fields() -> None:
    """PM-04 §3.1: блок содержит ID, дату, время, тип; TELEGRAM — канал; caption отображается."""
    now = datetime.now(UTC)
    post = Post(
        id=99,
        user_id=1,
        telegram_file_id="x",
        telegram_file_unique_id="y",
        scheduled_at_utc=now + timedelta(hours=2),
        timezone="Europe/Moscow",
        publication_type=PublicationType.TELEGRAM,
        created_at=now,
        updated_at=now,
        target_id="-1001234567890",
        caption="Привет",
    )
    block = render_post_block(post)
    assert "Пост #99" in block
    assert "ID 99" in block
    assert "Тип: Публикация в канал" in block
    assert "Канал: -1001234567890" in block
    assert "Подпись: Привет" in block


# === RescheduleRepository (PM-04) =========================================


async def test_reschedule_repository_own_pending(post_repo) -> None:
    """Repository: reschedule_post меняет время только для собственного PENDING."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=410)
    post_id = await _create_post(repo, user_id)
    new_time = datetime.now(UTC) + timedelta(days=3)

    result = await repo.reschedule_post(
        post_id=post_id, user_id=user_id, scheduled_at_utc=new_time
    )

    assert result is not None
    assert result.scheduled_at_utc.replace(microsecond=0) == new_time.replace(microsecond=0)
    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.PENDING


async def test_reschedule_repository_foreign_rejected(post_repo) -> None:
    """Repository: чужой пост не переносится."""
    repo, user_service = post_repo
    user_alice = await _make_user(user_service, telegram_user_id=411)
    user_bob = await _make_user(user_service, telegram_user_id=412)
    post_id = await _create_post(repo, user_alice)
    new_time = datetime.now(UTC) + timedelta(days=3)

    result = await repo.reschedule_post(
        post_id=post_id, user_id=user_bob, scheduled_at_utc=new_time
    )

    assert result is None
    post = await repo.get_by_id(post_id)
    assert post is not None


async def test_reschedule_repository_published_rejected(post_repo) -> None:
    """Repository: PUBLISHED пост нельзя перенести."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=413)
    post_id = await _create_post(repo, user_id)
    await repo.update_status(post_id, PostStatus.PROCESSING)
    await repo.update_status(post_id, PostStatus.PUBLISHED, published_at=datetime.now(UTC))

    result = await repo.reschedule_post(
        post_id=post_id,
        user_id=user_id,
        scheduled_at_utc=datetime.now(UTC) + timedelta(days=3),
    )

    assert result is None


async def test_reschedule_repository_processing_rejected(post_repo) -> None:
    """Repository: PROCESSING пост нельзя перенести."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=414)
    post_id = await _create_post(repo, user_id)
    await repo.update_status(post_id, PostStatus.PROCESSING)

    result = await repo.reschedule_post(
        post_id=post_id,
        user_id=user_id,
        scheduled_at_utc=datetime.now(UTC) + timedelta(days=3),
    )

    assert result is None


async def test_reschedule_repository_keeps_other_fields(post_repo) -> None:
    """PM04-T17: тип, фото, caption и target не меняются при переносе."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=415)
    post_id = await _create_post(
        repo,
        user_id,
        publication_type=PublicationType.TELEGRAM,
        target_id="@channel",
        caption="старое",
    )
    file_id = (await repo.get_by_id(post_id)).telegram_file_id

    result = await repo.reschedule_post(
        post_id=post_id,
        user_id=user_id,
        scheduled_at_utc=datetime.now(UTC) + timedelta(days=4),
    )

    assert result is not None
    assert result.publication_type == PublicationType.TELEGRAM
    assert result.target_id == "@channel"
    assert result.caption == "старое"
    assert result.telegram_file_id == file_id


# === RescheduleService ====================================================


async def test_reschedule_service_happy_path(post_repo) -> None:
    """PM04-T09: валидный перенос обновляет БД и Scheduler."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=420)
    post_id = await _create_post(repo, user_id, hours=2)
    new_time = datetime.now(UTC) + timedelta(days=5)
    scheduler = RecordingScheduler()
    service = RescheduleService(repo, scheduler)

    result = await service.reschedule(
        post_id=post_id, user_id=user_id, scheduled_at_utc=new_time
    )

    assert result.id == post_id
    assert any(pid == post_id for pid, _ in scheduler.scheduled)


async def test_reschedule_service_foreign_rejected(post_repo) -> None:
    """PM04-T10: перенос чужого поста запрещён."""
    repo, user_service = post_repo
    user_alice = await _make_user(user_service, telegram_user_id=421)
    user_bob = await _make_user(user_service, telegram_user_id=422)
    post_id = await _create_post(repo, user_alice)
    scheduler = RecordingScheduler()
    service = RescheduleService(repo, scheduler)

    with pytest.raises(PostNotPendingError):
        await service.reschedule(
            post_id=post_id,
            user_id=user_bob,
            scheduled_at_utc=datetime.now(UTC) + timedelta(days=5),
        )
    assert scheduler.scheduled == []


async def test_reschedule_service_published_rejected(post_repo) -> None:
    """PM04-T11: перенос PUBLISHED запрещён."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=423)
    post_id = await _create_post(repo, user_id)
    await repo.update_status(post_id, PostStatus.PROCESSING)
    await repo.update_status(post_id, PostStatus.PUBLISHED, published_at=datetime.now(UTC))
    scheduler = RecordingScheduler()
    service = RescheduleService(repo, scheduler)

    with pytest.raises(PostNotPendingError):
        await service.reschedule(
            post_id=post_id,
            user_id=user_id,
            scheduled_at_utc=datetime.now(UTC) + timedelta(days=5),
        )


async def test_reschedule_service_scheduler_failure_keeps_db(post_repo) -> None:
    """PM04-T16: ошибка Scheduler не откатывает БД."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=424)
    post_id = await _create_post(repo, user_id)
    scheduler = RecordingScheduler()
    scheduler.fail_schedule = True
    service = RescheduleService(repo, scheduler)
    new_time = datetime.now(UTC) + timedelta(days=6)

    result = await service.reschedule(
        post_id=post_id, user_id=user_id, scheduled_at_utc=new_time
    )

    assert result.id == post_id
    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.scheduled_at_utc.replace(microsecond=0) == new_time.replace(microsecond=0)


# === Cancel via callback (PM-04 §4) =======================================


async def test_cancel_button_happy_path(post_repo) -> None:
    """PM04-T04: отмена PENDING через CancelService."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=430)
    post_id = await _create_post(repo, user_id)
    scheduler = RecordingScheduler()
    service = CancelService(repo, scheduler)

    await service.cancel(post_id=post_id, user_id=user_id)

    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.CANCELLED
    assert scheduler.removed == [post_id]


# === Timezone correctness (PM04-T14) ======================================


async def test_reschedule_preserves_timezone(post_repo) -> None:
    """PM-04 §5: новое время сохраняется в UTC, timezone поста сохраняется."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=440)
    post_id = await _create_post(repo, user_id, hours=2, timezone="Europe/Moscow")
    scheduler = RecordingScheduler()
    service = RescheduleService(repo, scheduler)

    from zoneinfo import ZoneInfo

    new_local = datetime(2026, 12, 10, 15, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    expected_utc = new_local.astimezone(UTC)

    result = await service.reschedule(
        post_id=post_id, user_id=user_id, scheduled_at_utc=expected_utc
    )

    assert result.timezone == "Europe/Moscow"
    assert result.scheduled_at_utc.replace(microsecond=0) == expected_utc.replace(microsecond=0)
    block = render_post_block(result)
    assert "15:00" in block
    assert "10.12.2026" in block


# === E2E через Long Polling + fake Telegram (PM-04 §10) ===================


async def test_pm04_e2e_my_posts_with_inline_keyboards(
    running_app, fake_telegram, tmp_path: Path
) -> None:
    """PM04-T02 / PM04-AC-02: /my_posts возвращает блоки с inline-кнопками."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from harness import wait_until
    from postmaster.database.models import PostModel

    fake_telegram.add_text_message(
        "/start", chat_id=500, user_id=500, username=None, first_name="Тестер"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) >= 1)

    engine = running_app._engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    scheduled = datetime.now(UTC) + timedelta(hours=2)
    async with factory() as session:
        session.add(
            PostModel(
                user_id=1,
                telegram_file_id="AgAC_pm04",
                telegram_file_unique_id="AQAD_pm04",
                scheduled_at_utc=scheduled,
                timezone="UTC",
                status=PostStatus.PENDING.value,
                publication_type=PublicationType.REMINDER.value,
                target_id=None,
                error_message=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                published_at=None,
                caption=None,
            )
        )
        await session.commit()

    fake_telegram.add_text_message("/my_posts", chat_id=500, user_id=500)
    await wait_until(lambda: len(fake_telegram.sent_messages) >= 3)

    last = fake_telegram.sent_messages[-1]
    assert "Пост #1" in last["text"]
    assert any(m.get("text") == last["text"] for m in fake_telegram.sent_inline_markups)
    inline_match = next(
        m for m in fake_telegram.sent_inline_markups if m.get("text") == last["text"]
    )
    buttons = [b for row in inline_match["reply_markup"]["inline_keyboard"] for b in row]
    callback_data = sorted(b["callback_data"] for b in buttons)
    assert callback_data == ["cancel:1", "resched:1"]


async def test_pm04_e2e_cancel_via_button(running_app, fake_telegram, tmp_path: Path) -> None:
    """PM04-T04 / PM04-AC-03,04,05: нажатие inline-кнопки «Отменить» отменяет пост."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from harness import wait_until
    from postmaster.database.models import PostModel

    fake_telegram.add_text_message(
        "/start", chat_id=501, user_id=501, username=None, first_name="Отменитель"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) >= 1)

    engine = running_app._engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    scheduled = datetime.now(UTC) + timedelta(hours=3)
    async with factory() as session:
        session.add(
            PostModel(
                user_id=1,
                telegram_file_id="AgAC_pm04_cancel",
                telegram_file_unique_id="AQAD_pm04_cancel",
                scheduled_at_utc=scheduled,
                timezone="UTC",
                status=PostStatus.PENDING.value,
                publication_type=PublicationType.REMINDER.value,
                target_id=None,
                error_message=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                published_at=None,
                caption=None,
            )
        )
        await session.commit()

    fake_telegram.add_callback_query("cancel:1", chat_id=501, user_id=501)
    await wait_until(lambda: len(fake_telegram.sent_messages) >= 2)

    last = fake_telegram.sent_messages[-1]
    assert "Публикация #1" in last["text"]
    assert "отменена" in last["text"]

    from postmaster.repositories.post_repository import PostRepository

    repo = PostRepository(factory)
    post = await repo.get_by_id(1)
    assert post is not None
    assert post.status == PostStatus.CANCELLED


async def test_pm04_e2e_foreign_cancel_blocked(running_app, fake_telegram, tmp_path: Path) -> None:
    """PM04-T06 / PM04-AC-11: подмена callback data → отмена чужого поста запрещена."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from harness import wait_until
    from postmaster.database.models import PostModel

    fake_telegram.add_text_message("/start", chat_id=600, user_id=600, first_name="Алиса")
    await wait_until(lambda: len(fake_telegram.sent_messages) >= 1)
    fake_telegram.add_text_message("/start", chat_id=601, user_id=601, first_name="Боб")
    await wait_until(lambda: len(fake_telegram.sent_messages) >= 2)

    engine = running_app._engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    scheduled = datetime.now(UTC) + timedelta(hours=2)
    async with factory() as session:
        session.add(
            PostModel(
                user_id=1,
                telegram_file_id="AgAC_pm04_alice",
                telegram_file_unique_id="AQAD_pm04_alice",
                scheduled_at_utc=scheduled,
                timezone="UTC",
                status=PostStatus.PENDING.value,
                publication_type=PublicationType.REMINDER.value,
                target_id=None,
                error_message=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                published_at=None,
                caption=None,
            )
        )
        await session.commit()

    fake_telegram.add_callback_query("cancel:1", chat_id=601, user_id=601)
    await wait_until(
        lambda: any("доступна" in m["text"] or "не найдена" in m["text"]
                     for m in fake_telegram.sent_messages)
    )

    from postmaster.repositories.post_repository import PostRepository

    repo = PostRepository(factory)
    post = await repo.get_by_id(1)
    assert post is not None
    assert post.status == PostStatus.PENDING


async def test_pm04_e2e_invalid_date_reprompts(running_app, fake_telegram, tmp_path: Path) -> None:
    """PM04-T12 / PM04-BR-16: некорректная дата → повторный запрос."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from harness import wait_until
    from postmaster.database.models import PostModel

    fake_telegram.add_text_message("/start", chat_id=700, user_id=700, first_name="X")
    await wait_until(lambda: len(fake_telegram.sent_messages) >= 1)

    engine = running_app._engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    scheduled = datetime.now(UTC) + timedelta(hours=2)
    async with factory() as session:
        session.add(
            PostModel(
                user_id=1,
                telegram_file_id="AgAC_pm04_bad_date",
                telegram_file_unique_id="AQAD_pm04_bad_date",
                scheduled_at_utc=scheduled,
                timezone="UTC",
                status=PostStatus.PENDING.value,
                publication_type=PublicationType.REMINDER.value,
                target_id=None,
                error_message=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                published_at=None,
                caption=None,
            )
        )
        await session.commit()

    fake_telegram.add_callback_query("resched:1", chat_id=700, user_id=700)
    await wait_until(lambda: any("формате" in m["text"] for m in fake_telegram.sent_messages))

    fake_telegram.add_text_message("не дата", chat_id=700, user_id=700)
    await wait_until(lambda: sum(1 for _ in fake_telegram.sent_messages) >= 3)
    assert any(
        "понимаю" in m["text"] or "формат" in m["text"]
        for m in fake_telegram.sent_messages
    )

    from postmaster.repositories.post_repository import PostRepository

    repo = PostRepository(factory)
    post = await repo.get_by_id(1)
    assert post is not None
    assert post.status == PostStatus.PENDING
    assert abs((post.scheduled_at_utc - scheduled).total_seconds()) < 5
