"""Feishu inbound callback adapter.

目标：
1. 统一处理飞书 URL 验证与消息回调；
2. 将飞书事件归一化为 ChannelInboundEvent；
3. 为 HTTP 层提供轻量的解析结果，减少重复逻辑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.channels.normalizer import ChannelNormalizer
from src.channels.contract import ChannelAttachment, ChannelInboundEvent


@dataclass(slots=True)
class FeishuParseResult:
    """飞书回调解析结果。"""

    ok: bool
    status_code: int = 200
    detail: str = ""
    challenge: str | None = None
    channel_event: ChannelInboundEvent | None = None
    message_type: str = ""
    reply_hint: str = ""

    @property
    def is_url_verification(self) -> bool:
        return bool(self.challenge)


def parse_feishu_callback(
    payload: dict[str, Any],
    *,
    verification_token: str = "",
    mention_required_in_group: bool = True,
    allow_chat_ids: list[str] | None = None,
    allow_user_ids: list[str] | None = None,
) -> FeishuParseResult:
    """解析飞书回调并做基础校验。"""
    if not isinstance(payload, dict) or not payload:
        return FeishuParseResult(ok=False, status_code=400, detail="invalid_payload")

    # URL 验证
    if payload.get("type") == "url_verification":
        token = str(payload.get("token") or "")
        if verification_token and token != verification_token:
            return FeishuParseResult(ok=False, status_code=401, detail="invalid_verification_token")
        challenge = str(payload.get("challenge") or "")
        if not challenge:
            return FeishuParseResult(ok=False, status_code=400, detail="missing_challenge")
        return FeishuParseResult(ok=True, challenge=challenge, detail="url_verification_ok")

    # 加密回调（当前阶段不给静默失败，明确提示）
    if payload.get("encrypt"):
        return FeishuParseResult(
            ok=False,
            status_code=400,
            detail="encrypted_callback_not_supported_yet",
        )

    header = payload.get("header") or {}
    event = payload.get("event") or {}
    event_type = str(header.get("event_type") or "")
    if event_type != "im.message.receive_v1":
        return FeishuParseResult(ok=True, detail=f"ignored_event_type:{event_type or 'unknown'}")

    token = str(header.get("token") or "")
    if verification_token and token != verification_token:
        return FeishuParseResult(ok=False, status_code=401, detail="invalid_event_token")

    message = event.get("message") or {}
    sender = event.get("sender") or {}
    sender_id_obj = sender.get("sender_id") or {}
    sender_id = str(
        sender_id_obj.get("open_id")
        or sender_id_obj.get("union_id")
        or sender_id_obj.get("user_id")
        or "anonymous"
    )

    chat_id = str(message.get("chat_id") or "")
    if not chat_id:
        return FeishuParseResult(ok=False, status_code=400, detail="missing_chat_id")

    if allow_chat_ids and chat_id not in set(allow_chat_ids):
        return FeishuParseResult(ok=True, detail="ignored_chat_not_allowlisted")
    if allow_user_ids and sender_id not in set(allow_user_ids):
        return FeishuParseResult(ok=True, detail="ignored_user_not_allowlisted")

    chat_type = str(message.get("chat_type") or "")
    mentions_raw = event.get("mentions") or []
    mentions = _extract_mentions(mentions_raw)

    if mention_required_in_group and chat_type != "p2p" and not mentions:
        return FeishuParseResult(ok=True, detail="ignored_group_without_mention")

    message_id = str(message.get("message_id") or "")
    thread_id = str(message.get("root_id") or message.get("parent_id") or "")
    message_type = str(message.get("message_type") or "")
    content_text, attachments = _parse_message_content(message_type, message.get("content"))

    channel_event = ChannelNormalizer.normalize(
        channel="feishu",
        account_id=str(header.get("app_id") or "default"),
        chat_id=chat_id,
        thread_id=thread_id or None,
        message_id=message_id or None,
        sender_id=sender_id,
        sender_display=str(sender.get("sender_type") or ""),
        text=content_text,
        mentions=mentions,
        attachments=[item.model_dump(mode="json") for item in attachments],
        raw_payload=payload,
    )

    hint = ""
    if message_type != "text":
        hint = f"收到 {message_type} 类型消息，已按文本提示进入路由。"

    return FeishuParseResult(
        ok=True,
        detail="message_event_ok",
        channel_event=channel_event,
        message_type=message_type,
        reply_hint=hint,
    )


def parse_feishu_ws_event(
    event: Any,
    *,
    verification_token: str = "",
    mention_required_in_group: bool = True,
    allow_chat_ids: list[str] | None = None,
    allow_user_ids: list[str] | None = None,
) -> FeishuParseResult:
    """将 Feishu WS SDK 事件对象转换为回调载荷后复用同一套解析逻辑。"""
    payload = _ws_event_to_callback_payload(event)
    if not payload:
        return FeishuParseResult(ok=False, status_code=400, detail="invalid_ws_event")
    return parse_feishu_callback(
        payload,
        verification_token=verification_token,
        mention_required_in_group=mention_required_in_group,
        allow_chat_ids=allow_chat_ids,
        allow_user_ids=allow_user_ids,
    )


def _extract_mentions(mentions_raw: list[Any]) -> list[str]:
    result: list[str] = []
    for item in mentions_raw:
        if not isinstance(item, dict):
            continue
        mention_id = (
            item.get("id", {}).get("open_id")
            or item.get("id", {}).get("user_id")
            or item.get("id", {}).get("union_id")
            or item.get("name")
        )
        if mention_id:
            result.append(str(mention_id))
    return result


def _parse_message_content(message_type: str, content: Any) -> tuple[str, list[ChannelAttachment]]:
    content_obj: dict[str, Any] = {}
    if isinstance(content, str):
        try:
            content_obj = json.loads(content)
        except json.JSONDecodeError:
            content_obj = {}
    elif isinstance(content, dict):
        content_obj = content

    if message_type == "text":
        text = str(content_obj.get("text") or "").strip()
        return text, []

    attachments: list[ChannelAttachment] = []
    text_hint = f"[feishu:{message_type}]"

    if message_type == "image":
        image_key = str(content_obj.get("image_key") or "")
        attachments.append(
            ChannelAttachment(
                name="image",
                mime_type="image/*",
                metadata={"image_key": image_key} if image_key else {},
            )
        )
    elif message_type == "file":
        file_key = str(content_obj.get("file_key") or "")
        file_name = str(content_obj.get("file_name") or "file")
        attachments.append(
            ChannelAttachment(
                name=file_name,
                mime_type="application/octet-stream",
                metadata={"file_key": file_key} if file_key else {},
            )
        )
    elif message_type in {"audio", "video", "media"}:
        attachments.append(
            ChannelAttachment(
                name=message_type,
                mime_type=f"{message_type}/*",
                metadata=content_obj,
            )
        )
    else:
        # 未识别类型依旧保留一条 metadata，避免静默丢失。
        if content_obj:
            attachments.append(
                ChannelAttachment(
                    name=message_type or "unknown",
                    mime_type="application/octet-stream",
                    metadata=content_obj,
                )
            )

    return text_hint, attachments


def _ws_event_to_callback_payload(event: Any) -> dict[str, Any] | None:
    header = getattr(event, "header", None)
    body = getattr(event, "event", None)
    if not body:
        return None

    sender = getattr(body, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    message = getattr(body, "message", None)
    if not message:
        return None

    mentions_raw = []
    for item in list(getattr(message, "mentions", None) or []):
        mention_id = getattr(item, "id", None)
        mentions_raw.append(
            {
                "id": {
                    "open_id": getattr(mention_id, "open_id", None),
                    "user_id": getattr(mention_id, "user_id", None),
                    "union_id": getattr(mention_id, "union_id", None),
                },
                "name": getattr(item, "name", None),
            }
        )

    return {
        "header": {
            "event_type": getattr(header, "event_type", "im.message.receive_v1"),
            "token": getattr(header, "token", ""),
            "app_id": getattr(header, "app_id", ""),
        },
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": getattr(sender_id, "open_id", None),
                    "user_id": getattr(sender_id, "user_id", None),
                    "union_id": getattr(sender_id, "union_id", None),
                },
                "sender_type": getattr(sender, "sender_type", ""),
            },
            "message": {
                "message_id": getattr(message, "message_id", None),
                "root_id": getattr(message, "root_id", None),
                "parent_id": getattr(message, "parent_id", None),
                "chat_id": getattr(message, "chat_id", None),
                "chat_type": getattr(message, "chat_type", None),
                "message_type": getattr(message, "message_type", None),
                "content": getattr(message, "content", None),
            },
            "mentions": mentions_raw,
        },
    }
