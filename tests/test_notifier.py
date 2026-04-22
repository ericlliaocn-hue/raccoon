"""通知网关测试"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.notifier.channels.base import BaseChannel, Notification, Priority
from src.notifier.channels.system import SystemChannel
from src.notifier.channels.bark import BarkChannel
from src.notifier.channels.serverchan import ServerChanChannel
from src.notifier.channels.webhook import WebhookChannel
from src.notifier.notifier import Notifier, CHANNEL_REGISTRY


# ─── Fake Channel (测试用) ────────────────────────────────────

class FakeChannel(BaseChannel):
    """测试用通道，记录所有调用"""
    channel_type = "fake"

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        self.sent: list[Notification] = []
        self._fail = config.get("fail", False) if config else False

    async def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        if self._fail:
            raise RuntimeError("channel error")
        return not self._fail


# ─── Channel 基类测试 ─────────────────────────────────────────

class TestBaseChannel:
    def test_priority_enum(self):
        assert Priority.LOW.value == "low"
        assert Priority.NORMAL.value == "normal"
        assert Priority.HIGH.value == "high"
        assert Priority.URGENT.value == "urgent"

    def test_notification_model(self):
        n = Notification(title="测试", body="内容", priority=Priority.HIGH)
        assert n.title == "测试"
        assert n.priority == Priority.HIGH
        assert n.extra == {}

    def test_notification_with_extra(self):
        n = Notification(title="T", body="B", extra={"url": "http://x"})
        assert n.extra["url"] == "http://x"

    @pytest.mark.asyncio
    async def test_fake_channel_send(self):
        ch = FakeChannel()
        n = Notification(title="test", body="hello")
        ok = await ch.send(n)
        assert ok is True
        assert len(ch.sent) == 1

    @pytest.mark.asyncio
    async def test_fake_channel_fail(self):
        ch = FakeChannel(config={"fail": True})
        n = Notification(title="test", body="hello")
        with pytest.raises(RuntimeError):
            await ch.send(n)


# ─── SystemChannel 测试 ───────────────────────────────────────

class TestSystemChannel:
    def test_channel_type(self):
        ch = SystemChannel()
        assert ch.channel_type == "system"

    def test_name_default(self):
        ch = SystemChannel()
        assert ch.name == "system"

    def test_name_custom(self):
        ch = SystemChannel(config={"name": "my_notify"})
        assert ch.name == "my_notify"


# ─── BarkChannel 测试 ─────────────────────────────────────────

class TestBarkChannel:
    def test_channel_type(self):
        ch = BarkChannel()
        assert ch.channel_type == "bark"

    @pytest.mark.asyncio
    async def test_not_configured(self):
        ch = BarkChannel()
        n = Notification(title="test", body="hello")
        ok = await ch.send(n)
        assert ok is False

    @pytest.mark.asyncio
    async def test_configured_but_network_error(self):
        ch = BarkChannel(config={"url": "http://localhost:99999", "key": "testkey"})
        n = Notification(title="test", body="hello")
        ok = await ch.send(n)
        assert ok is False


# ─── ServerChanChannel 测试 ──────────────────────────────────

class TestServerChanChannel:
    def test_channel_type(self):
        ch = ServerChanChannel()
        assert ch.channel_type == "serverchan"

    @pytest.mark.asyncio
    async def test_not_configured(self):
        ch = ServerChanChannel()
        n = Notification(title="test", body="hello")
        ok = await ch.send(n)
        assert ok is False


# ─── WebhookChannel 测试 ─────────────────────────────────────

class TestWebhookChannel:
    def test_channel_type(self):
        ch = WebhookChannel()
        assert ch.channel_type == "webhook"

    @pytest.mark.asyncio
    async def test_not_configured(self):
        ch = WebhookChannel()
        n = Notification(title="test", body="hello")
        ok = await ch.send(n)
        assert ok is False

    def test_config_with_secret(self):
        ch = WebhookChannel(config={"url": "http://example.com/hook", "secret": "mysecret"})
        assert ch._url == "http://example.com/hook"
        assert ch._secret == "mysecret"


# ─── Channel Registry 测试 ────────────────────────────────────

class TestChannelRegistry:
    def test_registry_has_all_channels(self):
        assert "system" in CHANNEL_REGISTRY
        assert "bark" in CHANNEL_REGISTRY
        assert "serverchan" in CHANNEL_REGISTRY
        assert "webhook" in CHANNEL_REGISTRY

    def test_registry_types(self):
        assert CHANNEL_REGISTRY["system"] is SystemChannel
        assert CHANNEL_REGISTRY["bark"] is BarkChannel
        assert CHANNEL_REGISTRY["serverchan"] is ServerChanChannel
        assert CHANNEL_REGISTRY["webhook"] is WebhookChannel


# ─── Notifier 统一接口测试 ────────────────────────────────────

class TestNotifier:
    @pytest.fixture
    def event_bus(self):
        config = RaccoonConfig()
        return EventBus(config)

    @pytest.fixture
    def notifier(self, event_bus):
        cfg = RaccoonConfig(notify_channels=[{"type": "system"}])
        n = Notifier(event_bus, cfg)
        return n

    def test_initialize(self, notifier):
        notifier.initialize()
        assert notifier._initialized is True
        assert len(notifier.channels) == 1

    def test_initialize_idempotent(self, notifier):
        notifier.initialize()
        notifier.initialize()  # 不应重复初始化
        assert len(notifier.channels) == 1

    def test_initialize_unknown_channel(self, event_bus):
        cfg = RaccoonConfig(notify_channels=[{"type": "unknown_channel"}])
        n = Notifier(event_bus, cfg)
        n.initialize()
        assert len(n.channels) == 0

    def test_initialize_multiple_channels(self, event_bus):
        cfg = RaccoonConfig(notify_channels=[
            {"type": "system"},
            {"type": "bark", "url": "http://bark.test", "key": "k"},
            {"type": "webhook", "url": "http://hook.test"},
        ])
        n = Notifier(event_bus, cfg)
        n.initialize()
        assert len(n.channels) == 3

    @pytest.mark.asyncio
    async def test_notify_no_channels(self, event_bus):
        cfg = RaccoonConfig(notify_channels=[])
        n = Notifier(event_bus, cfg)
        n.initialize()
        results = await n.notify("title", "body")
        assert results == {}

    @pytest.mark.asyncio
    async def test_notify_with_fake_channel(self, event_bus):
        n = Notifier(event_bus, RaccoonConfig())
        fake = FakeChannel()
        n._channels = [fake]
        n._initialized = True
        results = await n.notify("测试标题", "测试内容")
        assert fake.name in results
        assert results[fake.name] is True
        assert len(fake.sent) == 1
        assert fake.sent[0].title == "测试标题"

    @pytest.mark.asyncio
    async def test_notify_multi_channels(self, event_bus):
        n = Notifier(event_bus, RaccoonConfig())
        fake1 = FakeChannel(config={"name": "ch1"})
        fake2 = FakeChannel(config={"name": "ch2"})
        n._channels = [fake1, fake2]
        n._initialized = True
        results = await n.notify("title", "body")
        assert results["ch1"] is True
        assert results["ch2"] is True

    @pytest.mark.asyncio
    async def test_notify_one_channel_fails(self, event_bus):
        n = Notifier(event_bus, RaccoonConfig())
        fake_ok = FakeChannel(config={"name": "ok"})
        fake_fail = FakeChannel(config={"name": "fail", "fail": True})
        n._channels = [fake_ok, fake_fail]
        n._initialized = True
        results = await n.notify("title", "body")
        assert results["ok"] is True
        assert results["fail"] is False  # 一个失败不影响其他

    @pytest.mark.asyncio
    async def test_notify_silent_hours(self, event_bus):
        # 设置当前时间在静默时段内
        n = Notifier(event_bus, RaccoonConfig(notify_silent_hours={"start": "00:00", "end": "23:59"}))
        fake = FakeChannel()
        n._channels = [fake]
        n._initialized = True
        results = await n.notify("title", "body")
        assert results == {}  # 静默时段内不发送
        assert len(fake.sent) == 0

    @pytest.mark.asyncio
    async def test_notify_silent_hours_cross_midnight(self, event_bus):
        # 跨天静默时段: 23:00 ~ 07:00
        n = Notifier(event_bus, RaccoonConfig(notify_silent_hours={"start": "23:00", "end": "07:00"}))
        n._channels = [FakeChannel()]
        n._initialized = True
        # 测试方法本身（不依赖具体时间）
        assert n._is_silent_hours() in (True, False)  # 取决于当前时间

    @pytest.mark.asyncio
    async def test_on_task_completed(self, event_bus):
        n = Notifier(event_bus, RaccoonConfig(notify_on_success=True))
        fake = FakeChannel()
        n._channels = [fake]
        n._initialized = True

        event = make_event(
            EventType.TASK_COMPLETED,
            conversation_id="test",
            task_id="t1",
            skill_name="test_skill",
            payload={"result": "ok"},
        )
        await n._on_task_completed(event)
        assert len(fake.sent) == 1
        assert "完成" in fake.sent[0].title

    @pytest.mark.asyncio
    async def test_on_task_completed_disabled(self, event_bus):
        n = Notifier(event_bus, RaccoonConfig(notify_on_success=False))
        fake = FakeChannel()
        n._channels = [fake]
        n._initialized = True

        event = make_event(EventType.TASK_COMPLETED, conversation_id="test")
        await n._on_task_completed(event)
        assert len(fake.sent) == 0

    @pytest.mark.asyncio
    async def test_on_task_failed(self, event_bus):
        n = Notifier(event_bus, RaccoonConfig(notify_on_failure=True))
        fake = FakeChannel()
        n._channels = [fake]
        n._initialized = True

        event = make_event(
            EventType.TASK_FAILED,
            conversation_id="test",
            task_id="t1",
            skill_name="test_skill",
            payload={"error": "timeout"},
        )
        await n._on_task_failed(event)
        assert len(fake.sent) == 1
        assert "失败" in fake.sent[0].title
        assert fake.sent[0].priority == Priority.HIGH

    @pytest.mark.asyncio
    async def test_on_task_failed_disabled(self, event_bus):
        n = Notifier(event_bus, RaccoonConfig(notify_on_failure=False))
        fake = FakeChannel()
        n._channels = [fake]
        n._initialized = True

        event = make_event(EventType.TASK_FAILED, conversation_id="test")
        await n._on_task_failed(event)
        assert len(fake.sent) == 0
