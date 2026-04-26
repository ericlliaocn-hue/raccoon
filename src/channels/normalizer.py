"""通道输入归一化。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from src.channels.contract import ChannelAttachment, ChannelInboundEvent

_PART_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9._:-]+")


class ChannelNormalizer:
    """将不同通道输入统一转换为 ChannelInboundEvent。"""

    @staticmethod
    def normalize(
        *,
        channel: str,
        account_id: str = "default",
        chat_id: str,
        text: str = "",
        sender_id: str = "anonymous",
        sender_display: str = "",
        thread_id: str | None = None,
        message_id: str | None = None,
        mentions: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
        trace_id: str | None = None,
        timestamp: datetime | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> ChannelInboundEvent:
        norm_channel = ChannelNormalizer._safe_part(channel, fallback="unknown")
        norm_account = ChannelNormalizer._safe_part(account_id, fallback="default")
        norm_chat = ChannelNormalizer._safe_part(chat_id, fallback="chat")
        norm_thread = ChannelNormalizer._safe_part(thread_id or "root", fallback="root")

        conv_id = conversation_id or f"{norm_channel}:{norm_account}:{norm_chat}:{norm_thread}"
        files = [ChannelAttachment(**item) for item in (attachments or [])]

        return ChannelInboundEvent(
            channel=norm_channel,
            account_id=norm_account,
            chat_id=norm_chat,
            thread_id=thread_id,
            message_id=message_id,
            sender_id=sender_id,
            sender_display=sender_display,
            conversation_id=conv_id,
            text=(text or "").strip(),
            mentions=list(mentions or []),
            attachments=files,
            timestamp=timestamp or datetime.now(timezone.utc),
            trace_id=trace_id or str(uuid.uuid4()),
            raw_payload=raw_payload or {},
        )

    @staticmethod
    def from_gateway_payload(
        *,
        source: str,
        text: str,
        conversation_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChannelInboundEvent:
        payload = extra or {}
        return ChannelNormalizer.normalize(
            channel=source,
            account_id=str(payload.get("account_id", "default")),
            chat_id=str(payload.get("chat_id", payload.get("conversation_id", "chat"))),
            thread_id=str(payload.get("thread_id") or "") or None,
            message_id=str(payload.get("message_id") or "") or None,
            sender_id=str(payload.get("sender_id", "gateway_user")),
            sender_display=str(payload.get("sender_display", "")),
            text=text,
            mentions=list(payload.get("mentions") or []),
            attachments=list(payload.get("attachments") or []),
            conversation_id=conversation_id,
            trace_id=str(payload.get("trace_id") or "") or None,
            raw_payload=payload,
        )

    @staticmethod
    def _safe_part(value: str | None, *, fallback: str) -> str:
        part = (value or "").strip()
        if not part:
            return fallback
        part = _PART_SANITIZE_RE.sub("_", part)
        return part[:128] if part else fallback

