"""Job 数据模型。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    UPLOADING = "uploading"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.DELIVERED, JobStatus.FAILED, JobStatus.CANCELLED}


class JobDeliveryState(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    RETRY = "retry"
    SENT = "sent"
    DLQ = "dlq"

    @property
    def is_terminal(self) -> bool:
        return self in {JobDeliveryState.SENT, JobDeliveryState.DLQ, JobDeliveryState.NOT_REQUIRED}


class JobArtifact(BaseModel):
    artifact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    mime_type: str = "application/octet-stream"
    size_bytes: int | None = None
    url: str | None = None
    preview_url: str | None = None
    sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobRecord(BaseModel):
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: str = "generic"
    conversation_id: str
    user_id: str = "anonymous"
    status: JobStatus = JobStatus.QUEUED
    progress: int = 0
    message: str = ""
    error: str | None = None
    estimated_seconds: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[JobArtifact] = Field(default_factory=list)
    delivery_state: JobDeliveryState = JobDeliveryState.NOT_REQUIRED
    delivery_attempts: int = 0
    delivery_max_attempts: int = 5
    next_delivery_at: datetime | None = None
    delivery_last_error: str | None = None
    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
