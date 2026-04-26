from __future__ import annotations

from src.adapters.auth import is_protected_http_endpoint


def test_feishu_callback_route_is_public() -> None:
    assert is_protected_http_endpoint("/channels/feishu/events", "POST") is False


def test_notify_routes_are_protected() -> None:
    assert is_protected_http_endpoint("/notify", "POST") is True
    assert is_protected_http_endpoint("/notify/channels", "GET") is True
