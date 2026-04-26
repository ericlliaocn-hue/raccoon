from __future__ import annotations

import asyncio

import pytest

import src.channels.adapters.feishu_ws_client as feishu_ws_module
from src.channels.adapters.feishu import FeishuParseResult
from src.channels.adapters.feishu_ws_client import FeishuWebSocketBridge, _resolve_feishu_domain
from src.config import RaccoonConfig


def _ws_test_config(tmp_path, **overrides) -> RaccoonConfig:
    data = {
        "skills_dir": tmp_path / "skills",
        "db_path": tmp_path / "data" / "memcore.db",
        "schedules_dir": tmp_path / "schedules",
        "workflows_dir": tmp_path / "workflows",
        "llm_provider": "mock",
        "notify_channels": [],
    }
    data.update(overrides)
    return RaccoonConfig(**data)


def test_resolve_feishu_domain() -> None:
    assert _resolve_feishu_domain("feishu") == "https://open.feishu.cn"
    assert _resolve_feishu_domain("lark") == "https://open.larksuite.com"
    assert _resolve_feishu_domain("https://open.feishu.cn/") == "https://open.feishu.cn"
    assert _resolve_feishu_domain("open.feishu.cn") == "https://open.feishu.cn"


@pytest.mark.asyncio
async def test_ws_bridge_noop_when_not_websocket_mode(tmp_path) -> None:
    config = _ws_test_config(tmp_path, feishu_enabled=True, feishu_mode="callback")

    async def _noop(_parsed) -> None:
        return None

    bridge = FeishuWebSocketBridge(config=config, on_parsed_event=_noop)
    await bridge.start()  # no-op
    await bridge.stop()


@pytest.mark.asyncio
async def test_ws_bridge_requires_credentials(tmp_path) -> None:
    config = _ws_test_config(tmp_path, feishu_enabled=True, feishu_mode="websocket")

    async def _noop(_parsed) -> None:
        return None

    bridge = FeishuWebSocketBridge(config=config, on_parsed_event=_noop)
    with pytest.raises(RuntimeError):
        await bridge.start()


def test_ws_message_parse_does_not_enforce_verification_token(tmp_path, monkeypatch) -> None:
    config = _ws_test_config(
        tmp_path,
        feishu_enabled=True,
        feishu_mode="websocket",
        feishu_verification_token="should_not_be_used",
    )

    async def _noop(_parsed) -> None:
        return None

    captured: dict[str, str] = {}

    def _fake_parse(event, **kwargs):  # noqa: ANN001
        captured["verification_token"] = kwargs.get("verification_token", "")
        return FeishuParseResult(ok=True, detail="ignored")

    monkeypatch.setattr(feishu_ws_module, "parse_feishu_ws_event", _fake_parse)

    bridge = FeishuWebSocketBridge(config=config, on_parsed_event=_noop)
    bridge._main_loop = asyncio.new_event_loop()
    try:
        bridge._on_ws_message(object())
    finally:
        bridge._main_loop.close()
        bridge._main_loop = None

    assert captured["verification_token"] == ""
