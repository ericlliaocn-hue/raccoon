"""工作流模板持久化

存储到 workflows/ 目录（JSON），参考 ScheduleStore 的持久化模式。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.config import RaccoonConfig
from src.types import WorkflowEntry, WorkflowStep

logger = structlog.get_logger(__name__)


class WorkflowStore:
    """工作流模板持久化存储"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._dir = self._config.workflows_dir
        self._workflows: dict[str, WorkflowEntry] = {}

    def recover(self) -> None:
        """从磁盘恢复工作流模板"""
        self._dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for f in self._dir.glob("*.json"):
            try:
                data = json.loads(f.read_text("utf-8"))
                entry = WorkflowEntry.model_validate(data)
                self._workflows[entry.workflow_id] = entry
                count += 1
            except Exception as e:
                logger.warning("workflow_recover_failed", file=f.name, error=str(e))
        logger.info("workflow_store_recovered", count=count)

    def add(self, entry: WorkflowEntry) -> WorkflowEntry:
        """添加工作流模板"""
        self._workflows[entry.workflow_id] = entry
        self._save(entry)
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
            self._delete_file(entry)
        return entry

    def update(self, entry: WorkflowEntry) -> WorkflowEntry:
        """更新工作流模板"""
        entry.updated_at = datetime.now(timezone.utc)
        self._workflows[entry.workflow_id] = entry
        self._save(entry)
        return entry

    def list_all(self) -> list[WorkflowEntry]:
        """列出所有工作流模板"""
        return list(self._workflows.values())

    # ─── 持久化 ─────────────────────────────────────────────

    def _save(self, entry: WorkflowEntry) -> None:
        """保存到磁盘"""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{entry.workflow_id}.json"
        path.write_text(entry.model_dump_json(indent=2), "utf-8")

    def _delete_file(self, entry: WorkflowEntry) -> None:
        """从磁盘删除"""
        path = self._dir / f"{entry.workflow_id}.json"
        if path.exists():
            path.unlink()
