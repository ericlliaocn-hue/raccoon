"""Skill 会话管理器：管理多轮交互会话的生命周期

核心职责：
1. 创建/获取/销毁 SkillSession
2. 会话拦截：活跃会话中的消息不走 Router，直接路由到会话
3. 超时回收：长时间无交互的会话自动过期
4. 状态持久化：会话 state 在步骤间传递
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from src.types import (
    Event,
    EventType,
    FlowDefinition,
    FlowStep,
    FlowStepType,
    MessageSource,
    RouteResult,
    RouteType,
    SessionStatus,
    SkillMetadata,
    SkillSession,
    make_event,
)

logger = structlog.get_logger(__name__)


class SkillSessionManager:
    """Skill 多轮交互会话管理器"""

    def __init__(self, timeout_check_interval: int = 60) -> None:
        # conversation_id → SkillSession
        self._sessions: dict[str, SkillSession] = {}
        self._timeout_check_interval = timeout_check_interval
        self._cleanup_task: asyncio.Task | None = None
        self._event_bus: Any = None  # 延迟注入

    def set_event_bus(self, event_bus: Any) -> None:
        """注入 EventBus，用于推送会话事件"""
        self._event_bus = event_bus

    async def start(self) -> None:
        """启动超时检查后台任务"""
        self._cleanup_task = asyncio.create_task(self._timeout_loop())

    async def stop(self) -> None:
        """停止超时检查"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    # ─── 会话生命周期 ─────────────────────────────────────────

    def create_session(
        self,
        conversation_id: str,
        skill_name: str,
        flow: FlowDefinition,
        timeout_seconds: int = 1800,
    ) -> SkillSession:
        """创建新的 Skill 会话"""
        # 如果已有活跃会话，先关闭
        existing = self._sessions.get(conversation_id)
        if existing and existing.status in (SessionStatus.ACTIVE, SessionStatus.PROCESSING):
            self.cancel_session(conversation_id, reason="新会话覆盖")

        session = SkillSession(
            conversation_id=conversation_id,
            skill_name=skill_name,
            flow=flow,
            status=SessionStatus.ACTIVE,
            timeout_seconds=timeout_seconds,
        )
        self._sessions[conversation_id] = session
        logger.info(
            "session_created",
            session_id=session.session_id,
            conversation_id=conversation_id,
            skill=skill_name,
            steps=len(flow.steps),
        )
        return session

    def get_session(self, conversation_id: str) -> SkillSession | None:
        """获取对话的活跃会话"""
        session = self._sessions.get(conversation_id)
        if session and session.status in (SessionStatus.ACTIVE, SessionStatus.PROCESSING):
            return session
        return None

    def get_background_session(self, conversation_id: str) -> SkillSession | None:
        """获取后台会话（用于结果推送）"""
        session = self._sessions.get(conversation_id)
        if session and session.status == SessionStatus.BACKGROUND:
            return session
        return None

    def complete_session(self, conversation_id: str, result: dict[str, Any] | None = None) -> None:
        """完成会话"""
        session = self._sessions.get(conversation_id)
        if not session:
            return
        session.status = SessionStatus.COMPLETED
        session.updated_at = datetime.now(timezone.utc)
        if result:
            session.state["_result"] = result
        logger.info("session_completed", session_id=session.session_id, skill=session.skill_name)

    def cancel_session(self, conversation_id: str, reason: str = "") -> None:
        """取消会话"""
        session = self._sessions.get(conversation_id)
        if not session:
            return
        session.status = SessionStatus.CANCELLED
        session.updated_at = datetime.now(timezone.utc)
        logger.info("session_cancelled", session_id=session.session_id, reason=reason)

    def release_to_background(self, conversation_id: str, task_id: str) -> None:
        """将会话转为后台模式（释放对话通道）"""
        session = self._sessions.get(conversation_id)
        if not session:
            return
        session.status = SessionStatus.BACKGROUND
        session.task_id = task_id
        session.updated_at = datetime.now(timezone.utc)
        logger.info(
            "session_background",
            session_id=session.session_id,
            task_id=task_id,
        )

    # ─── 会话拦截 ─────────────────────────────────────────────

    def should_intercept(self, conversation_id: str) -> bool:
        """判断消息是否应被会话拦截（不走 Router）"""
        session = self.get_session(conversation_id)
        return session is not None

    def intercept_route(self, conversation_id: str, user_text: str) -> RouteResult:
        """拦截路由：活跃会话中的消息路由到 SKILL_SESSION"""
        session = self.get_session(conversation_id)
        if not session:
            raise ValueError(f"No active session for conversation {conversation_id}")

        return RouteResult(
            route_type=RouteType.SKILL_SESSION,
            skill_name=session.skill_name,
            params={
                "session_id": session.session_id,
                "current_step_index": session.current_step_index,
                "state": session.state,
                "original_text": user_text,
            },
            confidence=1.0,
        )

    # ─── 步骤推进 ─────────────────────────────────────────────

    def advance_step(self, conversation_id: str) -> FlowStep | None:
        """推进到下一步，返回下一步定义；无下一步则返回 None"""
        session = self.get_session(conversation_id)
        if not session:
            return None

        next_index = session.current_step_index + 1
        if next_index >= len(session.flow.steps):
            return None

        session.current_step_index = next_index
        session.updated_at = datetime.now(timezone.utc)
        return session.flow.steps[next_index]

    def get_current_step(self, conversation_id: str) -> FlowStep | None:
        """获取当前步骤"""
        session = self.get_session(conversation_id)
        if not session:
            return None
        steps = session.flow.steps
        if session.current_step_index < len(steps):
            return steps[session.current_step_index]
        return None

    def update_state(self, conversation_id: str, key: str, value: Any) -> None:
        """更新会话状态"""
        session = self.get_session(conversation_id)
        if not session:
            return
        session.state[key] = value
        session.updated_at = datetime.now(timezone.utc)

    def set_processing(self, conversation_id: str) -> None:
        """标记会话正在处理中"""
        session = self.get_session(conversation_id)
        if session:
            session.status = SessionStatus.PROCESSING
            session.updated_at = datetime.now(timezone.utc)

    def set_active(self, conversation_id: str) -> None:
        """恢复会话为活跃状态"""
        session = self._sessions.get(conversation_id)
        if session and session.status == SessionStatus.PROCESSING:
            session.status = SessionStatus.ACTIVE
            session.updated_at = datetime.now(timezone.utc)

    # ─── 超时回收 ─────────────────────────────────────────────

    async def _timeout_loop(self) -> None:
        """定期检查超时会话"""
        while True:
            try:
                await asyncio.sleep(self._timeout_check_interval)
                self._check_timeouts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("timeout_check_error", error=str(e))

    def _check_timeouts(self) -> None:
        """检查并回收超时会话"""
        now = datetime.now(timezone.utc)
        expired: list[str] = []

        for conv_id, session in self._sessions.items():
            if session.status not in (SessionStatus.ACTIVE, SessionStatus.PROCESSING):
                continue
            elapsed = (now - session.updated_at).total_seconds()
            if elapsed > session.timeout_seconds:
                expired.append(conv_id)

        for conv_id in expired:
            session = self._sessions[conv_id]
            session.status = SessionStatus.TIMEOUT
            logger.warning("session_timeout", conversation_id=conv_id, skill=session.skill_name)

    # ─── 辅助 ─────────────────────────────────────────────────

    def list_active_sessions(self) -> list[SkillSession]:
        """列出所有活跃会话"""
        return [
            s for s in self._sessions.values()
            if s.status in (SessionStatus.ACTIVE, SessionStatus.PROCESSING)
        ]

    def list_background_sessions(self) -> list[SkillSession]:
        """列出所有后台会话"""
        return [s for s in self._sessions.values() if s.status == SessionStatus.BACKGROUND]
