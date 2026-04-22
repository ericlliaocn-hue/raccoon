"""EventBus 单元测试"""

import asyncio
import pytest

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.types import Event, EventType, make_event


@pytest.fixture
def event_bus():
    return EventBus(RaccoonConfig(event_queue_size=10))


@pytest.mark.asyncio
async def test_emit_and_dispatch(event_bus):
    """测试事件发射和分发"""
    received = []

    async def handler(event: Event):
        received.append(event)

    event_bus.on(EventType.USER_MESSAGE, handler)
    await event_bus.start()

    event = make_event(EventType.USER_MESSAGE, conversation_id="test-1", payload={"text": "hello"})
    await event_bus.emit(event)

    await asyncio.sleep(0.1)
    await event_bus.stop()

    assert len(received) == 1
    assert received[0].conversation_id == "test-1"
    assert received[0].payload["text"] == "hello"


@pytest.mark.asyncio
async def test_multiple_handlers(event_bus):
    """测试多个 handler"""
    results_a = []
    results_b = []

    async def handler_a(event: Event):
        results_a.append(event)

    async def handler_b(event: Event):
        results_b.append(event)

    event_bus.on(EventType.TASK_COMPLETED, handler_a)
    event_bus.on(EventType.TASK_COMPLETED, handler_b)
    await event_bus.start()

    event = make_event(EventType.TASK_COMPLETED, conversation_id="test-2")
    await event_bus.emit(event)

    await asyncio.sleep(0.1)
    await event_bus.stop()

    assert len(results_a) == 1
    assert len(results_b) == 1


@pytest.mark.asyncio
async def test_off_removes_handler(event_bus):
    """测试移除 handler"""
    received = []

    async def handler(event: Event):
        received.append(event)

    event_bus.on(EventType.USER_MESSAGE, handler)
    event_bus.off(EventType.USER_MESSAGE, handler)

    await event_bus.start()
    event = make_event(EventType.USER_MESSAGE, conversation_id="test-3")
    await event_bus.emit(event)

    await asyncio.sleep(0.1)
    await event_bus.stop()

    assert len(received) == 0


@pytest.mark.asyncio
async def test_global_handler(event_bus):
    """测试全局 handler"""
    received = []

    async def handler(event: Event):
        received.append(event)

    event_bus.on_any(handler)
    await event_bus.start()

    await event_bus.emit(make_event(EventType.USER_MESSAGE, conversation_id="t1"))
    await event_bus.emit(make_event(EventType.TASK_COMPLETED, conversation_id="t2"))

    await asyncio.sleep(0.2)
    await event_bus.stop()

    assert len(received) == 2


@pytest.mark.asyncio
async def test_queue_full_drops_oldest():
    """测试队列满时丢弃最旧事件"""
    bus = EventBus(RaccoonConfig(event_queue_size=2))
    await bus.start()

    for i in range(5):
        await bus.emit(make_event(EventType.USER_MESSAGE, conversation_id=f"t{i}"))

    await asyncio.sleep(0.1)
    assert bus.pending_count <= 2
    await bus.stop()
