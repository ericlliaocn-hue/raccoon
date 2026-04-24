"""审批引擎

完整版实现：
- 风险等级评估：根据 Skill risk_level + permissions 自动判定
- 人工审批队列：auto_approve=false 时，高风险操作进入等待状态
- 超时自动拒绝：默认 5 分钟审批超时自动取消任务
- 审批通知：需要审批时推送到配置的通知渠道
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

import structlog
from src.types import EventType, SkillMetadata, Task, make_event

if TYPE_CHECKING:
    from src.eventbus.bus import EventBus

logger = structlog.get_logger(__name__)


class ApprovalStatus(str, Enum):
    """审批状态"""
    PENDING = "pending"       # 等待审批
    APPROVED = "approved"     # 已批准
    REJECTED = "rejected"     # 已拒绝
    EXPIRED = "expired"       # 已超时
    AUTO_APPROVED = "auto_approved"  # 自动批准


class ApprovalResult:
    """审批结果"""
    def __init__(
        self,
        approved: bool,
        reason: str = "",
        status: ApprovalStatus = ApprovalStatus.APPROVED,
        approval_id: str | None = None,
    ) -> None:
        self.approved = approved
        self.reason = reason
        self.status = status
        self.approval_id = approval_id

    def __bool__(self) -> bool:
        return self.approved


# 风险等级评估规则
# permissions 中包含这些关键词时提升风险等级
_HIGH_RISK_PERMISSIONS = {"subprocess"}
_MEDIUM_RISK_PERMISSIONS = {"network", "filesystem"}


class RiskAssessor:
    """风险等级评估器"""

    @staticmethod
    def assess(metadata: SkillMetadata) -> str:
        """根据 Skill 元数据评估风险等级

        规则：
        1. 如果 metadata.risk_level 已明确设置（非 low），直接使用
        2. 根据 permissions 自动判定：
           - subprocess → high
           - filesystem → medium
           - network → medium
           - 无权限 → low
        3. requires_approval=True → high（显式要求人工确认）

        Returns:
            "low" / "medium" / "high"
        """
        # 优先使用显式设置
        if metadata.risk_level == "high":
            return "high"
        if metadata.risk_level == "medium":
            return "medium"

        # 根据 permissions 推断
        perms = set(metadata.permissions)
        if perms & _HIGH_RISK_PERMISSIONS:
            return "high"
        if perms & _MEDIUM_RISK_PERMISSIONS:
            return "medium"

        # requires_approval 标记进入人工审批通道
        if metadata.requires_approval:
            return "high"

        return "low"


class ApprovalEntry:
    """审批队列中的条目"""

    def __init__(
        self,
        task: Task,
        risk_level: str,
        timeout_seconds: int = 300,
    ) -> None:
        import uuid
        self.approval_id: str = str(uuid.uuid4())
        self.task = task
        self.risk_level = risk_level
        self.status: ApprovalStatus = ApprovalStatus.PENDING
        self.created_at = datetime.now(timezone.utc)
        self.timeout_seconds = timeout_seconds
        self.resolved_at: datetime | None = None
        self.resolved_by: str | None = None
        self.reason: str = ""

    @property
    def is_expired(self) -> bool:
        """是否已超时"""
        if self.status != ApprovalStatus.PENDING:
            return False
        elapsed = (datetime.now(timezone.utc) - self.created_at).total_seconds()
        return elapsed >= self.timeout_seconds

    @property
    def remaining_seconds(self) -> float:
        """剩余超时秒数"""
        if self.status != ApprovalStatus.PENDING:
            return 0
        elapsed = (datetime.now(timezone.utc) - self.created_at).total_seconds()
        return max(0, self.timeout_seconds - elapsed)


class ApprovalEngine:
    """审批引擎"""

    def __init__(
        self,
        auto_approve: bool = True,
        approval_timeout_seconds: int = 300,
        notifier=None,
        event_bus: "EventBus | None" = None,
    ) -> None:
        self._auto_approve = auto_approve
        self._timeout = approval_timeout_seconds
        self._notifier = notifier  # Notifier 实例，用于审批通知
        self._event_bus = event_bus
        self._queue: dict[str, ApprovalEntry] = {}  # approval_id → entry
        self._risk_assessor = RiskAssessor()
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动超时清理任务"""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """停止超时清理任务"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    async def _cleanup_loop(self) -> None:
        """定期检查超时的审批"""
        while True:
            try:
                await asyncio.sleep(10)
                await self._check_timeouts()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("approval_cleanup_error")

    async def _check_timeouts(self) -> None:
        """检查并处理超时的审批"""
        for entry in list(self._queue.values()):
            if entry.is_expired:
                entry.status = ApprovalStatus.EXPIRED
                entry.resolved_at = datetime.now(timezone.utc)
                entry.reason = "approval_timeout"
                await self._emit_resolution(entry, approved=False, reason=entry.reason)
                logger.warning(
                    "approval_expired",
                    approval_id=entry.approval_id,
                    task_id=entry.task.task_id,
                    skill=entry.task.skill_name,
                )

    async def review(
        self,
        task: Task,
        metadata: SkillMetadata | None = None,
        risk_level: str | None = None,
    ) -> ApprovalResult:
        """审批任务

        Args:
            task: 待审批的任务
            metadata: Skill 元数据（用于风险评估）
            risk_level: 手动指定风险等级（覆盖自动评估）

        Returns:
            ApprovalResult
        """
        # 1. 风险等级评估
        if risk_level is None and metadata is not None:
            risk_level = self._risk_assessor.assess(metadata)
        elif risk_level is None:
            risk_level = "low"

        # 2. 自动批准模式
        if self._auto_approve:
            logger.info(
                "approval_auto_approved",
                task_id=task.task_id,
                skill=task.skill_name,
                risk_level=risk_level,
            )
            return ApprovalResult(
                approved=True,
                reason="auto_approved",
                status=ApprovalStatus.AUTO_APPROVED,
            )

        # 3. 仅高风险或显式 requires_approval 进入人工审批，其余自动通过
        requires_manual_approval = risk_level == "high" or (
            metadata is not None and metadata.requires_approval
        )
        if not requires_manual_approval:
            logger.info(
                "approval_risk_auto_approved",
                task_id=task.task_id,
                skill=task.skill_name,
                risk_level=risk_level,
            )
            return ApprovalResult(
                approved=True,
                reason=f"{risk_level}_risk_auto_approved",
                status=ApprovalStatus.APPROVED,
            )

        # 4. 高风险需要人工审批
        entry = ApprovalEntry(task=task, risk_level=risk_level, timeout_seconds=self._timeout)
        self._queue[entry.approval_id] = entry

        # 5. 发送审批通知
        await self._send_approval_notification(entry)

        logger.info(
            "approval_pending",
            approval_id=entry.approval_id,
            task_id=task.task_id,
            skill=task.skill_name,
            risk_level=risk_level,
            timeout=self._timeout,
        )

        return ApprovalResult(
            approved=False,
            reason="pending_manual_approval",
            status=ApprovalStatus.PENDING,
            approval_id=entry.approval_id,
        )

    async def approve(self, approval_id: str, approver: str = "manual") -> ApprovalResult:
        """批准审批

        Args:
            approval_id: 审批 ID
            approver: 审批人

        Returns:
            ApprovalResult
        """
        entry = self._queue.get(approval_id)
        if not entry:
            return ApprovalResult(False, "not_found", ApprovalStatus.REJECTED)
        if entry.status != ApprovalStatus.PENDING:
            return ApprovalResult(False, f"already_{entry.status.value}", entry.status)

        entry.status = ApprovalStatus.APPROVED
        entry.resolved_at = datetime.now(timezone.utc)
        entry.resolved_by = approver
        entry.reason = "manual_approved"

        logger.info(
            "approval_approved",
            approval_id=approval_id,
            task_id=entry.task.task_id,
            approver=approver,
        )

        await self._emit_resolution(entry, approved=True, reason=entry.reason)
        return ApprovalResult(True, "manual_approved", ApprovalStatus.APPROVED, approval_id)

    async def reject(self, approval_id: str, reason: str = "", rejector: str = "manual") -> ApprovalResult:
        """拒绝审批

        Args:
            approval_id: 审批 ID
            reason: 拒绝原因
            rejector: 拒绝人

        Returns:
            ApprovalResult
        """
        entry = self._queue.get(approval_id)
        if not entry:
            return ApprovalResult(False, "not_found", ApprovalStatus.REJECTED)
        if entry.status != ApprovalStatus.PENDING:
            return ApprovalResult(False, f"already_{entry.status.value}", entry.status)

        entry.status = ApprovalStatus.REJECTED
        entry.resolved_at = datetime.now(timezone.utc)
        entry.resolved_by = rejector
        entry.reason = reason

        logger.info(
            "approval_rejected",
            approval_id=approval_id,
            task_id=entry.task.task_id,
            reason=reason,
            rejector=rejector,
        )

        await self._emit_resolution(entry, approved=False, reason=entry.reason)
        return ApprovalResult(False, reason or "manual_rejected", ApprovalStatus.REJECTED, approval_id)

    def get_pending(self) -> list[ApprovalEntry]:
        """获取待审批列表"""
        return [e for e in self._queue.values() if e.status == ApprovalStatus.PENDING]

    def get_entry(self, approval_id: str) -> ApprovalEntry | None:
        """获取审批条目"""
        return self._queue.get(approval_id)

    async def _emit_resolution(
        self,
        entry: ApprovalEntry,
        *,
        approved: bool,
        reason: str,
    ) -> None:
        """向执行器广播审批结果，供挂起任务恢复或失败。"""
        if not self._event_bus:
            return
        await self._event_bus.emit(
            make_event(
                EventType.APPROVAL_RESOLVED,
                conversation_id=entry.task.conversation_id,
                user_id=entry.task.user_id,
                task_id=entry.task.task_id,
                skill_name=entry.task.skill_name,
                payload={
                    "approval_id": entry.approval_id,
                    "task_id": entry.task.task_id,
                    "approved": approved,
                    "status": entry.status.value,
                    "reason": reason,
                    "risk_level": entry.risk_level,
                },
            )
        )

    async def _send_approval_notification(self, entry: ApprovalEntry) -> None:
        """发送审批通知"""
        if not self._notifier:
            return

        try:
            from src.notifier.templates import NotificationTemplate
            from src.notifier.channels.base import Priority

            rendered = NotificationTemplate.render(
                "approval",
                title=f"{entry.task.skill_name} ({entry.risk_level}风险)",
                body=f"任务: {entry.task.origin_message[:100]}\nSkill: {entry.task.skill_name}\n风险等级: {entry.risk_level}",
                timeout=f"{entry.timeout_seconds // 60} 分钟",
                approve_url=f"/approvals/{entry.approval_id}/approve",
                reject_url=f"/approvals/{entry.approval_id}/reject",
            )

            await self._notifier.notify(
                title=rendered["title"],
                body=rendered["body"],
                priority=Priority.HIGH,
                dedup_key=f"approval:{entry.approval_id}",
            )
        except Exception as e:
            logger.error("approval_notification_failed", error=str(e))
