"""主路由入口：三层依次匹配，返回 RouteResult

路由层级：
1. Layer1: 精准匹配（trigger_word O(1) 哈希）
2. Layer2: 意图分类（关键词规则 → 预留模型接口）
3. Layer3: 系统指令（/ 和 @ 前缀）
4. Layer4: LLM 兜底（MVP 返回 casual_chat）

每层返回 RouteResult 或 None（未命中），逐层穿透。
LLM 只在 Schema 填充阶段出现，不决定路由。

注意：多轮对话（如登录流程）的上下文接力由 Executor 层的 pending_task 机制处理，
不在 Router 层做，因为手机号/验证码等短文本在 Router 层无法可靠识别。
"""

from __future__ import annotations

import structlog

from src.types import RouteResult, RouteType, SkillMetadata
from src.router.trigger_map import TriggerMap
from src.router.intent_classifier import IntentClassifier, KeywordIntentClassifier
from src.router.system_commands import SystemCommands

logger = structlog.get_logger(__name__)


class Router:
    """三层路由器（纯无状态匹配，多轮对话由 Executor 层处理）"""

    def __init__(
        self,
        intent_classifier: IntentClassifier | None = None,
    ) -> None:
        self._trigger_map = TriggerMap()
        self._intent_classifier = intent_classifier or KeywordIntentClassifier()
        self._system_commands = SystemCommands()

    def register_skill(self, skill: SkillMetadata) -> None:
        """注册 Skill 到路由（构建 trigger_map + 别名映射）"""
        self._trigger_map.register(skill)
        self._system_commands.register_aliases(skill.name, skill.aliases)
        logger.info("skill_registered_to_router", skill=skill.name, aliases=skill.aliases)

    def unregister_skill(self, skill: SkillMetadata) -> None:
        """从路由中移除 Skill"""
        self._trigger_map.unregister(skill)
        self._system_commands.unregister_aliases(skill.aliases)
        logger.info("skill_unregistered_from_router", skill=skill.name)

    async def route(self, text: str) -> RouteResult:
        """三层路由：依次匹配，返回第一个命中的结果，否则 LLM 兜底"""
        log = logger.bind(text=text[:50])

        # Layer1: 精准匹配
        result = self._trigger_map.match(text)
        if result:
            log.info("route_matched", layer="trigger_map", skill=result.skill_name)
            return result

        # Layer2: 意图分类
        result = await self._intent_classifier.classify(text)
        if result:
            log.info("route_matched", layer="intent_classifier", intent=result.intent)
            return result

        # Layer3: 系统指令
        result = self._system_commands.parse(text)
        if result:
            log.info("route_matched", layer="system_commands", command=result.params)
            return result

        # Layer4: LLM 兜底
        log.info("route_fallback_llm")
        return RouteResult(
            route_type=RouteType.LLM,
            intent="casual_chat",
            confidence=0.0,
            params={"original_text": text},
        )

    @property
    def trigger_count(self) -> int:
        return self._trigger_map.size
