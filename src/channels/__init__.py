"""通道层基础模型与工具。"""

from src.channels.contract import (
    ChannelAdapterManifest,
    ChannelAttachment,
    ChannelCapabilityFlags,
    ChannelInboundEvent,
    ChannelMode,
    ChannelOutboundMessage,
    ChannelTarget,
)
from src.channels.normalizer import ChannelNormalizer
from src.channels.adapters.feishu import FeishuParseResult, parse_feishu_callback, parse_feishu_ws_event
from src.channels.adapters.feishu_api_client import FeishuApiClient
from src.channels.adapters.feishu_ws_client import FeishuWebSocketBridge

__all__ = [
    "ChannelAdapterManifest",
    "ChannelAttachment",
    "ChannelCapabilityFlags",
    "ChannelInboundEvent",
    "ChannelMode",
    "ChannelOutboundMessage",
    "ChannelTarget",
    "ChannelNormalizer",
    "FeishuParseResult",
    "parse_feishu_callback",
    "parse_feishu_ws_event",
    "FeishuApiClient",
    "FeishuWebSocketBridge",
]
