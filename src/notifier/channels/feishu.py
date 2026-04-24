"""飞书 Webhook 通知通道

HTTP POST 到飞书机器人 Webhook，支持签名验证。
文档: https://open.feishu.cn/document/ukTMukTMukTM/ucTM5YjL3ETO14yNxkjN
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import time

import httpx

import structlog

from src.notifier.channels.base import BaseChannel, Notification, Priority

logger = structlog.get_logger(__name__)


class FeishuChannel(BaseChannel):
    """飞书机器人 Webhook 推送"""

    channel_type = "feishu"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._webhook = self._config.get("webhook", "")
        self._secret = self._config.get("secret", "")  # 签名密钥

    def _gen_sign(self, timestamp: str) -> str:
        """生成签名"""
        if not self._secret:
            return ""
        string_to_sign = f"{timestamp}\n{self._secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(hmac_code).decode("utf-8")

    async def send(self, notification: Notification) -> bool:
        if not self._webhook:
            logger.warning("feishu_not_configured")
            return False

        try:
            timestamp = str(int(time.time()))

            # 构建富文本消息
            content = {
                "zh_cn": {
                    "title": notification.title,
                    "content": [
                        [{"tag": "text", "text": notification.body}],
                    ],
                }
            }

            payload = {
                "msg_type": "post",
                "content": {"post": content},
            }

            # 添加签名
            if self._secret:
                sign = self._gen_sign(timestamp)
                payload["timestamp"] = timestamp
                payload["sign"] = sign

            # 高优先级使用 interactive 卡片
            if notification.priority in (Priority.HIGH, Priority.URGENT):
                payload = {
                    "msg_type": "interactive",
                    "card": {
                        "header": {
                            "title": {"tag": "plain_text", "content": notification.title},
                            "template": "red" if notification.priority == Priority.URGENT else "orange",
                        },
                        "elements": [
                            {"tag": "markdown", "content": notification.body},
                        ],
                    },
                }
                if self._secret:
                    sign = self._gen_sign(timestamp)
                    payload["timestamp"] = timestamp
                    payload["sign"] = sign

            # 合并 extra
            payload.update(notification.extra)

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._webhook, json=payload)

            success = resp.status_code == 200
            if success:
                data = resp.json()
                if data.get("code") != 0:
                    logger.warning("feishu_api_error", code=data.get("code"), msg=data.get("msg"))
                    return False
                logger.debug("feishu_sent", title=notification.title)
            else:
                logger.warning("feishu_failed", status=resp.status_code, body=resp.text[:200])
            return success

        except Exception as e:
            logger.error("feishu_error", error=str(e))
            return False
