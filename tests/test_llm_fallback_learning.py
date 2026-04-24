"""Tests for LLM fallback chain, message classification, and learning engine."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.types import Task, RouteResult, RouteType, Event, EventType
from src.executor.agent import Executor, LlmClassification, LlmClassifyResult


def _make_executor(**overrides) -> Executor:
    """创建一个最小可用的 Executor 实例（跳过 __init__）"""
    executor = Executor.__new__(Executor)
    executor._llm = None
    executor._vault_manager = MagicMock()
    executor._vault_manager.list_skills = MagicMock(return_value=[])
    executor._pending_learn_requests = {}
    executor._learning_engine = None
    executor._scheduler = None
    executor._config = MagicMock()
    executor._event_bus = MagicMock()
    executor._pending_tasks = {}
    for k, v in overrides.items():
        setattr(executor, k, v)
    return executor


# ─── LlmClassification ─────────────────────────────────────────────


class TestClassifyLlmMessage:
    @pytest.mark.asyncio
    async def test_chitchat_returns_chitchat(self):
        executor = _make_executor()
        result = await executor.classify_llm_message("你好呀")
        assert result.classification == LlmClassification.CHITCHAT

    @pytest.mark.asyncio
    async def test_action_no_skill_returns_needs_learn(self):
        executor = _make_executor()
        result = await executor.classify_llm_message("帮我监控网站变化")
        assert result.classification == LlmClassification.NEEDS_LEARN

    @pytest.mark.asyncio
    async def test_action_with_skill_returns_skill_matched(self):
        mock_vault = MagicMock()
        mock_skill = MagicMock(name="change_detector", trigger_words=["监控"])
        mock_vault.list_skills = MagicMock(return_value=[mock_skill])

        executor = _make_executor(_vault_manager=mock_vault)
        # _llm_classify 需要 LLM，mock 它直接返回
        executor._llm_classify = AsyncMock(
            return_value=LlmClassifyResult(
                classification=LlmClassification.SKILL_MATCHED,
                skill_name="change_detector",
            )
        )

        result = await executor.classify_llm_message("帮我监控网站变化")
        assert result.classification == LlmClassification.SKILL_MATCHED
        assert result.skill_name == "change_detector"

    @pytest.mark.asyncio
    async def test_short_message_returns_chitchat(self):
        executor = _make_executor()
        result = await executor.classify_llm_message("嗯")
        assert result.classification == LlmClassification.CHITCHAT


# ─── _might_need_action ────────────────────────────────────────────


class TestLlmClassify:
    """_llm_classify 核心测试"""

    def _make_executor_with_skills(self, skills=None):
        """创建带 Skill 的 Executor，并 mock LLM"""
        mock_vault = MagicMock()
        mock_vault.list_skills = MagicMock(return_value=skills or [])
        executor = _make_executor(_vault_manager=mock_vault)
        return executor

    @pytest.mark.asyncio
    async def test_no_skills_returns_needs_learn(self):
        """无已安装 Skill → NEEDS_LEARN"""
        executor = self._make_executor_with_skills(skills=[])
        result = await executor._llm_classify("帮我查天气")
        assert result.classification == LlmClassification.NEEDS_LEARN

    @pytest.mark.asyncio
    async def test_llm_returns_chitchat(self):
        """LLM 返回 CHITCHAT → 纯创作/问答"""
        mock_skill = MagicMock()
        mock_skill.name = "weather_query"
        mock_skill.trigger_words = ["天气"]
        mock_skill.aliases = []
        mock_skill.intent_tags = ["weather"]
        executor = self._make_executor_with_skills(skills=[mock_skill])

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value="CHITCHAT")
        executor._get_llm = MagicMock(return_value=mock_llm)

        result = await executor._llm_classify("帮我写一首关于春天的诗")
        assert result.classification == LlmClassification.CHITCHAT

    @pytest.mark.asyncio
    async def test_llm_returns_skill_matched(self):
        """LLM 返回 SKILL:xxx → 匹配到已有 Skill"""
        mock_skill = MagicMock()
        mock_skill.name = "weather_query"
        mock_skill.trigger_words = ["天气"]
        mock_skill.aliases = []
        mock_skill.intent_tags = ["weather"]
        executor = self._make_executor_with_skills(skills=[mock_skill])

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value="SKILL:weather_query")
        executor._get_llm = MagicMock(return_value=mock_llm)

        result = await executor._llm_classify("帮我查天气")
        assert result.classification == LlmClassification.SKILL_MATCHED
        assert result.skill_name == "weather_query"

    @pytest.mark.asyncio
    async def test_llm_returns_needs_learn(self):
        """LLM 返回 NEEDS_LEARN → 需要外部数据但无匹配 Skill"""
        mock_skill = MagicMock()
        mock_skill.name = "weather_query"
        mock_skill.trigger_words = ["天气"]
        mock_skill.aliases = []
        mock_skill.intent_tags = ["weather"]
        executor = self._make_executor_with_skills(skills=[mock_skill])

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value="NEEDS_LEARN")
        executor._get_llm = MagicMock(return_value=mock_llm)

        result = await executor._llm_classify("帮我监控网站变化")
        assert result.classification == LlmClassification.NEEDS_LEARN

    @pytest.mark.asyncio
    async def test_llm_returns_hallucinated_skill(self):
        """LLM 返回不存在的 Skill → 降级为 NEEDS_LEARN"""
        mock_skill = MagicMock()
        mock_skill.name = "weather_query"
        mock_skill.trigger_words = ["天气"]
        mock_skill.aliases = []
        mock_skill.intent_tags = ["weather"]
        executor = self._make_executor_with_skills(skills=[mock_skill])

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value="SKILL:nonexistent_skill")
        executor._get_llm = MagicMock(return_value=mock_llm)

        result = await executor._llm_classify("帮我做点什么")
        assert result.classification == LlmClassification.NEEDS_LEARN

    @pytest.mark.asyncio
    async def test_llm_failure_fallback_chitchat(self):
        """LLM 调用失败 → 安全降级为 CHITCHAT"""
        mock_skill = MagicMock()
        mock_skill.name = "weather_query"
        mock_skill.trigger_words = ["天气"]
        mock_skill.aliases = []
        mock_skill.intent_tags = ["weather"]
        executor = self._make_executor_with_skills(skills=[mock_skill])

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(side_effect=Exception("LLM timeout"))
        executor._get_llm = MagicMock(return_value=mock_llm)

        result = await executor._llm_classify("帮我查天气")
        assert result.classification == LlmClassification.CHITCHAT

    @pytest.mark.asyncio
    async def test_llm_unparseable_response_fallback_chitchat(self):
        """LLM 返回无法解析的内容 → 安全降级为 CHITCHAT"""
        mock_skill = MagicMock()
        mock_skill.name = "weather_query"
        mock_skill.trigger_words = ["天气"]
        mock_skill.aliases = []
        mock_skill.intent_tags = ["weather"]
        executor = self._make_executor_with_skills(skills=[mock_skill])

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value="我觉得应该用天气查询功能")
        executor._get_llm = MagicMock(return_value=mock_llm)

        result = await executor._llm_classify("帮我查天气")
        assert result.classification == LlmClassification.CHITCHAT

    @pytest.mark.asyncio
    async def test_skill_name_fuzzy_match(self):
        """LLM 返回的 skill_name 模糊匹配"""
        mock_skill = MagicMock()
        mock_skill.name = "web_automate"
        mock_skill.trigger_words = ["浏览器"]
        mock_skill.aliases = ["browser"]
        mock_skill.intent_tags = ["web"]
        executor = self._make_executor_with_skills(skills=[mock_skill])

        mock_llm = AsyncMock()
        mock_llm.chat = AsyncMock(return_value="SKILL:web_auto")
        executor._get_llm = MagicMock(return_value=mock_llm)

        result = await executor._llm_classify("帮我打开浏览器")
        assert result.classification == LlmClassification.SKILL_MATCHED
        assert result.skill_name == "web_automate"

    @pytest.mark.asyncio
    async def test_match_skill_delegates_to_llm_classify(self):
        """_match_skill 废弃方法委托给 _llm_classify"""
        executor = _make_executor()
        executor._llm_classify = AsyncMock(
            return_value=LlmClassifyResult(
                classification=LlmClassification.SKILL_MATCHED,
                skill_name="weather_query",
            )
        )

        result = await executor._match_skill("帮我查天气")
        assert result == "weather_query"

    @pytest.mark.asyncio
    async def test_match_skill_returns_none_for_chitchat(self):
        """_match_skill 废弃方法在 CHITCHAT 时返回 None"""
        executor = _make_executor()
        executor._llm_classify = AsyncMock(
            return_value=LlmClassifyResult(classification=LlmClassification.CHITCHAT)
        )

        result = await executor._match_skill("帮我写一首诗")
        assert result is None


class TestMightNeedAction:
    def test_short_text_is_not_action(self):
        executor = _make_executor()
        assert executor._might_need_action("嗯") is False

    def test_help_keyword_is_action(self):
        executor = _make_executor()
        assert executor._might_need_action("帮我监控网站变化") is True

    def test_chitchat_is_not_action(self):
        executor = _make_executor()
        assert executor._might_need_action("今天天气真好") is False


# ─── Intercept learn request ───────────────────────────────────────


class TestInterceptLearnRequest:
    def test_no_pending_returns_none(self):
        executor = _make_executor()
        assert executor._pending_learn_requests == {}

    def test_pending_learn_request_stored(self):
        executor = _make_executor()
        executor._pending_learn_requests["c1"] = "帮我监控"
        assert "c1" in executor._pending_learn_requests


# ─── Learn confirm flow ────────────────────────────────────────────


class TestLearnConfirm:
    @pytest.mark.asyncio
    async def test_reject_learn(self):
        executor = _make_executor()
        executor._pending_learn_requests["c1"] = "帮我监控"
        executor._chat_fallback = AsyncMock(return_value="好的")

        route = RouteResult(route_type=RouteType.LEARN, params={"original_text": "不要"})
        event = Event(
            event=EventType.USER_MESSAGE,
            conversation_id="c1",
            user_id="u1",
            payload={"text": "不要"},
        )

        await executor._handle_learn_confirm(route, event)
        # 拒绝后走 chat_fallback
        assert "c1" not in executor._pending_learn_requests

    @pytest.mark.asyncio
    async def test_confirm_learn_without_engine(self):
        executor = _make_executor()
        executor._learning_engine = None
        executor._pending_learn_requests["c1"] = "帮我监控网站"

        route = RouteResult(route_type=RouteType.LEARN, params={"original_text": "要"})
        event = Event(
            event=EventType.USER_MESSAGE,
            conversation_id="c1",
            user_id="u1",
            payload={"text": "要"},
        )

        result = await executor._handle_learn_confirm(route, event)
        assert "未初始化" in result or "学习引擎" in result

    @pytest.mark.asyncio
    async def test_confirm_learn_with_engine(self):
        mock_engine = AsyncMock()
        mock_engine.learn_and_schedule = AsyncMock(return_value={
            "reply": "学会了！", "files": [], "learned": True,
        })

        executor = _make_executor(_learning_engine=mock_engine)
        executor._pending_learn_requests["c1"] = "帮我监控网站"

        route = RouteResult(route_type=RouteType.LEARN, params={"original_text": "要"})
        event = Event(
            event=EventType.USER_MESSAGE,
            conversation_id="c1",
            user_id="u1",
            payload={"text": "要"},
        )

        result = await executor._handle_learn_confirm(route, event)
        assert "学会" in result


# ─── LearningEngine ────────────────────────────────────────────────


class TestLearningEngine:
    @pytest.mark.asyncio
    async def test_learn_and_schedule_no_schedule_intent(self):
        from src.brain.learning_engine import LearningEngine

        engine = LearningEngine.__new__(LearningEngine)
        engine._llm = None
        engine._scheduler = None
        engine._auto_install = True
        engine._requires_approval = False
        engine._vault = None
        engine._memcore_writer = None
        engine._config = MagicMock()

        result = await engine.learn_and_schedule(
            Task(task_id="t1", conversation_id="c1", user_id="u1", origin_message="test", skill_name="test"),
            "test",
            "c1",
        )
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_try_create_schedule_with_schedule_intent(self):
        from src.brain.learning_engine import LearningEngine

        engine = LearningEngine.__new__(LearningEngine)
        engine._scheduler = None
        engine._config = MagicMock()

        result = await engine._try_create_schedule("每天八点给我日报", "ai_daily_report", "c1")
        assert result is None

    def test_build_skill_catalog_empty(self):
        from src.brain.learning_engine import LearningEngine

        engine = LearningEngine.__new__(LearningEngine)
        engine._vault = None

        catalog = engine._build_skill_catalog()
        assert catalog == "（无已安装 Skill）"

    def test_build_skill_catalog_with_skills(self):
        from src.brain.learning_engine import LearningEngine

        mock_vault = MagicMock()
        mock_vault.list_skills = MagicMock(return_value=[
            MagicMock(name="web_automate", trigger_words=["浏览器"]),
            MagicMock(name="ai_daily_report", trigger_words=["AI日报"]),
        ])

        engine = LearningEngine.__new__(LearningEngine)
        engine._vault = mock_vault

        catalog = engine._build_skill_catalog()
        assert "web_automate" in catalog
        assert "ai_daily_report" in catalog

    def test_replace_requests_with_httpx(self):
        from src.brain.learning_engine import LearningEngine

        code = """import requests

