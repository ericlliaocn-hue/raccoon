"""HTTP Webhook 入站网关

核心功能：
1. 接收外部 HTTP POST 请求（Bark 回调、Server酱消息、自定义 webhook）
2. Token 认证
3. 将请求转为 USER_MESSAGE 事件，注入 EventBus
4. 与通知网关形成闭环：外部发消息 → Raccoon 执行 → 通知推回外部
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.types import Event

logger = structlog.get_logger(__name__)


class GatewayInbound:
    """入站网关：接收外部请求，转为 EventBus 事件"""

    def __init__(self, event_bus: EventBus, config: RaccoonConfig | None = None) -> None:
        self._event_bus = event_bus
        self._config = config or RaccoonConfig()
        self._token = self._config.gateway_token
        self._enabled = bool(self._token)  # 有 token 才启用
        self._rate_limit: dict[str, list[float]] = {}  # source → [timestamps]
        self._rate_limit_per_minute = 60  # 每个 source 每分钟最多请求数

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def handle_inbound(
        self,
        text: str,
        source: str = "webhook",
        token: str | None = None,
        conversation_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Event:
        """处理入站请求

        Args:
            text: 消息文本
            source: 来源标识（bark/serverchan/webhook/自定义）
            token: 认证 Token
            conversation_id: 可选的对话 ID
            extra: 额外参数

        Returns:
            注入 EventBus 的事件

        Raises:
            GatewayAuthError: Token 认证失败
            GatewayRateLimitError: 请求频率超限
        """
        # 认证
        if not self._enabled:
            raise GatewayAuthError("入站网关未启用（未配置 gateway_token）")

        if token != self._token:
            raise GatewayAuthError("Token 认证失败")

        # 频率限制
        if not self._check_rate_limit(source):
            raise GatewayRateLimitError(f"请求频率超限: {source}")

        # 构建事件
        conv_id = conversation_id or f"gateway_{source}"
        event = make_event(
            EventType.USER_MESSAGE,
            conversation_id=conv_id,
            user_id=f"gateway:{source}",
            payload={
                "text": text,
                "source": source,
                **(extra or {}),
            },
        )

        # 注入 EventBus
        await self._event_bus.emit(event)
        logger.info("gateway_inbound_received", source=source, conversation_id=conv_id, text=text[:100])

        return event

    def _check_rate_limit(self, source: str) -> bool:
        """检查频率限制"""
        now = time.time()
        timestamps = self._rate_limit.get(source, [])

        # 清理 60 秒前的记录
        timestamps = [t for t in timestamps if now - t < 60]

        if len(timestamps) >= self._rate_limit_per_minute:
            self._rate_limit[source] = timestamps
            return False

        timestamps.append(now)
        self._rate_limit[source] = timestamps
        return True

    def get_stats(self) -> dict:
        """获取网关统计信息"""
        return {
            "enabled": self._enabled,
            "rate_limit_per_minute": self._rate_limit_per_minute,
            "active_sources": len(self._rate_limit),
        }


class GatewayAuthError(Exception):
    """网关认证错误"""
    pass


class GatewayRateLimitError(Exception):
    """网关频率限制错误"""
    pass
