"""Обёртка над APScheduler (SPEC-010, SPEC-013, PM06-R2-003).

Scheduler работает в event loop приложения. Его задания вызывают прикладные сервисы,
а не Telegram API напрямую (BR-04). При подключённом PublicationService выполнение
делегируется в publication_service.execute(post_id) с защитой через atomic claim.

PM06-R2-003: каждое задание несёт версию расписания и исходное время запуска.
Перед захватом Post репозиторий повторно проверяет актуальные scheduled_at_utc и
schedule_version, поэтому старое задание после переноса не публикует пост раньше
времени. :meth:`SchedulerService.reconcile_pending` восстанавливает согласованность
БД и APScheduler без обязательного рестарта (вызывается фоновым циклом приложения).
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

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

JOB_ID_PREFIX = "post:"
RECONCILE_INTERVAL_SECONDS = 30


class PostExecutionService(Protocol):
    async def execute(self, post_id: int) -> object: ...


class SchedulerService:
    """Запускает и останавливает Scheduler вместе с приложением (US-02)."""

    def __init__(
        self,
        post_repository: PostRepository | None = None,
        publication_service: PostExecutionService | None = None,
        *,
        publication_executor: Any | None = None,
        reconcile_interval_seconds: int = RECONCILE_INTERVAL_SECONDS,
    ) -> None:
        # Все времена в БД хранятся в UTC (SPEC-006), поэтому Scheduler работает в UTC.
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._posts = post_repository
        self._publication_service = publication_service
        # Совместимость со SPEC-010/011: адаптер с методом publish(post_id).
        self._publication_executor = publication_executor
        self._reconcile_interval = reconcile_interval_seconds
        # Одна регистрация просроченного PENDING-поста: APScheduler удаляет
        # одноразовую date-задачу после срабатывания, повторная постановка создала
        # бы лишнее срабатывание. Recovery обрабатывает такие посты отдельно.
        self._reconciled: dict[int, tuple[int, datetime]] = {}

    def schedule_post(
        self,
        *,
        post_id: int,
        run_at: datetime,
        schedule_version: int | None = None,
    ) -> None:
        """Одноразовое задание по id; при срабатывании запускает централизованное выполнение.

        ``schedule_version`` привязывает задание к актуальной версии расписания.
        Для заданий без версии (старые адаптеры/БД) защита обеспечивается проверкой
        scheduled_at_utc в репозитории при claim.
        """
        if run_at.tzinfo is None or run_at.utcoffset() != timedelta(0):
            raise ValueError("run_at должен быть aware UTC")
        if self._posts is None and self._publication_service is None:
            if self._publication_executor is None:
                raise RuntimeError("Scheduler требует PostRepository для чтения задания")
        if not self.running:
            raise RuntimeError("Scheduler не запущен")
        moment = run_at.astimezone(UTC)
        args: tuple[Any, ...]
        if schedule_version is None:
            args = (post_id,)
        else:
            args = (post_id, int(schedule_version), moment)
            self._reconciled[post_id] = (int(schedule_version), moment)
        self._scheduler.add_job(
            self._on_post_due,
            trigger="date",
            run_date=moment,
            args=args,
            id=f"{JOB_ID_PREFIX}{post_id}",
            replace_existing=True,
            # При простое процесса просроченная задача должна быть исполнена, а
            # не отброшена стандартной grace-паузой в одну секунду.
            misfire_grace_time=None,
        )
        log_event(
            logger,
            EVENT_SCHEDULER_JOB_ADDED,
            post_id=post_id,
            run_at=moment.isoformat(),
            schedule_version=schedule_version,
        )

    def remove_job(self, post_id: int) -> bool:
        """Удаляет задание поста из Scheduler. Возвращает True, если задание было."""
        self._reconciled.pop(post_id, None)
        if not self.running:
            logger.info("Scheduler не запущен, задание не удалено post_id=%s", post_id)
            return False
        try:
            removed = self._scheduler.remove_job(f"{JOB_ID_PREFIX}{post_id}")
        except Exception:
            logger.exception("Не удалось удалить задание Scheduler post_id=%s", post_id)
            return False
        if removed:
            log_event(
                logger,
                EVENT_SCHEDULER_JOB_REMOVED,
                level=logging.INFO,
                post_id=post_id,
            )
        return removed

    async def _on_post_due(
        self,
        post_id: int,
        schedule_version: int | None = None,
        run_at: datetime | None = None,
    ) -> None:
        if self._publication_executor is not None:
            # Legacy-контракт SPEC-010: перечитываем Post из БД перед публикацией.
            assert self._posts is not None
            post = await self._posts.get_by_id(post_id)
            if post is None:
                logger.info("Due job skipped: post not found post_id=%s", post_id)
                return
            await self._publication_executor.publish(post_id)
            return
        if self._publication_service is not None:
            kwargs: dict[str, Any] = {}
            if schedule_version is not None:
                kwargs["expected_schedule_version"] = int(schedule_version)
            if run_at is not None:
                kwargs["expected_run_at"] = run_at
            if not kwargs:
                # Прямой вызов без параметров задания (legacy SPEC-010/PM-03/PM-04,
                # Recovery): источник уже решил, что пост пора публиковать, поэтому
                # повторная проверка due-time не применяется. Задания APScheduler
                # всегда несут версию и run_at — для них защита сохраняется.
                kwargs["ignore_schedule"] = True
            if kwargs:
                await self._publication_service.execute(post_id, **kwargs)
            else:
                await self._publication_service.execute(post_id)
            return
        assert self._posts is not None
        post = await self._posts.get_by_id(post_id)
        logger.warning(
            "Post due but execution is not implemented post_id=%s exists=%s",
            post_id,
            post is not None,
        )

    async def reconcile_pending(self, *, now: datetime | None = None) -> int:
        """Сверяет PENDING-посты БД с заданиями APScheduler без рестарта.

        Возвращает число изменённых заданий. Просроченные посты регистрируются с
        run_date=now, чтобы существующий Recovery/MissedPostHandler обработал их.
        """
        if self._posts is None or not self.running:
            return 0
        posts = await self._posts.list_pending()
        jobs = {
            job.id: job
            for job in self._scheduler.get_jobs()
            if job.id and job.id.startswith(JOB_ID_PREFIX)
        }
        changes = 0
        active: set[int] = set()
        for post in posts:
            active.add(post.id)
            job_id = f"{JOB_ID_PREFIX}{post.id}"
            # APScheduler сам немедленно исполнит просроченную date-задачу;
            # подмена run_at на now скрыла бы исходное расписание и усложнила
            # проверку stale-job. Recovery/overdue-посты сохраняют поведение.
            run_at = post.scheduled_at_utc
            expected_args = (post.id, post.schedule_version, run_at)
            expected_marker = (post.schedule_version, run_at)
            job = jobs.pop(job_id, None)
            if job is None and self._reconciled.get(post.id) == expected_marker:
                # Одноразовое задание уже исполнено/снято как просроченное.
                continue
            if job is None or tuple(job.args or ()) != expected_args:
                self.schedule_post(
                    post_id=post.id,
                    run_at=run_at,
                    schedule_version=post.schedule_version,
                )
                changes += 1
            else:
                self._reconciled[post.id] = expected_marker
        for post_id in set(self._reconciled) - active:
            self._reconciled.pop(post_id, None)
        for stale_job_id in jobs:
            try:
                self._scheduler.remove_job(stale_job_id)
                changes += 1
            except Exception:
                logger.exception("Reconcile could not remove stale job %s", stale_job_id)
        if changes:
            logger.info("Scheduler reconciled %s jobs", changes)
        return changes

    @property
    def reconcile_interval(self) -> int:
        return self._reconcile_interval

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
