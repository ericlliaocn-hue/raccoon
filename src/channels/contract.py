"""Channel Contract v1.

定义多通道接入的统一消息信封与能力声明。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ChannelMode(str, Enum):
    WEBHOOK = "webhook"
    WEBSOCKET = "websocket"
    POLLING = "polling"


class DeliveryMode(str, Enum):
    IMMEDIATE = "immediate"
    DEFERRED = "deferred"


class ChannelAttachment(BaseModel):
    attachment_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    mime_type: str = "application/octet-stream"
    size_bytes: int | None = None
    url: str | None = None
    local_path: str | None = None
    sha256: str | None = None
    preview_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChannelInboundEvent(BaseModel):
    channel: str
    account_id: str = "default"
    chat_id: str
    thread_id: str | None = None
    message_id: str | None = None
    sender_id: str = "anonymous"
    sender_display: str = ""
    conversation_id: str
    text: str = ""
    mentions: list[str] = Field(default_factory=list)
    attachments: list[ChannelAttachment] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class ChannelTarget(BaseModel):
    channel: str
    account_id: str = "default"
    chat_id: str
    thread_id: str | None = None
    reply_to_message_id: str | None = None


class ChannelOutboundMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    target: ChannelTarget
    text: str = ""
    blocks: dict[str, Any] | None = None
    card: dict[str, Any] | None = None
    attachments: list[ChannelAttachment] = Field(default_factory=list)
    delivery_mode: DeliveryMode = DeliveryMode.IMMEDIATE
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChannelCapabilityFlags(BaseModel):
    inbound_text: bool = True
    outbound_text: bool = True
    inbound_file: bool = False
    outbound_file: bool = False
    reply_thread: bool = False
    edit_message: bool = False
    typing: bool = False
    mention_gate: bool = False
    group_policy: bool = False
    allowlist: bool = False
    signature_verify: bool = False
    rate_limit_hint: bool = False


class ChannelAdapterManifest(BaseModel):
    adapter_name: str
    channel: str
    version: str = "v1"
    modes: list[ChannelMode] = Field(default_factory=lambda: [ChannelMode.WEBHOOK])
    capabilities: ChannelCapabilityFlags = Field(default_factory=ChannelCapabilityFlags)
    metadata: dict[str, Any] = Field(default_factory=dict)

