"""Layer2: 意图分类（关键词规则 + LLM 模型 + 在线学习）

意图分类结果：
- skill_trigger: 触发某个 Skill（但 trigger_word 未精准命中）
- status_query: 查询任务状态
- task_operation: 任务操作（取消等）
- casual_chat: 闲聊 → LLM 兜底

v0.3.4 增强：
- ModelIntentClassifier: 使用 LLM 轻量分类替代纯关键词匹配
- OnlineLearning: 用户纠正 → 更新分类模型（关键词库动态扩展）
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

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
    """v0.3.4: LLM 轻量模型意图分类

    使用 LLM 单次调用进行意图分类，比关键词匹配更灵活。
    分类结果与 KeywordIntentClassifier 格式一致，可无缝替换。
    """

    _CLASSIFY_PROMPT = """你是意图分类器。根据用户消息判断意图类型。

可选意图：
- skill_trigger: 用户想执行某个 Skill（如搜索、生成图片、查看热搜）
- status_query: 用户查询任务进度/状态
- task_operation: 用户想取消/停止任务
- casual_chat: 闲聊、创作、翻译、总结等纯文本需求

用户消息: {text}

请返回 JSON：
{{"intent": "意图类型", "skill_name": "如果skill_trigger则填Skill名，否则null", "confidence": 0.0-1.0, "params": {{}}}}

只返回 JSON，不要其他内容。"""

    def __init__(self, config=None) -> None:
        self._config = config
        self._keyword_classifier = KeywordIntentClassifier()
        self._llm = None
        self._online_learning = OnlineLearningStore()

    def _get_llm(self):
        """延迟初始化 LLM"""
        if self._llm is None:
            from src.config import RaccoonConfig
            from src.llm import LLMFactory
            config = self._config or RaccoonConfig()
            if config.llm_provider == "mock":
                return None
            self._llm = LLMFactory.create(config)
        return self._llm

    async def classify(self, text: str) -> RouteResult | None:
        """分类流程：关键词优先 → LLM 分类 → 在线学习修正"""

        # 1. 关键词优先（高置信度，快速）
        keyword_result = await self._keyword_classifier.classify(text)
        if keyword_result and keyword_result.confidence >= 0.85:
            return keyword_result

        # 2. 在线学习修正（用户纠正过的模式）
        learned_result = self._online_learning.lookup(text)
        if learned_result:
            return learned_result

        # 3. LLM 分类（轻量单次调用）
        llm = self._get_llm()
        if llm is None:
            # Mock 模式退回关键词
            return keyword_result

        try:
            prompt = self._CLASSIFY_PROMPT.format(text=text)
            messages = [{"role": "user", "content": prompt}]
            reply = await llm.chat(messages, temperature=0.1, max_tokens=256)
            return self._parse_llm_result(reply, text)
        except Exception as e:
            logger.warning("model_intent_classify_failed", error=str(e))
            return keyword_result

    def _parse_llm_result(self, reply: str, original_text: str) -> RouteResult | None:
        """解析 LLM 分类结果"""
        try:
            json_str = reply.strip()
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0]
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0]

            data = json.loads(json_str.strip())
            intent = data.get("intent", "casual_chat")
            skill_name = data.get("skill_name")
            confidence = float(data.get("confidence", 0.7))
            params = data.get("params", {})

            # intent → RouteType 映射
            intent_type = IntentType(intent)
            route_type = INTENT_ROUTE_MAP.get(intent_type, RouteType.LLM)

            return RouteResult(
                route_type=route_type,
                skill_name=skill_name,
                intent=intent,
                params=params,
                confidence=confidence,
            )

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning("model_intent_parse_failed", reply=reply[:100], error=str(e))
            return None

    # ─── 在线学习接口 ─────────────────────────────────────────

    def record_correction(self, text: str, correct_intent: str, correct_route_type: RouteType) -> None:
        """记录用户纠正（#10: 在线学习）"""
        self._online_learning.record(text, correct_intent, correct_route_type)
        logger.info("intent_correction_recorded", text=text[:30], correct_intent=correct_intent)

    def get_learning_stats(self) -> dict[str, Any]:
        """获取在线学习统计"""
        return self._online_learning.stats()


# ─── 在线学习存储（#10）─────────────────────────────────────────

class OnlineLearningStore:
    """在线学习：用户纠正 → 动态关键词库扩展

    当用户纠正分类结果时，将纠正记录存入本地。
    后续相同/相似文本直接使用纠正后的分类。
    """

    def __init__(self) -> None:
        self._corrections: list[dict[str, Any]] = []
        self._max_corrections = 100  # 最多保留 100 条纠正

    def record(self, text: str, correct_intent: str, correct_route_type: RouteType) -> None:
        """记录一条纠正"""
        # 提取关键词（简单：取文本中的核心词）
        keywords = self._extract_keywords(text)

        correction = {
            "text": text.lower(),
            "keywords": keywords,
            "intent": correct_intent,
            "route_type": correct_route_type.value,
            "timestamp": __import__("time").time(),
        }

        # 去重：相同关键词的旧纠正被覆盖
        self._corrections = [
            c for c in self._corrections
            if not (set(c["keywords"]) & set(keywords) and c["intent"] == correct_intent)
        ]
        self._corrections.append(correction)

        # 限制数量
        if len(self._corrections) > self._max_corrections:
            self._corrections = self._corrections[-self._max_corrections:]

    def lookup(self, text: str) -> RouteResult | None:
        """查找是否有纠正记录匹配"""
        text_lower = text.lower()
        keywords = self._extract_keywords(text)

        best_match = None
        best_score = 0.0

        for correction in self._corrections:
            # 关键词重叠度评分
            overlap = len(set(keywords) & set(correction["keywords"]))
            total = max(len(set(keywords) | set(correction["keywords"])), 1)
            score = overlap / total

            # 文本相似度（简单子串匹配）
            if correction["text"] in text_lower or text_lower in correction["text"]:
                score += 0.3

            if score > best_score and score >= 0.5:
                best_score = score
                best_match = correction

        if best_match:
            return RouteResult(
                route_type=RouteType(best_match["route_type"]),
                intent=best_match["intent"],
                confidence=min(best_score, 0.9),
            )
        return None

    def stats(self) -> dict[str, Any]:
        """获取学习统计"""
        intent_counts: dict[str, int] = {}
        for c in self._corrections:
            intent_counts[c["intent"]] = intent_counts.get(c["intent"], 0) + 1
        return {
            "total_corrections": len(self._corrections),
            "intent_distribution": intent_counts,
        }

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """提取关键词（简单实现：去掉停用词后分词）"""
        # 中文停用词
        stopwords = {"的", "了", "吗", "呢", "啊", "吧", "是", "在", "有", "我", "你", "他", "她", "它"}
        # 简单分词：按空格和标点分割
        words = []
        for w in text.lower().split():
            # 去掉标点
            clean = "".join(c for c in w if c.isalnum() or "\u4e00" <= c <= "\u9fff")
            if clean and clean not in stopwords and len(clean) > 1:
                words.append(clean)
        # 如果没有有效分词（纯中文无空格），取整个文本作为关键词
        if not words and len(text) > 2:
            words = [text.lower()]
        return words