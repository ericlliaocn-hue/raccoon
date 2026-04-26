"""Job 管理器：状态流转、事件发射、持久化封装。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Iterable

import structlog

from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.jobs.models import JobArtifact, JobDeliveryState, JobRecord, JobStatus
from src.jobs.store import JobStore

logger = structlog.get_logger(__name__)


class JobManager:
    """长任务管理器。"""

    def __init__(
        self,
        event_bus: EventBus | None = None,
        *,
        store: JobStore | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._store = store
        self._lock = asyncio.Lock()
        self._jobs: dict[str, JobRecord] = {}

    async def create_job(
        self,
        *,
        kind: str,
        conversation_id: str,
        user_id: str = "anonymous",
        payload: dict | None = None,
        metadata: dict | None = None,
        estimated_seconds: int | None = None,
        message: str = "",
        delivery_required: bool | None = None,
        delivery_max_attempts: int = 5,
    ) -> JobRecord:
        now = _utcnow()
        payload_data = dict(payload or {})
        metadata_data = dict(metadata or {})
        require_delivery = self._resolve_delivery_required(
            delivery_required=delivery_required,
            payload=payload_data,
            metadata=metadata_data,
        )

        job = JobRecord(
            kind=kind,
            conversation_id=conversation_id,
            user_id=user_id,
            payload=payload_data,
            metadata=metadata_data,
            status=JobStatus.QUEUED,
            message=message,
            estimated_seconds=estimated_seconds,
            delivery_state=JobDeliveryState.PENDING if require_delivery else JobDeliveryState.NOT_REQUIRED,
            delivery_max_attempts=max(1, int(delivery_max_attempts)),
            next_delivery_at=now if require_delivery else None,
            created_at=now,
            updated_at=now,
        )
        await self._save(job)
        await self._emit(EventType.JOB_CREATED, job, payload={"kind": job.kind})
        return job.model_copy(deep=True)

    async def get_job(self, job_id: str) -> JobRecord | None:
        job = await self._load(job_id)
        return job.model_copy(deep=True) if job else None

    async def list_jobs(
        self,
        *,
        limit: int = 100,
        status: JobStatus | None = None,
        conversation_id: str | None = None,
        delivery_state: JobDeliveryState | None = None,
    ) -> list[JobRecord]:
        if self._store:
            jobs = self._store.list_jobs(
                limit=max(1, int(limit)),
                status=status,
                conversation_id=conversation_id,
                delivery_state=delivery_state,
            )
            return [item.model_copy(deep=True) for item in jobs]

        async with self._lock:
            items = list(self._jobs.values())
            if status is not None:
                items = [item for item in items if item.status == status]
            if conversation_id:
                items = [item for item in items if item.conversation_id == conversation_id]
            if delivery_state is not None:
                items = [item for item in items if item.delivery_state == delivery_state]
            items.sort(key=lambda item: item.created_at, reverse=True)
            return [item.model_copy(deep=True) for item in items[: max(1, int(limit))]]

    async def mark_running(self, job_id: str, *, message: str | None = None) -> JobRecord | None:
        async with self._lock:
            job = await self._get_mutable(job_id)
            if not job or job.status.is_terminal:
                return None
            job.status = JobStatus.RUNNING
            if message is not None:
                job.message = message
            job.updated_at = _utcnow()
            await self._persist_locked(job)
            copied = job.model_copy(deep=True)
        await self._emit(
            EventType.JOB_PROGRESS,
            copied,
            payload={"progress": copied.progress, "message": copied.message, "status": copied.status.value},
        )
        return copied

    async def update_progress(
        self,
        job_id: str,
        *,
        progress: int,
        message: str | None = None,
    ) -> JobRecord | None:
        async with self._lock:
            job = await self._get_mutable(job_id)
            if not job or job.status.is_terminal:
                return None
            if job.status == JobStatus.QUEUED:
                job.status = JobStatus.RUNNING
            job.progress = max(0, min(100, int(progress)))
            if message is not None:
                job.message = message
            job.updated_at = _utcnow()
            await self._persist_locked(job)
            copied = job.model_copy(deep=True)

        await self._emit(
            EventType.JOB_PROGRESS,
            copied,
            payload={"progress": copied.progress, "message": copied.message, "status": copied.status.value},
        )
        return copied

    async def complete(
        self,
        job_id: str,
        *,
        message: str = "",
        artifacts: Iterable[JobArtifact] | None = None,
        delivery_required: bool | None = None,
    ) -> JobRecord | None:
        async with self._lock:
            job = await self._get_mutable(job_id)
            if not job or job.status.is_terminal:
                return None

            if artifacts is not None:
                job.artifacts = [item.model_copy(deep=True) for item in artifacts]
            if message:
                job.message = message

            job.progress = 100
            now = _utcnow()
            require_delivery = self._resolve_delivery_required(
                delivery_required=delivery_required,
                payload=job.payload,
                metadata=job.metadata,
                current_state=job.delivery_state,
            )

            if require_delivery:
                job.status = JobStatus.UPLOADING
                job.delivery_state = JobDeliveryState.PENDING
                job.next_delivery_at = now
                job.completed_at = None
            else:
                job.status = JobStatus.DELIVERED
                job.delivery_state = JobDeliveryState.NOT_REQUIRED
                job.next_delivery_at = None
                job.delivery_last_error = None
                job.completed_at = now
            job.updated_at = now

            await self._persist_locked(job)
            copied = job.model_copy(deep=True)

        if copied.status == JobStatus.UPLOADING:
            await self._emit(
                EventType.JOB_PROGRESS,
                copied,
                payload={
                    "progress": copied.progress,
                    "message": copied.message or "任务已完成，正在投递结果",
                    "status": copied.status.value,
                    "delivery_state": copied.delivery_state.value,
                },
            )
        else:
            await self._emit(EventType.JOB_COMPLETED, copied)
        return copied

    async def fail(self, job_id: str, *, error: str, message: str = "") -> JobRecord | None:
        async with self._lock:
            job = await self._get_mutable(job_id)
            if not job:
                return None
            if job.status.is_terminal:
                return job.model_copy(deep=True)

            job.status = JobStatus.FAILED
            job.error = error
            if message:
                job.message = message
            job.delivery_state = JobDeliveryState.DLQ if job.delivery_state in {JobDeliveryState.PENDING, JobDeliveryState.RETRY} else job.delivery_state
            job.next_delivery_at = None
            job.updated_at = _utcnow()
            job.completed_at = job.updated_at
            await self._persist_locked(job)
            copied = job.model_copy(deep=True)

        await self._emit(EventType.JOB_FAILED, copied, payload={"error": copied.error, "message": copied.message})
        return copied

    async def cancel(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            job = await self._get_mutable(job_id)
            if not job:
                return None
            if job.status.is_terminal:
                return job.model_copy(deep=True)

            job.status = JobStatus.CANCELLED
            job.next_delivery_at = None
            job.updated_at = _utcnow()
            job.completed_at = job.updated_at
            await self._persist_locked(job)
            copied = job.model_copy(deep=True)

        await self._emit(EventType.JOB_CANCELLED, copied)
        return copied

    async def mark_delivery_retry(
        self,
        job_id: str,
        *,
        error: str,
        retry_in_seconds: int,
    ) -> JobRecord | None:
        emitted_failed = False
        async with self._lock:
            job = await self._get_mutable(job_id)
            if not job or job.status.is_terminal:
                return None

            next_attempt = job.delivery_attempts + 1
            if next_attempt >= job.delivery_max_attempts:
                copied = await self._mark_delivery_dlq_locked(job, error=error)
                emitted_failed = True
            else:
                job.status = JobStatus.UPLOADING
                job.delivery_state = JobDeliveryState.RETRY
                job.delivery_attempts = next_attempt
                job.delivery_last_error = error
                job.next_delivery_at = _utcnow() + timedelta(seconds=max(1, int(retry_in_seconds)))
                job.updated_at = _utcnow()
                await self._persist_locked(job)
                copied = job.model_copy(deep=True)

        if emitted_failed:
            await self._emit(EventType.JOB_FAILED, copied, payload={"error": copied.error, "message": copied.message})
            return copied

        await self._emit(
            EventType.JOB_PROGRESS,
            copied,
            payload={
                "progress": copied.progress,
                "status": copied.status.value,
                "delivery_state": copied.delivery_state.value,
                "delivery_attempts": copied.delivery_attempts,
                "delivery_max_attempts": copied.delivery_max_attempts,
                "delivery_error": copied.delivery_last_error,
                "next_delivery_at": copied.next_delivery_at.isoformat() if copied.next_delivery_at else None,
            },
        )
        return copied

    async def mark_delivery_sent(self, job_id: str, *, message: str | None = None) -> JobRecord | None:
        async with self._lock:
            job = await self._get_mutable(job_id)
            if not job:
                return None

            job.status = JobStatus.DELIVERED
            job.delivery_state = JobDeliveryState.SENT
            job.delivery_last_error = None
            job.next_delivery_at = None
            if message:
                job.message = message
            job.updated_at = _utcnow()
            job.completed_at = job.updated_at
            await self._persist_locked(job)
            copied = job.model_copy(deep=True)

        await self._emit(EventType.JOB_COMPLETED, copied)
        return copied

    async def mark_delivery_dlq(self, job_id: str, *, error: str) -> JobRecord | None:
        async with self._lock:
            job = await self._get_mutable(job_id)
            if not job:
                return None
            copied = await self._mark_delivery_dlq_locked(job, error=error)

        await self._emit(EventType.JOB_FAILED, copied, payload={"error": copied.error, "message": copied.message})
        return copied

    async def _mark_delivery_dlq_locked(self, job: JobRecord, *, error: str) -> JobRecord:
        job.status = JobStatus.FAILED
        job.delivery_state = JobDeliveryState.DLQ
        job.delivery_attempts = max(job.delivery_attempts + 1, job.delivery_max_attempts)
        job.delivery_last_error = error
        job.error = error
        job.next_delivery_at = None
        job.updated_at = _utcnow()
        job.completed_at = job.updated_at
        await self._persist_locked(job)
        return job.model_copy(deep=True)

    async def _save(self, job: JobRecord) -> None:
        async with self._lock:
            await self._persist_locked(job)

    async def _load(self, job_id: str) -> JobRecord | None:
        if self._store:
            return self._store.get(job_id)
        async with self._lock:
            item = self._jobs.get(job_id)
            return item.model_copy(deep=True) if item else None

    async def _get_mutable(self, job_id: str) -> JobRecord | None:
        if self._store:
            return self._store.get(job_id)
        return self._jobs.get(job_id)

    async def _persist_locked(self, job: JobRecord) -> None:
        if self._store:
            self._store.upsert(job)
            return
        self._jobs[job.job_id] = job.model_copy(deep=True)

    async def _emit(self, event_type: EventType, job: JobRecord, *, payload: dict | None = None) -> None:
        if not self._event_bus:
            return

        merged_payload = self._to_event_payload(job)
        if payload:
            merged_payload.update(payload)
        await self._event_bus.emit(
            make_event(
                event_type,
                conversation_id=job.conversation_id,
                user_id=job.user_id,
                payload=merged_payload,
            )
        )

    @staticmethod
    def _resolve_delivery_required(
        *,
        delivery_required: bool | None,
        payload: dict,
        metadata: dict,
        current_state: JobDeliveryState | None = None,
    ) -> bool:
        if delivery_required is not None:
            return bool(delivery_required)
        if current_state in {JobDeliveryState.PENDING, JobDeliveryState.RETRY, JobDeliveryState.SENT}:
            return True
        if bool(metadata.get("delivery_required")):
            return True
        if bool(payload.get("delivery_required")):
            return True
        return False

    @staticmethod
    def _to_event_payload(job: JobRecord) -> dict:
        return {
            "job_id": job.job_id,
            "kind": job.kind,
            "status": job.status.value,
            "progress": job.progress,
            "message": job.message,
            "error": job.error,
            "artifacts": [item.model_dump(mode="json") for item in job.artifacts],
            "trace_id": job.trace_id,
            "delivery_state": job.delivery_state.value,
            "delivery_attempts": job.delivery_attempts,
            "delivery_max_attempts": job.delivery_max_attempts,
            "next_delivery_at": job.next_delivery_at.isoformat() if job.next_delivery_at else None,
            "delivery_last_error": job.delivery_last_error,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
