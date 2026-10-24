"""SPEC-018: Команда /cancel.

Тесты проверяют:
- valid id: PENDING пост пользователя отменяется атомарно.
- missing id: несуществующий пост → PostNotFoundForUserError.
- invalid id: некорректный формат → INVALID_ID_TEXT.
- ownership: чужой пост → PostNotFoundForUserError.
- cancel race: одновременные cancel двух пользователей — выигрывает владелец.
- scheduler cleanup: задание Scheduler удаляется после успешной отмены.
- запись из БД не удаляется.
- PROCESSING/PUBLISHED/FAILED/CANCELLED не отменяются.
- scheduler remove failure не возвращает Post в PENDING.
- handler работает только в личных чатах.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.handlers.cancel import (
    CANCEL_FAILED,
    CANCEL_NOT_FOUND,
    CANCEL_OK,
    CANCEL_USAGE,
    INVALID_ID_TEXT,
    register_cancel_handler,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.cancel_service import (
    CancelService,
    PostNotFoundForUserError,
)
from postmaster.services.scheduler_service import SchedulerService
from postmaster.services.user_service import UserService


class RecordingScheduler:
    """Заглушка Scheduler с записью вызовов remove_job."""

    def __init__(self) -> None:
        self.removed: list[int] = []
        self.fail_remove: bool = False
        self.fail_register: bool = False

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        if self.fail_register:
            raise RuntimeError("register failed")

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


async def _create_pending_post(
    repo: PostRepository,
    user_id: int,
    *,
    publication_type: PublicationType = PublicationType.REMINDER,
    target_id: str | None = None,
) -> int:
    post = await repo.create_post(
        user_id=user_id,
        telegram_file_id=f"file_{user_id}",
        telegram_file_unique_id=f"unique_{user_id}",
        scheduled_at_utc=datetime.now(UTC) + timedelta(hours=2),
        timezone="UTC",
        publication_type=publication_type,
        target_id=target_id,
    )
    return post.id


# === CancelService tests (SPEC-018 §8) ===


async def test_cancel_valid_id(post_repo) -> None:
    """Валидный id PENDING-поста → отмена успешна."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=100)
    post_id = await _create_pending_post(repo, user_id)
    scheduler = RecordingScheduler()
    service = CancelService(repo, scheduler)

    await service.cancel(post_id=post_id, user_id=user_id)

    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.CANCELLED
    assert scheduler.removed == [post_id]


async def test_cancel_post_record_remains(post_repo) -> None:
    """Запись из БД не удаляется (SPEC-018 §1, §4)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=101)
    post_id = await _create_pending_post(repo, user_id)
    scheduler = RecordingScheduler()
    service = CancelService(repo, scheduler)

    await service.cancel(post_id=post_id, user_id=user_id)

    assert (await repo.get_by_id(post_id)) is not None


async def test_cancel_missing_id(post_repo) -> None:
    """Несуществующий post_id → PostNotFoundForUserError."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=102)
    scheduler = RecordingScheduler()
    service = CancelService(repo, scheduler)

    with pytest.raises(PostNotFoundForUserError):
        await service.cancel(post_id=99999, user_id=user_id)

    assert scheduler.removed == []


async def test_cancel_foreign_post(post_repo) -> None:
    """Чужой пост не отменяется (ownership, SPEC-018 §7)."""
    repo, user_service = post_repo
    user_alice = await _make_user(user_service, telegram_user_id=103)
    user_bob = await _make_user(user_service, telegram_user_id=104)
    post_id = await _create_pending_post(repo, user_alice)
    scheduler = RecordingScheduler()
    service = CancelService(repo, scheduler)

    with pytest.raises(PostNotFoundForUserError):
        await service.cancel(post_id=post_id, user_id=user_bob)

    # Пост остаётся PENDING у владельца
    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.PENDING
    assert scheduler.removed == []


async def test_cancel_processing_post_rejected(post_repo) -> None:
    """PROCESSING пост не отменяется (SPEC-018 §7)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=105)
    post_id = await _create_pending_post(repo, user_id)
    await repo.update_status(post_id, PostStatus.PROCESSING)

    scheduler = RecordingScheduler()
    service = CancelService(repo, scheduler)

    with pytest.raises(PostNotFoundForUserError):
        await service.cancel(post_id=post_id, user_id=user_id)

    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.PROCESSING


async def test_cancel_published_post_rejected(post_repo) -> None:
    """PUBLISHED пост не отменяется (SPEC-018 §7)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=106)
    post_id = await _create_pending_post(repo, user_id)
    await repo.update_status(post_id, PostStatus.PROCESSING)
    await repo.update_status(post_id, PostStatus.PUBLISHED, published_at=datetime.now(UTC))

    scheduler = RecordingScheduler()
    service = CancelService(repo, scheduler)

    with pytest.raises(PostNotFoundForUserError):
        await service.cancel(post_id=post_id, user_id=user_id)

    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.PUBLISHED


