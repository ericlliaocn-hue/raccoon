"""Bark 推送通道

HTTP POST 到 bark_url/push
文档: https://github.com/Finb/Bark
"""

from __future__ import annotations

import httpx

import structlog

from src.notifier.channels.base import BaseChannel, Notification, Priority

logger = structlog.get_logger(__name__)

# Bark 优先级映射
PRIORITY_MAP = {
    Priority.LOW: "passive",
    Priority.NORMAL: "active",
    Priority.HIGH: "timeSensitive",
    Priority.URGENT: "critical",
}


class BarkChannel(BaseChannel):
    """Bark 推送"""

    channel_type = "bark"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._url = self._config.get("url", "").rstrip("/")
        self._key = self._config.get("key", "")

    async def send(self, notification: Notification) -> bool:
        if not self._url or not self._key:
            logger.warning("bark_not_configured")
            return False

        try:
            endpoint = f"{self._url}/{self._key}"
            payload = {
                "title": notification.title,
                "body": notification.body,
                "level": PRIORITY_MAP.get(notification.priority, "active"),
                "sound": self._config.get("sound", "birdsong"),
            }
            # 合并 extra 中的 Bark 特定参数
            payload.update(notification.extra)

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(endpoint, json=payload)

            success = resp.status_code == 200
            if success:
                logger.debug("bark_sent", title=notification.title)
            else:
                logger.warning("bark_failed", status=resp.status_code, body=resp.text[:200])
            return success

        except Exception as e:
            logger.error("bark_error", error=str(e))
            return False
