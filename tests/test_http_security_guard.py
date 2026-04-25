from __future__ import annotations

from src.config import RaccoonConfig

import raccoon


def _config(**overrides) -> RaccoonConfig:
    data = {
        "http_auth_token": "",
        "http_enforce_remote_auth": True,
        "http_min_auth_token_length": 16,
    }
    data.update(overrides)
    return RaccoonConfig(**data)


def test_remote_http_requires_token_when_guard_enabled():
    ok, reason = raccoon._validate_http_security_guard("0.0.0.0", _config())
    assert ok is False
    assert "未配置 http_auth_token" in reason


def test_remote_http_rejects_short_token():
    ok, reason = raccoon._validate_http_security_guard(
        "0.0.0.0",
        _config(http_auth_token="short-token", http_min_auth_token_length=16),
    )
    assert ok is False
    assert "长度不足" in reason


def test_remote_http_accepts_strong_token():
    ok, reason = raccoon._validate_http_security_guard(
        "0.0.0.0",
        _config(http_auth_token="abcdefghijklmnopqrstuvwxyz1234"),
    )
    assert ok is True
    assert reason == ""


def test_local_http_does_not_require_token():
    ok, reason = raccoon._validate_http_security_guard("127.0.0.1", _config())
    assert ok is True
    assert reason == ""
