"""通用 Webhook 通知通道

HTTP POST 到自定义 URL，JSON payload。
"""

from __future__ import annotations

import httpx

import structlog

from src.notifier.channels.base import BaseChannel, Notification

logger = structlog.get_logger(__name__)


class WebhookChannel(BaseChannel):
    """通用 Webhook 通道"""

    channel_type = "webhook"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._url = self._config.get("url", "")
        self._secret = self._config.get("secret", "")
        self._method = self._config.get("method", "POST").upper()

    async def send(self, notification: Notification) -> bool:
        if not self._url:
            logger.warning("webhook_not_configured")
            return False

        try:
            payload = {
                "title": notification.title,
                "body": notification.body,
                "priority": notification.priority.value,
                **notification.extra,
            }

            headers = {"Content-Type": "application/json"}
            if self._secret:
                headers["Authorization"] = f"Bearer {self._secret}"

            async with httpx.AsyncClient(timeout=10) as client:
                if self._method == "GET":
                    resp = await client.get(self._url, params=payload, headers=headers)
                else:
                    resp = await client.post(self._url, json=payload, headers=headers)

            success = 200 <= resp.status_code < 300
            if success:
                logger.debug("webhook_sent", url=self._url, title=notification.title)
            else:
                logger.warning("webhook_failed", status=resp.status_code, body=resp.text[:200])
            return success

        except Exception as e:
            logger.error("webhook_error", error=str(e))
            return False
