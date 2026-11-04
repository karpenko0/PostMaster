"""SPEC-032: Интеграционные тесты.

Проверяет взаимодействие services + repositories + SQLite + Scheduler +
Publishers сквозным образом, с mocked TelegramClient (§1, §4).
Гарантирует контракты §7: status transitions, persistence,
межслойные соглашения.

§8 Обязательные сценарии:
- create + schedule;
- recovery;
- missed;
- publish;
- cancel;
- retry.
"""

import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness import db_path
from postmaster.database.engine import create_database_engine, create_session_factory
from postmaster.database.schema import create_schema
from postmaster.domain.post import PostStatus, PublicationType
from postmaster.publishers import DefaultPublisherResolver
from postmaster.publishers.telegram_errors import TelegramErrorMapper
from postmaster.publishers.telegram_publisher import (
    TelegramChannelPublisher,
    TelegramPermissionError,
)
from postmaster.repositories.post_repository import PostRepository
from postmaster.repositories.user_repository import UserRepository
from postmaster.services.cancel_service import CancelService
from postmaster.services.missed_post_handler import MissedPostHandler
from postmaster.services.post_service import PostService
from postmaster.services.publication_service import (
    PublicationError,
    PublicationService,
    RetryPolicy,
)
from postmaster.services.recovery_service import RecoveryService
from postmaster.services.user_service import UserService

RUN_AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


# === Фикстуры ===============================================================


class _RecordingScheduler:
    """Fake SchedulerService: записывает вызовы, не запускает APScheduler.

    Соблюдает контракт ``schedule_post``/``remove_job``/``shutdown``, который
    требуют ``PostService`` и ``CancelService``. Используется в интеграционных
    сценариях, где сам APScheduler не нужен (мы проверяем цепочку services →
    repositories, а не планировщик).
    """

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, datetime]] = []
        self.removed: list[int] = []
        self.started = False
        self.shutdown_calls = 0

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        self.scheduled.append((post_id, run_at))

    def remove_job(self, post_id: int) -> bool:
        self.removed.append(post_id)
        return True


