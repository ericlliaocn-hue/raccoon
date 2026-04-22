"""双向通信模块

提供 HTTP webhook 入站端点，接收外部指令，转为 EventBus 事件。
"""

from src.gateway.inbound import GatewayInbound

__all__ = ["GatewayInbound"]
