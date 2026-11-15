"""Обёртка над APScheduler (SPEC-010, SPEC-013).

Scheduler работает в event loop приложения. Его задания вызывают прикладные сервисы,
а не Telegram API напрямую (BR-04). При подключённом PublicationService выполнение
делегируется в publication_service.execute(post_id) с защитой через atomic claim (SPEC-013 §19).

PM-06 / BUG-04: задание помнит время, на которое оно зарегистрировано (schedule version).
Перед публикацией версия сверяется с фактическим ``scheduled_at_utc`` и статусом в БД:
если публикацию перенесли, отменили или время в БД не совпадает, срабатывание старого
задания не приводит к публикации. При расхождении Scheduler сам перерегистрирует
задание по актуальному времени из БД, поэтому согласованность восстанавливается
без перезапуска приложения.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from postmaster.repositories.post_repository import PostRepository
from postmaster.utils.logging_config import (
    EVENT_SCHEDULER_JOB_ADDED,
    EVENT_SCHEDULER_JOB_REMOVED,
    EVENT_SCHEDULER_START,
    EVENT_SCHEDULER_STOP,
    log_event,
)

logger = logging.getLogger(__name__)

# Сколько секунд просроченное задание ещё считается сработавшим (APScheduler).
# Актуальность времени проверяется отдельно по БД, поэтому значение влияет только
# на надёжность доставки, а не на корректность публикации.
MISFIRE_GRACE_SECONDS = 300


class PostExecutionService(Protocol):
    async def execute(self, post_id: int) -> object: ...


class SchedulerService:
    """Запускает и останавливает Scheduler вместе с приложением (US-02)."""

    def __init__(
        self,
        post_repository: PostRepository | None = None,
        publication_service: PostExecutionService | None = None,
        *,
        publication_executor: PostExecutionService | None = None,
    ) -> None:
        # Все времена в БД хранятся в UTC (SPEC-006), поэтому Scheduler работает в UTC.
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._posts = post_repository
        # ``publication_executor`` — историческое имя того же контракта (execute(post_id)),
        # которым пользуются тесты SPEC-010/011/018 и внешние интеграции. Оба имени
        # взаимозаменяемы, публичный интерфейс не ломается.
        self._publication_service = (
            publication_service if publication_service is not None else publication_executor
        )
        # Допуск на задержку срабатывания. Актуальность задания определяется сравнением
        # его версии (ожидаемого времени) с БД, поэтому опоздание безопасно: при
        # расхождении задание перерегистрируется, а не публикует пост (PM-06 / BUG-04).
        self._misfire_grace_time = MISFIRE_GRACE_SECONDS

    def schedule_post(self, *, post_id: int, run_at: datetime) -> None:
        """Одноразовое задание по id; при срабатывании запускает централизованное выполнение.

        ``run_at`` — ожидаемое время публикации (schedule version задания): при срабатывании
        оно сверяется с БД, поэтому старое задание не может опубликовать пост не вовремя.
        """
        if run_at.tzinfo is None or run_at.utcoffset() != timedelta(0):
            raise ValueError("run_at должен быть aware UTC")
        if self._posts is None and self._publication_service is None:
            raise RuntimeError("Scheduler требует PostRepository для чтения задания")
        if not self.running:
            raise RuntimeError("Scheduler не запущен")
        expected = run_at.astimezone(UTC)
        self._scheduler.add_job(
            self._on_post_due,
            trigger="date",
            run_date=expected,
            args=(post_id,),
            kwargs={"expected_run_at": expected},
            id=f"post:{post_id}",
            replace_existing=True,
            misfire_grace_time=self._misfire_grace_time,
            coalesce=True,
            max_instances=1,
        )
        log_event(
            logger,
            EVENT_SCHEDULER_JOB_ADDED,
            post_id=post_id,
            run_at=expected.isoformat(),
        )

    def remove_job(self, post_id: int) -> bool:
        """Удаляет задание поста из Scheduler. Возвращает True, если задание было.

        Вызывается после успешной отмены публикации (SPEC-018 §1). Ошибка удаления
        не должна возвращать пост в PENDING: отмена уже зафиксирована в БД.
        """
        if not self.running:
            logger.info("Scheduler не запущен, задание не удалено post_id=%s", post_id)
            return False
        try:
            # APScheduler удаляет задание без возвращаемого значения и бросает
            # JobLookupError, если задания нет (PM-06: раньше успех не отличался от отказа).
            self._scheduler.remove_job(f"post:{post_id}")
        except JobLookupError:
            logger.info("Задание Scheduler не найдено post_id=%s", post_id)
            return False
        except Exception:
            logger.exception("Не удалось удалить задание Scheduler post_id=%s", post_id)
            return False
        log_event(
            logger,
            EVENT_SCHEDULER_JOB_REMOVED,
            level=logging.INFO,
            post_id=post_id,
        )
        return True

    async def _on_post_due(self, post_id: int, expected_run_at: datetime | None = None) -> None:
        """Срабатывание задания. Устаревшее задание не публикует пост (PM-06 / BUG-04)."""
        fresh = await self._fresh_post(post_id) if self._posts is not None else None
        if self._posts is not None:
            if fresh is None:
                logger.info("Scheduler fire skipped post_id=%s: post not found", post_id)
                return
            if not self._is_job_actual(post_id, fresh, expected_run_at):
                await self._resync_job(post_id, fresh)
                return
        if self._publication_service is not None:
            await self._publication_service.execute(post_id)
            return
        # Пока конкретные Publisher не подключены (SPEC-014/015), оставляем PENDING
        # для будущего recovery, не меняя статус в самом Scheduler (SPEC-013 §19).
        logger.warning(
            "Post due but execution is not implemented post_id=%s exists=%s",
            post_id,
            fresh is not None,
        )

    async def _fresh_post(self, post_id: int):
        """Актуальная запись из БД или None. Ошибка чтения не отменяет публикацию."""
        assert self._posts is not None
        try:
            return await self._posts.get_by_id(post_id)
        except Exception:
            logger.exception("Scheduler could not read post post_id=%s", post_id)
            return None

    @staticmethod
    def _is_job_actual(post_id: int, post, expected_run_at: datetime | None) -> bool:
        """Задание актуально, если пост всё ещё PENDING и время совпадает с БД."""
        status = getattr(post, "status", None)
        pending = getattr(status, "value", status)
        if pending != "PENDING":
            logger.info("Scheduler fire skipped post_id=%s status=%s", post_id, pending)
            return False
        if expected_run_at is None:
            # Задание без версии (совместимость): время сверяет publication layer.
            return True
        actual = getattr(post, "scheduled_at_utc", None)
        if actual is None:
            return False
        if actual != expected_run_at:
            logger.warning(
                "Scheduler stale job detected post_id=%s expected=%s actual=%s",
                post_id,
                expected_run_at.isoformat(),
                actual.isoformat(),
            )
            return False
        return True

    async def _resync_job(self, post_id: int, post) -> None:
        """Перерегистрирует задание по актуальному времени из БД без рестарта."""
        status = getattr(post, "status", None)
        pending = getattr(status, "value", status)
        if pending != "PENDING":
            self.remove_job(post_id)
            return
        actual = getattr(post, "scheduled_at_utc", None)
        if actual is None:
            return
        if not self.running:
            return
        try:
            self.schedule_post(post_id=post_id, run_at=actual)
        except Exception:
            logger.exception("Scheduler resync failed post_id=%s", post_id)
            return
        log_event(
            logger,
            EVENT_SCHEDULER_JOB_ADDED,
            post_id=post_id,
            run_at=actual.isoformat(),
        )

    async def resync_post(self, post_id: int) -> bool:
        """Перерегистрирует задание поста по данным БД (PM-06 / BUG-04).

        Возвращает True, если задание актуально (перерегистрировано или снято как
        ненужное), и False, если синхронизация не удалась. Ошибка не скрывается
        успешным результатом: вызывающая сторона получает False.
        """
        if self._posts is None:
            return False
        try:
            post = await self._posts.get_by_id(post_id)
        except Exception:
            logger.exception("Scheduler resync could not read post post_id=%s", post_id)
            return False
        if post is None:
            self.remove_job(post_id)
            return True
        status = getattr(post, "status", None)
        if getattr(status, "value", status) != "PENDING":
            self.remove_job(post_id)
            return True
        if not self.running:
            return False
        try:
            self.schedule_post(post_id=post_id, run_at=post.scheduled_at_utc)
        except Exception:
            logger.exception("Scheduler resync failed post_id=%s", post_id)
            return False
        return True

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def start(self) -> None:
        """Запускает Scheduler. Вызывать из работающего event loop."""
        self._scheduler.start()
        log_event(logger, EVENT_SCHEDULER_START)

    def shutdown(self) -> None:
        """Останавливает Scheduler, если он запущен. Повторный вызов безопасен."""
        if not self.running:
            return
        self._scheduler.shutdown(wait=False)
        log_event(logger, EVENT_SCHEDULER_STOP)
