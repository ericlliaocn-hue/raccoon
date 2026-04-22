"""审批流引擎（MVP 简化：自动通过 + 记录审计日志）

完整版应包含：
- 风险等级评估
- 人工审批队列
- 超时自动拒绝

MVP：所有请求自动通过，仅记录审计日志。
"""

from __future__ import annotations

import structlog
from src.types import Task

logger = structlog.get_logger(__name__)


class ApprovalResult:
    """审批结果"""
    def __init__(self, approved: bool, reason: str = "") -> None:
        self.approved = approved
        self.reason = reason

    def __bool__(self) -> bool:
        return self.approved


class ApprovalEngine:
    """审批引擎"""

    def __init__(self, auto_approve: bool = True) -> None:
        self._auto_approve = auto_approve

    async def review(self, task: Task, risk_level: str = "low") -> ApprovalResult:
        """审批任务

        MVP: auto_approve=True 时全部通过，仅记录日志
        """
        if self._auto_approve:
            logger.info(
                "approval_auto_approved",
                task_id=task.task_id,
                skill=task.skill_name,
                risk_level=risk_level,
            )
            return ApprovalResult(approved=True, reason="auto_approved")

        # 非自动模式：高风险拒绝，其他通过
        if risk_level == "high":
            logger.warning(
                "approval_rejected",
                task_id=task.task_id,
                skill=task.skill_name,
                risk_level=risk_level,
            )
            return ApprovalResult(approved=False, reason="high_risk_requires_manual_approval")

        logger.info(
            "approval_approved",
            task_id=task.task_id,
            skill=task.skill_name,
            risk_level=risk_level,
        )
        return ApprovalResult(approved=True, reason="low_risk_auto_approved")
