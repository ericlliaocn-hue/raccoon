"""Layer1: trigger_word 哈希表，O(1) 查找

Skill 注册时构建 trigger_map，运行时只读。
trigger_word → skill_name 映射。
"""

from __future__ import annotations

import structlog
from src.types import RouteResult, RouteType, SkillMetadata

logger = structlog.get_logger(__name__)


class TriggerMap:
    """精准匹配层：trigger_word → skill_name 的 O(1) 哈希表"""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}  # trigger_word → skill_name
        self._alias_map: dict[str, str] = {}  # alias → skill_name

    def register(self, skill: SkillMetadata) -> None:
        """注册 Skill 的 trigger_words 和 aliases"""
        for word in skill.trigger_words:
            word_lower = word.lower()
            if word_lower in self._map:
                existing = self._map[word_lower]
                logger.warning(
                    "trigger_word_conflict",
                    word=word_lower,
                    existing_skill=existing,
                    new_skill=skill.name,
                )
            self._map[word_lower] = skill.name
        # 别名也注册到子串匹配中
        for alias in skill.aliases:
            alias_lower = alias.lower()
            if alias_lower in self._alias_map:
                existing = self._alias_map[alias_lower]
                logger.warning(
                    "alias_conflict",
                    alias=alias_lower,
                    existing_skill=existing,
                    new_skill=skill.name,
                )
            self._alias_map[alias_lower] = skill.name
        logger.debug(
            "trigger_map_updated",
            skill=skill.name,
            triggers=skill.trigger_words,
            aliases=skill.aliases,
            total=len(self._map) + len(self._alias_map),
        )

    def unregister(self, skill: SkillMetadata) -> None:
        """移除 Skill 的 trigger_words 和 aliases"""
        for word in skill.trigger_words:
            word_lower = word.lower()
            if self._map.get(word_lower) == skill.name:
                del self._map[word_lower]
        for alias in skill.aliases:
            alias_lower = alias.lower()
            if self._alias_map.get(alias_lower) == skill.name:
                del self._alias_map[alias_lower]

    def match(self, text: str) -> RouteResult | None:
        """多触发词加权匹配：统计每个 skill 命中的触发词/别名总长度，选得分最高的

        匹配策略：
        1. 先尝试整句精准匹配（confidence=1.0）
        2. 子串包含匹配：遍历所有 trigger_word 和 alias，按 skill 聚合得分
           得分 = 该 skill 所有命中词的长度之和
           同一 skill 的多个词命中时，只计算不重叠的部分
        3. 选得分最高的 skill；同分时选最长单词对应的 skill
        """
        text_lower = text.lower()

        # 合并 trigger_words 和 aliases 为统一查找表
        # alias 得分略低（乘 0.8），因为别名不如触发词精确
        combined: dict[str, tuple[str, float]] = {}  # word → (skill_name, weight)
        for word, skill in self._map.items():
            combined[word] = (skill, 1.0)
        for alias, skill in self._alias_map.items():
            if alias not in self._map:  # trigger_word 优先
                combined[alias] = (skill, 0.8)

        # 先尝试整句匹配
        if text_lower.strip() in self._map:
            skill_name = self._map[text_lower.strip()]
            return RouteResult(
                route_type=RouteType.SKILL,
                skill_name=skill_name,
                confidence=1.0,
            )
        # 整句匹配别名
        if text_lower.strip() in self._alias_map:
            skill_name = self._alias_map[text_lower.strip()]
            return RouteResult(
                route_type=RouteType.SKILL,
                skill_name=skill_name,
                confidence=0.95,
            )

        # 多词加权匹配：收集所有命中，按 skill 聚合得分
        skill_scores: dict[str, float] = {}  # skill_name → 总得分
        skill_best_trigger: dict[str, tuple[str, int]] = {}  # skill_name → (最长词, 位置)

        for word, (skill, weight) in combined.items():
            idx = text_lower.find(word)
            if idx >= 0:
                score = len(word) * weight
                skill_scores[skill] = skill_scores.get(skill, 0.0) + score
                if skill not in skill_best_trigger or len(word) > len(skill_best_trigger[skill][0]):
                    skill_best_trigger[skill] = (word, idx)

        if not skill_scores:
            return None

        # 选得分最高的 skill；同分时选最长词的 skill
        best_skill = max(
            skill_scores.keys(),
            key=lambda s: (skill_scores[s], len(skill_best_trigger[s][0])),
        )

        # 提取参数：用该 skill 最长词的位置来切分 rest
        best_trigger, best_idx = skill_best_trigger[best_skill]
        rest = text_lower[:best_idx].strip()
        after = text_lower[best_idx + len(best_trigger):].strip()
        params = {}
        if rest:
            params["rest"] = rest
        elif after:
            params["rest"] = after

        # confidence 基于得分：命中越多/越长，越可信，但上限 0.95
        confidence = min(0.95, 0.7 + skill_scores[best_skill] * 0.02)

        return RouteResult(
            route_type=RouteType.SKILL,
            skill_name=best_skill,
            confidence=confidence,
            params=params,
        )

    @property
    def size(self) -> int:
        return len(self._map) + len(self._alias_map)

    def all_triggers(self) -> dict[str, str]:
        """返回所有 trigger → skill 映射（调试用）"""
        result = dict(self._map)
        result.update(self._alias_map)
        return result
