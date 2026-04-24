"""系统通知通道

macOS: osascript -e 'display notification ...'
Linux: notify-send
"""

from __future__ import annotations

import asyncio
import platform

import structlog

from src.notifier.channels.base import BaseChannel, Notification

logger = structlog.get_logger(__name__)


class SystemChannel(BaseChannel):
    """系统原生通知"""

    channel_type = "system"

    async def send(self, notification: Notification) -> bool:
        try:
            system = platform.system()
            if system == "Darwin":
                return await self._send_macos(notification)
            elif system == "Linux":
                return await self._send_linux(notification)
            else:
                logger.warning("system_notify_unsupported", system=system)
                return False
        except Exception as e:
            logger.error("system_notify_failed", error=str(e))
            return False

    async def _send_macos(self, notification: Notification) -> bool:
        """macOS osascript 通知"""
        title = notification.title.replace('"', '\\"')
        body = notification.body.replace('"', '\\"')
        sound = self._config.get("sound", "default")
        script = f'display notification "{body}" with title "{title}" sound name "{sound}"'

        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        success = proc.returncode == 0
        if success:
            logger.debug("system_notify_sent_macos", title=notification.title)
        return success

    async def _send_linux(self, notification: Notification) -> bool:
        """Linux notify-send 通知"""
        proc = await asyncio.create_subprocess_exec(
            "notify-send", notification.title, notification.body,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        success = proc.returncode == 0
        if success:
            logger.debug("system_notify_sent_linux", title=notification.title)
        return success
