"""asyncio 调度引擎

核心流程：
1. 每秒检查所有 enabled 的 schedule
2. 如果 cron 匹配当前时间 → 向 EventBus 发 SCHEDULE_TRIGGERED 事件
3. 更新 schedule 的 last_run / next_run
4. 防止同一分钟内重复触发（记录 last_run 的分钟精度）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.scheduler.cron_parser import CronParser
from src.scheduler.schedule_store import ScheduleStore
from src.types import ScheduleEntry

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

    async def start(self) -> None:
        """启动调度引擎"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("scheduler_started", schedules=self._store.count())

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
            try:
                cron = CronParser(entry.cron)
                if not cron.matches(now):
                    continue

                # 防止同一分钟内重复触发
                last_key = self._last_triggered.get(entry.schedule_id, "")
                if last_key == now_minute:
                    continue

                # 触发
                self._last_triggered[entry.schedule_id] = now_minute
                entry.last_run = now
                entry.next_run = cron.next_time(now)
                self._store.update(entry)

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
                    },
                )
                await self._event_bus.emit(event)

                logger.info(
                    "schedule_triggered",
                    schedule_id=entry.schedule_id,
                    name=entry.name,
                    cron=entry.cron,
                )

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
        """启用/禁用定时任务"""
        entry = self._store.get(schedule_id)
        if not entry:
            return None
        entry.enabled = not entry.enabled
        if not entry.enabled:
            self._last_triggered.pop(schedule_id, None)
        self._store.update(entry)
        return entry

    def list_schedules(self) -> list[ScheduleEntry]:
        """列出所有定时任务"""
        return list(self._store.all_schedules())
