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

__all__ = [
    "ChannelAdapterManifest",
    "ChannelAttachment",
    "ChannelCapabilityFlags",
    "ChannelInboundEvent",
    "ChannelMode",
    "ChannelOutboundMessage",
    "ChannelTarget",
    "ChannelNormalizer",
]

