"""学习运行记录持久化（SQLite）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import structlog

from src.config import RaccoonConfig
from src.types import LearningRun, LearningRunStatus

logger = structlog.get_logger(__name__)

_CREATE_LEARNING_RUNS = """
CREATE TABLE IF NOT EXISTS learning_runs (
    run_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'anonymous',
    request_text TEXT NOT NULL,
    source_task_id TEXT,
    status TEXT NOT NULL,
    skill_name TEXT,
    candidate_skill_name TEXT,
    candidate_confidence REAL NOT NULL DEFAULT 0,
    staging_dir TEXT NOT NULL DEFAULT '',
    analysis TEXT NOT NULL DEFAULT '{}',
    validation TEXT NOT NULL DEFAULT '{}',
    dependencies TEXT NOT NULL DEFAULT '[]',
    repair_count INTEGER NOT NULL DEFAULT 0,
    approval_id TEXT,
    approval_status TEXT,
    execution_result TEXT,
    reply TEXT,
    error TEXT,
    schedule_created TEXT,
    attempt_log TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CREATE_LEARNING_RUNS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_learning_runs_created_at
ON learning_runs(created_at DESC);
"""


class LearningRunStore:
    """学习运行记录存储。"""

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
            conn.executescript(_CREATE_LEARNING_RUNS + _CREATE_LEARNING_RUNS_INDEX)
            conn.commit()
        logger.info("learning_store_initialized", db=str(self._db_path))

    def add(self, run: LearningRun) -> LearningRun:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO learning_runs (
                    run_id, conversation_id, user_id, request_text, source_task_id,
                    status, skill_name, candidate_skill_name, candidate_confidence,
                    staging_dir, analysis, validation, dependencies, repair_count,
                    approval_id, approval_status, execution_result, reply, error,
                    schedule_created, attempt_log, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._params(run),
            )
            conn.commit()
        return run

    def update(self, run: LearningRun) -> LearningRun:
        run.updated_at = datetime.now(timezone.utc)
        return self.add(run)

    def get(self, run_id: str) -> LearningRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM learning_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        return self._row_to_run(row)

    def list_recent(self, limit: int = 50) -> list[LearningRun]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM learning_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def find_by_approval(self, approval_id: str) -> LearningRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM learning_runs WHERE approval_id = ? ORDER BY created_at DESC LIMIT 1",
                (approval_id,),
            ).fetchone()
        if not row:
            return None
        return self._row_to_run(row)

    def _params(self, run: LearningRun) -> tuple:
        return (
            run.run_id,
            run.conversation_id,
            run.user_id,
            run.request_text,
            run.source_task_id,
            run.status.value if isinstance(run.status, LearningRunStatus) else str(run.status),
            run.skill_name,
            run.candidate_skill_name,
            float(run.candidate_confidence),
            run.staging_dir,
            json.dumps(run.analysis, ensure_ascii=False),
            json.dumps(run.validation, ensure_ascii=False),
            json.dumps(run.dependencies, ensure_ascii=False),
            int(run.repair_count),
            run.approval_id,
            run.approval_status,
            json.dumps(run.execution_result, ensure_ascii=False) if run.execution_result is not None else None,
            run.reply,
            run.error,
            run.schedule_created,
            json.dumps(run.attempt_log, ensure_ascii=False),
            run.created_at.isoformat(),
            run.updated_at.isoformat(),
        )

    def _row_to_run(self, row: sqlite3.Row) -> LearningRun:
        def parse_json(value: str | None, default):
            if not value:
                return default
            try:
                return json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return default

        return LearningRun(
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            user_id=row["user_id"],
            request_text=row["request_text"],
            source_task_id=row["source_task_id"],
            status=LearningRunStatus(row["status"]),
            skill_name=row["skill_name"],
            candidate_skill_name=row["candidate_skill_name"],
            candidate_confidence=row["candidate_confidence"] or 0.0,
            staging_dir=row["staging_dir"] or "",
            analysis=parse_json(row["analysis"], {}),
            validation=parse_json(row["validation"], {}),
            dependencies=parse_json(row["dependencies"], []),
            repair_count=row["repair_count"] or 0,
            approval_id=row["approval_id"],
            approval_status=row["approval_status"],
            execution_result=parse_json(row["execution_result"], None),
            reply=row["reply"],
            error=row["error"],
            schedule_created=row["schedule_created"],
            attempt_log=parse_json(row["attempt_log"], []),
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else datetime.now(timezone.utc),
        )
