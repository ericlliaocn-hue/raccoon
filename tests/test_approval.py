"""审批引擎单元测试"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.supervisor.approval_engine import (
    ApprovalEngine,
    ApprovalEntry,
    ApprovalResult,
    ApprovalStatus,
    RiskAssessor,
)
from src.types import SkillMetadata, Task, TaskStatus


# ─── Helpers ──────────────────────────────────────────────────


def _make_task(skill_name: str = "test_skill", message: str = "test message") -> Task:
    return Task(
        conversation_id="test-conv",
        user_id="test_user",
        origin_message=message,
        skill_name=skill_name,
    )


def _make_metadata(
    risk_level: str = "low",
    permissions: list[str] | None = None,
    requires_approval: bool = False,
) -> SkillMetadata:
    return SkillMetadata(
        name="test_skill",
        risk_level=risk_level,
        permissions=permissions or [],
        requires_approval=requires_approval,
    )


# ─── RiskAssessor Tests ──────────────────────────────────────


class TestRiskAssessor:
    """风险等级评估器测试"""

    def test_explicit_high(self):
        meta = _make_metadata(risk_level="high")
        assert RiskAssessor.assess(meta) == "high"

    def test_explicit_medium(self):
        meta = _make_metadata(risk_level="medium")
        assert RiskAssessor.assess(meta) == "medium"

    def test_explicit_low(self):
        meta = _make_metadata(risk_level="low")
        assert RiskAssessor.assess(meta) == "low"

    def test_subprocess_permission_is_high(self):
        meta = _make_metadata(permissions=["subprocess"])
        assert RiskAssessor.assess(meta) == "high"

    def test_filesystem_permission_is_high(self):
        meta = _make_metadata(permissions=["filesystem"])
        assert RiskAssessor.assess(meta) == "high"

    def test_network_permission_is_high(self):
        """network 在 _HIGH_RISK_PERMISSIONS 中，优先匹配为 high"""
        meta = _make_metadata(permissions=["network"])
        assert RiskAssessor.assess(meta) == "high"

    def test_no_permissions_is_low(self):
        meta = _make_metadata(permissions=[])
        assert RiskAssessor.assess(meta) == "low"

    def test_requires_approval_upgrades_to_medium(self):
        meta = _make_metadata(requires_approval=True)
        assert RiskAssessor.assess(meta) == "medium"

    def test_high_risk_overrides_permissions(self):
        """显式 high 优先于 permissions 推断"""
        meta = _make_metadata(risk_level="high", permissions=[])
        assert RiskAssessor.assess(meta) == "high"

    def test_mixed_permissions_takes_highest(self):
        """subprocess + network → high"""
        meta = _make_metadata(permissions=["network", "subprocess"])
        assert RiskAssessor.assess(meta) == "high"


# ─── ApprovalEntry Tests ─────────────────────────────────────


class TestApprovalEntry:
    """审批条目测试"""

    def test_initial_status_is_pending(self):
        entry = ApprovalEntry(task=_make_task(), risk_level="medium")
        assert entry.status == ApprovalStatus.PENDING
        assert entry.approval_id  # UUID 已生成

    def test_not_expired_initially(self):
        entry = ApprovalEntry(task=_make_task(), risk_level="medium", timeout_seconds=300)
        assert not entry.is_expired

    def test_remaining_seconds_positive(self):
        entry = ApprovalEntry(task=_make_task(), risk_level="medium", timeout_seconds=300)
        assert entry.remaining_seconds > 0

    def test_expired_after_timeout(self):
        entry = ApprovalEntry(task=_make_task(), risk_level="medium", timeout_seconds=0)
        # timeout_seconds=0 意味着立即过期
        assert entry.is_expired

    def test_non_pending_not_expired(self):
        entry = ApprovalEntry(task=_make_task(), risk_level="medium")
        entry.status = ApprovalStatus.APPROVED
        assert not entry.is_expired
        assert entry.remaining_seconds == 0


# ─── ApprovalEngine Tests ────────────────────────────────────


class TestApprovalEngine:
    """审批引擎测试"""

    @pytest.mark.asyncio
    async def test_auto_approve_mode(self):
        """自动批准模式下所有任务直接通过"""
        engine = ApprovalEngine(auto_approve=True)
        task = _make_task()
        result = await engine.review(task, risk_level="high")
        assert result.approved is True
        assert result.status == ApprovalStatus.AUTO_APPROVED

    @pytest.mark.asyncio
    async def test_auto_approve_with_metadata(self):
        """自动批准模式 + metadata 风险评估"""
        engine = ApprovalEngine(auto_approve=True)
        meta = _make_metadata(risk_level="high")
        result = await engine.review(_make_task(), metadata=meta)
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_manual_mode_low_risk_auto_approved(self):
        """手动模式下低风险自动通过"""
        engine = ApprovalEngine(auto_approve=False)
        result = await engine.review(_make_task(), risk_level="low")
        assert result.approved is True
        assert result.status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_manual_mode_medium_risk_needs_approval(self):
        """手动模式下中风险需要审批"""
        engine = ApprovalEngine(auto_approve=False)
        result = await engine.review(_make_task(), risk_level="medium")
        assert result.approved is False
        assert result.status == ApprovalStatus.PENDING
        assert result.approval_id is not None

    @pytest.mark.asyncio
    async def test_manual_mode_high_risk_needs_approval(self):
        """手动模式下高风险需要审批"""
        engine = ApprovalEngine(auto_approve=False)
        result = await engine.review(_make_task(), risk_level="high")
        assert result.approved is False
        assert result.status == ApprovalStatus.PENDING

    @pytest.mark.asyncio
    async def test_approve_pending(self):
        """批准待审批条目"""
        engine = ApprovalEngine(auto_approve=False)
        result = await engine.review(_make_task(), risk_level="high")
        approval_id = result.approval_id

        approve_result = await engine.approve(approval_id, approver="admin")
        assert approve_result.approved is True
        assert approve_result.status == ApprovalStatus.APPROVED

    @pytest.mark.asyncio
    async def test_reject_pending(self):
        """拒绝待审批条目"""
        engine = ApprovalEngine(auto_approve=False)
        result = await engine.review(_make_task(), risk_level="high")
        approval_id = result.approval_id

        reject_result = await engine.reject(approval_id, reason="too risky", rejector="admin")
        assert reject_result.approved is False
        assert reject_result.status == ApprovalStatus.REJECTED

    @pytest.mark.asyncio
    async def test_approve_nonexistent(self):
        """批准不存在的条目"""
        engine = ApprovalEngine(auto_approve=False)
        result = await engine.approve("nonexistent-id")
        assert result.approved is False
        assert result.reason == "not_found"

    @pytest.mark.asyncio
    async def test_reject_nonexistent(self):
        """拒绝不存在的条目"""
        engine = ApprovalEngine(auto_approve=False)
        result = await engine.reject("nonexistent-id")
        assert result.approved is False
        assert result.reason == "not_found"

    @pytest.mark.asyncio
    async def test_approve_already_approved(self):
        """重复批准已批准的条目"""
        engine = ApprovalEngine(auto_approve=False)
        result = await engine.review(_make_task(), risk_level="high")
        approval_id = result.approval_id

        await engine.approve(approval_id)
        second = await engine.approve(approval_id)
        assert second.approved is False
        assert "already_approved" in second.reason

    @pytest.mark.asyncio
    async def test_reject_already_approved(self):
        """拒绝已批准的条目"""
        engine = ApprovalEngine(auto_approve=False)
        result = await engine.review(_make_task(), risk_level="high")
        approval_id = result.approval_id

        await engine.approve(approval_id)
        reject_result = await engine.reject(approval_id)
        assert reject_result.approved is False
        assert "already_approved" in reject_result.reason

    @pytest.mark.asyncio
    async def test_get_pending(self):
        """获取待审批列表"""
        engine = ApprovalEngine(auto_approve=False)
        await engine.review(_make_task("skill_a"), risk_level="high")
        await engine.review(_make_task("skill_b"), risk_level="medium")
        # low risk auto-approved, not in pending
        await engine.review(_make_task("skill_c"), risk_level="low")

        pending = engine.get_pending()
        assert len(pending) == 2
        names = {e.task.skill_name for e in pending}
        assert names == {"skill_a", "skill_b"}

    @pytest.mark.asyncio
    async def test_get_entry(self):
        """获取指定审批条目"""
        engine = ApprovalEngine(auto_approve=False)
        result = await engine.review(_make_task(), risk_level="high")
        entry = engine.get_entry(result.approval_id)
        assert entry is not None
        assert entry.task.skill_name == "test_skill"

    @pytest.mark.asyncio
    async def test_get_entry_nonexistent(self):
        """获取不存在的条目"""
        engine = ApprovalEngine(auto_approve=False)
        assert engine.get_entry("nonexistent") is None

    @pytest.mark.asyncio
    async def test_timeout_cleanup(self):
        """超时自动过期"""
        engine = ApprovalEngine(auto_approve=False, approval_timeout_seconds=0)
        await engine.review(_make_task(), risk_level="high")

        # 触发超时检查
        await engine._check_timeouts()

        pending = engine.get_pending()
        assert len(pending) == 0  # 已过期，不在 pending 列表中

        # 检查条目状态
        all_entries = list(engine._queue.values())
        assert all_entries[0].status == ApprovalStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_review_with_metadata_risk_assessment(self):
        """review 时自动从 metadata 评估风险"""
        engine = ApprovalEngine(auto_approve=False)
        meta = _make_metadata(permissions=["subprocess"])
        result = await engine.review(_make_task(), metadata=meta)
        # subprocess → high risk → needs approval
        assert result.approved is False
        assert result.status == ApprovalStatus.PENDING

    @pytest.mark.asyncio
    async def test_review_no_metadata_no_risk_level_defaults_low(self):
        """无 metadata 无 risk_level 默认 low"""
        engine = ApprovalEngine(auto_approve=False)
        result = await engine.review(_make_task())
        assert result.approved is True  # low → auto-approved

    @pytest.mark.asyncio
    async def test_approval_result_bool(self):
        """ApprovalResult __bool__ 测试"""
        assert bool(ApprovalResult(True, "ok")) is True
        assert bool(ApprovalResult(False, "nope")) is False


# ─── ApprovalEngine + Notifier Integration ───────────────────


class TestApprovalNotification:
    """审批通知集成测试"""

    @pytest.mark.asyncio
    async def test_notification_sent_on_pending(self):
        """需要审批时发送通知"""
        notifications = []

        class MockNotifier:
            async def notify(self, **kwargs):
                notifications.append(kwargs)
                return {}

        engine = ApprovalEngine(auto_approve=False, notifier=MockNotifier())
        await engine.review(_make_task(), risk_level="high")

        assert len(notifications) == 1
        assert "title" in notifications[0]
        assert "body" in notifications[0]

    @pytest.mark.asyncio
    async def test_no_notification_on_auto_approve(self):
        """自动批准时不发送通知"""
        notifications = []

        class MockNotifier:
            async def notify(self, **kwargs):
                notifications.append(kwargs)
                return {}

        engine = ApprovalEngine(auto_approve=True, notifier=MockNotifier())
        await engine.review(_make_task(), risk_level="high")

        assert len(notifications) == 0

    @pytest.mark.asyncio
    async def test_no_notification_on_low_risk(self):
        """低风险自动通过时不发送通知"""
        notifications = []

        class MockNotifier:
            async def notify(self, **kwargs):
                notifications.append(kwargs)
                return {}

        engine = ApprovalEngine(auto_approve=False, notifier=MockNotifier())
        await engine.review(_make_task(), risk_level="low")

        assert len(notifications) == 0


# ─── ApprovalEngine Lifecycle ────────────────────────────────


class TestApprovalEngineLifecycle:
    """审批引擎生命周期测试"""

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        """启动和停止"""
        engine = ApprovalEngine()
        await engine.start()
        assert engine._cleanup_task is not None
        await engine.stop()
        assert engine._cleanup_task is None or engine._cleanup_task.cancelled()

    @pytest.mark.asyncio
    async def test_cleanup_loop_catches_exceptions(self):
        """清理循环中的异常不会中断循环"""
        engine = ApprovalEngine(auto_approve=False, approval_timeout_seconds=0)
        await engine.review(_make_task(), risk_level="high")

        # 手动调用 _check_timeouts 不应抛异常
        await engine._check_timeouts()
