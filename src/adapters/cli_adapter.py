"""CLI 适配器（click + rich）

交互式命令行，模拟对话场景。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from src.config import load_config
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.executor.agent import Executor
from src.router.router import Router
from src.skill_vault.vault_manager import VaultManager
from src.types import Event, RouteResult, RouteType

if TYPE_CHECKING:
    from src.supervisor.audit_logger import AuditLogger

console = Console()


class CLISession:
    """CLI 会话：交互式对话"""

    def __init__(
        self,
        event_bus: EventBus,
        router: Router,
        executor: Executor,
        vault_manager: VaultManager,
        audit_logger: "AuditLogger | None" = None,
    ) -> None:
        self._event_bus = event_bus
        self._router = router
        self._executor = executor
        self._vault_manager = vault_manager
        self._audit_logger = audit_logger
        self._conversation_id = str(uuid.uuid4())
        self._user_id = "cli_user"

    async def start(self) -> None:
        """启动 CLI 交互循环"""
        # 注册事件处理器：监听 task_completed / task_failed
        self._event_bus.on(EventType.TASK_COMPLETED, self._on_task_completed)
        self._event_bus.on(EventType.TASK_FAILED, self._on_task_failed)

        # 启动 EventBus
        await self._event_bus.start()

        # 注册已安装的 Skill 到 Router
        for skill in self._vault_manager.list_skills():
            self._router.register_skill(skill)

        # 欢迎信息
        console.print(Panel(
            "[bold green]浣熊 (Project Raccoon)[/bold green]\n"
            "事件驱动的 AI 智能体框架 v0.1.0\n\n"
            "输入消息开始对话，输入 /help 查看指令，Ctrl+C 退出",
            title="🦝 Raccoon",
            border_style="green",
        ))

        # 主循环
        try:
            while True:
                try:
                    text = Prompt.ask("[bold cyan]你[/bold cyan]")
                    if not text.strip():
                        continue
                except (EOFError, KeyboardInterrupt):
                    break

                response = await self._process_message(text)
                if response:
                    console.print(Panel(
                        response,
                        title="[bold yellow]浣熊[/bold yellow]",
                        border_style="yellow",
                    ))
        finally:
            await self._event_bus.stop()

    async def _process_message(self, text: str) -> str:
        """处理用户消息：EventBus → Router → Executor"""
        # 发射 user_message 事件
        event = make_event(
            EventType.USER_MESSAGE,
            conversation_id=self._conversation_id,
            user_id=self._user_id,
            payload={"text": text},
        )
        await self._event_bus.emit(event)

        # 检查是否有挂起的多轮对话（如登录流程），有则直接路由到对应 Skill
        pending = self._executor.get_pending_context(self._conversation_id)
        if pending:
            route = RouteResult(
                route_type=RouteType.SKILL,
                skill_name=pending.skill_name,
                params={
                    "action": "continue",
                    "original_text": text,
                    "login_step": pending.login_step,
                    "pending_prompt": pending.pending_prompt,
                    "pending_params": pending.pending_params,
                },
                confidence=0.95,
            )
        else:
            route = await self._router.route(text)

        # 执行
        response = await self._executor.handle_route_result(route, event)

        # 审计日志
        if self._audit_logger:
            self._audit_logger.log(
                action="user_message",
                actor=self._user_id,
                target=self._conversation_id,
                detail={"text": text, "route_type": route.route_type.value},
            )

        return response or "(无响应)"

    async def _on_task_completed(self, event: Event) -> None:
        """任务完成回调"""
        skill = event.skill_name or "unknown"
        result = event.payload
        console.print(Panel(
            f"[green]✓ 任务完成[/green]\n"
            f"Skill: {skill}\n"
            f"结果: {result}",
            title="[bold green]任务完成[/bold green]",
            border_style="green",
        ))

    async def _on_task_failed(self, event: Event) -> None:
        """任务失败回调"""
        skill = event.skill_name or "unknown"
        error = event.payload.get("error", "未知错误")
        console.print(Panel(
            f"[red]✗ 任务失败[/red]\n"
            f"Skill: {skill}\n"
            f"错误: {error}",
            title="[bold red]任务失败[/bold red]",
            border_style="red",
        ))


def run_cli() -> None:
    """CLI 入口"""
    config = load_config()
    event_bus = EventBus(config)
    vault_manager = VaultManager(config)
    router = Router()
    executor = Executor(event_bus, vault_manager, config)

    from src.supervisor.audit_logger import AuditLogger
    audit_logger = AuditLogger(config)

    session = CLISession(event_bus, router, executor, vault_manager, audit_logger)
    asyncio.run(session.start())
