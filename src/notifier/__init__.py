"""通知网关模块

提供多通道通知能力：系统通知、Bark、Server酱、Webhook、钉钉、飞书、Email。
支持模板系统和通知去重。
"""

from src.notifier.notifier import Notifier
from src.notifier.channels.base import BaseChannel, Notification
from src.notifier.templates import NotificationTemplate
from src.notifier.dedup import NotificationDedup

__all__ = ["Notifier", "BaseChannel", "Notification", "NotificationTemplate", "NotificationDedup"]
