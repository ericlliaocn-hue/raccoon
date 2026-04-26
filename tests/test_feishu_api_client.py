from __future__ import annotations

import json
import httpx
import pytest

from src.channels.adapters.feishu_api_client import FeishuApiClient, _resolve_feishu_domain


def test_feishu_api_domain_resolver() -> None:
    assert _resolve_feishu_domain("feishu") == "https://open.feishu.cn"
    assert _resolve_feishu_domain("lark") == "https://open.larksuite.com"
    assert _resolve_feishu_domain("https://open.feishu.cn/") == "https://open.feishu.cn"
    assert _resolve_feishu_domain("open.feishu.cn") == "https://open.feishu.cn"


@pytest.mark.asyncio
async def test_send_text_uses_cached_token() -> None:
    state = {"token_calls": 0, "send_calls": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            state["token_calls"] += 1
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t_demo", "expire": 7200})
        if request.url.path.endswith("/im/v1/messages"):
            state["send_calls"] += 1
            return httpx.Response(200, json={"code": 0, "msg": "ok"})
        return httpx.Response(404, json={"code": 404, "msg": "not_found"})

    transport = httpx.MockTransport(handler)
    client = FeishuApiClient(
        app_id="cli_x",
        app_secret="sec_x",
        domain="feishu",
        transport=transport,
    )
    try:
        ok1, detail1 = await client.send_text(chat_id="oc_1", text="hello")
        ok2, detail2 = await client.send_text(chat_id="oc_1", text="world")
        assert ok1 is True and ok2 is True
        assert detail1 in {"ok", "ok_after_token_refresh"}
        assert detail2 in {"ok", "ok_after_token_refresh"}
        assert state["token_calls"] == 1
        assert state["send_calls"] == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_local_image_success(tmp_path) -> None:
    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    state = {"token_calls": 0, "upload_calls": 0, "send_calls": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            state["token_calls"] += 1
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t_demo", "expire": 7200})
        if request.url.path.endswith("/im/v1/images"):
            state["upload_calls"] += 1
            return httpx.Response(200, json={"code": 0, "data": {"image_key": "img_key_1"}})
        if request.url.path.endswith("/im/v1/messages"):
            state["send_calls"] += 1
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["msg_type"] == "image"
            assert "image_key" in payload["content"]
            return httpx.Response(200, json={"code": 0, "msg": "ok"})
        return httpx.Response(404, json={"code": 404, "msg": "not_found"})

    transport = httpx.MockTransport(handler)
    client = FeishuApiClient(
        app_id="cli_x",
        app_secret="sec_x",
        domain="feishu",
        transport=transport,
    )
    try:
        ok, detail = await client.send_local_file(chat_id="oc_1", file_path=image_path, mime_type="image/png")
        assert ok is True
        assert detail == "ok"
        assert state["token_calls"] == 1
        assert state["upload_calls"] == 1
        assert state["send_calls"] == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_send_text_retries_after_token_error() -> None:
    state = {"token_calls": 0, "send_calls": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            state["token_calls"] += 1
            token = f"t_{state['token_calls']}"
            return httpx.Response(200, json={"code": 0, "tenant_access_token": token, "expire": 7200})
        if request.url.path.endswith("/im/v1/messages"):
            state["send_calls"] += 1
            if state["send_calls"] == 1:
                return httpx.Response(200, json={"code": 99991663, "msg": "tenant_access_token is invalid"})
            return httpx.Response(200, json={"code": 0, "msg": "ok"})
        return httpx.Response(404, json={"code": 404, "msg": "not_found"})

    transport = httpx.MockTransport(handler)
    client = FeishuApiClient(
        app_id="cli_x",
        app_secret="sec_x",
        domain="feishu",
        transport=transport,
    )
    try:
        ok, detail = await client.send_text(chat_id="oc_1", text="hello")
        assert ok is True
        assert detail == "ok_after_token_refresh"
        assert state["token_calls"] == 2
        assert state["send_calls"] == 2
    finally:
        await client.close()
