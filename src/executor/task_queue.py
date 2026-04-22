"""任务队列（含 conversation_id 绑定）

- 内存中维护活跃任务
- 支持按 task_id / conversation_id 查询
- 任务持久化到 tasks/ 目录（JSON）
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import structlog
from src.config import RaccoonConfig
from src.types import Task, TaskStatus

logger = structlog.get_logger(__name__)


class TaskQueue:
    """任务队列：内存 + 持久化"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._tasks: dict[str, Task] = {}
        self._tasks_dir = Path(self._config.skills_dir).parent / "tasks"
        self._tasks_dir.mkdir(parents=True, exist_ok=True)

    def add(self, task: Task) -> None:
        """添加任务到队列"""
        self._tasks[task.task_id] = task
        self._persist(task)
        logger.info("task_added", task_id=task.task_id, skill=task.skill_name)

    def get(self, task_id: str) -> Task | None:
        """按 task_id 查找"""
        return self._tasks.get(task_id)

    def get_by_conversation(self, conversation_id: str) -> list[Task]:
        """按 conversation_id 查找所有任务"""
        return [
            t for t in self._tasks.values()
            if t.conversation_id == conversation_id
        ]

    def get_active_by_conversation(self, conversation_id: str) -> list[Task]:
        """获取对话中的活跃任务（非终态）"""
        terminal = {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}
        return [
            t for t in self._tasks.values()
            if t.conversation_id == conversation_id and t.status not in terminal
        ]

    def update(self, task: Task) -> None:
        """更新任务"""
        task.updated_at = datetime.now(timezone.utc)
        self._tasks[task.task_id] = task
        self._persist(task)
        logger.debug("task_updated", task_id=task.task_id, status=task.status.value)

    def remove(self, task_id: str) -> None:
        """移除任务"""
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._delete_persisted(task_id)

    def all_tasks(self) -> Iterator[Task]:
        """迭代所有任务"""
        yield from self._tasks.values()

    def active_count(self) -> int:
        """活跃任务数"""
        terminal = {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED}
        return sum(1 for t in self._tasks.values() if t.status not in terminal)

    # ─── 持久化 ────────────────────────────────────────────────

    def _persist(self, task: Task) -> None:
        """持久化任务到 JSON 文件"""
        path = self._tasks_dir / f"{task.task_id}.json"
        path.write_text(task.model_dump_json(indent=2), encoding="utf-8")

    def _delete_persisted(self, task_id: str) -> None:
        """删除持久化文件"""
        path = self._tasks_dir / f"{task_id}.json"
        if path.exists():
            path.unlink()

    def recover(self) -> int:
        """从持久化目录恢复任务（重启后调用）"""
        count = 0
        for path in self._tasks_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                task = Task.model_validate(data)
                self._tasks[task.task_id] = task
                count += 1
            except Exception:
                logger.warning("task_recover_failed", path=str(path))
        if count:
            logger.info("tasks_recovered", count=count)
        return count
