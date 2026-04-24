"""Router 单元测试"""

import pytest

from src.router.router import Router
from src.router.trigger_map import TriggerMap
from src.router.intent_classifier import KeywordIntentClassifier
from src.router.system_commands import SystemCommands
from src.types import RouteType, SkillMetadata


# ─── TriggerMap ────────────────────────────────────────────────

@pytest.fixture
def trigger_map():
    return TriggerMap()


@pytest.fixture
def echo_skill():
    return SkillMetadata(
        name="echo",
        version="0.1.0",
        description="回显",
        trigger_words=["echo", "回显"],
    )


def test_register_and_match(trigger_map, echo_skill):
    trigger_map.register(echo_skill)
    result = trigger_map.match("echo")
    assert result is not None
    assert result.skill_name == "echo"
    assert result.route_type == RouteType.SKILL


def test_match_chinese(trigger_map, echo_skill):
    trigger_map.register(echo_skill)
    result = trigger_map.match("回显")
    assert result is not None
    assert result.skill_name == "echo"


def test_trigger_map_no_match(trigger_map, echo_skill):
    trigger_map.register(echo_skill)
    result = trigger_map.match("hello world")
    assert result is None


def test_match_with_punctuation(trigger_map, echo_skill):
    trigger_map.register(echo_skill)
    result = trigger_map.match("echo!")
    assert result is not None
    assert result.skill_name == "echo"


def test_unregister(trigger_map, echo_skill):
    trigger_map.register(echo_skill)
    trigger_map.unregister(echo_skill)
    result = trigger_map.match("echo")
    assert result is None


# ─── TriggerMap 多触发词加权匹配 ────────────────────────────────

@pytest.fixture
def multi_skill_map():
    """注册多个有触发词交叉的 skill，测试加权匹配"""
    tm = TriggerMap()
    tm.register(SkillMetadata(
        name="web_automate", version="1.0.0", description="浏览器自动化",
        trigger_words=["打开浏览器", "浏览器操作", "网页操作", "网页点击", "网页输入",
                       "自动浏览", "帮我操作浏览器", "用我的Chrome", "网页截图",
                       "网页自动化", "网页搜索", "web automate"],
    ))
    tm.register(SkillMetadata(
        name="screenshot", version="1.0.0", description="截屏",
        trigger_words=["截图", "截屏", "屏幕截图", "截个图", "截一下", "screenshot", "抓屏"],
    ))
    tm.register(SkillMetadata(
        name="file_search", version="1.0.0", description="文件搜索",
        trigger_words=["找文件", "搜索文件", "查找文件", "找一下", "搜一下", "find file", "search file"],
    ))
    tm.register(SkillMetadata(
        name="app_control", version="1.0.0", description="应用控制",
        trigger_words=["打开应用", "打开app", "启动应用", "运行应用", "关闭应用", "退出应用",
                       "正在运行的应用", "应用列表", "打开", "启动", "列出应用", "kill应用", "结束进程"],
    ))
    return tm


def test_weighted_multi_trigger_web_automate_wins(multi_skill_map):
    """'打开浏览器搜索百度并截图' → web_automate 命中多个触发词，得分高于 screenshot"""
    result = multi_skill_map.match("打开浏览器搜索百度并截图")
    assert result is not None
    assert result.skill_name == "web_automate"


def test_weighted_screenshot_alone(multi_skill_map):
    """'截图' → 只有 screenshot 命中"""
    result = multi_skill_map.match("截图")
    assert result is not None
    assert result.skill_name == "screenshot"


def test_weighted_file_search_no_conflict(multi_skill_map):
    """'搜索文件 raccoon.py' → file_search 命中'搜索文件'(4字)，web_automate 没命中"""
    result = multi_skill_map.match("搜索文件 raccoon.py")
    assert result is not None
    assert result.skill_name == "file_search"


def test_weighted_app_control_vs_web(multi_skill_map):
    """'打开应用微信' → app_control 命中'打开应用'(4字)+'打开'(2字)=6, web_automate 命中'打开'(2字)=2"""
    result = multi_skill_map.match("打开应用微信")
    assert result is not None
    assert result.skill_name == "app_control"


def test_weighted_web_automate_explicit(multi_skill_map):
    """'浏览器操作' → web_automate 明确命中"""
    result = multi_skill_map.match("浏览器操作")
    assert result is not None
    assert result.skill_name == "web_automate"


def test_weighted_web_search_trigger(multi_skill_map):
    """'网页搜索 coding plan' → web_automate 命中'网页搜索'"""
    result = multi_skill_map.match("网页搜索 coding plan")
    assert result is not None
    assert result.skill_name == "web_automate"


# ─── IntentClassifier ─────────────────────────────────────────

@pytest.fixture
def classifier():
    return KeywordIntentClassifier()


@pytest.mark.asyncio
async def test_status_query(classifier):
    result = await classifier.classify("进度怎么样了")
    assert result is not None
    assert result.route_type == RouteType.TASK_STATUS


@pytest.mark.asyncio
async def test_cancel_operation(classifier):
    result = await classifier.classify("取消任务")
    assert result is not None
    assert result.route_type == RouteType.TASK_OPERATION
    assert result.params["operation"] == "cancel"


@pytest.mark.asyncio
async def test_intent_classifier_no_match(classifier):
    result = await classifier.classify("今天天气不错")
    assert result is None


# ─── SystemCommands ────────────────────────────────────────────

@pytest.fixture
def sys_cmd():
    return SystemCommands()


def test_help_command(sys_cmd):
    result = sys_cmd.parse("/help")
    assert result is not None
    assert result.route_type == RouteType.SYSTEM
    assert result.params["command"] == "help"


def test_cancel_command(sys_cmd):
    result = sys_cmd.parse("/cancel task-123")
    assert result is not None
    assert result.route_type == RouteType.TASK_OPERATION
    assert result.params["task_id"] == "task-123"


def test_skill_direct_call(sys_cmd):
    result = sys_cmd.parse("@echo hello world")
    assert result is not None
    assert result.route_type == RouteType.SKILL
    assert result.skill_name == "echo"
    assert result.params["args"] == "hello world"


def test_not_a_command(sys_cmd):
    result = sys_cmd.parse("just a normal message")
    assert result is None


# ─── Router (集成) ─────────────────────────────────────────────

@pytest.fixture
def router():
    return Router()


@pytest.mark.asyncio
async def test_router_trigger_match(router, echo_skill):
    router.register_skill(echo_skill)
    result = await router.route("echo")
    assert result.route_type == RouteType.SKILL
    assert result.skill_name == "echo"


@pytest.mark.asyncio
async def test_router_system_command(router):
    result = await router.route("/help")
    assert result.route_type == RouteType.SYSTEM


@pytest.mark.asyncio
async def test_router_intent_classifier(router):
    result = await router.route("进度查询")
    assert result.route_type == RouteType.TASK_STATUS


@pytest.mark.asyncio
async def test_router_llm_fallback(router):
    result = await router.route("你好呀")
    assert result.route_type == RouteType.LLM
