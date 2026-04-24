"""浏览器会话管理器

跨 Skill 共享浏览器实例，避免每个 Skill 都启动新浏览器。
支持按模式（headless/headed/cdp）管理会话池。

使用方式：
    from session_manager import get_session_manager

    mgr = get_session_manager()
    session = await mgr.acquire("cdp")
    # 使用 session.engine.actions.xxx
    await mgr.release(session.id)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

try:
    from .browser_engine import BrowserEngine
except ImportError:  # 兼容直接在 skills/web_automate 目录运行
    from browser_engine import BrowserEngine

logger = logging.getLogger("raccoon.session_manager")


@dataclass
class BrowserSession:
    """浏览器会话"""

    id: str
    mode: str
    engine: BrowserEngine
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    in_use: bool = True
    owner: str = ""  # 占用者标识（如 skill_name）
    last_run: dict[str, Any] = field(default_factory=dict)  # 最近一次执行信息（断点恢复）

    def touch(self) -> None:
        """更新最后使用时间"""
        self.last_used_at = time.time()


class BrowserSessionManager:
    """浏览器会话管理器

    核心设计：
    - 按模式维护会话池
    - acquire() 获取或创建会话
    - release() 归还会话（不关闭浏览器）
    - cleanup() 清理超时会话
    - shutdown() 关闭所有会话
    """

    def __init__(self, max_idle_seconds: int = 600, max_sessions_per_mode: int = 3) -> None:
        self._sessions: dict[str, BrowserSession] = {}
        self._max_idle_seconds = max_idle_seconds
        self._max_sessions_per_mode = max_sessions_per_mode
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    async def start_cleanup_loop(self) -> None:
        """启动后台清理任务"""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        """定期清理超时空闲会话"""
        while True:
            await asyncio.sleep(60)
            try:
                await self.cleanup()
            except Exception as e:
                logger.error("session_cleanup_error: %s", e)

    async def acquire(self, mode: str = "cdp", owner: str = "", reuse_url: str = "") -> BrowserSession:
        """获取浏览器会话

        优先复用空闲的同模式会话；传入 reuse_url 时先找已打开相同站点的会话。
        """
        async with self._lock:
            reuse_domain = self._extract_domain(reuse_url)

            # 1. reuse_url 命中已有 tab 时，优先复用对应会话
            if reuse_url:
                for session in self._sessions.values():
                    if not session.in_use and session.mode == mode:
                        if self._domain_state(session, reuse_domain) == "invalid":
                            logger.info("session_skipped_invalid_domain: id=%s domain=%s", session.id, reuse_domain)
                            continue
                        if session.engine.select_reuse_page(reuse_url):
                            session.in_use = True
                            session.owner = owner
                            session.touch()
                            logger.info(
                                "session_reused_by_url: id=%s mode=%s owner=%s url=%s",
                                session.id,
                                mode,
                                owner,
                                reuse_url,
                            )
                            return session

            # 2. 尝试复用空闲会话
            for session in self._sessions.values():
                if not session.in_use and session.mode == mode:
                    if self._domain_state(session, reuse_domain) == "invalid":
                        continue
                    if reuse_url:
                        session.engine.select_reuse_page(reuse_url)
                    session.in_use = True
                    session.owner = owner
                    session.touch()
                    logger.info("session_reused: id=%s mode=%s owner=%s", session.id, mode, owner)
                    return session

            # 3. 检查同模式会话数量限制
            mode_count = sum(1 for s in self._sessions.values() if s.mode == mode)
            if mode_count >= self._max_sessions_per_mode:
                # 强制关闭最旧的空闲会话
                oldest_idle: BrowserSession | None = None
                for s in self._sessions.values():
                    if s.mode == mode and not s.in_use:
                        if oldest_idle is None or s.last_used_at < oldest_idle.last_used_at:
                            oldest_idle = s
                if oldest_idle:
                    await self._close_session(oldest_idle.id)
                else:
                    raise RuntimeError(f"浏览器会话池已满（模式：{mode}，上限：{self._max_sessions_per_mode}）")

            # 4. 创建新会话
            engine = BrowserEngine(mode)
            start_msg = await engine.start(reuse_url=reuse_url)
            if start_msg.startswith("❌"):
                raise RuntimeError(start_msg)

            session = BrowserSession(
                id=uuid.uuid4().hex[:8],
                mode=mode,
                engine=engine,
                owner=owner,
            )
            self._sessions[session.id] = session
            logger.info("session_created: id=%s mode=%s owner=%s", session.id, mode, owner)
            return session

    def _extract_domain(self, reuse_url: str) -> str:
        if not reuse_url:
            return ""
        try:
            parsed = urlparse(reuse_url if "://" in reuse_url else f"https://{reuse_url}")
            return parsed.netloc.lower()
        except Exception:
            return ""

    def _domain_state(self, session: BrowserSession, domain: str) -> str:
        if not domain:
            return "valid"
        try:
            runtime = session.engine.get_runtime_artifacts()
            return str((runtime.get("domain_health") or {}).get(domain, "valid"))
        except Exception:
            return "valid"

    async def release(self, session_id: str) -> None:
        """释放浏览器会话（归还到池中，不关闭浏览器）"""
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                logger.warning("session_release_not_found: id=%s", session_id)
                return
            session.in_use = False
            session.owner = ""
            session.touch()
            logger.info("session_released: id=%s", session_id)

    async def cleanup(self) -> int:
        """清理超时空闲会话，返回清理数量"""
        now = time.time()
        to_close: list[str] = []

        async with self._lock:
            for sid, session in self._sessions.items():
                if not session.in_use and (now - session.last_used_at) > self._max_idle_seconds:
                    to_close.append(sid)

        for sid in to_close:
            await self._close_session(sid)

        if to_close:
            logger.info("session_cleanup: closed=%d", len(to_close))
        return len(to_close)

    async def _close_session(self, session_id: str) -> None:
        """关闭并移除会话"""
        session = self._sessions.pop(session_id, None)
        if session:
            try:
                await session.engine.stop()
            except Exception as e:
                logger.warning("session_close_error: id=%s error=%s", session_id, e)

    async def shutdown(self) -> None:
        """关闭所有会话"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            self._cleanup_task = None

        async with self._lock:
            session_ids = list(self._sessions.keys())

        for sid in session_ids:
            await self._close_session(sid)

        logger.info("session_manager_shutdown: closed=%d", len(session_ids))

    def get_stats(self) -> dict[str, Any]:
        """获取会话池统计信息"""
        total = len(self._sessions)
        in_use = sum(1 for s in self._sessions.values() if s.in_use)
        idle = total - in_use
        by_mode: dict[str, int] = {}
        for s in self._sessions.values():
            by_mode[s.mode] = by_mode.get(s.mode, 0) + 1
        return {
            "total": total,
            "in_use": in_use,
            "idle": idle,
            "by_mode": by_mode,
            "max_sessions_per_mode": self._max_sessions_per_mode,
            "max_idle_seconds": self._max_idle_seconds,
        }


# ── 全局单例 ─────────────────────────────────────────────

_instance: BrowserSessionManager | None = None


def get_session_manager() -> BrowserSessionManager:
    """获取全局 BrowserSessionManager 单例"""
    global _instance
    if _instance is None:
        _instance = BrowserSessionManager()
    return _instance
