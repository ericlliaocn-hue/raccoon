"""统一通知接口

核心功能：
1. 从 config.notify_channels 加载通道配置，实例化对应 Channel
2. 监听 EventBus 的 TASK_COMPLETED / TASK_FAILED / SCHEDULE_TRIGGERED 事件
3. 根据通知策略（静默时段、成功/失败开关、去重）决定是否发送
4. 使用模板系统渲染不同场景的通知内容
5. 多通道并行发送，一个通道失败不影响其他
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
from src.notifier.channels.dingtalk import DingTalkChannel
from src.notifier.channels.feishu import FeishuChannel
from src.notifier.channels.email import EmailChannel
from src.notifier.dedup import NotificationDedup
from src.notifier.templates import NotificationTemplate
from src.types import Event

logger = structlog.get_logger(__name__)

# 通道类型 → 实现类映射
CHANNEL_REGISTRY: dict[str, type[BaseChannel]] = {
    "system": SystemChannel,
    "bark": BarkChannel,
    "serverchan": ServerChanChannel,
    "webhook": WebhookChannel,
    "dingtalk": DingTalkChannel,
    "feishu": FeishuChannel,
    "email": EmailChannel,
}


class Notifier:
    """统一通知管理器"""

    def __init__(self, event_bus: EventBus, config: RaccoonConfig | None = None) -> None:
        self._event_bus = event_bus
        self._config = config or RaccoonConfig()
        self._channels: list[BaseChannel] = []
        self._initialized = False
        self._dedup = NotificationDedup(
            window_seconds=self._config.notify_dedup_window_seconds
        )

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
        template: str | None = None,
        dedup_key: str | None = None,
    ) -> dict[str, bool]:
        """发送通知到所有通道，返回 {channel_name: success}

        Args:
            title: 通知标题
            body: 通知正文
            priority: 优先级
            extra: 通道特定参数
            template: 模板名称（None 则使用默认格式）
            dedup_key: 去重键（None 则不去重）
        """
        # 去重检查
        if dedup_key and not self._dedup.should_send(dedup_key):
            return {}

        # 静默时段检查
        if self._is_silent_hours():
            logger.info("notify_silent_hours_skipped", title=title)
            return {}

        # 模板渲染
        if template:
            rendered = NotificationTemplate.render(
                template, title=title, body=body, **(extra or {})
            )
            title = rendered.get("title", title)
            body = rendered.get("body", body)
            html_body = rendered.get("html_body")
            if html_body and extra is None:
                extra = {}
            if html_body:
                extra = extra or {}
                extra["html_body"] = html_body

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

    async def notify_with_template(
        self,
        template_name: str,
        dedup_key: str | None = None,
        **kwargs,
    ) -> dict[str, bool]:
        """使用模板发送通知"""
        rendered = NotificationTemplate.render(template_name, **kwargs)
        return await self.notify(
            title=rendered.get("title", ""),
            body=rendered.get("body", ""),
            priority=kwargs.get("priority", Priority.NORMAL),
            extra=kwargs.get("extra"),
            template=None,  # 已经渲染过了
            dedup_key=dedup_key,
        )

    @property
    def channels(self) -> list[BaseChannel]:
        return list(self._channels)

    def cleanup_dedup(self) -> int:
        """清理过期的去重记录"""
        return self._dedup.cleanup()

    # ─── EventBus 事件处理 ─────────────────────────────────────

    async def _on_task_completed(self, event: Event) -> None:
        """任务完成通知"""
        if not self._config.notify_on_success:
            return

        skill = event.skill_name or "未知"
        schedule_id = event.payload.get("schedule_id")
        dedup_key = f"task_completed:{event.task_id}" if event.task_id else None

        # 定时任务使用 schedule_result 模板
        template = "schedule_result" if schedule_id else None
        extra: dict = {}
        if schedule_id:
            extra["schedule_name"] = event.payload.get("schedule_name", "")
            extra["cron"] = event.payload.get("cron", "")
            extra["result"] = "成功"

        title = f"任务完成: {skill}"
        body = f"任务 {event.task_id} 已成功完成。" if event.task_id else "任务已成功完成。"
        if event.payload.get("result"):
            result_text = str(event.payload["result"])[:200]
            body = f"{body}\n结果: {result_text}"

        if template:
            rendered = NotificationTemplate.render(
                template,
                title=title,
                body=body,
                schedule_name=event.payload.get("schedule_name", skill),
                result="✅ 成功",
                cron=event.payload.get("cron", ""),
                **extra,
            )
            title = rendered["title"]
            body = rendered["body"]

        await self.notify(title, body, Priority.NORMAL, extra, dedup_key=dedup_key)

    async def _on_task_failed(self, event: Event) -> None:
        """任务失败通知"""
        if not self._config.notify_on_failure:
            return

        skill = event.skill_name or "未知"
        schedule_id = event.payload.get("schedule_id")
        dedup_key = f"task_failed:{event.task_id}" if event.task_id else None

        template = "alert" if not schedule_id else "schedule_result"
        extra: dict = {}
        if schedule_id:
            extra["schedule_name"] = event.payload.get("schedule_name", "")
            extra["cron"] = event.payload.get("cron", "")
            extra["result"] = "失败"

        title = f"任务失败: {skill}"
        body = f"任务 {event.task_id} 执行失败。" if event.task_id else "任务执行失败。"
        if event.payload.get("error"):
            body = f"{body}\n错误: {event.payload['error']}"

        if template == "schedule_result":
            rendered = NotificationTemplate.render(
                template,
                title=title,
                body=body,
                schedule_name=event.payload.get("schedule_name", skill),
                result="❌ 失败",
                cron=event.payload.get("cron", ""),
                **extra,
            )
            title = rendered["title"]
            body = rendered["body"]
        else:
            rendered = NotificationTemplate.render(template, title=title, body=body)
            title = rendered["title"]
            body = rendered["body"]

        await self.notify(title, body, Priority.HIGH, extra, dedup_key=dedup_key)

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
                # 同一天内
                return start_min <= now_min < end_min
            else:
                # 跨天: 23:00 ~ 07:00
                return now_min >= start_min or now_min < end_min
        except (ValueError, TypeError):
            return False