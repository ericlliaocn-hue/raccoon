"""Executor 主逻辑：接收路由结果，调度任务

核心流程：
1. Router 返回 RouteResult(skill) → 创建 Task(PENDING)
2. EventBus 回复"收到"
3. Executor 加载 Skill → 执行 → 状态转换(RUNNING→SUCCESS/FAILED)
4. 完成后 EventBus(task_completed/task_failed) → 主动注入对话

多轮对话：
- 旧机制（need_login）：Skill 返回 need_login=True 时挂起
- 新机制（SkillSession + FlowEngine）：interactive=True 的 Skill 走流程编排
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.executor.task_queue import TaskQueue
from src.executor.task_state_machine import TaskStateMachine, TransitionError
from src.executor.media_pusher import MediaPusher
from src.executor.file_manager import FileManager
from src.executor.skill_session_manager import SkillSessionManager
from src.executor.flow_engine import FlowEngine
from src.llm import LLMFactory, LLMClient
from src.types import (
    Event,
    EventType,
    FlowDefinition,
    RouteResult,
    RouteType,
    SessionStatus,
    SkillMetadata,
    Task,
    TaskStatus,
    make_event,
)

if TYPE_CHECKING:
    from src.skill_vault.vault_manager import VaultManager

logger = structlog.get_logger(__name__)


@dataclass
class PendingContext:
    """挂起的任务上下文：Skill 等待用户输入（如登录流程）"""
    skill_name: str
    conversation_id: str
    login_step: str = ""  # "input_phone" / "input_code" 等
    pending_prompt: str = ""
    pending_params: dict[str, Any] = field(default_factory=dict)


class Executor:
    """执行器：接收路由结果，创建/执行任务，管理多轮对话"""

    def __init__(
        self,
        event_bus: EventBus,
        vault_manager: "VaultManager",
        config: RaccoonConfig | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._vault_manager = vault_manager
        self._config = config or RaccoonConfig()
        self._task_queue = TaskQueue(self._config)
        self._media_pusher = MediaPusher()
        self._state_machine = TaskStateMachine()
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent_tasks)
        self._llm: LLMClient | None = None  # 延迟初始化
        self._file_manager = FileManager()
        self._media_pusher = MediaPusher(self._file_manager)
        # 多轮对话：conversation_id → PendingContext
        self._pending_tasks: dict[str, PendingContext] = {}

        # 新机制：SkillSession + FlowEngine
        self._session_manager = SkillSessionManager()
        self._session_manager.set_event_bus(event_bus)
        self._flow_engine = FlowEngine(
            event_bus, self._session_manager, vault_manager, self._config
        )

        # 恢复持久化任务
        self._task_queue.recover()

    @property
    def session_manager(self) -> SkillSessionManager:
        """暴露 SessionManager 供 http_adapter 使用"""
        return self._session_manager

    @property
    def flow_engine(self) -> FlowEngine:
        """暴露 FlowEngine"""
        return self._flow_engine

    def get_pending_context(self, conversation_id: str) -> PendingContext | None:
        """获取对话的挂起任务上下文"""
        return self._pending_tasks.get(conversation_id)

    async def handle_route_result(
        self, route: RouteResult, event: Event
    ) -> str | None:
        """处理路由结果，返回回复消息"""
        if route.route_type == RouteType.SKILL:
            return await self._handle_skill(route, event)
        elif route.route_type == RouteType.SKILL_SESSION:
            return await self._handle_skill_session(route, event)
        elif route.route_type == RouteType.TASK_STATUS:
            return await self._handle_status_query(route, event)
        elif route.route_type == RouteType.TASK_OPERATION:
            return await self._handle_task_operation(route, event)
        elif route.route_type == RouteType.SYSTEM:
            return await self._handle_system(route, event)
        elif route.route_type == RouteType.LLM:
            return await self._handle_llm(route, event)
        return None

    # ─── Skill 执行 ────────────────────────────────────────────

    async def _handle_skill(self, route: RouteResult, event: Event) -> str:
        """创建任务并异步执行 Skill"""
        skill_name = route.skill_name
        if not skill_name:
            return "错误：路由结果缺少 skill_name"

        # 检查是否为 interactive Skill → 走 FlowEngine
        skill_meta = self._vault_manager.get_skill(skill_name)
        if skill_meta and skill_meta.interactive and skill_meta.flow:
            return await self._handle_interactive_skill(skill_meta, route, event)

        # 创建 Task
        task = Task(
            conversation_id=event.conversation_id,
            user_id=event.user_id,
            origin_message=event.payload.get("text", ""),
            skill_name=skill_name,
        )
        self._task_queue.add(task)

        # 发射 task_submitted 事件
        await self._event_bus.emit(
            make_event(
                EventType.TASK_SUBMITTED,
                conversation_id=event.conversation_id,
                user_id=event.user_id,
                task_id=task.task_id,
                skill_name=skill_name,
                status=TaskStatus.PENDING,
            )
        )

        # 异步执行
        asyncio_task = asyncio.create_task(
            self._execute_task(task, route.params, event)
        )
        self._running_tasks[task.task_id] = asyncio_task

        return f"收到！任务已创建 [{task.task_id[:8]}] Skill: {skill_name}"

    async def _execute_task(
        self, task: Task, params: dict, event: Event
    ) -> None:
        """执行单个任务（带并发限制）"""
        async with self._semaphore:
            try:
                # PENDING → RUNNING
                self._state_machine.transition(task, TaskStatus.RUNNING)
                self._task_queue.update(task)

                # 加载 Skill 并执行
                skill_runner = self._vault_manager.get_skill_runner(task.skill_name)
                if skill_runner is None:
                    raise RuntimeError(f"Skill not found: {task.skill_name}")

                result = await asyncio.wait_for(
                    skill_runner.run(task, params),
                    timeout=self._config.task_timeout_seconds,
                )

                # 检查 cancel_token（Skill 执行期间用户可能已请求取消）
                # request_cancel 可能已将状态改为 CANCELLING，需要兼容两种情况
                if task.cancel_token:
                    self._transition_to_cancelled(task)
                    self._task_queue.update(task)
                    await self._event_bus.emit(
                        make_event(
                            EventType.TASK_FAILED,
                            conversation_id=task.conversation_id,
                            user_id=task.user_id,
                            task_id=task.task_id,
                            skill_name=task.skill_name,
                            status=TaskStatus.CANCELLED,
                            payload={"reason": "cancelled_by_user"},
                        )
                    )
                    return

                # RUNNING → SUCCESS
                task.result = result
                self._state_machine.transition(task, TaskStatus.SUCCESS)
                self._task_queue.update(task)

                # 检查 need_login：Skill 返回需要登录时，挂起任务
                if isinstance(result, dict) and result.get("need_login"):
                    self._set_pending(task, result)
                else:
                    # 登录完成或普通成功，清除挂起
                    self._clear_pending(task)

                # 推送结果，获取文件信息
                logger.debug("before_media_push", task_id=task.task_id, has_result=bool(result))
                files_info = await self._media_pusher.push(task, task.conversation_id)
                logger.debug("after_media_push", task_id=task.task_id, files_count=len(files_info))

                # 发射 task_completed 事件（payload 含文件元信息）
                payload = (result or {}).copy()
                if files_info:
                    payload["_files"] = files_info
                await self._event_bus.emit(
                    make_event(
                        EventType.TASK_COMPLETED,
                        conversation_id=task.conversation_id,
                        user_id=task.user_id,
                        task_id=task.task_id,
                        skill_name=task.skill_name,
                        status=TaskStatus.SUCCESS,
                        payload=payload,
                    )
                )

            except asyncio.TimeoutError:
                # RUNNING → FAILED
                task.error = f"Task timed out ({self._config.task_timeout_seconds}s)"
                try:
                    self._state_machine.transition(task, TaskStatus.FAILED)
                except TransitionError:
                    pass
                self._task_queue.update(task)
                await self._emit_failure(task, "timeout")

            except asyncio.CancelledError:
                # asyncio task 被 cancel，需要转到 CANCELLED
                self._transition_to_cancelled(task)
                self._task_queue.update(task)
                await self._emit_failure(task, "cancelled")

            except Exception as e:
                # RUNNING → FAILED
                task.error = str(e)
                try:
                    self._state_machine.transition(task, TaskStatus.FAILED)
                except TransitionError:
                    pass
                self._task_queue.update(task)
                await self._emit_failure(task, str(e))

            finally:
                self._running_tasks.pop(task.task_id, None)

    async def _emit_failure(self, task: Task, reason: str) -> None:
        """发射任务失败事件"""
        await self._event_bus.emit(
            make_event(
                EventType.TASK_FAILED,
                conversation_id=task.conversation_id,
                user_id=task.user_id,
                task_id=task.task_id,
                skill_name=task.skill_name,
                status=task.status,
                payload={"error": reason},
            )
        )

    def _transition_to_cancelled(self, task: Task) -> None:
        """安全地将任务转到 CANCELLED 状态，兼容 RUNNING 和 CANCELLING 两种当前状态"""
        try:
            if task.status == TaskStatus.CANCELLING:
                # request_cancel 已将状态改为 CANCELLING，直接转 CANCELLED
                self._state_machine.transition(task, TaskStatus.CANCELLED)
            elif task.status == TaskStatus.RUNNING:
                # 需要先经过 CANCELLING 中间态
                self._state_machine.transition(task, TaskStatus.CANCELLING)
                self._state_machine.transition(task, TaskStatus.CANCELLED)
            # 其他状态（终态）忽略
        except TransitionError:
            logger.warning(
                "cancel_transition_failed",
                task_id=task.task_id,
                current_status=task.status.value,
            )

    # ─── 状态查询 ──────────────────────────────────────────────

    async def _handle_status_query(self, route: RouteResult, event: Event) -> str:
        """处理任务状态查询"""
        task_id = route.params.get("task_id")

        if task_id:
            task = self._task_queue.get(task_id)
            if task:
                return f"任务 [{task_id[:8]}] 状态: {task.status.value} | Skill: {task.skill_name}"
            return f"未找到任务: {task_id[:8]}"

        # 查询对话下所有活跃任务
        active = self._task_queue.get_active_by_conversation(event.conversation_id)
        if not active:
            return "当前没有活跃任务"

        lines = ["活跃任务:"]
        for t in active:
            lines.append(f"  [{t.task_id[:8]}] {t.skill_name} - {t.status.value}")
        return "\n".join(lines)

    # ─── 任务操作 ──────────────────────────────────────────────

    async def _handle_task_operation(self, route: RouteResult, event: Event) -> str:
        """处理任务操作（取消等）"""
        operation = route.params.get("operation", "")

        if operation == "cancel":
            task_id = route.params.get("task_id")
            if not task_id:
                # 取消对话下所有活跃任务
                active = self._task_queue.get_active_by_conversation(event.conversation_id)
                if not active:
                    return "没有可取消的任务"
                for t in active:
                    self._state_machine.request_cancel(t)
                    self._task_queue.update(t)
                return f"已请求取消 {len(active)} 个任务"

            task = self._task_queue.get(task_id)
            if not task:
                return f"未找到任务: {task_id[:8]}"

            self._state_machine.request_cancel(task)
            self._task_queue.update(task)
            return f"已请求取消任务 [{task_id[:8]}]"

        return f"未知操作: {operation}"

    # ─── 系统指令 ──────────────────────────────────────────────

    async def _handle_system(self, route: RouteResult, event: Event) -> str:
        """处理系统指令"""
        command = route.params.get("command", "")

        if command == "help":
            from src.router.system_commands import SystemCommands
            lines = ["可用指令:"]
            for cmd, desc in SystemCommands.BUILTIN_COMMANDS.items():
                lines.append(f"  /{cmd} - {desc}")
            lines.append("  @<skill_name> <args> - 直接调用 Skill")
            return "\n".join(lines)

        elif command == "skills":
            skills = self._vault_manager.list_skills()
            if not skills:
                return "没有已安装的 Skill"
            lines = ["已安装的 Skill:"]
            for s in skills:
                lines.append(f"  {s.name} v{s.version} - {s.description}")
            return "\n".join(lines)

        elif command == "config":
            return f"配置: max_concurrent={self._config.max_concurrent_tasks}, timeout={self._config.task_timeout_seconds}s"

        elif command == "install":
            url = route.params.get("args", "").strip()
            if not url:
                return "用法: /install <git_url>"
            try:
                skill = await self._vault_manager.install(url)
                return f"已安装 Skill: {skill.name} v{skill.version}"
            except Exception as e:
                return f"安装失败: {e}"

        elif command == "uninstall":
            name = route.params.get("args", "").strip()
            if not name:
                return "用法: /uninstall <skill_name>"
            try:
                self._vault_manager.uninstall(name)
                return f"已卸载 Skill: {name}"
            except Exception as e:
                return f"卸载失败: {e}"

        else:
            return f"未知指令: /{command}，输入 /help 查看帮助"

    # ─── LLM 兜底 ──────────────────────────────────────────────

    async def _handle_interactive_skill(
        self, skill_meta: SkillMetadata, route: RouteResult, event: Event
    ) -> str:
        """处理 interactive Skill：启动 FlowEngine 流程"""
        flow = skill_meta.flow
        if not flow or not flow.steps:
            return f"⚠️ Skill「{skill_meta.name}」声明了交互模式但没有定义流程步骤"

        initial_params = route.params.copy()
        initial_params["origin_message"] = event.payload.get("text", "")

        return await self._flow_engine.start_flow(
            conversation_id=event.conversation_id,
            skill_name=skill_meta.name,
            flow=flow,
            initial_params=initial_params,
            timeout_seconds=skill_meta.timeout_seconds,
        )

    async def _handle_skill_session(self, route: RouteResult, event: Event) -> str:
        """处理活跃 Skill 会话中的用户输入"""
        conversation_id = event.conversation_id
        user_text = event.payload.get("text", "")

        # 检查是否取消
        if user_text.strip() in ("取消", "cancel", "退出", "exit", "quit"):
            return await self._flow_engine.cancel_flow(conversation_id)

        return await self._flow_engine.handle_session_input(
            conversation_id=conversation_id,
            user_text=user_text,
        )

    # ─── LLM 兜底（原有） ────────────────────────────────────

    def _get_llm(self) -> LLMClient:
        """延迟初始化 LLM 客户端"""
        if self._llm is None:
            self._llm = LLMFactory.create(self._config)
        return self._llm

    async def _handle_llm(self, route: RouteResult, event: Event) -> str:
        """LLM 兜底响应"""
        text = route.params.get("original_text", "")
        try:
            llm = self._get_llm()
            messages = [{"role": "user", "content": text}]
            reply = await llm.chat(
                messages,
                temperature=self._config.llm_temperature,
                max_tokens=self._config.llm_max_tokens,
            )
            return reply
        except Exception as e:
            logger.error("llm_call_failed", error=str(e))
            return f"⚠️ LLM 调用失败: {e}\n\n请检查 config.json 中的 llm_api_key 是否正确配置。"

    # ─── 辅助 ──────────────────────────────────────────────────

    @property
    def task_queue(self) -> TaskQueue:
        return self._task_queue

    # ─── 多轮对话：pending task 管理 ──────────────────────────

    def _set_pending(self, task: Task, result: dict) -> None:
        """Skill 返回 need_login 时，挂起任务，后续同 conversation_id 消息直接路由到该 Skill"""
        conv_id = task.conversation_id
        ctx = PendingContext(
            skill_name=task.skill_name,
            conversation_id=conv_id,
            login_step=result.get("login_step", ""),
            pending_prompt=result.get("pending_prompt", ""),
            pending_params=result.get("pending_params", {}),
        )
        self._pending_tasks[conv_id] = ctx
        logger.info("task_pending_set", conversation_id=conv_id, skill=task.skill_name, login_step=ctx.login_step)

    def _clear_pending(self, task: Task) -> None:
        """登录完成或普通任务成功时，清除挂起"""
        conv_id = task.conversation_id
        ctx = self._pending_tasks.get(conv_id)
        if ctx and ctx.skill_name == task.skill_name:
            del self._pending_tasks[conv_id]
            logger.info("task_pending_cleared", conversation_id=conv_id, skill=task.skill_name)
