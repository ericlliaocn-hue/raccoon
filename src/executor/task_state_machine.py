"""任务状态机（PENDING→RUNNING→CANCELLING→CANCELLED）

合法转换：
PENDING → RUNNING, CANCELLED
RUNNING → SUCCESS, FAILED, CANCELLING
CANCELLING → CANCELLED

非法转换抛 TransitionError。
"""

from __future__ import annotations

import structlog
from src.types import Task, TaskStatus, VALID_TRANSITIONS

logger = structlog.get_logger(__name__)


class TransitionError(Exception):
    """非法状态转换"""
    def __init__(self, task_id: str, from_status: TaskStatus, to_status: TaskStatus) -> None:
        self.task_id = task_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Invalid transition: {task_id} {from_status.value} → {to_status.value}"
        )


class TaskStateMachine:
    """任务状态机"""

    @staticmethod
    def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
        """检查是否可以转换"""
        return target in VALID_TRANSITIONS.get(current, set())

    @staticmethod
    def transition(task: Task, target: TaskStatus) -> Task:
        """执行状态转换，返回更新后的 Task"""
        if not TaskStateMachine.can_transition(task.status, target):
            raise TransitionError(task.task_id, task.status, target)

        old_status = task.status
        task.status = target
        from datetime import datetime, timezone
        task.updated_at = datetime.now(timezone.utc)

        logger.info(
            "task_transition",
            task_id=task.task_id,
            from_status=old_status.value,
            to_status=target.value,
        )
        return task

    @staticmethod
    def request_cancel(task: Task) -> Task:
        """请求取消任务：RUNNING → CANCELLING，设置 cancel_token"""
        task.cancel_token = True
        if task.status == TaskStatus.PENDING:
            return TaskStateMachine.transition(task, TaskStatus.CANCELLED)
        if task.status == TaskStatus.RUNNING:
            return TaskStateMachine.transition(task, TaskStatus.CANCELLING)
        # 已经在终态，忽略
        return task
