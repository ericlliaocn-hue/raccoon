"""Layer2: 意图分类（MVP 关键词规则，预留模型接口）

意图分类结果：
- skill_trigger: 触发某个 Skill（但 trigger_word 未精准命中）
- status_query: 查询任务状态
- task_operation: 任务操作（取消等）
- casual_chat: 闲聊 → LLM 兜底
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum

import structlog
from src.types import RouteResult, RouteType

logger = structlog.get_logger(__name__)


class IntentType(str, Enum):
    SKILL_TRIGGER = "skill_trigger"
    STATUS_QUERY = "status_query"
    TASK_OPERATION = "task_operation"
    CASUAL_CHAT = "casual_chat"


# ─── 关键词规则 ────────────────────────────────────────────────

STATUS_QUERY_PATTERNS = [
    "进度", "状态", "怎么样了", "完成了吗", "到哪了",
    "progress", "status", "how is it",
]

TASK_CANCEL_PATTERNS = [
    "取消", "停止", "别做了", "算了", "cancel", "stop", "abort",
]

# intent → RouteType 映射
INTENT_ROUTE_MAP: dict[IntentType, RouteType] = {
    IntentType.STATUS_QUERY: RouteType.TASK_STATUS,
    IntentType.TASK_OPERATION: RouteType.TASK_OPERATION,
    IntentType.SKILL_TRIGGER: RouteType.SKILL,
    IntentType.CASUAL_CHAT: RouteType.LLM,
}


class IntentClassifier(ABC):
    """意图分类器抽象接口"""

    @abstractmethod
    async def classify(self, text: str) -> RouteResult | None:
        """分类意图，返回 RouteResult 或 None（未命中）"""
        ...


class KeywordIntentClassifier(IntentClassifier):
    """MVP 实现：关键词规则匹配"""

    def __init__(self) -> None:
        self._status_patterns = [p.lower() for p in STATUS_QUERY_PATTERNS]
        self._cancel_patterns = [p.lower() for p in TASK_CANCEL_PATTERNS]

    async def classify(self, text: str) -> RouteResult | None:
        text_lower = text.lower()

        # 1. 任务状态查询
        for pattern in self._status_patterns:
            if pattern in text_lower:
                return RouteResult(
                    route_type=RouteType.TASK_STATUS,
                    intent=IntentType.STATUS_QUERY.value,
                    confidence=0.85,
                )

        # 2. 任务操作（取消）
        for pattern in self._cancel_patterns:
            if pattern in text_lower:
                return RouteResult(
                    route_type=RouteType.TASK_OPERATION,
                    intent=IntentType.TASK_OPERATION.value,
                    params={"operation": "cancel"},
                    confidence=0.85,
                )

        # 3. 未命中 → None，交给下一层
        return None


class ModelIntentClassifier(IntentClassifier):
    """预留：基于本地模型的意图分类（未来实现）"""

    async def classify(self, text: str) -> RouteResult | None:
        # TODO: 接入 onnxruntime / sklearn 模型
        return None
