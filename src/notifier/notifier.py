"""统一通知接口

核心功能：
1. 从 config.notify_channels 加载通道配置，实例化对应 Channel
2. 监听 EventBus 的 TASK_COMPLETED / TASK_FAILED / SCHEDULE_TRIGGERED 事件
3. 根据通知策略（静默时段、成功/失败开关）决定是否发送
4. 多通道并行发送，一个通道失败不影响其他
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType
from src.notifier.channels.base import BaseChannel, Notification, Priority
from src.notifier.channels.system import SystemChannel
from src.notifier.channels.bark import BarkChannel
from src.notifier.channels.serverchan import ServerChanChannel
from src.notifier.channels.webhook import WebhookChannel
from src.types import Event

logger = structlog.get_logger(__name__)

# 通道类型 → 实现类映射
CHANNEL_REGISTRY: dict[str, type[BaseChannel]] = {
    "system": SystemChannel,
    "bark": BarkChannel,
    "serverchan": ServerChanChannel,
    "webhook": WebhookChannel,
}


class Notifier:
    """统一通知管理器"""

    def __init__(self, event_bus: EventBus, config: RaccoonConfig | None = None) -> None:
        self._event_bus = event_bus
        self._config = config or RaccoonConfig()
        self._channels: list[BaseChannel] = []
        self._initialized = False

    def initialize(self) -> None:
        """从配置加载通知通道（应在 EventBus 启动前调用）"""
        if self._initialized:
            return

        for ch_cfg in self._config.notify_channels:
            ch_type = ch_cfg.get("type", "")
            cls = CHANNEL_REGISTRY.get(ch_type)
            if cls:
                channel = cls(config=ch_cfg)
                self._channels.append(channel)
                logger.info("notify_channel_loaded", type=ch_type, name=channel.name)
            else:
                logger.warning("notify_channel_unknown", type=ch_type)

        # 注册 EventBus 事件监听
        self._event_bus.on(EventType.TASK_COMPLETED, self._on_task_completed)
        self._event_bus.on(EventType.TASK_FAILED, self._on_task_failed)

        self._initialized = True
        logger.info("notifier_initialized", channels=len(self._channels))

    # ─── 公开接口 ─────────────────────────────────────────────

    async def notify(
        self,
        title: str,
        body: str,
        priority: Priority = Priority.NORMAL,
        extra: dict | None = None,
    ) -> dict[str, bool]:
        """发送通知到所有通道，返回 {channel_name: success}"""
        if self._is_silent_hours():
            logger.info("notify_silent_hours_skipped", title=title)
            return {}

        notification = Notification(title=title, body=body, priority=priority, extra=extra or {})
        results: dict[str, bool] = {}

        for channel in self._channels:
            try:
                ok = await channel.send(notification)
                results[channel.name] = ok
            except Exception as e:
                logger.error(
                    "notify_channel_error",
                    channel=channel.name,
                    error=str(e),
                )
                results[channel.name] = False

        logger.debug("notify_sent", title=title, results=results)
        return results

    @property
    def channels(self) -> list[BaseChannel]:
        return list(self._channels)

    # ─── EventBus 事件处理 ─────────────────────────────────────

    async def _on_task_completed(self, event: Event) -> None:
        """任务完成通知"""
        if not self._config.notify_on_success:
            return

        skill = event.skill_name or "未知"
        title = f"任务完成: {skill}"
        body = f"任务 {event.task_id} 已成功完成。" if event.task_id else "任务已成功完成。"
        if event.payload.get("result"):
            result_text = str(event.payload["result"])[:200]
            body = f"{body}\n结果: {result_text}"

        await self.notify(title, body, Priority.NORMAL)

    async def _on_task_failed(self, event: Event) -> None:
        """任务失败通知"""
        if not self._config.notify_on_failure:
            return

        skill = event.skill_name or "未知"
        title = f"任务失败: {skill}"
        body = f"任务 {event.task_id} 执行失败。" if event.task_id else "任务执行失败。"
        if event.payload.get("error"):
            body = f"{body}\n错误: {event.payload['error']}"

        await self.notify(title, body, Priority.HIGH)

    # ─── 静默时段 ─────────────────────────────────────────────

    def _is_silent_hours(self) -> bool:
        """检查当前是否在静默时段"""
        silent = self._config.notify_silent_hours
        if not silent:
            return False

        start_str = silent.get("start")
        end_str = silent.get("end")
        if not start_str or not end_str:
            return False

        try:
            now = datetime.now()
            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))

            start_min = start_h * 60 + start_m
            end_min = end_h * 60 + end_m
            now_min = now.hour * 60 + now.minute

            if start_min <= end_min:
                # 同一天内: 23:00 ~ 07:00 这种跨天情况
                return start_min <= now_min < end_min
            else:
                # 跨天: 23:00 ~ 07:00
                return now_min >= start_min or now_min < end_min
        except (ValueError, TypeError):
            return False
