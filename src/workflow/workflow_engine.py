"""工作流执行引擎

核心功能：
1. 按 WorkflowEntry.steps 逐步执行，步骤间数据传递
2. 支持 input_from（从第几步输出取输入）和 condition（条件分支）
3. 支持 if/else、循环、并行步骤（v0.3.4）
4. LLM 驱动的任务分解（通过 Planner 集成）
5. 中间状态持久化（崩溃可恢复）
6. 工作流模板市场（预置模板）
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.router.router import Router
from src.executor.agent import Executor
from src.types import WorkflowEntry, WorkflowStep
from src.workflow.workflow_store import WorkflowStore

logger = structlog.get_logger(__name__)


class WorkflowExecution:
    """一次工作流执行的上下文"""

    def __init__(self, workflow: WorkflowEntry, conversation_id: str) -> None:
        self.execution_id = str(uuid.uuid4())[:8]
        self.workflow = workflow
        self.conversation_id = conversation_id
        self.step_results: dict[int, Any] = {}
        self.current_step = 0
        self.status = "running"
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
        self._planner = None

    def _get_planner(self):
        if self._planner is None:
            from src.brain.planner import Planner
            self._planner = Planner(self._config)
        return self._planner

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

        self._store.save_execution_state(
            execution_id=ctx.execution_id,
            workflow_id=workflow.workflow_id,
            conversation_id=conv_id,
            current_step=0,
            status="running",
            step_results={},
            started_at=ctx.started_at.isoformat(),
        )

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
            await self._execute_steps(ctx, workflow.steps, initial_params or {})
            if ctx.status == "running":
                ctx.status = "completed"
        except Exception as e:
            ctx.status = "failed"
            ctx.error = str(e)
            logger.error("workflow_execution_failed", execution_id=ctx.execution_id, error=str(e))
        finally:
            ctx.finished_at = datetime.now(timezone.utc)
            self._store.save_execution_state(
                execution_id=ctx.execution_id,
                workflow_id=workflow.workflow_id,
                conversation_id=conv_id,
                current_step=ctx.current_step,
                status=ctx.status,
                step_results=ctx.step_results,
                error=ctx.error,
                started_at=ctx.started_at.isoformat(),
                finished_at=ctx.finished_at.isoformat() if ctx.finished_at else None,
            )
            await self._emit_finish_event(ctx)

        return ctx

    async def execute_from_decomposition(
        self,
        text: str,
        conversation_id: str | None = None,
        max_steps: int = 5,
    ) -> WorkflowExecution | None:
        """LLM 自动分解任务并执行（#1: Planner 集成）"""
        planner = self._get_planner()
        plan = await planner.decompose(text, max_steps=max_steps)

        if plan.is_single_step():
            logger.info("workflow_single_step_skip", text=text[:50])
            return None

        workflow_steps = plan.to_workflow_steps()
        workflow = WorkflowEntry(
            name=f"auto_decompose_{text[:20]}",
            description=f"LLM 自动分解: {text[:100]}",
            steps=workflow_steps,
        )
        self._store.add(workflow)
        logger.info("workflow_auto_decomposed", text=text[:50], steps_count=len(workflow_steps))
        return await self.execute(workflow, conversation_id)

    async def resume_execution(self, execution_id: str) -> WorkflowExecution | None:
        """从崩溃中恢复执行（#4: 崩溃恢复）"""
        state = self._store.get_execution_state(execution_id)
        if not state or state["status"] != "running":
            logger.warning("workflow_resume_no_state", execution_id=execution_id)
            return None

        workflow = self._store.get(state["workflow_id"])
        if not workflow:
            logger.warning("workflow_resume_no_workflow", execution_id=execution_id)
            return None

        ctx = WorkflowExecution(workflow=workflow, conversation_id=state["conversation_id"])
        ctx.execution_id = execution_id
        ctx.current_step = state["current_step"]
        try:
            saved_results = json.loads(state["step_results"])
            ctx.step_results = {int(k): v for k, v in saved_results.items()}
        except (json.JSONDecodeError, TypeError):
            ctx.step_results = {}

        self._executions[ctx.execution_id] = ctx
        logger.info("workflow_resuming", execution_id=execution_id, from_step=ctx.current_step)

        remaining_steps = workflow.steps[ctx.current_step:]
        try:
            await self._execute_steps(ctx, remaining_steps, {})
            if ctx.status == "running":
                ctx.status = "completed"
        except Exception as e:
            ctx.status = "failed"
            ctx.error = str(e)

        ctx.finished_at = datetime.now(timezone.utc)
        self._store.save_execution_state(
            execution_id=ctx.execution_id,
            workflow_id=workflow.workflow_id,
            conversation_id=ctx.conversation_id,
            current_step=ctx.current_step,
            status=ctx.status,
            step_results=ctx.step_results,
            error=ctx.error,
            started_at=state["started_at"],
            finished_at=ctx.finished_at.isoformat(),
        )
        return ctx

    async def execute_by_id(
        self, workflow_id: str, conversation_id: str | None = None, initial_params: dict | None = None,
    ) -> WorkflowExecution | None:
        workflow = self._store.get(workflow_id)
        if not workflow:
            logger.warning("workflow_not_found", workflow_id=workflow_id)
            return None
        return await self.execute(workflow, conversation_id, initial_params)

    async def execute_by_name(
        self, name: str, conversation_id: str | None = None, initial_params: dict | None = None,
    ) -> WorkflowExecution | None:
        workflow = self._store.get_by_name(name)
        if not workflow:
            logger.warning("workflow_not_found_by_name", name=name)
            return None
        return await self.execute(workflow, conversation_id, initial_params)

    def get_execution(self, execution_id: str) -> WorkflowExecution | None:
        return self._executions.get(execution_id)

    def list_executions(self) -> list[WorkflowExecution]:
        return list(self._executions.values())

    # ─── 步骤执行 ─────────────────────────────────────────────

    async def _execute_steps(
        self, ctx: WorkflowExecution, steps: list[WorkflowStep], initial_params: dict,
    ) -> None:
        for step in steps:
            if ctx.status != "running":
                break
            await self._execute_step(ctx, step, initial_params)

    async def _execute_step(
        self, ctx: WorkflowExecution, step: WorkflowStep, initial_params: dict,
    ) -> None:
        # 检查 condition
        if step.condition:
            if step.condition == "on_success" and ctx.status != "running":
                return
            if step.condition == "on_failure":
                prev_result = ctx.step_results.get(step.step_id - 1)
                if prev_result is not None and not (isinstance(prev_result, dict) and prev_result.get("error")):
                    return

        # if/else 分支
        if step.type == "if":
            await self._execute_if_step(ctx, step, initial_params)
            return

        # 循环
        if step.type == "loop":
            await self._execute_loop_step(ctx, step, initial_params)
            return

        # 并行
        if step.type == "parallel":
            await self._execute_parallel_step(ctx, step, initial_params)
            return

        # 构建参数
        params = dict(step.params)
        params.update(initial_params)
        if step.input_from is not None:
            prev_output = ctx.step_results.get(step.input_from)
            if prev_output is not None:
                params["_input"] = prev_output
                if isinstance(prev_output, dict):
                    params.update(prev_output)

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

            # 每步持久化中间状态
            self._store.save_execution_state(
                execution_id=ctx.execution_id,
                workflow_id=ctx.workflow.workflow_id,
                conversation_id=ctx.conversation_id,
                current_step=ctx.current_step,
                status=ctx.status,
                step_results=ctx.step_results,
                started_at=ctx.started_at.isoformat(),
            )

        except Exception as e:
            ctx.step_results[step.step_id] = {"error": str(e)}
            logger.error("workflow_step_failed", execution_id=ctx.execution_id, step_id=step.step_id, error=str(e))
            raise

    # ─── if/else ──────────────────────────────────────────────

    async def _execute_if_step(
        self, ctx: WorkflowExecution, step: WorkflowStep, initial_params: dict,
    ) -> None:
        condition_met = self._evaluate_condition(step.if_condition, ctx)
        if condition_met and step.then_steps:
            await self._execute_steps(ctx, step.then_steps, initial_params)
        elif not condition_met and step.else_steps:
            await self._execute_steps(ctx, step.else_steps, initial_params)
        ctx.step_results[step.step_id] = {"branch": "then" if condition_met else "else"}
        ctx.current_step = step.step_id

    # ─── 循环 ─────────────────────────────────────────────────

    async def _execute_loop_step(
        self, ctx: WorkflowExecution, step: WorkflowStep, initial_params: dict,
    ) -> None:
        items = self._resolve_loop_items(step.loop_over, ctx)
        if not items:
            ctx.step_results[step.step_id] = {"loop_result": [], "iterations": 0}
            ctx.current_step = step.step_id
            return

        loop_results = []
        for i, item in enumerate(items):
            if i >= step.max_iterations:
                logger.warning("workflow_loop_max_iterations", step_id=step.step_id)
                break
            if ctx.status != "running":
                break

            loop_params = dict(initial_params)
            if step.loop_var:
                loop_params[step.loop_var] = item
            loop_params["_loop_index"] = i

            if step.loop_steps:
                await self._execute_steps(ctx, step.loop_steps, loop_params)
                last_step = step.loop_steps[-1] if step.loop_steps else None
                if last_step and last_step.step_id in ctx.step_results:
                    loop_results.append(ctx.step_results[last_step.step_id])

        ctx.step_results[step.step_id] = {"loop_result": loop_results, "iterations": len(loop_results)}
        ctx.current_step = step.step_id

    # ─── 并行 ─────────────────────────────────────────────────

    async def _execute_parallel_step(
        self, ctx: WorkflowExecution, step: WorkflowStep, initial_params: dict,
    ) -> None:
        if not step.parallel_steps:
            ctx.step_results[step.step_id] = {"parallel_result": []}
            ctx.current_step = step.step_id
            return

        async def _run_branch(branch_step: WorkflowStep, idx: int) -> tuple[int, Any]:
            params = dict(branch_step.params)
            params.update(initial_params)
            if branch_step.input_from is not None:
                prev_output = ctx.step_results.get(branch_step.input_from)
                if prev_output is not None:
                    params["_input"] = prev_output

            if branch_step.type == "skill" and branch_step.skill_name:
                result = await self._execute_skill_step(ctx, branch_step, params)
            elif branch_step.type == "llm":
                result = await self._execute_llm_step(ctx, branch_step, params)
            elif branch_step.type == "system":
                result = await self._execute_system_step(ctx, branch_step, params)
            else:
                result = {"error": f"unknown step type: {branch_step.type}"}
            return (idx, result)

        tasks = [_run_branch(s, i) for i, s in enumerate(step.parallel_steps)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        parallel_results = []
        for r in results:
            if isinstance(r, Exception):
                parallel_results.append({"error": str(r)})
            else:
                parallel_results.append(r[1])

        ctx.step_results[step.step_id] = {"parallel_result": parallel_results}
        ctx.current_step = step.step_id

    # ─── 条件评估 ─────────────────────────────────────────────

    def _evaluate_condition(self, condition: str | None, ctx: WorkflowExecution) -> bool:
        """评估 if 条件表达式

        支持: "step_N.success" / "step_N.error" / "step_N.exists"
        """
        if not condition:
            return True

        condition = condition.strip()
        if condition.startswith("step_"):
            parts = condition.split(".")
            if len(parts) >= 2:
                try:
                    step_id = int(parts[0].replace("step_", ""))
                    result = ctx.step_results.get(step_id)
                    attr = parts[1]
                    if attr == "success":
                        return result is not None and not (isinstance(result, dict) and result.get("error"))
                    elif attr == "error":
                        return result is not None and isinstance(result, dict) and result.get("error")
                    elif attr == "exists":
                        return result is not None
                except (ValueError, IndexError):
                    pass

        # 简单比较
        for op in [">", "==", "<"]:
            if op in condition:
                try:
                    left, right = condition.split(op, 1)
                    left_val = self._resolve_value(left.strip(), ctx)
                    right_val = self._resolve_value(right.strip(), ctx)
                    if op == ">":
                        return float(left_val) > float(right_val)
                    elif op == "==":
                        return str(left_val) == str(right_val)
                    elif op == "<":
                        return float(left_val) < float(right_val)
                except (ValueError, TypeError):
                    pass

        return bool(condition)

    def _resolve_value(self, expr: str, ctx: WorkflowExecution) -> Any:
        """解析条件表达式中的值"""
        if expr.startswith("step_"):
            parts = expr.split(".")
            step_id = int(parts[0].replace("step_", ""))
            result = ctx.step_results.get(step_id)
            if result is None:
                return None
            if len(parts) > 2 and isinstance(result, dict):
                return result.get(parts[2])
            return result
        # 尝试数值
        try:
            return float(expr)
        except ValueError:
            return expr

    def _resolve_loop_items(self, loop_over: str | None, ctx: WorkflowExecution) -> list:
        """解析循环数据来源"""
        if not loop_over:
            return []

        if loop_over.startswith("step_"):
            parts = loop_over.split(".")
            try:
                step_id = int(parts[0].replace("step_", ""))
                result = ctx.step_results.get(step_id)
                if result is None:
                    return []
                if isinstance(result, dict) and len(parts) > 1:
                    # step_0.result.items → result.get("result", {}).get("items", [])
                    val = result
                    for key in parts[1:]:
                        if isinstance(val, dict):
                            val = val.get(key, [])
                        else:
                            return []
                    if isinstance(val, list):
                        return val
                elif isinstance(result, list):
                    return result
            except (ValueError, IndexError):
                pass

        return []

    # ─── 基础步骤执行 ─────────────────────────────────────────

    async def _execute_skill_step(
        self, ctx: WorkflowExecution, step: WorkflowStep, params: dict,
    ) -> Any:
        from src.types import RouteResult, RouteType
        route = RouteResult(route_type=RouteType.SKILL, skill_name=step.skill_name, params=params)
        event = make_event(
            EventType.USER_MESSAGE,
            conversation_id=ctx.conversation_id,
            payload={"text": step.params.get("text", f"执行工作流步骤: {step.skill_name}")},
        )
        reply = await self._executor.handle_route_result(route, event)
        return {"reply": reply}

    async def _execute_llm_step(
        self, ctx: WorkflowExecution, step: WorkflowStep, params: dict,
    ) -> Any:
        from src.types import RouteResult, RouteType
        text = params.get("text", step.params.get("text", ""))
        route = RouteResult(route_type=RouteType.LLM, params={"original_text": text, **params})
        event = make_event(EventType.USER_MESSAGE, conversation_id=ctx.conversation_id, payload={"text": text})
        reply = await self._executor.handle_route_result(route, event)
        return {"reply": reply}

    async def _execute_system_step(
        self, ctx: WorkflowExecution, step: WorkflowStep, params: dict,
    ) -> Any:
        from src.types import RouteResult, RouteType
        command = step.params.get("command", "")
        route = RouteResult(route_type=RouteType.SYSTEM, params={"command": command, **params})
        event = make_event(EventType.USER_MESSAGE, conversation_id=ctx.conversation_id, payload={"text": f"/{command}"})
        reply = await self._executor.handle_route_result(route, event)
        return {"reply": reply}

    # ─── 事件发射 ─────────────────────────────────────────────

    async def _emit_finish_event(self, ctx: WorkflowExecution) -> None:
        await self._event_bus.emit(
            make_event(
                EventType.TASK_COMPLETED if ctx.status == "completed" else EventType.TASK_FAILED,
                conversation_id=ctx.conversation_id,
                payload={
                    "type": "workflow_finished",
                    "execution_id": ctx.execution_id,
                    "workflow_name": ctx.workflow.name,
                    "status": ctx.status,
                    "error": ctx.error,
                    "step_results": {str(k): str(v)[:200] for k, v in ctx.step_results.items()},
                },
            )
        )