"""双向通信测试"""

from __future__ import annotations

import time

import pytest

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType
from src.gateway.inbound import GatewayInbound, GatewayAuthError, GatewayRateLimitError


class TestGatewayInbound:
    @pytest.fixture
    def gateway_with_token(self):
        config = RaccoonConfig(gateway_token="test_secret_123")
        event_bus = EventBus(config)
        return GatewayInbound(event_bus, config)

    @pytest.fixture
    def gateway_without_token(self):
        config = RaccoonConfig(gateway_token="")
        event_bus = EventBus(config)
        return GatewayInbound(event_bus, config)

    def test_enabled_with_token(self, gateway_with_token):
        assert gateway_with_token.enabled is True

    def test_disabled_without_token(self, gateway_without_token):
        assert gateway_without_token.enabled is False

    @pytest.mark.asyncio
    async def test_handle_inbound_success(self, gateway_with_token):
        event = await gateway_with_token.handle_inbound(
            text="查看天气",
            source="bark",
            token="test_secret_123",
        )
        assert event.event == EventType.USER_MESSAGE
        assert event.payload["text"] == "查看天气"
        assert event.payload["source"] == "bark"
        assert event.user_id == "gateway:bark"

    @pytest.mark.asyncio
    async def test_handle_inbound_with_conversation_id(self, gateway_with_token):
        event = await gateway_with_token.handle_inbound(
            text="hello",
            source="webhook",
            token="test_secret_123",
            conversation_id="conv_123",
        )
        assert event.conversation_id == "conv_123"

    @pytest.mark.asyncio
    async def test_handle_inbound_default_conversation_id(self, gateway_with_token):
        event = await gateway_with_token.handle_inbound(
            text="hello",
            source="serverchan",
            token="test_secret_123",
        )
        assert event.conversation_id == "gateway_serverchan"

    @pytest.mark.asyncio
    async def test_handle_inbound_with_extra(self, gateway_with_token):
        event = await gateway_with_token.handle_inbound(
            text="hello",
            source="webhook",
            token="test_secret_123",
            extra={"priority": "high", "url": "http://example.com"},
        )
        assert event.payload["priority"] == "high"
        assert event.payload["url"] == "http://example.com"

    @pytest.mark.asyncio
    async def test_auth_failure_wrong_token(self, gateway_with_token):
        with pytest.raises(GatewayAuthError):
            await gateway_with_token.handle_inbound(
                text="hello",
                source="webhook",
                token="wrong_token",
            )

    @pytest.mark.asyncio
    async def test_auth_failure_no_token(self, gateway_with_token):
        with pytest.raises(GatewayAuthError):
            await gateway_with_token.handle_inbound(
                text="hello",
                source="webhook",
                token=None,
            )

    @pytest.mark.asyncio
    async def test_auth_failure_gateway_disabled(self, gateway_without_token):
        with pytest.raises(GatewayAuthError, match="未启用"):
            await gateway_without_token.handle_inbound(
                text="hello",
                source="webhook",
                token="anything",
            )

    @pytest.mark.asyncio
    async def test_rate_limit(self, gateway_with_token):
        # 快速发送大量请求
        for i in range(60):
            await gateway_with_token.handle_inbound(
                text=f"msg_{i}",
                source="ratelimited",
                token="test_secret_123",
            )
        # 第 61 次应该被限流
        with pytest.raises(GatewayRateLimitError):
            await gateway_with_token.handle_inbound(
                text="overflow",
                source="ratelimited",
                token="test_secret_123",
            )

    @pytest.mark.asyncio
    async def test_rate_limit_per_source(self, gateway_with_token):
        # source_a 发 60 次
        for i in range(60):
            await gateway_with_token.handle_inbound(
                text=f"msg_a_{i}",
                source="source_a",
                token="test_secret_123",
            )
        # source_b 应该不受影响
        event = await gateway_with_token.handle_inbound(
            text="msg_b",
            source="source_b",
            token="test_secret_123",
        )
        assert event is not None

    def test_get_stats(self, gateway_with_token):
        stats = gateway_with_token.get_stats()
        assert stats["enabled"] is True
        assert stats["rate_limit_per_minute"] == 60
        assert stats["active_sources"] == 0

    def test_get_stats_disabled(self, gateway_without_token):
        stats = gateway_without_token.get_stats()
        assert stats["enabled"] is False


class TestGatewayErrors:
    def test_auth_error_is_exception(self):
        err = GatewayAuthError("test")
        assert isinstance(err, Exception)
        assert str(err) == "test"

    def test_rate_limit_error_is_exception(self):
        err = GatewayRateLimitError("test")
        assert isinstance(err, Exception)
        assert str(err) == "test"
