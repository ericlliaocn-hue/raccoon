"""钉钉 Webhook 通知通道

HTTP POST 到钉钉机器人 Webhook，支持签名验证。
文档: https://open.dingtalk.com/document/robots/custom-robot-access
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import time
import urllib.parse

import httpx

import structlog

from src.notifier.channels.base import BaseChannel, Notification, Priority

logger = structlog.get_logger(__name__)

# 钉钉优先级映射
PRIORITY_MAP = {
    Priority.LOW: "0",       # 普通消息
    Priority.NORMAL: "0",
    Priority.HIGH: "1",     # 重要消息（@all）
    Priority.URGENT: "1",
}


class DingTalkChannel(BaseChannel):
    """钉钉机器人 Webhook 推送"""

    channel_type = "dingtalk"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._webhook = self._config.get("webhook", "")
        self._secret = self._config.get("secret", "")  # 加签密钥
        self._at_mobiles: list[str] = self._config.get("at_mobiles", [])
        self._at_all: bool = self._config.get("at_all", False)

    def _sign_url(self) -> str:
        """生成带签名的 Webhook URL"""
        if not self._secret:
            return self._webhook

        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self._secret}"
        hmac_code = hmac.new(
            self._secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{self._webhook}&timestamp={timestamp}&sign={sign}"

    async def send(self, notification: Notification) -> bool:
        if not self._webhook:
            logger.warning("dingtalk_not_configured")
            return False

        try:
            url = self._sign_url()

            # 构建 Markdown 格式消息
            text = f"### {notification.title}\n\n{notification.body}"
            at_dict: dict = {}
            if self._at_all or notification.priority in (Priority.HIGH, Priority.URGENT):
                at_dict["atAll"] = True
            if self._at_mobiles:
                at_dict["atMobiles"] = self._at_mobiles

            payload = {
                "msgtype": "markdown",
                "markdown": {"title": notification.title, "text": text},
                "at": at_dict,
            }
            # 合并 extra
            payload.update(notification.extra)

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)

            success = resp.status_code == 200
            if success:
                data = resp.json()
                if data.get("errcode") != 0:
                    logger.warning("dingtalk_api_error", errcode=data.get("errcode"), errmsg=data.get("errmsg"))
                    return False
                logger.debug("dingtalk_sent", title=notification.title)
            else:
                logger.warning("dingtalk_failed", status=resp.status_code, body=resp.text[:200])
            return success

        except Exception as e:
            logger.error("dingtalk_error", error=str(e))
            return False
