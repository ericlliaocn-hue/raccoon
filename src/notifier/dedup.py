"""通知去重

同一任务在指定时间窗口内不重复推送。
默认 5 分钟窗口，基于 (schedule_id/task_id + title) 去重。
"""

from __future__ import annotations

import time

import structlog

logger = structlog.get_logger(__name__)


class NotificationDedup:
    """通知去重器"""

    def __init__(self, window_seconds: int = 300) -> None:
        """Args:
            window_seconds: 去重时间窗口（秒），默认 5 分钟
        """
        self._window = window_seconds
        self._sent: dict[str, float] = {}  # dedup_key → timestamp

    def should_send(self, key: str) -> bool:
        """检查是否应该发送通知

        Args:
            key: 去重键（通常由 schedule_id/task_id + title 组合）

        Returns:
            True 表示应该发送，False 表示重复应跳过
        """
        now = time.monotonic()
        last_sent = self._sent.get(key)

        if last_sent is not None and (now - last_sent) < self._window:
            logger.debug("notification_dedup_skipped", key=key)
            return False

        self._sent[key] = now
        return True

    def cleanup(self) -> int:
        """清理过期的去重记录，返回清理数量"""
        now = time.monotonic()
        expired_keys = [
            k for k, ts in self._sent.items()
            if (now - ts) >= self._window
        ]
        for k in expired_keys:
            del self._sent[k]
        return len(expired_keys)

    def reset(self, key: str | None = None) -> None:
        """重置去重记录

        Args:
            key: 指定键重置，None 则重置全部
        """
        if key:
            self._sent.pop(key, None)
        else:
            self._sent.clear()
