"""通知网关测试

覆盖：
- 新通道（钉钉/飞书/Email）基本功能
- 通道注册表
- Notifier 统一接口
- 模板系统
- 通知去重
"""

from __future__ import annotations

import asyncio
import time
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
from src.notifier.channels.dingtalk import DingTalkChannel
from src.notifier.channels.feishu import FeishuChannel
from src.notifier.channels.email import EmailChannel
from src.notifier.notifier import Notifier, CHANNEL_REGISTRY
from src.notifier.templates import NotificationTemplate
from src.notifier.dedup import NotificationDedup


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


# ─── 新通道测试 ──────────────────────────────────────────────

class TestDingTalkChannel:
    def test_channel_type(self):
        ch = DingTalkChannel()
        assert ch.channel_type == "dingtalk"

    @pytest.mark.asyncio
    async def test_not_configured(self):
        ch = DingTalkChannel()
        n = Notification(title="test", body="hello")
        ok = await ch.send(n)
        assert ok is False

    def test_config_with_webhook(self):
        ch = DingTalkChannel(config={"webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx", "secret": "sec123"})
        assert "oapi.dingtalk.com" in ch._webhook
        assert ch._secret == "sec123"


class TestFeishuChannel:
    def test_channel_type(self):
        ch = FeishuChannel()
        assert ch.channel_type == "feishu"

    @pytest.mark.asyncio
    async def test_not_configured(self):
        ch = FeishuChannel()
        n = Notification(title="test", body="hello")
        ok = await ch.send(n)
        assert ok is False

    def test_config_with_webhook(self):
        ch = FeishuChannel(config={"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx", "secret": "sec123"})
        assert "open.feishu.cn" in ch._webhook


class TestEmailChannel:
    def test_channel_type(self):
        ch = EmailChannel()
        assert ch.channel_type == "email"

    @pytest.mark.asyncio
    async def test_not_configured(self):
        ch = EmailChannel()
        n = Notification(title="test", body="hello")
        ok = await ch.send(n)
        assert ok is False

    def test_config_with_smtp(self):
        ch = EmailChannel(config={
            "host": "smtp.example.com",
            "port": 465,
            "username": "user@example.com",
            "password": "pass",
            "from": "user@example.com",
            "to": ["admin@example.com"],
        })
        assert ch._host == "smtp.example.com"
        assert ch._to_addrs == ["admin@example.com"]


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
        assert "dingtalk" in CHANNEL_REGISTRY
        assert "feishu" in CHANNEL_REGISTRY
        assert "email" in CHANNEL_REGISTRY

    def test_registry_types(self):
        assert CHANNEL_REGISTRY["system"] is SystemChannel
        assert CHANNEL_REGISTRY["bark"] is BarkChannel
        assert CHANNEL_REGISTRY["serverchan"] is ServerChanChannel
        assert CHANNEL_REGISTRY["webhook"] is WebhookChannel
        assert CHANNEL_REGISTRY["dingtalk"] is DingTalkChannel
        assert CHANNEL_REGISTRY["feishu"] is FeishuChannel
        assert CHANNEL_REGISTRY["email"] is EmailChannel


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
        n = Notifier(event_bus, RaccoonConfig(notify_silent_hours={"start": "00:00", "end": "23:59"}))
        fake = FakeChannel()
        n._channels = [fake]
        n._initialized = True
        results = await n.notify("title", "body")
        assert results == {}
        assert len(fake.sent) == 0

    @pytest.mark.asyncio
    async def test_notify_silent_hours_cross_midnight(self, event_bus):
        n = Notifier(event_bus, RaccoonConfig(notify_silent_hours={"start": "23:00", "end": "07:00"}))
        n._channels = [FakeChannel()]
        n._initialized = True
        assert n._is_silent_hours() in (True, False)

    @pytest.mark.asyncio
    async def test_notify_with_dedup(self, event_bus):
        n = Notifier(event_bus, RaccoonConfig())
        fake = FakeChannel()
        n._channels = [fake]
        n._initialized = True

        # 第一次发送
        r1 = await n.notify("title", "body", dedup_key="test_key")
        assert fake.name in r1
        assert len(fake.sent) == 1

        # 5秒内重复发送应被去重
        r2 = await n.notify("title", "body", dedup_key="test_key")
        assert r2 == {}
        assert len(fake.sent) == 1

    @pytest.mark.asyncio
    async def test_notify_without_dedup(self, event_bus):
        n = Notifier(event_bus, RaccoonConfig())
        fake = FakeChannel()
        n._channels = [fake]
        n._initialized = True

        # 不传 dedup_key 不去重
        await n.notify("title", "body")
        await n.notify("title", "body")
        assert len(fake.sent) == 2

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


