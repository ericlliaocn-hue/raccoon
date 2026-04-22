"""Server酱推送通道

HTTP POST 到 sctapi.ftqq.com
文档: https://sct.ftqq.com/
"""

from __future__ import annotations

import httpx

import structlog

from src.notifier.channels.base import BaseChannel, Notification

logger = structlog.get_logger(__name__)


class ServerChanChannel(BaseChannel):
    """Server酱推送"""

    channel_type = "serverchan"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._sendkey = self._config.get("sendkey", "")

    async def send(self, notification: Notification) -> bool:
        if not self._sendkey:
            logger.warning("serverchan_not_configured")
            return False

        try:
            url = f"https://sctapi.ftqq.com/{self._sendkey}.send"
            payload = {
                "title": notification.title,
                "desp": notification.body,
            }
            # 合并 extra 中的 Server酱特定参数（channel, openid 等）
            payload.update(notification.extra)

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)

            success = resp.status_code == 200
            if success:
                data = resp.json()
                if data.get("code") != 0:
                    logger.warning("serverchan_api_error", code=data.get("code"), message=data.get("message"))
                    return False
                logger.debug("serverchan_sent", title=notification.title)
            else:
                logger.warning("serverchan_failed", status=resp.status_code)
            return success

        except Exception as e:
            logger.error("serverchan_error", error=str(e))
            return False
