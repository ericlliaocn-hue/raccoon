"""通知网关模块

提供多通道通知能力：系统通知、Bark、Server酱、Webhook。
"""

from src.notifier.notifier import Notifier
from src.notifier.channels.base import BaseChannel, Notification

__all__ = ["Notifier", "BaseChannel", "Notification"]
