"""工作流执行引擎

核心功能：
1. 按 WorkflowEntry.steps 逐步执行，步骤间数据传递
2. 支持 input_from（从第几步输出取输入）和 condition（条件分支）
3. 每步复用 Router → Executor 流程
4. LLM 驱动的任务分解（通过 Planner 升级）
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.router.router import Router
from src.executor.agent import Executor
from src.types import Event, WorkflowEntry, WorkflowStep
from src.workflow.workflow_store import WorkflowStore

logger = structlog.get_logger(__name__)


class WorkflowExecution:
    """一次工作流执行的上下文"""

    def __init__(self, workflow: WorkflowEntry, conversation_id: str) -> None:
        self.execution_id = str(uuid.uuid4())[:8]
        self.workflow = workflow
        self.conversation_id = conversation_id
        self.step_results: dict[int, Any] = {}  # step_id → output
        self.current_step = 0
        self.status = "running"  # running / completed / failed / cancelled
        self.error: str | None = None
        self.started_at = datetime.now(timezone.utc)
        self.finished_at: datetime | None = None


class WorkflowEngine:
    """工作流执行引擎"""

    def __init__(
        self,
        event_bus: EventBus,
        router: Router,
        executor: Executor,
        store: WorkflowStore,
        config: RaccoonConfig | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._router = router
        self._executor = executor
        self._store = store
        self._config = config or RaccoonConfig()
        self._executions: dict[str, WorkflowExecution] = {}

    # ─── 公开接口 ─────────────────────────────────────────────

    async def execute(
        self,
        workflow: WorkflowEntry,
        conversation_id: str | None = None,
        initial_params: dict | None = None,
    ) -> WorkflowExecution:
        """执行工作流"""
        conv_id = conversation_id or str(uuid.uuid4())
        ctx = WorkflowExecution(workflow=workflow, conversation_id=conv_id)
        self._executions[ctx.execution_id] = ctx

        # 发射工作流开始事件
        await self._event_bus.emit(
            make_event(
                EventType.TASK_PROGRESS,
                conversation_id=conv_id,
                payload={
                    "type": "workflow_started",
                    "execution_id": ctx.execution_id,
                    "workflow_name": workflow.name,
                    "total_steps": len(workflow.steps),
                },
            )
        )

        try:
            for step in workflow.steps:
                if ctx.status != "running":
                    break
                await self._execute_step(ctx, step, initial_params or {})

            if ctx.status == "running":
                ctx.status = "completed"

        except Exception as e:
            ctx.status = "failed"
            ctx.error = str(e)
            logger.error("workflow_execution_failed", execution_id=ctx.execution_id, error=str(e))

        finally:
            ctx.finished_at = datetime.now(timezone.utc)

            # 发射工作流完成事件
            await self._event_bus.emit(
                make_event(
                    EventType.TASK_COMPLETED if ctx.status == "completed" else EventType.TASK_FAILED,
                    conversation_id=conv_id,
                    payload={
                        "type": "workflow_finished",
                        "execution_id": ctx.execution_id,
                        "workflow_name": workflow.name,
                        "status": ctx.status,
                        "error": ctx.error,
                        "step_results": {str(k): str(v)[:200] for k, v in ctx.step_results.items()},
                    },
                )
            )

        return ctx

    async def execute_by_id(
        self,
        workflow_id: str,
        conversation_id: str | None = None,
        initial_params: dict | None = None,
    ) -> WorkflowExecution | None:
        """按 ID 执行工作流"""
        workflow = self._store.get(workflow_id)
        if not workflow:
            logger.warning("workflow_not_found", workflow_id=workflow_id)
            return None
        return await self.execute(workflow, conversation_id, initial_params)

    async def execute_by_name(
        self,
        name: str,
        conversation_id: str | None = None,
        initial_params: dict | None = None,
    ) -> WorkflowExecution | None:
        """按名称执行工作流"""
        workflow = self._store.get_by_name(name)
        if not workflow:
            logger.warning("workflow_not_found_by_name", name=name)
            return None
        return await self.execute(workflow, conversation_id, initial_params)

    def get_execution(self, execution_id: str) -> WorkflowExecution | None:
        """获取执行上下文"""
        return self._executions.get(execution_id)

    def list_executions(self) -> list[WorkflowExecution]:
        """列出所有执行"""
        return list(self._executions.values())

    # ─── 步骤执行 ─────────────────────────────────────────────

    async def _execute_step(
        self,
        ctx: WorkflowExecution,
        step: WorkflowStep,
        initial_params: dict,
    ) -> None:
        """执行单个步骤"""
        # 检查条件
        if step.condition:
            prev_step = ctx.step_results.get(step.step_id - 1)
            if step.condition == "on_success" and ctx.status != "running":
                logger.debug("workflow_step_skipped_condition", step_id=step.step_id, condition=step.condition)
                return
            if step.condition == "on_failure":
                # 只有前一步失败时才执行
                prev_result = ctx.step_results.get(step.step_id - 1)
                if prev_result is not None:
                    logger.debug("workflow_step_skipped_condition", step_id=step.step_id, condition=step.condition)
                    return

        # 构建步骤参数
        params = dict(step.params)
        params.update(initial_params)

        # 从前序步骤获取输入
        if step.input_from is not None:
            prev_output = ctx.step_results.get(step.input_from)
            if prev_output is not None:
                params["_input"] = prev_output
                if isinstance(prev_output, dict):
                    params.update(prev_output)

        # 发射步骤开始事件
        await self._event_bus.emit(
            make_event(
                EventType.TASK_PROGRESS,
                conversation_id=ctx.conversation_id,
                payload={
                    "type": "workflow_step_started",
                    "execution_id": ctx.execution_id,
                    "step_id": step.step_id,
                    "step_type": step.type,
                },
            )
        )

        try:
            result = None

            if step.type == "skill" and step.skill_name:
                result = await self._execute_skill_step(ctx, step, params)
            elif step.type == "llm":
                result = await self._execute_llm_step(ctx, step, params)
            elif step.type == "system":
                result = await self._execute_system_step(ctx, step, params)
            else:
                logger.warning("workflow_step_unknown_type", step_id=step.step_id, type=step.type)
                result = {"error": f"unknown step type: {step.type}"}

            ctx.step_results[step.step_id] = result
            ctx.current_step = step.step_id

            logger.debug("workflow_step_completed", execution_id=ctx.execution_id, step_id=step.step_id)

        except Exception as e:
            ctx.step_results[step.step_id] = {"error": str(e)}
            logger.error("workflow_step_failed", execution_id=ctx.execution_id, step_id=step.step_id, error=str(e))
            raise

    async def _execute_skill_step(
        self, ctx: WorkflowExecution, step: WorkflowStep, params: dict
    ) -> Any:
        """执行 Skill 步骤"""
        from src.types import RouteResult, RouteType

        route = RouteResult(
            route_type=RouteType.SKILL,
            skill_name=step.skill_name,
            params=params,
        )
        event = make_event(
            EventType.USER_MESSAGE,
            conversation_id=ctx.conversation_id,
            payload={"text": step.params.get("text", f"执行工作流步骤: {step.skill_name}")},
        )
        reply = await self._executor.handle_route_result(route, event)
        return {"reply": reply}

    async def _execute_llm_step(
        self, ctx: WorkflowExecution, step: WorkflowStep, params: dict
    ) -> Any:
        """执行 LLM 步骤"""
        from src.types import RouteResult, RouteType

        text = params.get("text", step.params.get("text", ""))
        route = RouteResult(
            route_type=RouteType.LLM,
            params={"original_text": text, **params},
        )
        event = make_event(
            EventType.USER_MESSAGE,
            conversation_id=ctx.conversation_id,
            payload={"text": text},
        )
        reply = await self._executor.handle_route_result(route, event)
        return {"reply": reply}

    async def _execute_system_step(
        self, ctx: WorkflowExecution, step: WorkflowStep, params: dict
    ) -> Any:
        """执行系统指令步骤"""
        from src.types import RouteResult, RouteType

        command = step.params.get("command", "")
        route = RouteResult(
            route_type=RouteType.SYSTEM,
            params={"command": command, **params},
        )
        event = make_event(
            EventType.USER_MESSAGE,
            conversation_id=ctx.conversation_id,
            payload={"text": f"/{command}"},
        )
        reply = await self._executor.handle_route_result(route, event)
        return {"reply": reply}
