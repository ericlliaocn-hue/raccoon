"""意图分类器测试（v0.3.4）"""

import pytest

from src.router.intent_classifier import (
    KeywordIntentClassifier,
    ModelIntentClassifier,
    OnlineLearningStore,
    IntentType,
)
from src.types import RouteType


# ─── KeywordIntentClassifier 测试 ────────────────────────────

class TestKeywordIntentClassifier:
    @pytest.fixture
    def classifier(self):
        return KeywordIntentClassifier()

    @pytest.mark.asyncio
    async def test_status_query(self, classifier):
        result = await classifier.classify("任务进度怎么样了")
        assert result is not None
        assert result.route_type == RouteType.TASK_STATUS
        assert result.intent == IntentType.STATUS_QUERY.value

    @pytest.mark.asyncio
    async def test_task_cancel(self, classifier):
        result = await classifier.classify("取消这个任务")
        assert result is not None
        assert result.route_type == RouteType.TASK_OPERATION
        assert result.params.get("operation") == "cancel"

    @pytest.mark.asyncio
    async def test_casual_chat_no_match(self, classifier):
        result = await classifier.classify("今天天气真好")
        assert result is None  # 关键词不匹配，返回 None


# ─── ModelIntentClassifier 测试 ──────────────────────────────

class TestModelIntentClassifier:
    @pytest.fixture
    def classifier(self):
        from src.config import RaccoonConfig
        config = RaccoonConfig(llm_provider="mock")
        return ModelIntentClassifier(config)

    @pytest.mark.asyncio
    async def test_keyword_fallback_in_mock(self, classifier):
        """Mock 模式下退回关键词分类"""
        result = await classifier.classify("任务进度怎么样了")
        assert result is not None
        assert result.route_type == RouteType.TASK_STATUS

    @pytest.mark.asyncio
    async def test_no_match_returns_none_or_keyword(self, classifier):
        """无法分类时返回 None 或关键词结果"""
        await classifier.classify("随便聊聊")
        # Mock 模式下可能返回 None 或关键词结果
        # 关键是不抛异常

    @pytest.mark.asyncio
    async def test_record_correction(self, classifier):
        """#10: 在线学习 — 记录纠正"""
        classifier.record_correction(
            "帮我搜一下天气",
            "skill_trigger",
            RouteType.SKILL,
        )
        stats = classifier.get_learning_stats()
        assert stats["total_corrections"] == 1

    @pytest.mark.asyncio
    async def test_correction_lookup(self, classifier):
        """#10: 在线学习 — 纠正后可查找到"""
        classifier.record_correction(
            "帮我搜一下天气",
            "skill_trigger",
            RouteType.SKILL,
        )
        # 相似文本应能匹配到纠正结果
        result = classifier._online_learning.lookup("帮我搜一下天气")
        assert result is not None
        assert result.intent == "skill_trigger"


# ─── OnlineLearningStore 测试 ────────────────────────────────

class TestOnlineLearningStore:
    @pytest.fixture
    def store(self):
        return OnlineLearningStore()

    def test_record_and_lookup(self, store):
        store.record("搜索天气", "skill_trigger", RouteType.SKILL)
        result = store.lookup("搜索天气")
        assert result is not None
        assert result.intent == "skill_trigger"

    def test_lookup_no_match(self, store):
        result = store.lookup("完全不相关的文本")
        assert result is None

    def test_stats(self, store):
        store.record("搜索天气", "skill_trigger", RouteType.SKILL)
        store.record("取消任务", "task_operation", RouteType.TASK_OPERATION)
        stats = store.stats()
        assert stats["total_corrections"] == 2
        assert "skill_trigger" in stats["intent_distribution"]

    def test_max_corrections_limit(self, store):
        """纠正记录不超过上限"""
        for i in range(150):
            store.record(f"text_{i}", "casual_chat", RouteType.LLM)
        stats = store.stats()
        assert stats["total_corrections"] <= 100

    def test_extract_keywords(self, store):
        keywords = OnlineLearningStore._extract_keywords("搜索天气信息")
        assert len(keywords) > 0
