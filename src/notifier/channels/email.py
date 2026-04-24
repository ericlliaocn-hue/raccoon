"""Email 通知通道（SMTP）

使用 aiosmtplib 异步发送邮件。
"""

from __future__ import annotations

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

import structlog

from src.notifier.channels.base import BaseChannel, Notification, Priority

logger = structlog.get_logger(__name__)


class EmailChannel(BaseChannel):
    """Email SMTP 通知通道"""

    channel_type = "email"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self._host = self._config.get("host", "")
        self._port = int(self._config.get("port", 465))
        self._username = self._config.get("username", "")
        self._password = self._config.get("password", "")
        self._from_addr = self._config.get("from", self._username)
        self._to_addrs: list[str] = self._config.get("to", [])
        self._use_tls = self._config.get("tls", True)
        self._subject_prefix = self._config.get("subject_prefix", "[Raccoon]")

    async def send(self, notification: Notification) -> bool:
        if not self._host or not self._to_addrs:
            logger.warning("email_not_configured")
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"{self._subject_prefix} {notification.title}"
            msg["From"] = self._from_addr
            msg["To"] = ", ".join(self._to_addrs)

            # 纯文本版本
            text_part = MIMEText(notification.body, "plain", "utf-8")
            msg.attach(text_part)

            # HTML 版本（简报类通知用富文本）
            html_body = notification.extra.get("html_body")
            if html_body:
                html_part = MIMEText(html_body, "html", "utf-8")
                msg.attach(html_part)

            # 优先级标记
            if notification.priority in (Priority.HIGH, Priority.URGENT):
                msg["X-Priority"] = "1"  # High

            await aiosmtplib.send(
                msg,
                hostname=self._host,
                port=self._port,
                username=self._username or None,
                password=self._password or None,
                use_tls=self._use_tls,
            )

            logger.debug("email_sent", to=self._to_addrs, title=notification.title)
            return True

        except Exception as e:
            logger.error("email_error", error=str(e))
            return False
