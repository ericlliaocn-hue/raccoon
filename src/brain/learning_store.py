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
    scenario_id TEXT,
    status TEXT NOT NULL,
    first_pass INTEGER NOT NULL DEFAULT 0,
    final_success INTEGER NOT NULL DEFAULT 0,
    failure_code TEXT,
    quality_score REAL NOT NULL DEFAULT 0,
    skill_name TEXT,
    candidate_skill_name TEXT,
    candidate_confidence REAL NOT NULL DEFAULT 0,
    staging_dir TEXT NOT NULL DEFAULT '',
    analysis TEXT NOT NULL DEFAULT '{}',
    validation TEXT NOT NULL DEFAULT '{}',
    artifacts TEXT NOT NULL DEFAULT '{}',
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

_CREATE_SCENARIO_INDEX = """
CREATE INDEX IF NOT EXISTS idx_learning_runs_scenario
ON learning_runs(scenario_id, created_at DESC);
"""

_CREATE_FAILURE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_learning_runs_failure
ON learning_runs(failure_code, created_at DESC);
"""

_MIGRATION_COLUMNS: dict[str, str] = {
    "scenario_id": "ALTER TABLE learning_runs ADD COLUMN scenario_id TEXT",
    "first_pass": "ALTER TABLE learning_runs ADD COLUMN first_pass INTEGER NOT NULL DEFAULT 0",
    "final_success": "ALTER TABLE learning_runs ADD COLUMN final_success INTEGER NOT NULL DEFAULT 0",
    "failure_code": "ALTER TABLE learning_runs ADD COLUMN failure_code TEXT",
    "quality_score": "ALTER TABLE learning_runs ADD COLUMN quality_score REAL NOT NULL DEFAULT 0",
    "artifacts": "ALTER TABLE learning_runs ADD COLUMN artifacts TEXT NOT NULL DEFAULT '{}'",
}


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
            conn.executescript(
                _CREATE_LEARNING_RUNS
                + _CREATE_LEARNING_RUNS_INDEX
                + _CREATE_SCENARIO_INDEX
                + _CREATE_FAILURE_INDEX
            )
            self._ensure_columns(conn)
            conn.commit()
        logger.info("learning_store_initialized", db=str(self._db_path))

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(learning_runs)").fetchall()
        }
        for column, ddl in _MIGRATION_COLUMNS.items():
            if column not in existing:
                conn.execute(ddl)
                logger.info("learning_store_column_added", column=column)

    def add(self, run: LearningRun) -> LearningRun:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO learning_runs (
                    run_id, conversation_id, user_id, request_text, source_task_id,
                    scenario_id, status, first_pass, final_success, failure_code, quality_score,
                    skill_name, candidate_skill_name, candidate_confidence,
                    staging_dir, analysis, validation, artifacts, dependencies, repair_count,
                    approval_id, approval_status, execution_result, reply, error,
                    schedule_created, attempt_log, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def list_recent(
        self,
        limit: int = 50,
        *,
        scenario_id: str | None = None,
        failure_code: str | None = None,
        first_pass: bool | None = None,
    ) -> list[LearningRun]:
        where: list[str] = []
        params: list[object] = []
        if scenario_id:
            where.append("scenario_id = ?")
            params.append(scenario_id)
        if failure_code:
            where.append("failure_code = ?")
            params.append(failure_code)
        if first_pass is not None:
            where.append("first_pass = ?")
            params.append(1 if first_pass else 0)

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM learning_runs {where_sql} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def aggregate_core_scenarios(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    scenario_id,
                    COUNT(*) AS total_runs,
                    SUM(CASE WHEN first_pass = 1 THEN 1 ELSE 0 END) AS first_pass_runs,
                    SUM(CASE WHEN final_success = 1 THEN 1 ELSE 0 END) AS final_success_runs,
                    AVG(quality_score) AS avg_quality_score,
                    AVG(CASE WHEN repair_count IS NULL THEN 0 ELSE repair_count END) AS avg_repair_count
                FROM learning_runs
                WHERE scenario_id IS NOT NULL AND scenario_id != ''
                GROUP BY scenario_id
                ORDER BY scenario_id ASC
                """
            ).fetchall()

        result: list[dict[str, object]] = []
        for row in rows:
            total = int(row["total_runs"] or 0)
            first_pass_runs = int(row["first_pass_runs"] or 0)
            final_success_runs = int(row["final_success_runs"] or 0)
            result.append(
                {
                    "scenario_id": row["scenario_id"],
                    "runs": total,
                    "first_pass_rate": (first_pass_runs / total) if total else 0.0,
                    "final_success_rate": (final_success_runs / total) if total else 0.0,
                    "avg_repair_count": float(row["avg_repair_count"] or 0.0),
                    "avg_quality_score": float(row["avg_quality_score"] or 0.0),
                }
            )
        return result

    def top_failure_clusters(self, days: int = 7, limit: int = 10) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    failure_code,
                    COUNT(*) AS failures,
                    COUNT(DISTINCT conversation_id) AS conversations
                FROM learning_runs
                WHERE failure_code IS NOT NULL
                  AND failure_code != ''
                  AND final_success = 0
                  AND datetime(created_at) >= datetime('now', ?)
                GROUP BY failure_code
                ORDER BY failures DESC, conversations DESC
                LIMIT ?
                """,
                (f"-{int(days)} day", int(limit)),
            ).fetchall()
        return [
            {
                "failure_code": row["failure_code"],
                "failures": int(row["failures"] or 0),
                "conversations": int(row["conversations"] or 0),
            }
            for row in rows
        ]

    def list_new_skill_candidates(
        self,
        *,
        days: int = 7,
        min_failures: int = 5,
        min_conversations: int = 2,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    failure_code,
                    COALESCE(scenario_id, '') AS scenario_id,
                    COUNT(*) AS failures,
                    COUNT(DISTINCT conversation_id) AS conversations,
                    AVG(quality_score) AS avg_quality_score,
                    MAX(updated_at) AS last_seen
                FROM learning_runs
                WHERE failure_code IS NOT NULL
                  AND failure_code != ''
                  AND final_success = 0
                  AND datetime(created_at) >= datetime('now', ?)
                GROUP BY failure_code, COALESCE(scenario_id, '')
                HAVING COUNT(*) >= ? AND COUNT(DISTINCT conversation_id) >= ?
                ORDER BY failures DESC, conversations DESC, last_seen DESC
                LIMIT ?
                """,
                (f"-{int(days)} day", int(min_failures), int(min_conversations), int(limit)),
            ).fetchall()

            candidates: list[dict[str, object]] = []
            for row in rows:
                failure_code = str(row["failure_code"] or "")
                scenario_id = str(row["scenario_id"] or "")
                sample = conn.execute(
                    """
                    SELECT request_text, skill_name, run_id
                    FROM learning_runs
                    WHERE failure_code = ?
                      AND COALESCE(scenario_id, '') = ?
                      AND final_success = 0
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (failure_code, scenario_id),
                ).fetchone()
                sample_request = str(sample["request_text"]) if sample else ""
                sample_skill = str(sample["skill_name"]) if sample and sample["skill_name"] else ""
                sample_run_id = str(sample["run_id"]) if sample and sample["run_id"] else ""
                suggested_skill = sample_skill or f"candidate_{failure_code}"
                if scenario_id:
                    suggested_skill = f"{suggested_skill}_{scenario_id}"
                suggested_skill = suggested_skill.lower().replace(" ", "_")

                candidates.append(
                    {
                        "failure_code": failure_code,
                        "scenario_id": scenario_id or None,
                        "failures": int(row["failures"] or 0),
                        "conversations": int(row["conversations"] or 0),
                        "avg_quality_score": float(row["avg_quality_score"] or 0.0),
                        "last_seen": row["last_seen"],
                        "sample_request": sample_request,
                        "sample_run_id": sample_run_id,
                        "suggested_skill_name": suggested_skill,
                        "triggered": True,
                    }
                )
            return candidates

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
            run.scenario_id,
            run.status.value if isinstance(run.status, LearningRunStatus) else str(run.status),
            1 if run.first_pass else 0,
            1 if run.final_success else 0,
            run.failure_code,
            float(run.quality_score or 0.0),
            run.skill_name,
            run.candidate_skill_name,
            float(run.candidate_confidence),
            run.staging_dir,
            json.dumps(run.analysis, ensure_ascii=False),
            json.dumps(run.validation, ensure_ascii=False),
            json.dumps(run.artifacts, ensure_ascii=False),
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
            scenario_id=row["scenario_id"],
            status=LearningRunStatus(row["status"]),
            first_pass=bool(row["first_pass"]),
            final_success=bool(row["final_success"]),
            failure_code=row["failure_code"],
            quality_score=float(row["quality_score"] or 0.0),
            skill_name=row["skill_name"],
            candidate_skill_name=row["candidate_skill_name"],
            candidate_confidence=row["candidate_confidence"] or 0.0,
            staging_dir=row["staging_dir"] or "",
            analysis=parse_json(row["analysis"], {}),
            validation=parse_json(row["validation"], {}),
            artifacts=parse_json(row["artifacts"], {}),
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
