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
from dataclasses import dataclass, field
from enum import Enum
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
    RouteResult,
    RouteType,
    SkillMetadata,
    Task,
    TaskStatus,
    make_event,
)

if TYPE_CHECKING:
    from src.skill_vault.vault_manager import VaultManager
    from src.brain.learning_engine import LearningEngine
    from src.scheduler.scheduler import Scheduler
    from src.supervisor.approval_engine import ApprovalEngine

logger = structlog.get_logger(__name__)


@dataclass
class PendingContext:
    """挂起的任务上下文：Skill 等待用户输入（如登录流程）"""
    skill_name: str
    conversation_id: str
    login_step: str = ""  # "input_phone" / "input_code" 等
    pending_prompt: str = ""
    pending_params: dict[str, Any] = field(default_factory=dict)


class LlmClassification(str, Enum):
    """LLM 消息分类结果（供 adapter 层决定响应方式）"""
    CHITCHAT = "chitchat"           # 闲聊 → 流式
    SKILL_MATCHED = "skill_matched" # 匹配到 Skill → 一次性
    NEEDS_LEARN = "needs_learn"     # 需要学习确认 → 一次性


@dataclass
class LlmClassifyResult:
    """LLM 消息分类结果"""
    classification: LlmClassification
    skill_name: str | None = None   # SKILL_MATCHED 时的 skill_name
    confidence: float = 1.0


@dataclass
class LlmStreamDecision:
    """LLM 路由在流式接口里的展示决策。

    分类和 Skill/学习分支都留在 Executor 内部；adapter 只负责把结果渲染成 SSE。
    """

    stream_chat: bool
    reply: str | None = None
    source: str = "llm"
    skill_name: str = ""