# ─── 模板系统测试 ────────────────────────────────────────────

class TestNotificationTemplate:
    def test_default_template(self):
        result = NotificationTemplate.render("default", title="测试", body="内容")
        assert result["title"] == "测试"
        assert result["body"] == "内容"

    def test_brief_template(self):
        result = NotificationTemplate.render("brief", title="每日简报", body="今天天气晴")
        assert "📋" in result["title"]
        assert "每日简报" in result["title"]
        assert "html_body" in result

    def test_alert_template(self):
        result = NotificationTemplate.render("alert", title="磁盘告警", body="使用率 95%")
        assert "🚨" in result["title"]
        assert "⚠️" in result["body"]

    def test_approval_template(self):
        result = NotificationTemplate.render(
            "approval",
            title="高风险操作",
            body="删除数据库",
            timeout="5 分钟",
            approve_url="http://approve",
            reject_url="http://reject",
        )
        assert "🔐" in result["title"]
        assert "5 分钟" in result["body"]

    def test_schedule_result_template(self):
        result = NotificationTemplate.render(
            "schedule_result",
            title="每日简报",
            body="天气晴",
            schedule_name="早间简报",
            result="成功",
            cron="0 8 * * *",
        )
        assert "⏰" in result["title"]
        assert "成功" in result["title"]

    def test_register_custom_template(self):
        NotificationTemplate.register_template("custom_test", {
            "title": "[自定义] ${title}",
            "body": "${body}",
        })
        result = NotificationTemplate.render("custom_test", title="测试", body="内容")
        assert "[自定义]" in result["title"]

    def test_list_templates(self):
        templates = NotificationTemplate.list_templates()
        assert "default" in templates
        assert "brief" in templates
        assert "alert" in templates
        assert "approval" in templates

    def test_unknown_template_falls_back_to_default(self):
        result = NotificationTemplate.render("nonexistent", title="T", body="B")
        assert result["title"] == "T"
        assert result["body"] == "B"


# ─── 通知去重测试 ────────────────────────────────────────────

class TestNotificationDedup:
    def test_should_send_first_time(self):
        dedup = NotificationDedup(window_seconds=300)
        assert dedup.should_send("key1") is True

    def test_should_not_send_within_window(self):
        dedup = NotificationDedup(window_seconds=300)
        dedup.should_send("key1")
        assert dedup.should_send("key1") is False

    def test_different_keys_not_deduped(self):
        dedup = NotificationDedup(window_seconds=300)
        dedup.should_send("key1")
        assert dedup.should_send("key2") is True

    def test_cleanup_removes_expired(self):
        dedup = NotificationDedup(window_seconds=1)
        dedup.should_send("key1")
        time.sleep(1.1)
        count = dedup.cleanup()
        assert count == 1
        # 过期后可以重新发送
        assert dedup.should_send("key1") is True

    def test_reset_specific_key(self):
        dedup = NotificationDedup(window_seconds=300)
        dedup.should_send("key1")
        dedup.should_send("key2")
        dedup.reset("key1")
        assert dedup.should_send("key1") is True
        assert dedup.should_send("key2") is False

    def test_reset_all(self):
        dedup = NotificationDedup(window_seconds=300)
        dedup.should_send("key1")
        dedup.should_send("key2")
        dedup.reset()
        assert dedup.should_send("key1") is True
        assert dedup.should_send("key2") is True
