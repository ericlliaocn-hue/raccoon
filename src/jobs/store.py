"""Job 持久化存储（SQLite）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import structlog

from src.config import RaccoonConfig
from src.jobs.models import JobArtifact, JobDeliveryState, JobRecord, JobStatus

logger = structlog.get_logger(__name__)

_CREATE_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'anonymous',
    status TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    estimated_seconds INTEGER,
    payload TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}',
    delivery_state TEXT NOT NULL DEFAULT 'not_required',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivery_max_attempts INTEGER NOT NULL DEFAULT 5,
    next_delivery_at TEXT,
    delivery_last_error TEXT,
    trace_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
"""

_CREATE_JOBS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_jobs_created_at
ON jobs(created_at DESC);
"""

_CREATE_DELIVERY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_jobs_delivery_due
ON jobs(delivery_state, next_delivery_at, updated_at);
"""

_CREATE_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS job_artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    name TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes INTEGER,
    url TEXT,
    preview_url TEXT,
    sha256 TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
"""

_CREATE_ARTIFACTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_job_artifacts_job_id
ON job_artifacts(job_id);
"""


class JobStore:
    """Job 持久化存储。"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._db_path = self._config.db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_CREATE_JOBS + _CREATE_JOBS_INDEX + _CREATE_DELIVERY_INDEX + _CREATE_ARTIFACTS + _CREATE_ARTIFACTS_INDEX)
            conn.commit()
        logger.info("job_store_initialized", db=str(self._db_path))

    def upsert(self, job: JobRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO jobs (
                    job_id, kind, conversation_id, user_id, status, progress, message, error,
                    estimated_seconds, payload, metadata,
                    delivery_state, delivery_attempts, delivery_max_attempts,
                    next_delivery_at, delivery_last_error,
                    trace_id, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.kind,
                    job.conversation_id,
                    job.user_id,
                    job.status.value,
                    int(job.progress),
                    job.message,
                    job.error,
                    job.estimated_seconds,
                    json.dumps(job.payload, ensure_ascii=False),
                    json.dumps(job.metadata, ensure_ascii=False),
                    job.delivery_state.value,
                    int(job.delivery_attempts),
                    int(job.delivery_max_attempts),
                    job.next_delivery_at.isoformat() if job.next_delivery_at else None,
                    job.delivery_last_error,
                    job.trace_id,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                    job.completed_at.isoformat() if job.completed_at else None,
                ),
            )
            conn.execute("DELETE FROM job_artifacts WHERE job_id = ?", (job.job_id,))
            for item in job.artifacts:
                conn.execute(
                    """
                    INSERT INTO job_artifacts (
                        artifact_id, job_id, name, mime_type, size_bytes, url, preview_url, sha256, metadata, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.artifact_id,
                        job.job_id,
                        item.name,
                        item.mime_type,
                        item.size_bytes,
                        item.url,
                        item.preview_url,
                        item.sha256,
                        json.dumps(item.metadata, ensure_ascii=False),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            conn.commit()

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            artifacts = conn.execute(
                "SELECT * FROM job_artifacts WHERE job_id = ? ORDER BY created_at ASC",
                (job_id,),
            ).fetchall()
        return self._row_to_job(row, artifacts)

    def list_jobs(
        self,
        *,
        limit: int = 100,
        status: JobStatus | None = None,
        conversation_id: str | None = None,
        delivery_state: JobDeliveryState | None = None,
    ) -> list[JobRecord]:
        where: list[str] = []
        params: list[object] = []
        if status is not None:
            where.append("status = ?")
            params.append(status.value)
        if conversation_id:
            where.append("conversation_id = ?")
            params.append(conversation_id)
        if delivery_state is not None:
            where.append("delivery_state = ?")
            params.append(delivery_state.value)

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {where_sql} ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, int(limit))),
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            artifacts_by_job: dict[str, list[sqlite3.Row]] = {job_id: [] for job_id in job_ids}
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                artifacts = conn.execute(
                    f"SELECT * FROM job_artifacts WHERE job_id IN ({placeholders}) ORDER BY created_at ASC",
                    tuple(job_ids),
                ).fetchall()
                for item in artifacts:
                    artifacts_by_job[str(item["job_id"])].append(item)

        return [self._row_to_job(row, artifacts_by_job.get(str(row["job_id"]), [])) for row in rows]

    def list_due_deliveries(self, *, now: datetime, limit: int = 100) -> list[JobRecord]:
        now_iso = now.isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE delivery_state IN ('pending', 'retry')
                  AND (
                    next_delivery_at IS NULL
                    OR next_delivery_at <= ?
                  )
                ORDER BY COALESCE(next_delivery_at, updated_at) ASC
                LIMIT ?
                """,
                (now_iso, max(1, int(limit))),
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            artifacts_by_job: dict[str, list[sqlite3.Row]] = {job_id: [] for job_id in job_ids}
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                artifacts = conn.execute(
                    f"SELECT * FROM job_artifacts WHERE job_id IN ({placeholders}) ORDER BY created_at ASC",
                    tuple(job_ids),
                ).fetchall()
                for item in artifacts:
                    artifacts_by_job[str(item["job_id"])].append(item)
        return [self._row_to_job(row, artifacts_by_job.get(str(row["job_id"]), [])) for row in rows]

    def _row_to_job(self, row: sqlite3.Row, artifacts: list[sqlite3.Row]) -> JobRecord:
        return JobRecord(
            job_id=str(row["job_id"]),
            kind=str(row["kind"]),
            conversation_id=str(row["conversation_id"]),
            user_id=str(row["user_id"]),
            status=JobStatus(str(row["status"])),
            progress=int(row["progress"] or 0),
            message=str(row["message"] or ""),
            error=row["error"],
            estimated_seconds=row["estimated_seconds"],
            payload=self._load_json(row["payload"], {}),
            metadata=self._load_json(row["metadata"], {}),
            artifacts=[
                JobArtifact(
                    artifact_id=str(item["artifact_id"]),
                    name=str(item["name"]),
                    mime_type=str(item["mime_type"] or "application/octet-stream"),
                    size_bytes=item["size_bytes"],
                    url=item["url"],
                    preview_url=item["preview_url"],
                    sha256=item["sha256"],
                    metadata=self._load_json(item["metadata"], {}),
                )
                for item in artifacts
            ],
            delivery_state=JobDeliveryState(str(row["delivery_state"] or "not_required")),
            delivery_attempts=int(row["delivery_attempts"] or 0),
            delivery_max_attempts=int(row["delivery_max_attempts"] or 5),
            next_delivery_at=self._parse_dt(row["next_delivery_at"]),
            delivery_last_error=row["delivery_last_error"],
            trace_id=str(row["trace_id"]),
            created_at=self._parse_dt(row["created_at"]) or datetime.now(timezone.utc),
            updated_at=self._parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
            completed_at=self._parse_dt(row["completed_at"]),
        )

    @staticmethod
    def _load_json(value: object, default: object) -> object:
        if value is None:
            return default
        try:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            return json.loads(str(value))
        except (json.JSONDecodeError, TypeError, ValueError):
            return default

    @staticmethod
    def _parse_dt(value: object) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
