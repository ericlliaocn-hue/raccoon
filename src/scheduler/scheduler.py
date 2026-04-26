"""asyncio 调度引擎

核心流程：
1. 每秒检查所有 enabled 的 schedule
2. 如果 cron 匹配当前时间 → 向 EventBus 发 SCHEDULE_TRIGGERED 事件
3. 更新 schedule 的 last_run / next_run / 执行记录
4. 防止同一分钟内重复触发（内存 + SQLite PID 锁双层防护）
5. 失败自动重试（根据 retry_policy）
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import structlog

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.scheduler.cron_parser import CronParser
from src.scheduler.schedule_store import ScheduleStore
from src.types import ScheduleEntry, ScheduleStatus

logger = structlog.get_logger(__name__)


class Scheduler:
    """定时调度引擎"""

    def __init__(
        self,
        event_bus: EventBus,
        store: ScheduleStore,
        config: RaccoonConfig | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._store = store
        self._config = config or RaccoonConfig()
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_triggered: dict[str, str] = {}  # schedule_id → "YYYY-MM-DD HH:MM" 防重复
        self._pid = os.getpid()

    async def start(self) -> None:
        """启动调度引擎"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("scheduler_started", schedules=self._store.count(), pid=self._pid)

    async def stop(self) -> None:
        """停止调度引擎"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("scheduler_stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def store(self) -> ScheduleStore:
        return self._store

    # ─── 核心循环 ─────────────────────────────────────────────

    async def _loop(self) -> None:
        """每秒检查一次 schedule 列表"""
        while self._running:
            try:
                await asyncio.sleep(1)
                await self._check_schedules()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("scheduler_loop_error")

    async def _check_schedules(self) -> None:
        """检查并触发到期的 schedule"""
        now = datetime.now(timezone.utc)
        now_minute = now.strftime("%Y-%m-%d %H:%M")

        for entry in self._store.get_enabled():
            # 防止使用过期快照：删除/暂停后不应继续触发或回写。
            live_entry = self._store.get(entry.schedule_id)
            if not live_entry or not live_entry.enabled:
                continue
            entry = live_entry
            try:
                cron = CronParser(entry.cron)
                if not cron.matches(now):
                    continue

                # 防止同一分钟内重复触发（内存层）
                last_key = self._last_triggered.get(entry.schedule_id, "")
                if last_key == now_minute:
                    continue

                # 跨进程 PID 锁（SQLite 层）
                if not self._store.try_acquire_lock(entry.schedule_id, self._pid):
                    logger.debug("schedule_locked_by_other", schedule_id=entry.schedule_id)
                    continue

                try:
                    # 获取锁后再确认一次，避免删除操作与当前轮询竞态导致“删后回写”。
                    live_entry = self._store.get(entry.schedule_id)
                    if not live_entry or not live_entry.enabled:
                        continue
                    entry = live_entry

                    # 触发
                    self._last_triggered[entry.schedule_id] = now_minute
                    entry.last_run = now
                    entry.next_run = cron.next_time(now)

                    # 记录执行开始
                    run_id = self._store.record_run_start(entry.schedule_id)

                    # 向 EventBus 发事件
                    event = make_event(
                        EventType.SCHEDULE_TRIGGERED,
                        conversation_id=entry.conversation_id,
                        user_id=entry.user_id,
                        payload={
                            "text": entry.message,
                            "schedule_id": entry.schedule_id,
                            "schedule_name": entry.name,
                            "cron": entry.cron,
                            "run_id": run_id,
                        },
                    )
                    await self._event_bus.emit(event)

                    # 更新调度状态
                    entry.last_run_result = "triggered"
                    if self._store.get(entry.schedule_id):
                        self._store.update(entry)

                    logger.info(
                        "schedule_triggered",
                        schedule_id=entry.schedule_id,
                        name=entry.name,
                        cron=entry.cron,
                        run_id=run_id,
                    )
                finally:
                    # 释放 PID 锁（触发后立即释放，执行由 Executor 异步完成）
                    self._store.release_lock(entry.schedule_id, self._pid)

            except Exception:
                logger.exception(
                    "schedule_check_error",
                    schedule_id=entry.schedule_id,
                )

    # ─── 管理接口 ─────────────────────────────────────────────

    async def add_schedule(self, entry: ScheduleEntry) -> ScheduleEntry:
        """添加定时任务"""
        # 预计算 next_run
        try:
            cron = CronParser(entry.cron)
            entry.next_run = cron.next_time(datetime.now(timezone.utc))
        except Exception:
            pass
        self._store.add(entry)
        return entry

    async def remove_schedule(self, schedule_id: str) -> ScheduleEntry | None:
        """删除定时任务"""
        self._last_triggered.pop(schedule_id, None)
        return self._store.remove(schedule_id)

    async def toggle_schedule(self, schedule_id: str) -> ScheduleEntry | None:
        """启用/禁用定时任务（兼容旧接口）"""
        entry = self._store.get(schedule_id)
        if not entry:
            return None
        entry.set_enabled(not entry.enabled)
        if not entry.enabled:
            self._last_triggered.pop(schedule_id, None)
        self._store.update(entry)
        return entry

    async def pause_schedule(self, schedule_id: str) -> ScheduleEntry | None:
        """暂停定时任务（不触发但保留配置，可恢复）"""
        entry = self._store.get(schedule_id)
        if not entry:
            return None
        entry.status = ScheduleStatus.PAUSED
        self._last_triggered.pop(schedule_id, None)
        self._store.update(entry)
        logger.info("schedule_paused", schedule_id=schedule_id, name=entry.name)
        return entry

    async def resume_schedule(self, schedule_id: str) -> ScheduleEntry | None:
        """恢复暂停的定时任务"""
        entry = self._store.get(schedule_id)
        if not entry or entry.status != ScheduleStatus.PAUSED:
            return None
        entry.status = ScheduleStatus.ENABLED
        self._store.update(entry)
        logger.info("schedule_resumed", schedule_id=schedule_id, name=entry.name)
        return entry

    async def record_run_result(
        self,
        schedule_id: str,
        run_id: int,
        success: bool,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """记录执行结果（由 Executor 回调）

        如果失败且重试策略允许，会增加 retry_count 并重新触发。
        """
        entry = self._store.get(schedule_id)
        if not entry:
            return

        result = "success" if success else "failed"
        self._store.record_run_finish(run_id, result, error, duration_ms)

        if success:
            entry.last_run_result = "success"
            entry.last_run_error = None
            entry.retry_count = 0  # 成功后重置重试计数
            self._store.update(entry)
        else:
            entry.last_run_result = "failed"
            entry.last_run_error = error
            self._store.update(entry)

            # 检查是否需要重试
            policy = entry.retry_policy
            if policy.retry_on_failure and entry.retry_count < policy.max_retries:
                entry.retry_count += 1
                entry.last_run_result = "retrying"
                self._store.update(entry)

                logger.info(
                    "schedule_retry_scheduled",
                    schedule_id=schedule_id,
                    retry_count=entry.retry_count,
                    max_retries=policy.max_retries,
                    interval=policy.retry_interval_seconds,
                )

                # 延迟后重新触发
                async def _retry():
                    await asyncio.sleep(policy.retry_interval_seconds)
                    latest = self._store.get(entry.schedule_id)
                    if latest is None or not latest.enabled:
                        logger.info("schedule_retry_skipped", schedule_id=entry.schedule_id, reason="missing_or_disabled")
                        return
                    latest_policy = latest.retry_policy
                    if latest.retry_count > latest_policy.max_retries:
                        logger.info("schedule_retry_skipped", schedule_id=entry.schedule_id, reason="retry_limit_exceeded")
                        return
                    retry_run_id = self._store.record_run_start(latest.schedule_id)
                    latest.last_run = datetime.now(timezone.utc)
                    self._store.update(latest)
                    retry_event = make_event(
                        EventType.SCHEDULE_TRIGGERED,
                        conversation_id=latest.conversation_id,
                        user_id=latest.user_id,
                        payload={
                            "text": latest.message,
                            "schedule_id": latest.schedule_id,
                            "schedule_name": f"{latest.name} (重试#{latest.retry_count})",
                            "cron": latest.cron,
                            "run_id": retry_run_id,
                            "is_retry": True,
                            "retry_count": latest.retry_count,
                        },
                    )
                    await self._event_bus.emit(retry_event)

                asyncio.create_task(_retry())

    def list_schedules(self) -> list[ScheduleEntry]:
        """列出所有定时任务"""
        return list(self._store.all_schedules())

    def get_schedule_runs(self, schedule_id: str, limit: int = 10) -> list[dict]:
        """获取定时任务执行记录"""
        return self._store.get_recent_runs(schedule_id, limit)