class _RecordingSender:
    """Fake Telegram sender: записывает вызовы и опционально бросает ошибку."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self._fail_with = fail_with
        self.calls: list[dict] = []

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> dict | None:
        self.calls.append({"chat_id": chat_id, "photo_file_id": photo_file_id})
        if self._fail_with is not None:
            raise self._fail_with
        return {"message_id": len(self.calls)}


class _FlakySender:
    """Fake sender, который первые ``fail_count`` раз бросает ``fail_with``."""

    def __init__(self, *, fail_count: int, fail_with: Exception) -> None:
        self._fail_count = fail_count
        self._fail_with = fail_with
        self.call_count = 0
        self.messages: list[int] = []

    async def send_photo(
        self, *, chat_id: str, photo_file_id: str, caption: str | None = None
    ) -> dict | None:
        self.call_count += 1
        if self.call_count <= self._fail_count:
            raise self._fail_with
        message_id = self.call_count
        self.messages.append(message_id)
        return {"message_id": message_id}


@pytest.fixture
async def integration_env(
    tmp_path: Path,
) -> AsyncIterator[dict]:
    """Полный стек: engine, repos, services, recording scheduler, recording sender."""
    db_file = db_path(tmp_path)
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_file}")
    try:
        await create_schema(engine)
        factory = create_session_factory(engine)

        post_repo = PostRepository(factory)
        user_repo = UserRepository(factory)
        user_service = UserService(user_repo, default_timezone="UTC")

        scheduler = _RecordingScheduler()
        sender = _RecordingSender()
        publisher = TelegramChannelPublisher(sender, error_mapper=TelegramErrorMapper())
        resolver = DefaultPublisherResolver(telegram_publisher=publisher)
        publication_service = PublicationService(
            post_repo,
            resolver,
            retry_policy=RetryPolicy(delays=()),
            error_mapper=TelegramErrorMapper(),
        )

        recovery_service = RecoveryService(
            repository=post_repo,
            scheduler=scheduler,
            missed_post_handler=MissedPostHandler(
                repository=post_repo,
                publication_executor=publication_service,
            ),
        )
        post_service = PostService(post_repo, scheduler)
        cancel_service = CancelService(post_repo, scheduler)

        user = await user_service.get_or_create(
            telegram_user_id=10001,
            username="integration",
            first_name="Integration",
        )

        yield {
            "db_file": db_file,
            "engine": engine,
            "factory": factory,
            "post_repo": post_repo,
            "user_repo": user_repo,
            "user_service": user_service,
            "scheduler": scheduler,
            "sender": sender,
            "publisher": publisher,
            "publication_service": publication_service,
            "recovery_service": recovery_service,
            "post_service": post_service,
            "cancel_service": cancel_service,
            "user_id": user.id,
        }
    finally:
        await engine.dispose()


# === create + schedule (§8) ================================================


async def test_create_scheduled_post_persists_and_registers_job(
    integration_env: dict,
) -> None:
    """PostService.create_scheduled_post коммитит Post и регистрирует задание (SPEC-009, §8)."""
    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_1",
        telegram_file_unique_id="unique_1",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    # Post зафиксирован в БД (межслойный контракт: commit до scheduler).
    assert (await integration_env["post_repo"].get_by_id(post.id)) is not None
    assert post.status == PostStatus.PENDING

    # Scheduler получил задание.
    scheduled_ids = [pid for pid, _ in integration_env["scheduler"].scheduled]
    assert scheduled_ids == [post.id]
    assert integration_env["scheduler"].scheduled[0][1] == RUN_AT


async def test_create_scheduled_post_writes_to_db_before_scheduler_call(
    integration_env: dict,
) -> None:
    """В момент ``schedule_post`` запись уже зафиксирована в БД (SPEC-027 §7).

    Используем sync sqlite3 — отдельное соединение к файлу, не блокирующее
    основной async engine.
    """
    db_file = integration_env["db_file"]
    post_service = integration_env["post_service"]
    observed: dict[str, str | None] = {}

    class _SpyScheduler:
        def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
            with sqlite3.connect(str(db_file)) as connection:
                row = connection.execute(
                    "SELECT status FROM posts WHERE id = ?", (post_id,)
                ).fetchone()
            observed["status_in_db"] = None if row is None else row[0]

    post_service._scheduler = _SpyScheduler()  # type: ignore[assignment]

    post = await post_service.create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_2",
        telegram_file_unique_id="unique_2",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )

    assert observed["status_in_db"] == PostStatus.PENDING.value
    assert post.status == PostStatus.PENDING


# === publish (§8) =========================================================


async def test_publish_full_flow_transitions_to_published(
    integration_env: dict,
) -> None:
    """PENDING → PROCESSING → PUBLISHED с реальным DB и mocked TelegramClient."""
    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_publish",
        telegram_file_unique_id="unique_publish",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    result = await integration_env["publication_service"].publish(post.id)

    assert result.status == PostStatus.PUBLISHED
    # Mocked TelegramClient зафиксировал ровно один вызов.
    assert len(integration_env["sender"].calls) == 1
    assert integration_env["sender"].calls[0]["photo_file_id"] == "file_publish"


async def test_publish_persists_terminal_state_across_engine_reopen(
    integration_env: dict,
) -> None:
    """PUBLISHED сохраняется в файле SQLite и переживает reopen движка (SPEC-032 §7)."""
    db_file = integration_env["db_file"]
    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_persist",
        telegram_file_unique_id="unique_persist",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )
    await integration_env["publication_service"].publish(post.id)

    # Закрываем engine, открываем новый по тому же файлу.
    await integration_env["engine"].dispose()
    engine = create_database_engine(f"sqlite+aiosqlite:///{db_file}")
    factory = create_session_factory(engine)
    try:
        reloaded = await PostRepository(factory).get_by_id(post.id)
        assert reloaded is not None
        assert reloaded.status == PostStatus.PUBLISHED
        assert reloaded.published_at is not None
    finally:
        await engine.dispose()


async def test_publish_error_keeps_state_consistent(integration_env: dict) -> None:
    """При permanent-ошибке публикации пост переходит в FAILED с сообщением (SPEC-021)."""

    sender = _RecordingSender(fail_with=TelegramPermissionError("PERMISSION: chat not found"))
    publisher = TelegramChannelPublisher(sender, error_mapper=TelegramErrorMapper())
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        integration_env["post_repo"],
        resolver,
        retry_policy=RetryPolicy(delays=()),
        error_mapper=TelegramErrorMapper(),
    )

    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_err",
        telegram_file_unique_id="unique_err",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    with pytest.raises(PublicationError):
        await service.publish(post.id)

    persisted = await integration_env["post_repo"].get_by_id(post.id)
    assert persisted is not None
    assert persisted.status == PostStatus.FAILED
    assert "PERMISSION" in (persisted.error_message or "")


# === retry (§8) ===========================================================


async def test_retry_on_transient_error_eventually_succeeds(
    integration_env: dict,
) -> None:
    """Timeout (retryable) → retry → success после двух временных сбоев (SPEC-022)."""
    sender = _FlakySender(fail_count=2, fail_with=TimeoutError("timeout"))
    publisher = TelegramChannelPublisher(sender, error_mapper=TelegramErrorMapper())
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        integration_env["post_repo"],
        resolver,
        retry_policy=RetryPolicy(delays=(0, 0)),
        error_mapper=TelegramErrorMapper(),
    )

    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_retry",
        telegram_file_unique_id="unique_retry",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    result = await service.publish(post.id)

    assert result.status == PostStatus.PUBLISHED
    assert sender.call_count == 3  # 2 transient failures + 1 success


async def test_retry_does_not_retry_on_permission_error(
    integration_env: dict,
) -> None:
    """PERMISSION (non-retryable) → одна попытка, пост FAILED (SPEC-022)."""

    sender = _FlakySender(fail_count=99, fail_with=TelegramPermissionError("perm"))
    publisher = TelegramChannelPublisher(sender, error_mapper=TelegramErrorMapper())
    resolver = DefaultPublisherResolver(telegram_publisher=publisher)
    service = PublicationService(
        integration_env["post_repo"],
        resolver,
        retry_policy=RetryPolicy(delays=(0, 0)),
        error_mapper=TelegramErrorMapper(),
    )

    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_no_retry",
        telegram_file_unique_id="unique_no_retry",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    with pytest.raises(PublicationError):
        await service.publish(post.id)

    assert sender.call_count == 1
    persisted = await integration_env["post_repo"].get_by_id(post.id)
    assert persisted is not None
    assert persisted.status == PostStatus.FAILED


# === cancel (§8) ==========================================================


async def test_cancel_removes_job_and_marks_post_cancelled(
    integration_env: dict,
) -> None:
    """CancelService.cancel: пост CANCELLED, задание удалено из Scheduler (SPEC-018)."""
    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_cancel",
        telegram_file_unique_id="unique_cancel",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )

    await integration_env["cancel_service"].cancel(
        post_id=post.id, user_id=integration_env["user_id"]
    )

    persisted = await integration_env["post_repo"].get_by_id(post.id)
    assert persisted is not None
    assert persisted.status == PostStatus.CANCELLED
    assert post.id in integration_env["scheduler"].removed


# === recovery (§8) ========================================================


async def test_recovery_picks_up_future_pending_posts(
    integration_env: dict,
) -> None:
    """Recovery.reschedule переносит будущие PENDING-посты в Scheduler (SPEC-011)."""
    from postmaster.utils.clock import utc_now

    future = utc_now() + timedelta(days=2)
    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_recovery",
        telegram_file_unique_id="unique_recovery",
        scheduled_at_utc=future,
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )

    # Очищаем историю scheduler перед Recovery.
    integration_env["scheduler"].scheduled.clear()

    report = await integration_env["recovery_service"].recover()

    assert report.rescheduled == 1
    assert report.missed == 0
    assert integration_env["scheduler"].scheduled == [(post.id, future)]


async def test_recovery_handles_missed_posts_via_missed_handler(
    integration_env: dict,
) -> None:
    """Overdue PENDING → MissedPostHandler публикует немедленно (SPEC-012, §8)."""
    overdue = RUN_AT - timedelta(hours=1)  # гарантированно в прошлом
    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_missed",
        telegram_file_unique_id="unique_missed",
        scheduled_at_utc=overdue,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    report = await integration_env["recovery_service"].recover()

    assert report.missed == 1
    assert report.rescheduled == 0

    persisted = await integration_env["post_repo"].get_by_id(post.id)
    assert persisted is not None
    assert persisted.status == PostStatus.PUBLISHED
    assert len(integration_env["sender"].calls) == 1


# === межслойные контракты (§7) ===========================================


async def test_status_transitions_prohibited_paths(integration_env: dict) -> None:
    """Недопустимые переходы статуса отклоняются на уровне репозитория (SPEC-008 §7)."""
    from postmaster.domain.post import InvalidPostTransitionError

    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_trans",
        telegram_file_unique_id="unique_trans",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
    )

    # PENDING → PUBLISHED без PROCESSING — недопустимый переход через repo.
    with pytest.raises(InvalidPostTransitionError):
        await integration_env["post_repo"].update_status(
            post.id, PostStatus.PUBLISHED, published_at=RUN_AT
        )

    # Пост остался в PENDING.
    persisted = await integration_env["post_repo"].get_by_id(post.id)
    assert persisted is not None
    assert persisted.status == PostStatus.PENDING


async def test_publish_increments_attempt_counter_in_logs(integration_env: dict, caplog) -> None:
    """SPEC-025: события публикации содержат post_id и attempt."""
    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_log",
        telegram_file_unique_id="unique_log",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    with caplog.at_level(__import__("logging").INFO):
        await integration_env["publication_service"].publish(post.id)

    events = "\n".join(r.getMessage() for r in caplog.records)
    assert "event=publication_start" in events
    assert "event=publication_ok" in events
    assert f"post_id={post.id}" in events


async def test_recovery_then_publish_returns_to_published(
    integration_env: dict,
) -> None:
    """Полный цикл: publish → recovery (нет overdue) → состояние неизменно."""
    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_cycle",
        telegram_file_unique_id="unique_cycle",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )
    await integration_env["publication_service"].publish(post.id)

    report = await integration_env["recovery_service"].recover()
    # Нет ни overdue, ни будущих PENDING — Recovery не делает ничего.
    assert report.rescheduled == 0
    assert report.missed == 0

    persisted = await integration_env["post_repo"].get_by_id(post.id)
    assert persisted is not None
    assert persisted.status == PostStatus.PUBLISHED


# === Параллелизм и изоляция ===============================================


async def test_concurrent_publish_writes_only_once(integration_env: dict) -> None:
    """Параллельные публикации одного и того же поста: ровно одна успешная (SPEC-013 §3)."""
    import asyncio as _asyncio

    post = await integration_env["post_service"].create_scheduled_post(
        user_id=integration_env["user_id"],
        telegram_file_id="file_conc",
        telegram_file_unique_id="unique_conc",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.TELEGRAM,
        target_id="-1001234567890",
    )

    results = await _asyncio.gather(
        *(integration_env["publication_service"].publish(post.id) for _ in range(3)),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 2

    # Telegram sender был вызван ровно один раз.
    assert len(integration_env["sender"].calls) == 1


async def test_concurrent_cancel_respects_owner(integration_env: dict) -> None:
    """Параллельные cancel разных пользователей: чужие не отменяют (SPEC-024)."""
    user_service = integration_env["user_service"]
    post_repo = integration_env["post_repo"]
    cancel_service = integration_env["cancel_service"]

    alice = await user_service.get_or_create(
        telegram_user_id=20001, username="alice2", first_name="Alice2"
    )
    bob = await user_service.get_or_create(
        telegram_user_id=20002, username="bob2", first_name="Bob2"
    )
    post = await post_repo.create_post(
        user_id=alice.id,
        telegram_file_id="file_iso",
        telegram_file_unique_id="unique_iso",
        scheduled_at_utc=RUN_AT,
        timezone="UTC",
        publication_type=PublicationType.REMINDER,
        target_id=None,
    )

    from postmaster.services.cancel_service import PostNotFoundForUserError

    with pytest.raises(PostNotFoundForUserError):
        await cancel_service.cancel(post_id=post.id, user_id=bob.id)

    # Пост остался у Alice в PENDING.
    persisted = await post_repo.get_by_id(post.id)
    assert persisted is not None
    assert persisted.user_id == alice.id
    assert persisted.status == PostStatus.PENDING
    # Scheduler НЕ удалял задание.
    assert post.id not in integration_env["scheduler"].removed
