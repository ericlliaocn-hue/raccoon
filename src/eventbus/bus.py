"""EventBus 核心：emit / on / off / 事件循环主循环

设计要点：
- 基于 asyncio.Queue 实现单消费者事件循环
- 多生产者通过 emit() 推入事件
- 支持 handler 注册（on/off），按事件类型分发
- 事件按顺序处理，保证因果一致性
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable

import structlog

from src.config import RaccoonConfig
from src.types import Event, EventType

logger = structlog.get_logger(__name__)

# Handler 类型：同步或异步函数，接收 Event
EventHandler = Callable[[Event], Awaitable[None] | None]


class EventBus:
    """事件总线：核心循环 + handler 注册/分发"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._queue: asyncio.Queue[Event] = asyncio.Queue(
            maxsize=self._config.event_queue_size
        )
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._global_handlers: list[EventHandler] = []
        self._running = False
        self._loop_task: asyncio.Task | None = None

    # ─── Handler 注册 ─────────────────────────────────────────

    def on(self, event_type: EventType, handler: EventHandler) -> None:
        """注册事件处理器"""
        self._handlers[event_type].append(handler)
        logger.debug("handler_registered", event_type=event_type.value, handler=handler.__name__)

    def off(self, event_type: EventType, handler: EventHandler) -> None:
        """移除事件处理器"""
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
            logger.debug("handler_removed", event_type=event_type.value, handler=handler.__name__)

    def on_any(self, handler: EventHandler) -> None:
        """注册全局事件处理器（所有事件都会触发）"""
        self._global_handlers.append(handler)

    # ─── 事件发射 ─────────────────────────────────────────────

    async def emit(self, event: Event) -> None:
        """发射事件到队列"""
        try:
            self._queue.put_nowait(event)
            logger.debug(
                "event_emitted",
                event_type=event.event.value,
                event_id=event.event_id,
                conversation_id=event.conversation_id,
            )
        except asyncio.QueueFull:
            logger.error("event_queue_full", event_type=event.event.value)
            # 丢弃最旧事件，推入新事件
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            await self._queue.put(event)

    def emit_sync(self, event: Event) -> None:
        """同步发射（在事件循环内调用 emit）"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.emit(event))
        except RuntimeError:
            # 没有运行中的事件循环，忽略
            logger.warning("emit_sync_no_loop", event_type=event.event.value)

    # ─── 事件循环 ─────────────────────────────────────────────

    async def start(self) -> None:
        """启动事件循环"""
        if self._running:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._event_loop())
        logger.info("eventbus_started")

    async def stop(self) -> None:
        """停止事件循环"""
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        logger.info("eventbus_stopped")

    async def _event_loop(self) -> None:
        """核心事件循环：单消费者，按序处理"""
        logger.info("event_loop_started")
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            await self._dispatch(event)

    async def _dispatch(self, event: Event) -> None:
        """分发事件到对应 handler"""
        log = logger.bind(
            event_type=event.event.value,
            event_id=event.event_id,
            conversation_id=event.conversation_id,
        )

        # 全局 handler
        for handler in self._global_handlers:
            await self._invoke_handler(handler, event, log)

        # 类型 handler
        handlers = self._handlers.get(event.event, [])
        if not handlers and not self._global_handlers:
            log.debug("no_handler_for_event")
            return

        for handler in handlers:
            await self._invoke_handler(handler, event, log)

    async def _invoke_handler(
        self, handler: EventHandler, event: Event, log: logging.Logger
    ) -> None:
        """调用 handler，处理同步/异步"""
        try:
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("handler_error", handler=handler.__name__)

    # ─── 辅助 ─────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()