class Executor:
    """执行器：接收路由结果，创建/执行任务，管理多轮对话"""

    def __init__(
        self,
        event_bus: EventBus,
        vault_manager: "VaultManager",
        config: RaccoonConfig | None = None,
        approval_engine: "ApprovalEngine | None" = None,
    ) -> None:
        self._event_bus = event_bus
        self._vault_manager = vault_manager
        self._config = config or RaccoonConfig()
        self._task_queue = TaskQueue(self._config)
        self._media_pusher = MediaPusher()
        self._state_machine = TaskStateMachine()
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._pending_approvals: dict[str, tuple[Task, dict[str, Any], Event]] = {}
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
            event_bus, self._session_manager, vault_manager, self._config, approval_engine
        )
        self._approval_engine = approval_engine
        self._event_bus.on(EventType.APPROVAL_RESOLVED, self._on_approval_resolved)

        # L3 学习链路：LearningEngine + Scheduler（延迟注入）
        self._learning_engine: LearningEngine | None = None
        self._scheduler: Scheduler | None = None
        # 待确认的学习请求：conversation_id → 用户原始消息
        self._pending_learn_requests: dict[str, str] = {}

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

    def set_learning_engine(self, engine: "LearningEngine") -> None:
        """注入 LearningEngine 实例"""
        self._learning_engine = engine

    def set_scheduler(self, scheduler: "Scheduler") -> None:
        """注入 Scheduler 实例"""
        self._scheduler = scheduler

    def intercept_learn_request(self, conversation_id: str, text: str) -> RouteResult | None:
        """检查是否有待确认的学习请求，有则返回 LEARN RouteResult

        在 adapter 层路由前调用，优先级仅次于 SkillSession 拦截。
        """
        if conversation_id in self._pending_learn_requests:
            return RouteResult(
                route_type=RouteType.LEARN,
                params={"original_text": text},
                confidence=0.95,
            )
        return None

    async def classify_llm_message(self, text: str) -> LlmClassifyResult:
        """对 LLM 路由消息做三段式前两步分类（启发式 → Skill 匹配）

        供 adapter 层决定响应方式：闲聊走流式，Skill匹配/学习确认走一次性返回。
        """
        # 第一段：启发式判断
        if not self._might_need_action(text):
            return LlmClassifyResult(classification=LlmClassification.CHITCHAT)

        # 第二段：LLM 精确分类（一次调用完成"需不需要Skill + 匹配哪个"）
        return await self._llm_classify(text)

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
        elif route.route_type == RouteType.LEARN:
            return await self._handle_learn_confirm(route, event)
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
        params, task_context = self._split_params_and_context(route.params, event)
        task = Task(
            conversation_id=event.conversation_id,
            user_id=event.user_id,
            origin_message=event.payload.get("text", ""),
            skill_name=skill_name,
            context=task_context,
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
                payload=task_context,
            )
        )

        approval_reply = await self._review_or_schedule_task(task, params, event, skill_meta)
        if approval_reply:
            return approval_reply

        return f"收到！任务已创建 [{task.task_id[:8]}] Skill: {skill_name}"

    def _split_params_and_context(
        self,
        params: dict[str, Any],
        event: Event,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """拆分 Skill 参数和运行上下文，避免调度/审批元数据污染 Skill 入参。"""
        run_params = dict(params or {})
        context = dict(run_params.pop("_context", {}) or {})
        for key in ("schedule_id", "run_id", "schedule_name", "cron", "is_retry"):
            if key in event.payload and key not in context:
                context[key] = event.payload[key]
        return run_params, context

    async def _review_or_schedule_task(
        self,
        task: Task,
        params: dict[str, Any],
        event: Event,
        skill_meta: SkillMetadata | None,
    ) -> str | None:
        """审批通过则调度执行；等待审批/拒绝则返回用户可见提示。"""
        if self._approval_engine is None:
            self._schedule_task_execution(task, params, event)
            return None

        result = await self._approval_engine.review(task, metadata=skill_meta)
        if result.approved:
            self._schedule_task_execution(task, params, event)
            return None

        if result.status.value == "pending" and result.approval_id:
            task.context["approval_id"] = result.approval_id
            self._state_machine.transition(task, TaskStatus.PENDING_APPROVAL)
            self._task_queue.update(task)
            self._pending_approvals[result.approval_id] = (task, params, event)
            return (
                f"任务 [{task.task_id[:8]}] 等待审批，审批 ID: {result.approval_id[:8]}。"
                "批准后会自动继续执行。"
            )

        task.error = result.reason or "approval_rejected"
        self._state_machine.transition(task, TaskStatus.FAILED)
        self._task_queue.update(task)
        await self._emit_failure(task, task.error)
        return f"任务 [{task.task_id[:8]}] 未执行：{task.error}"

    def _schedule_task_execution(self, task: Task, params: dict[str, Any], event: Event) -> None:
        """创建 asyncio task 并登记运行中任务。"""
        asyncio_task = asyncio.create_task(self._execute_task(task, params, event))
        self._running_tasks[task.task_id] = asyncio_task

    async def _on_approval_resolved(self, event: Event) -> None:
        """审批完成后恢复或失败挂起任务。"""
        approval_id = event.payload.get("approval_id")
        if not approval_id:
            return
        pending = self._pending_approvals.pop(approval_id, None)
        if not pending:
            return

        task, params, original_event = pending
        approved = bool(event.payload.get("approved"))
        if approved:
            self._schedule_task_execution(task, params, original_event)
            return

        reason = event.payload.get("reason") or event.payload.get("status") or "approval_rejected"
        task.error = str(reason)
        try:
            self._state_machine.transition(task, TaskStatus.FAILED)
        except TransitionError:
            pass
        self._task_queue.update(task)
        await self._emit_failure(task, str(reason))

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
                            payload=self._payload_with_context(task, {"reason": "cancelled_by_user"}),
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

                # 发射 task_completed 事件（payload 含文件元信息和运行上下文）
                payload = (result or {}).copy()
                if files_info:
                    payload["_files"] = files_info
                payload = self._payload_with_context(task, payload)
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
                payload=self._payload_with_context(task, {"error": reason}),
            )
        )

    def _payload_with_context(self, task: Task, payload: dict[str, Any]) -> dict[str, Any]:
        """把 Task.context 展开到事件 payload，调度/通知依赖顶层字段。"""
        merged = dict(payload)
        if task.context:
            merged.update({k: v for k, v in task.context.items() if k not in merged})
            merged["_context"] = dict(task.context)
        return merged

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
        _, task_context = self._split_params_and_context(route.params, event)
        if task_context:
            initial_params["_context"] = task_context

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

    # ─── LLM 兜底（三段式改造） ────────────────────────────────

    # 启发式信号词：判断用户消息是否可能需要执行动作
    _ACTION_SIGNALS = (
        # "帮我" 是所有"帮我X"的前缀，子串匹配即可覆盖
        "帮我",
        "每天", "每周", "每月", "定时", "自动", "监控",
        "抓取", "爬取", "下载", "截图", "截屏",
        "生成", "转换", "压缩", "批量", "整理",
        "浏览器", "打开网页", "网页操作", "填表",
        "发送消息", "推送", "通知",
        "写一个", "做一个", "搞一个", "来一个",
    )

    def _get_llm(self) -> LLMClient:
        """延迟初始化 LLM 客户端"""
        if self._llm is None:
            self._llm = LLMFactory.create(self._config)
        return self._llm

    def _might_need_action(self, text: str) -> bool:
        """极简启发式：判断消息是否可能需要执行动作（而非闲聊）

        误判可接受——最坏情况多问一句"要不要创建"。
        """
        t = text.lower().strip()
        if len(t) < 4:
            return False
        for signal in self._ACTION_SIGNALS:
            if signal in t:
                return True
        return False

    async def _llm_classify(self, text: str) -> LlmClassifyResult:
        """用 LLM 一次性判断：闲聊 / Skill匹配 / 需要学习

        替代原来的 _is_pure_creative_task + _match_skill 两段式判断，
        让 LLM 同时理解"需不需要外部数据/动作"和"匹配哪个 Skill"。

        Returns:
            LlmClassifyResult: 三种分类之一
        """
        skills = self._vault_manager.list_skills()

        # 无 Skill 可匹配 → 直接 NEEDS_LEARN
        if not skills:
            return LlmClassifyResult(classification=LlmClassification.NEEDS_LEARN)

        # 构建 Skill 清单
        skill_catalog = self._build_skill_catalog(skills)

        prompt = f"""判断用户消息应该由什么方式处理。

已有 Skill 清单：
{skill_catalog}

用户消息：{text}

分类规则：
1. 如果用户消息是闲聊、问答、创意写作（写诗/写故事/写文案/翻译/润色/总结等纯文本生成），不需要外部数据或工具操作 → 回复 CHITCHAT
2. 如果某个已有 Skill 能完全满足用户需求 → 回复 SKILL:skill_name
3. 如果用户需要执行动作/获取外部数据，但没有合适的 Skill → 回复 NEEDS_LEARN

关键判断标准——"需不需要外部数据/动作"：
- "写一首诗""讲个笑话""翻译一下""润色这段话""总结一下" → CHITCHAT（LLM 直接生成）
- "查天气""抓取网页""生成图片""监控价格""每天推送" → 需要外部数据/动作
- "打开网页"/"浏览器自动化"类 Skill 只能打开页面和操作浏览器，不能获取、抓取、分析页面内容
- 不确定时优先选 CHITCHAT

只回复分类结果（CHITCHAT / SKILL:skill_name:confidence / NEEDS_LEARN），不要解释。
confidence 是 0.0-1.0 的小数；只有非常确定时才高于 0.7。"""

        try:
            llm = self._get_llm()
            response = await llm.chat(
                [
                    {"role": "system", "content": "你是消息分类器。只回复分类结果：CHITCHAT、SKILL:skill_name:confidence、或 NEEDS_LEARN。不要解释。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=30,
            )
            result = response.strip()
            logger.info("llm_classify_result", input=text[:50], output=result)

            # 解析 CHITCHAT
            if "CHITCHAT" in result.upper():
                return LlmClassifyResult(classification=LlmClassification.CHITCHAT)

            # 解析 SKILL:xxx
            if result.upper().startswith("SKILL:"):
                parts = result.split(":")
                skill_name = parts[1].strip().lower() if len(parts) > 1 else ""
                confidence = 0.6
                if len(parts) > 2:
                    try:
                        confidence = max(0.0, min(1.0, float(parts[2].strip())))
                    except ValueError:
                        confidence = 0.6
                matched = self._verify_skill_name(skill_name, skills)
                if matched:
                    return LlmClassifyResult(
                        classification=LlmClassification.SKILL_MATCHED,
                        skill_name=matched,
                        confidence=confidence,
                    )
                # LLM 返回了不存在的 Skill → 降级为 NEEDS_LEARN
                logger.warning("llm_classify_skill_not_found", llm_returned=skill_name)
                return LlmClassifyResult(classification=LlmClassification.NEEDS_LEARN)

            # 解析 NEEDS_LEARN
            if "LEARN" in result.upper():
                return LlmClassifyResult(classification=LlmClassification.NEEDS_LEARN)

            # 无法解析 → 安全降级为 CHITCHAT
            logger.warning("llm_classify_unparseable", raw=result)
            return LlmClassifyResult(classification=LlmClassification.CHITCHAT)

        except Exception as e:
            # LLM 调用失败 → 安全降级为 CHITCHAT
            logger.warning("llm_classify_failed", error=str(e))
            return LlmClassifyResult(classification=LlmClassification.CHITCHAT)

    def _build_skill_catalog(self, skills: list) -> str:
        """构建 Skill 清单摘要"""
        skill_lines = []
        for s in skills:
            tags = ", ".join(s.intent_tags) if s.intent_tags else ""
            triggers = ", ".join(s.trigger_words[:3]) if s.trigger_words else ""
            skill_lines.append(
                f"- {s.name}: {s.description} | 触发词: {triggers} | 标签: {tags}"
            )
        return "\n".join(skill_lines)

    def _verify_skill_name(self, name: str, skills: list) -> str | None:
        """验证 LLM 返回的 skill_name 是否真实存在"""
        for s in skills:
            if s.name == name:
                return s.name
        # 模糊匹配：返回的可能是别名
        for s in skills:
            if name in s.name or name in [a.lower() for a in s.aliases]:
                return s.name
        return None


    async def _match_skill(self, text: str) -> str | None:
        """[废弃] 用 LLM 模糊匹配本地 Skill 清单，请使用 _llm_classify 代替

        保留此方法以兼容可能的调用方，内部委托给 _llm_classify。
        """
        result = await self._llm_classify(text)
        if result.classification == LlmClassification.SKILL_MATCHED:
            return result.skill_name
        return None

    async def _handle_llm(self, route: RouteResult, event: Event) -> str:
        """LLM 兜底响应（两段式：启发式 → LLM 分类）"""
        text = route.params.get("original_text", "")

        # ── 第一段：启发式快速短路 ──
        if not self._might_need_action(text):
            return await self._chat_fallback(text)

        # ── 第二段：LLM 精确分类 ──
        classify = await self._llm_classify(text)
        return await self.handle_llm_classification(classify, text, event)

    async def handle_llm_classification(
        self,
        classify: LlmClassifyResult,
        text: str,
        event: Event,
    ) -> str:
        """按统一的 LLM fallback 分类结果响应，供 /message 和 /chat/stream 共用。"""
        conv_id = event.conversation_id
        if classify.classification == LlmClassification.CHITCHAT:
            return await self._chat_fallback(text)

        if classify.classification == LlmClassification.SKILL_MATCHED:
            threshold = getattr(self._config, "llm_skill_match_threshold", 0.7)
            if classify.confidence < threshold:
                logger.info(
                    "llm_fallback_low_confidence",
                    skill=classify.skill_name,
                    confidence=classify.confidence,
                    threshold=threshold,
                )
                self._pending_learn_requests[conv_id] = text
                return (
                    f"我不太确定「{text[:30]}」该不该交给 "
                    f"{classify.skill_name or '某个 Skill'} 处理。\n\n"
                    "要我按新技能需求来学习/创建吗？回复「要」继续，回复「不要」我就正常聊天。"
                )

            logger.info("llm_fallback_matched_skill", skill=classify.skill_name)
            skill_route = RouteResult(
                route_type=RouteType.SKILL,
                skill_name=classify.skill_name,
                params={"rest": text},
                confidence=classify.confidence,
            )
            return await self._handle_skill(skill_route, event)

        # NEEDS_LEARN → 问用户要不要创建
        self._pending_learn_requests[conv_id] = text
        return (
            f"🤔 我目前没有能处理「{text[:30]}」的技能。\n\n"
            "需要我帮你创建一个新技能吗？回复「要」我就开始学习，回复「不要」就正常聊天。"
        )

    async def decide_llm_stream(self, text: str, event: Event) -> LlmStreamDecision:
        """给 /chat/stream 使用的统一 LLM fallback 决策。"""
        classify = await self.classify_llm_message(text)
        if classify.classification == LlmClassification.CHITCHAT:
            return LlmStreamDecision(stream_chat=True)

        reply = await self.handle_llm_classification(classify, text, event)
        if classify.classification == LlmClassification.SKILL_MATCHED:
            return LlmStreamDecision(
                stream_chat=False,
                reply=reply,
                source="skill_matched",
                skill_name=classify.skill_name or "",
            )
        return LlmStreamDecision(
            stream_chat=False,
            reply=reply,
            source="learn_confirm",
        )

    async def _handle_learn_confirm(self, route: RouteResult, event: Event) -> str:
        """处理用户对学习请求的确认/拒绝"""
        conv_id = event.conversation_id
        user_text = event.payload.get("text", "").strip().lower()
        original_request = self._pending_learn_requests.pop(conv_id, None)

        if not original_request:
            # 没有待确认的请求，当作普通消息处理
            return await self._chat_fallback(event.payload.get("text", ""))

        # 判断用户是否确认
        confirm_words = {"要", "好", "可以", "行", "是的", "是的", "创建", "学习", "帮我创建", "yes", "y", "ok", "确定", "确认"}
        reject_words = {"不要", "不用", "算了", "取消", "no", "n", "否", "不了", "别"}

        if user_text in reject_words:
            return await self._chat_fallback(event.payload.get("text", ""))

        if user_text not in confirm_words:
            # 模糊回复，当作拒绝，走闲聊
            return await self._chat_fallback(event.payload.get("text", ""))

        # ── 用户确认：走 LearningEngine ──
        if not self._learning_engine:
            return "⚠️ 学习引擎未初始化，无法创建新技能。请联系管理员配置。"

        try:
            task = Task(
                conversation_id=conv_id,
                user_id=event.user_id,
                origin_message=original_request,
                skill_name="learning",
            )

            result = await self._learning_engine.learn_and_schedule(
                task=task,
                user_message=original_request,
                conversation_id=conv_id,
            )

            reply = result.get("reply", "✅ 学习完成")
            if result.get("schedule_created"):
                reply += f"\n⏰ 已创建定时任务：{result['schedule_created']}"
            return reply

        except Exception as e:
            logger.error("learn_confirm_failed", error=str(e))
            return f"❌ 学习过程出错：{e}"

    async def _chat_fallback(self, text: str) -> str:
        """纯闲聊兜底"""
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
