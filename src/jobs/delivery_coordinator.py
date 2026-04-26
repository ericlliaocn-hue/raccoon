"""Job 投递协调器：处理 deferred delivery 重试与 DLQ。"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import Awaitable, Callable

import structlog

from src.config import RaccoonConfig
from src.jobs.manager import JobManager
from src.jobs.models import JobRecord, JobStatus
from src.jobs.store import JobStore

logger = structlog.get_logger(__name__)

DeliveryHandler = Callable[[JobRecord], Awaitable[None] | None]


class JobDeliveryCoordinator:
    """异步投递协调器。"""

    def __init__(
        self,
        manager: JobManager,
        store: JobStore,
        config: RaccoonConfig | None = None,
        *,
        delivery_handler: DeliveryHandler | None = None,
        poll_interval_seconds: float = 2.0,
        batch_size: int = 50,
    ) -> None:
        self._manager = manager
        self._store = store
        self._config = config or RaccoonConfig()
        self._delivery_handler = delivery_handler or _default_delivery_handler
        self._poll_interval_seconds = max(0.5, float(poll_interval_seconds))
        self._batch_size = max(1, int(batch_size))
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "job_delivery_coordinator_started",
            poll_interval_seconds=self._poll_interval_seconds,
            batch_size=self._batch_size,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("job_delivery_coordinator_stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    async def run_once(self, *, now: datetime | None = None) -> int:
        """单次扫描（便于测试或手动触发）。"""
        current = now or datetime.now(timezone.utc)
        due_jobs = self._store.list_due_deliveries(now=current, limit=self._batch_size)
        for job in due_jobs:
            await self._dispatch(job)
        return len(due_jobs)

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
                await asyncio.sleep(self._poll_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("job_delivery_coordinator_loop_error", error=str(exc))
                await asyncio.sleep(self._poll_interval_seconds)

    async def _dispatch(self, job: JobRecord) -> None:
        if job.status in {JobStatus.CANCELLED, JobStatus.FAILED}:
            return

        try:
            result = self._delivery_handler(job)
            if asyncio.iscoroutine(result):
                await result
            await self._manager.mark_delivery_sent(job.job_id)
            logger.info(
                "job_delivery_sent",
                job_id=job.job_id,
                attempts=job.delivery_attempts,
            )
        except Exception as exc:
            error = str(exc)
            next_attempt = job.delivery_attempts + 1
            if next_attempt >= job.delivery_max_attempts:
                await self._manager.mark_delivery_dlq(job.job_id, error=error)
                logger.error(
                    "job_delivery_dlq",
                    job_id=job.job_id,
                    attempts=next_attempt,
                    max_attempts=job.delivery_max_attempts,
                    error=error,
                )
                return

            retry_in = _backoff_seconds(next_attempt)
            await self._manager.mark_delivery_retry(
                job.job_id,
                error=error,
                retry_in_seconds=retry_in,
            )
            logger.warning(
                "job_delivery_retry_scheduled",
                job_id=job.job_id,
                attempts=next_attempt,
                retry_in_seconds=retry_in,
                error=error,
            )


async def _default_delivery_handler(job: JobRecord) -> None:
    """默认投递行为：当前阶段仅标记成功（用于 Web/SSE 可见性）。"""
    _ = job
    return None


def _backoff_seconds(attempt: int) -> int:
    # 2, 4, 8, 16 ... 最大 5 分钟
    return min(300, int(math.pow(2, max(1, attempt))))