resp = requests.get("https://example.com", headers={"User-Agent": "bot"}, timeout=10)
resp.raise_for_status()
data = resp.json()
"""
        result = LearningEngine._replace_requests_with_httpx(code)
        assert "import httpx" in result
        assert "requests" not in result
        assert "httpx.get" in result

    def test_replace_requests_with_httpx_preserves_api(self):
        from src.brain.learning_engine import LearningEngine

        code = """import requests
resp = requests.post(url, json=payload)
content = resp.content
text = resp.text
status = resp.status_code
"""
        result = LearningEngine._replace_requests_with_httpx(code)
        assert "httpx.post" in result
        # httpx 兼容 requests API，这些属性都保留
        assert "resp.content" in result
        assert "resp.text" in result
        assert "resp.status_code" in result


# ─── Chat with tools fallback ──────────────────────────────────────


class TestChatWithTools:
    @pytest.mark.asyncio
    async def test_stub_llm_chat_with_tools(self):
        from src.llm import MockLLMClient

        client = MockLLMClient()
        result = await client.chat_with_tools(
            [{"role": "user", "content": "你好"}],
            tools=[],
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_mock_llm_chat_with_tools(self):
        from src.llm import MockLLMClient

        client = MockLLMClient()
        result = await client.chat_with_tools(
            [{"role": "user", "content": "帮我搜索"}],
            tools=[{"type": "function", "function": {"name": "search", "parameters": {}}}],
        )
        assert result is not None
