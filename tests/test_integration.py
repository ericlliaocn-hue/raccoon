"""端到端集成测试

测试完整事件闭环：
用户消息 → Adapter → EventBus → Router → Executor → Skill → 结果注入
"""

import asyncio
import pytest

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.executor.agent import Executor
from src.router.router import Router
from src.skill_vault.vault_manager import VaultManager
from src.types import Event, RouteType, SkillMetadata


@pytest.fixture
def config(tmp_path):
    return RaccoonConfig(
        skills_dir=tmp_path / "skills",
        db_path=tmp_path / "data" / "memcore.db",
        event_queue_size=50,
    )


@pytest.fixture
async def system(config):
    """组装完整系统"""
    event_bus = EventBus(config)
    vault = VaultManager(config)
    router = Router()
    executor = Executor(event_bus, vault, config)

    # 注册已安装的 Skill
    for skill in vault.list_skills():
        router.register_skill(skill)

    # 确保 echo Skill 注册到 Router（tmp_path 下可能没有预装 Skill）
    if not vault.get_skill("echo"):
        echo_meta = SkillMetadata(
            name="echo",
            version="0.1.0",
            description="回显",
            trigger_words=["echo", "回显"],
        )
        router.register_skill(echo_meta)

    await event_bus.start()
    yield event_bus, router, executor, vault
    await event_bus.stop()


@pytest.mark.asyncio
async def test_echo_skill_e2e(system):
    """测试 echo Skill 端到端"""
    event_bus, router, executor, vault = system

    # 1. 用户发送消息
    event = make_event(
        EventType.USER_MESSAGE,
        conversation_id="e2e-test-1",
        user_id="tester",
        payload={"text": "echo hello world"},
    )
    await event_bus.emit(event)

    # 2. 路由
    route = await router.route("echo hello world")
    assert route.route_type == RouteType.SKILL
    assert route.skill_name == "echo"

    # 3. 执行
    reply = await executor.handle_route_result(route, event)
    assert "收到" in reply or "echo" in reply.lower()

    # 4. 等待任务完成
    await asyncio.sleep(1.0)

    # 5. 检查任务状态
    tasks = list(executor.task_queue.all_tasks())
    assert len(tasks) >= 1


@pytest.mark.asyncio
async def test_system_command_help(system):
    """测试 /help 系统指令"""
    event_bus, router, executor, vault = system

    event = make_event(
        EventType.USER_MESSAGE,
        conversation_id="e2e-test-2",
        payload={"text": "/help"},
    )

    route = await router.route("/help")
    reply = await executor.handle_route_result(route, event)
    assert "可用指令" in reply or "help" in reply.lower()


@pytest.mark.asyncio
async def test_task_status_query(system):
    """测试任务状态查询"""
    event_bus, router, executor, vault = system

    event = make_event(
        EventType.USER_MESSAGE,
        conversation_id="e2e-test-3",
        payload={"text": "进度查询"},
    )

    route = await router.route("进度查询")
    assert route.route_type == RouteType.TASK_STATUS

    reply = await executor.handle_route_result(route, event)
    assert "活跃任务" in reply or "没有" in reply


@pytest.mark.asyncio
async def test_llm_fallback(system):
    """测试 LLM 兜底"""
    event_bus, router, executor, vault = system

    event = make_event(
        EventType.USER_MESSAGE,
        conversation_id="e2e-test-4",
        payload={"text": "你好呀，今天天气怎么样"},
    )

    route = await router.route("你好呀，今天天气怎么样")
    assert route.route_type == RouteType.LLM

    reply = await executor.handle_route_result(route, event)
    # LLM 可能返回真实回复或 mock/fallback
    assert isinstance(reply, str) and len(reply) > 0


@pytest.mark.asyncio
async def test_eventbus_event_flow(config):
    """测试 EventBus 事件流转"""
    event_bus = EventBus(config)
    events_received = []

    async def capture(event: Event):
        events_received.append(event)

    event_bus.on(EventType.TASK_COMPLETED, capture)
    event_bus.on(EventType.TASK_FAILED, capture)
    await event_bus.start()

    # 模拟任务完成事件
    await event_bus.emit(
        make_event(EventType.TASK_COMPLETED, conversation_id="flow-test", task_id="t1")
    )
    await event_bus.emit(
        make_event(EventType.TASK_FAILED, conversation_id="flow-test", task_id="t2")
    )

    await asyncio.sleep(0.2)
    await event_bus.stop()

    assert len(events_received) == 2
    assert events_received[0].event == EventType.TASK_COMPLETED
    assert events_received[1].event == EventType.TASK_FAILED