async def test_cancel_failed_post_rejected(post_repo) -> None:
    """FAILED пост не отменяется (SPEC-018 §7)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=107)
    post_id = await _create_pending_post(repo, user_id)
    await repo.update_status(post_id, PostStatus.PROCESSING)
    await repo.update_status(post_id, PostStatus.FAILED, error_message="boom")

    scheduler = RecordingScheduler()
    service = CancelService(repo, scheduler)

    with pytest.raises(PostNotFoundForUserError):
        await service.cancel(post_id=post_id, user_id=user_id)

    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.FAILED


async def test_cancel_already_cancelled_rejected(post_repo) -> None:
    """CANCELLED пост не отменяется повторно (SPEC-018 §7)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=108)
    post_id = await _create_pending_post(repo, user_id)
    await repo.update_status(post_id, PostStatus.CANCELLED)

    scheduler = RecordingScheduler()
    service = CancelService(repo, scheduler)

    with pytest.raises(PostNotFoundForUserError):
        await service.cancel(post_id=post_id, user_id=user_id)


async def test_cancel_scheduler_remove_failure_keeps_post_cancelled(post_repo) -> None:
    """Ошибка удаления задания Scheduler не возвращает пост в PENDING (§7)."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=109)
    post_id = await _create_pending_post(repo, user_id)
    scheduler = RecordingScheduler()
    scheduler.fail_remove = True
    service = CancelService(repo, scheduler)

    # Отмена не падает — scheduler cleanup ошибка проглатывается и логируется
    await service.cancel(post_id=post_id, user_id=user_id)

    # Пост остался CANCELLED в БД
    post = await repo.get_by_id(post_id)
    assert post is not None
    assert post.status == PostStatus.CANCELLED


async def test_cancel_race_concurrent_cancels(post_repo) -> None:
    """Два конкурирующих cancel одного и того же поста — только один успешен."""
    import asyncio

    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=110)
    post_id = await _create_pending_post(repo, user_id)
    scheduler = RecordingScheduler()
    service = CancelService(repo, scheduler)

    # Запускаем две отмены параллельно — только первая должна пройти
    results = await asyncio.gather(
        service.cancel(post_id=post_id, user_id=user_id),
        service.cancel(post_id=post_id, user_id=user_id),
        return_exceptions=True,
    )

    successes = [r for r in results if r is None]
    errors = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], PostNotFoundForUserError)
    # Один вызов remove_job
    assert scheduler.removed == [post_id]


async def test_cancel_race_with_claim(post_repo) -> None:
    """Cancel и execute (claim) конкурируют атомарно — cancel выигрывает."""
    import asyncio

    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=111)
    post_id = await _create_pending_post(repo, user_id)

    class _NoopExecutor:
        def __init__(self) -> None:
            self.published = False

        async def publish(self, post_id: int) -> None:
            # Если claim выиграл — симулируем небольшую задержку
            await asyncio.sleep(0.05)
            self.published = True

    executor = _NoopExecutor()
    scheduler = SchedulerService(repo, publication_executor=executor)
    scheduler.start()
    try:
        service = CancelService(repo, scheduler)

        # Параллельно: cancel и claim
        cancel_task = asyncio.create_task(service.cancel(post_id=post_id, user_id=user_id))
        claim_task = asyncio.create_task(repo.claim_for_processing(post_id))

        results = await asyncio.gather(cancel_task, claim_task, return_exceptions=True)

        # Cancel выиграл
        assert results[0] is None
        # Claim вернул None (пост уже CANCELLED)
        assert results[1] is None

        # Пост остался CANCELLED
        post = await repo.get_by_id(post_id)
        assert post is not None
        assert post.status == PostStatus.CANCELLED
    finally:
        scheduler.shutdown()


async def test_cancel_with_real_scheduler_removes_job(post_repo) -> None:
    """С реальным SchedulerService задание удаляется после отмены."""
    repo, user_service = post_repo
    user_id = await _make_user(user_service, telegram_user_id=112)
    post_id = await _create_pending_post(repo, user_id)
    run_at = datetime.now(UTC) + timedelta(hours=2)

    class _NoopExecutor:
        async def publish(self, post_id: int) -> None:
            return None

    scheduler = SchedulerService(repo, publication_executor=_NoopExecutor())
    scheduler.start()
    try:
        scheduler.schedule_post(post_id=post_id, run_at=run_at)
        assert scheduler._scheduler.get_job(f"post:{post_id}") is not None

        service = CancelService(repo, scheduler)
        await service.cancel(post_id=post_id, user_id=user_id)

        assert scheduler._scheduler.get_job(f"post:{post_id}") is None
    finally:
        scheduler.shutdown()


# === Handler tests (SPEC-018 §7, §8) ===


async def test_handler_register_only_private_chat() -> None:
    """Handler /cancel зарегистрирован только для приватных чатов."""

    class _StubBot:
        def __init__(self) -> None:
            self.handlers: list[dict] = []

        def message_handler(self, *, commands: list[str], chat_types: list[str]):
            def decorator(func):
                self.handlers.append({"commands": commands, "chat_types": chat_types})
                return func

            return decorator

    bot = _StubBot()  # type: ignore[arg-type]
    register_cancel_handler(bot, None, None)  # type: ignore[arg-type]

    assert len(bot.handlers) == 1
    assert bot.handlers[0]["commands"] == ["cancel"]
    assert bot.handlers[0]["chat_types"] == ["private"]


def test_cancel_message_constants() -> None:
    """Константы сообщений handler не пустые."""
    assert CANCEL_USAGE
    assert CANCEL_NOT_FOUND
    assert CANCEL_FAILED
    assert CANCEL_OK
    assert INVALID_ID_TEXT


async def test_cancel_e2e_via_fake_telegram(running_app, fake_telegram, tmp_path: Path) -> None:
    """/cancel через Long Polling: успешная отмена + подтверждение."""
    from harness import wait_until

    # Регистрируем пользователя
    fake_telegram.add_text_message(
        "/start", chat_id=300, user_id=300, username=None, first_name="Отменитель"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)

    # Создаём пост напрямую через engine
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from postmaster.database.models import PostModel

    engine = running_app._engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    scheduled = datetime.now(UTC) + timedelta(hours=2)
    async with factory() as session:
        session.add(
            PostModel(
                user_id=1,
                telegram_file_id="AgAC_cancel_test",
                telegram_file_unique_id="AQAD_cancel_test",
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

    # /cancel 1 — успешная отмена
    fake_telegram.add_text_message("/cancel 1", chat_id=300, user_id=300)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    last = fake_telegram.sent_messages[-1]
    assert last["chat_id"] == "300"
    assert "ID 1" in last["text"]
    assert "отменена" in last["text"]


async def test_cancel_e2e_invalid_id(running_app, fake_telegram, tmp_path: Path) -> None:
    """/cancel без числового id → INVALID_ID_TEXT."""
    from harness import wait_until

    fake_telegram.add_text_message(
        "/start", chat_id=301, user_id=301, username=None, first_name="Тестер"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)

    fake_telegram.add_text_message("/cancel abc", chat_id=301, user_id=301)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    last = fake_telegram.sent_messages[-1]
    assert "ID" in last["text"] or "числ" in last["text"]


async def test_cancel_e2e_missing_id(running_app, fake_telegram, tmp_path: Path) -> None:
    """/cancel без аргумента → CANCEL_USAGE."""
    from harness import wait_until

    fake_telegram.add_text_message(
        "/start", chat_id=302, user_id=302, username=None, first_name="Тестер"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)

    fake_telegram.add_text_message("/cancel", chat_id=302, user_id=302)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    last = fake_telegram.sent_messages[-1]
    assert "/my_posts" in last["text"]


async def test_cancel_e2e_foreign_post(running_app, fake_telegram, tmp_path: Path) -> None:
    """/cancel чужого поста → CANCEL_NOT_FOUND."""
    from harness import wait_until

    # Регистрируем двух пользователей
    fake_telegram.add_text_message(
        "/start", chat_id=303, user_id=303, username=None, first_name="Алиса"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 1)
    fake_telegram.add_text_message(
        "/start", chat_id=304, user_id=304, username=None, first_name="Боб"
    )
    await wait_until(lambda: len(fake_telegram.sent_messages) == 2)

    # Алиса создаёт пост
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from postmaster.database.models import PostModel

    engine = running_app._engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    scheduled = datetime.now(UTC) + timedelta(hours=2)
    async with factory() as session:
        session.add(
            PostModel(
                user_id=1,  # Алиса (первый зарегистрированный)
                telegram_file_id="AgAC_alice",
                telegram_file_unique_id="AQAD_alice",
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

    # Боб пытается отменить пост Алисы
    fake_telegram.add_text_message("/cancel 1", chat_id=304, user_id=304)
    await wait_until(lambda: len(fake_telegram.sent_messages) == 3)

    last = fake_telegram.sent_messages[-1]
    assert "не найдена" in last["text"].lower() or "ожидающих" in last["text"].lower()

    # Пост остался PENDING
    async with factory() as session:
        model = await session.get(PostModel, 1)
        assert model is not None
        assert model.status == PostStatus.PENDING.value
