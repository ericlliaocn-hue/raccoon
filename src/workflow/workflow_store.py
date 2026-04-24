"""工作流模板持久化（SQLite）

从 JSON 文件迁移到 SQLite，与 MemCore / ScheduleStore 共享 data/memcore.db：
- workflows 表：存储工作流模板配置
- workflow_executions 表：存储执行中间状态（崩溃恢复）
- 启动时自动从旧 JSON 目录迁移（兼容升级）
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.config import RaccoonConfig
from src.types import WorkflowEntry, WorkflowStep

logger = structlog.get_logger(__name__)

# DDL
_CREATE_WORKFLOWS = """
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    conversation_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT 'workflow',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CREATE_WORKFLOW_EXECUTIONS = """
CREATE TABLE IF NOT EXISTS workflow_executions (
    execution_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    step_results TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
);
"""

_CREATE_EXEC_INDEX = """
CREATE INDEX IF NOT EXISTS idx_wf_exec_workflow
ON workflow_executions(workflow_id, status);
"""


class WorkflowStore:
    """工作流模板持久化存储：SQLite"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._db_path = self._config.db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._workflows: dict[str, WorkflowEntry] = {}
        self._init_db()
        self._load_all()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        """初始化数据库表"""
        with self._connect() as conn:
            conn.executescript(
                _CREATE_WORKFLOWS + _CREATE_WORKFLOW_EXECUTIONS + _CREATE_EXEC_INDEX
            )
            conn.commit()
        logger.info("workflow_store_initialized", db=str(self._db_path))

    def _load_all(self) -> None:
        """从 SQLite 加载所有工作流到内存"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM workflows").fetchall()

        for row in rows:
            try:
                entry = self._row_to_entry(row)
                self._workflows[entry.workflow_id] = entry
            except Exception:
                logger.warning("workflow_load_failed", workflow_id=row["workflow_id"])

        # 兼容：如果 SQLite 为空但旧 JSON 目录存在，自动迁移
        if not self._workflows:
            self._migrate_from_json()

        logger.info("workflows_loaded", count=len(self._workflows))

    def _row_to_entry(self, row: sqlite3.Row) -> WorkflowEntry:
        """将数据库行转换为 WorkflowEntry"""
        steps = []
        if row["steps"]:
            try:
                steps = [WorkflowStep.model_validate(s) for s in json.loads(row["steps"])]
            except (json.JSONDecodeError, TypeError):
                pass

        return WorkflowEntry(
            workflow_id=row["workflow_id"],
            name=row["name"],
            description=row["description"],
            steps=steps,
            conversation_id=row["conversation_id"],
            user_id=row["user_id"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else datetime.now(timezone.utc),
        )

    def _entry_to_params(self, entry: WorkflowEntry) -> tuple:
        """将 WorkflowEntry 转换为数据库参数"""
        steps_json = json.dumps(
            [s.model_dump() for s in entry.steps],
            ensure_ascii=False,
        )
        return (
            entry.workflow_id,
            entry.name,
            entry.description,
            steps_json,
            entry.conversation_id,
            entry.user_id,
            entry.created_at.isoformat(),
            entry.updated_at.isoformat(),
        )

    # ─── 兼容迁移 ─────────────────────────────────────────────

    def _migrate_from_json(self) -> None:
        """从旧版 JSON 目录迁移到 SQLite"""
        old_dir = self._config.workflows_dir
        if not old_dir.exists():
            return

        count = 0
        for path in old_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entry = WorkflowEntry.model_validate(data)
                self._workflows[entry.workflow_id] = entry
                self._upsert_to_db(entry)
                count += 1
            except Exception:
                logger.warning("workflow_migrate_failed", path=str(path))

        if count:
            logger.info("workflows_migrated_from_json", count=count)
            try:
                old_dir.rename(old_dir.with_name("workflows.migrated"))
            except Exception:
                pass

    # ─── CRUD ──────────────────────────────────────────────────

    def add(self, entry: WorkflowEntry) -> WorkflowEntry:
        """添加工作流模板"""
        self._workflows[entry.workflow_id] = entry
        self._upsert_to_db(entry)
        logger.info("workflow_added", workflow_id=entry.workflow_id, name=entry.name)
        return entry

    def get(self, workflow_id: str) -> WorkflowEntry | None:
        """获取工作流模板"""
        return self._workflows.get(workflow_id)

    def get_by_name(self, name: str) -> WorkflowEntry | None:
        """按名称获取工作流模板"""
        for entry in self._workflows.values():
            if entry.name == name:
                return entry
        return None

    def remove(self, workflow_id: str) -> WorkflowEntry | None:
        """删除工作流模板"""
        entry = self._workflows.pop(workflow_id, None)
        if entry:
            self._delete_from_db(workflow_id)
            logger.info("workflow_removed", workflow_id=workflow_id, name=entry.name)
        return entry

    def update(self, entry: WorkflowEntry) -> WorkflowEntry:
        """更新工作流模板"""
        entry.updated_at = datetime.now(timezone.utc)
        self._workflows[entry.workflow_id] = entry
        self._upsert_to_db(entry)
        return entry

    def list_all(self) -> list[WorkflowEntry]:
        """列出所有工作流模板"""
        return list(self._workflows.values())

    # ─── 执行状态持久化（崩溃恢复）──────────────────────────

    def save_execution_state(
        self,
        execution_id: str,
        workflow_id: str,
        conversation_id: str,
        current_step: int,
        status: str,
        step_results: dict[int, Any],
        error: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        """保存执行中间状态（用于崩溃恢复）"""
        now = datetime.now(timezone.utc).isoformat()
        results_json = json.dumps(
            {str(k): str(v)[:500] for k, v in step_results.items()},
            ensure_ascii=False,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO workflow_executions (
                    execution_id, workflow_id, conversation_id, current_step,
                    status, step_results, error, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    current_step=excluded.current_step,
                    status=excluded.status,
                    step_results=excluded.step_results,
                    error=excluded.error,
                    finished_at=excluded.finished_at
                """,
                (execution_id, workflow_id, conversation_id, current_step,
                 status, results_json, error, started_at or now, finished_at),
            )
            conn.commit()

    def get_execution_state(self, execution_id: str) -> dict | None:
        """获取执行状态（用于崩溃恢复）"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        if row:
            return dict(row)
        return None

    def get_pending_executions(self) -> list[dict]:
        """获取所有未完成的执行（崩溃恢复用）"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_executions WHERE status = 'running'",
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_execution_state(self, execution_id: str) -> None:
        """清除执行状态"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM workflow_executions WHERE execution_id = ?",
                (execution_id,),
            )
            conn.commit()

    # ─── 持久化内部方法 ────────────────────────────────────────

    def _upsert_to_db(self, entry: WorkflowEntry) -> None:
        """写入或更新到 SQLite"""
        params = self._entry_to_params(entry)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO workflows (
                    workflow_id, name, description, steps, conversation_id,
                    user_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    name=excluded.name, description=excluded.description,
                    steps=excluded.steps, conversation_id=excluded.conversation_id,
                    user_id=excluded.user_id, updated_at=excluded.updated_at
                """,
                params,
            )
            conn.commit()

    def _delete_from_db(self, workflow_id: str) -> None:
        """从 SQLite 删除"""
        with self._connect() as conn:
            conn.execute("DELETE FROM workflows WHERE workflow_id = ?", (workflow_id,))
            conn.execute(
                "DELETE FROM workflow_executions WHERE workflow_id = ?",
                (workflow_id,),
            )
            conn.commit()

    # ─── 兼容旧接口 ────────────────────────────────────────────

    def recover(self) -> int:
        """兼容旧接口：从 SQLite 加载时已在 _load_all 中完成"""
        return len(self._workflows)