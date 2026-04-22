"""通知通道抽象基类

所有通知通道继承 BaseChannel，实现 send() 方法。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

from pydantic import BaseModel, Field


class Priority(str, Enum):
    """通知优先级"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class Notification(BaseModel):
    """通知内容"""
    title: str
    body: str
    priority: Priority = Priority.NORMAL
    extra: dict = Field(default_factory=dict)  # 通道特定的额外参数


class BaseChannel(ABC):
    """通知通道抽象基类"""

    channel_type: str = "base"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}

    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """发送通知，返回是否成功"""
        ...

    @property
    def name(self) -> str:
        return self._config.get("name", self.channel_type)
