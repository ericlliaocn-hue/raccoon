"""Feishu OpenAPI text reply client.

用途：
- 按 chat_id 动态回发消息到来源会话；
- 维护 tenant_access_token 缓存，减少重复鉴权开销；
- 失败时返回结构化原因，便于上层决定是否走 webhook 兜底。
"""

from __future__ import annotations

import mimetypes
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class FeishuApiClient:
    """飞书 API 轻量客户端（当前仅实现文本回发）。"""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        domain: str = "feishu",
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = _resolve_feishu_domain(domain)
        self._http = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)

        self._token = ""
        self._token_expire_at = 0.0
        self._token_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    async def send_text(self, *, chat_id: str, text: str) -> tuple[bool, str]:
        """发送文本消息到指定 chat_id。"""
        if not chat_id.strip():
            return False, "missing_chat_id"
        if not text.strip():
            return False, "empty_reply_text"

        token = await self._get_tenant_access_token(force_refresh=False)
        ok, code, msg = await self._send_message_with_token(
            token=token,
            chat_id=chat_id,
            msg_type="text",
            content={"text": text},
        )
        if ok:
            return True, "ok"

        if _should_refresh_token(code=code, msg=msg):
            token = await self._get_tenant_access_token(force_refresh=True)
            ok, code, msg = await self._send_message_with_token(
                token=token,
                chat_id=chat_id,
                msg_type="text",
                content={"text": text},
            )
            if ok:
                return True, "ok_after_token_refresh"

        return False, f"code={code},msg={msg}"

    async def send_local_file(
        self,
        *,
        chat_id: str,
        file_path: str | Path,
        mime_type: str | None = None,
    ) -> tuple[bool, str]:
        """发送本地文件到 chat_id；图片走 image，其余走 file。"""
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            return False, "file_not_found"

        mime = (mime_type or mimetypes.guess_type(str(path))[0] or "").lower()
        is_image = mime.startswith("image/") or path.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
        }

        token = await self._get_tenant_access_token(force_refresh=False)
        if is_image:
            ok, code, msg, key = await self._upload_image_with_token(token=token, file_path=path, mime_type=mime)
            if not ok and _should_refresh_token(code=code, msg=msg):
                token = await self._get_tenant_access_token(force_refresh=True)
                ok, code, msg, key = await self._upload_image_with_token(token=token, file_path=path, mime_type=mime)
            if not ok or not key:
                return False, f"image_upload_failed code={code},msg={msg}"
            ok, code, msg = await self._send_message_with_token(
                token=token,
                chat_id=chat_id,
                msg_type="image",
                content={"image_key": key},
            )
            return (True, "ok") if ok else (False, f"image_send_failed code={code},msg={msg}")

        ok, code, msg, key = await self._upload_file_with_token(token=token, file_path=path, mime_type=mime)
        if not ok and _should_refresh_token(code=code, msg=msg):
            token = await self._get_tenant_access_token(force_refresh=True)
            ok, code, msg, key = await self._upload_file_with_token(token=token, file_path=path, mime_type=mime)
        if not ok or not key:
            return False, f"file_upload_failed code={code},msg={msg}"
        ok, code, msg = await self._send_message_with_token(
            token=token,
            chat_id=chat_id,
            msg_type="file",
            content={"file_key": key},
        )
        return (True, "ok") if ok else (False, f"file_send_failed code={code},msg={msg}")

    async def _get_tenant_access_token(self, *, force_refresh: bool) -> str:
        async with self._token_lock:
            now = time.time()
            if (
                not force_refresh
                and self._token
                and now < self._token_expire_at
            ):
                return self._token

            url = f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal"
            payload = {"app_id": self._app_id, "app_secret": self._app_secret}
            resp = await self._http.post(url, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"feishu_token_http_status={resp.status_code}")
            data = _json_or_empty(resp)
            code = int(data.get("code", -1))
            if code != 0:
                msg = str(data.get("msg") or "unknown")
                raise RuntimeError(f"feishu_token_failed code={code} msg={msg}")

            token = str(data.get("tenant_access_token") or "")
            if not token:
                raise RuntimeError("feishu_token_missing")

            expire = int(data.get("expire", 7200) or 7200)
            # 提前 2 分钟刷新，降低临界点失败概率
            self._token = token
            self._token_expire_at = now + max(60, expire - 120)
            return self._token

    async def _send_message_with_token(
        self,
        *,
        token: str,
        chat_id: str,
        msg_type: str,
        content: dict[str, Any],
    ) -> tuple[bool, int, str]:
        url = f"{self._base_url}/open-apis/im/v1/messages"
        params = {"receive_id_type": "chat_id"}
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "receive_id": chat_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        resp = await self._http.post(url, params=params, headers=headers, json=payload)
        if resp.status_code != 200:
            return False, resp.status_code, f"http_{resp.status_code}"
        data = _json_or_empty(resp)
        code = int(data.get("code", -1))
        msg = str(data.get("msg") or "")
        if code == 0:
            return True, code, "ok"
        return False, code, msg or "unknown"

    async def _upload_image_with_token(
        self,
        *,
        token: str,
        file_path: Path,
        mime_type: str,
    ) -> tuple[bool, int, str, str]:
        url = f"{self._base_url}/open-apis/im/v1/images"
        headers = {"Authorization": f"Bearer {token}"}
        with file_path.open("rb") as f:
            files = {"image": (file_path.name, f, mime_type or "application/octet-stream")}
            data = {"image_type": "message"}
            resp = await self._http.post(url, headers=headers, data=data, files=files)
        if resp.status_code != 200:
            return False, resp.status_code, f"http_{resp.status_code}", ""
        body = _json_or_empty(resp)
        code = int(body.get("code", -1))
        msg = str(body.get("msg") or "")
        image_key = str((body.get("data") or {}).get("image_key") or "")
        if code == 0 and image_key:
            return True, code, "ok", image_key
        return False, code, msg or "unknown", image_key

    async def _upload_file_with_token(
        self,
        *,
        token: str,
        file_path: Path,
        mime_type: str,
    ) -> tuple[bool, int, str, str]:
        url = f"{self._base_url}/open-apis/im/v1/files"
        headers = {"Authorization": f"Bearer {token}"}
        with file_path.open("rb") as f:
            files = {"file": (file_path.name, f, mime_type or "application/octet-stream")}
            data = {"file_type": "stream", "file_name": file_path.name}
            resp = await self._http.post(url, headers=headers, data=data, files=files)
        if resp.status_code != 200:
            return False, resp.status_code, f"http_{resp.status_code}", ""
        body = _json_or_empty(resp)
        code = int(body.get("code", -1))
        msg = str(body.get("msg") or "")
        file_key = str((body.get("data") or {}).get("file_key") or "")
        if code == 0 and file_key:
            return True, code, "ok", file_key
        return False, code, msg or "unknown", file_key


def _json_or_empty(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _resolve_feishu_domain(domain: str) -> str:
    value = str(domain or "").strip()
    if not value or value == "feishu":
        return "https://open.feishu.cn"
    if value == "lark":
        return "https://open.larksuite.com"
    if value.startswith("http://") or value.startswith("https://"):
        return value.rstrip("/")
    return f"https://{value.rstrip('/')}"


def _should_refresh_token(*, code: int, msg: str) -> bool:
    # 常见 token 过期/无效错误码（飞书官方错误码在不同端有细微差异，故加消息兜底）
    if code in {99991663, 99991664, 99991661, 99991668}:
        return True
    m = (msg or "").lower()
    return "tenant_access_token" in m or "token" in m
