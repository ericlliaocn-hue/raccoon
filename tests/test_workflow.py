"""工作流编排测试"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.types import WorkflowEntry, WorkflowStep
from src.workflow.workflow_store import WorkflowStore
from src.workflow.workflow_engine import WorkflowEngine, WorkflowExecution
from src.workflow.workflow_templates import WorkflowTemplateMarket
from src.brain.planner import Plan, Planner


# ─── WorkflowStore 测试 ──────────────────────────────────────

class TestWorkflowStore:
    @pytest.fixture
    def store(self, tmp_path):
        cfg = RaccoonConfig(
            workflows_dir=tmp_path / "workflows",
            db_path=tmp_path / "test_workflow.db",
        )
        s = WorkflowStore(cfg)
        return s

    def test_add_and_get(self, store):
        entry = WorkflowEntry(name="test_wf", steps=[
            WorkflowStep(step_id=0, type="skill", skill_name="echo"),
        ])
        store.add(entry)
        got = store.get(entry.workflow_id)
        assert got is not None
        assert got.name == "test_wf"
        assert len(got.steps) == 1

    def test_get_not_found(self, store):
        assert store.get("nonexistent") is None

    def test_get_by_name(self, store):
        entry = WorkflowEntry(name="unique_name", steps=[])
        store.add(entry)
        got = store.get_by_name("unique_name")
        assert got is not None
        assert got.workflow_id == entry.workflow_id

    def test_get_by_name_not_found(self, store):
        assert store.get_by_name("no_such_name") is None

    def test_remove(self, store):
        entry = WorkflowEntry(name="to_remove", steps=[])
        store.add(entry)
        removed = store.remove(entry.workflow_id)
        assert removed is not None
        assert store.get(entry.workflow_id) is None

    def test_remove_not_found(self, store):
        assert store.remove("nonexistent") is None

    def test_list_all(self, store):
        for i in range(3):
            store.add(WorkflowEntry(name=f"wf_{i}", steps=[]))
        all_wf = store.list_all()
        assert len(all_wf) == 3

    def test_update(self, store):
        entry = WorkflowEntry(name="original", steps=[])
        store.add(entry)
        entry.name = "updated"
        store.update(entry)
        got = store.get(entry.workflow_id)
        assert got.name == "updated"

    def test_persistence(self, tmp_path):
        cfg = RaccoonConfig(
            workflows_dir=tmp_path / "workflows",
            db_path=tmp_path / "test_workflow.db",
        )
        store1 = WorkflowStore(cfg)
        entry = WorkflowEntry(name="persist_test", steps=[
            WorkflowStep(step_id=0, type="skill", skill_name="echo"),
        ])
        store1.add(entry)

        # 新 store 实例应能恢复
        store2 = WorkflowStore(cfg)
        got = store2.get(entry.workflow_id)
        assert got is not None
        assert got.name == "persist_test"

    def test_execution_state_crud(self, store):
        """测试执行状态持久化"""
        entry = WorkflowEntry(name="exec_test", steps=[])
        store.add(entry)

        store.save_execution_state(
            execution_id="exec_001",
            workflow_id=entry.workflow_id,
            conversation_id="conv_001",
            current_step=2,
            status="running",
            step_results={0: "ok", 1: "done"},
        )

        state = store.get_execution_state("exec_001")
        assert state is not None
        assert state["current_step"] == 2
        assert state["status"] == "running"

        # 更新状态
        store.save_execution_state(
            execution_id="exec_001",
            workflow_id=entry.workflow_id,
            conversation_id="conv_001",
            current_step=3,
            status="completed",
            step_results={0: "ok", 1: "done", 2: "final"},
        )

        state = store.get_execution_state("exec_001")
        assert state["status"] == "completed"

        # 清除
        store.clear_execution_state("exec_001")
        assert store.get_execution_state("exec_001") is None

    def test_pending_executions(self, store):
        """测试获取未完成执行"""
        entry = WorkflowEntry(name="pending_test", steps=[])
        store.add(entry)

        store.save_execution_state(
            execution_id="exec_p1",
            workflow_id=entry.workflow_id,
            conversation_id="conv_001",
            current_step=1,
            status="running",
            step_results={},
        )
        store.save_execution_state(
            execution_id="exec_p2",
            workflow_id=entry.workflow_id,
            conversation_id="conv_002",
            current_step=0,
            status="completed",
            step_results={},
        )

        pending = store.get_pending_executions()
        assert len(pending) == 1
        assert pending[0]["execution_id"] == "exec_p1"


# ─── WorkflowEngine 测试 ─────────────────────────────────────

class TestWorkflowEngine:
    @pytest.fixture
    def components(self, tmp_path):
        config = RaccoonConfig(db_path=tmp_path / "test_workflow.db")
        event_bus = EventBus(config)
        router = MagicMock()
        executor = AsyncMock()
        store = WorkflowStore(config)
        engine = WorkflowEngine(event_bus, router, executor, store, config)
        return engine, event_bus, router, executor, store

    @pytest.mark.asyncio
    async def test_execute_simple_workflow(self, components):
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="ok")

        workflow = WorkflowEntry(name="simple", steps=[
            WorkflowStep(step_id=0, type="system", params={"command": "help"}),
        ])
        ctx = await engine.execute(workflow)
        assert ctx.status == "completed"
        assert 0 in ctx.step_results

    @pytest.mark.asyncio
    async def test_execute_multi_step(self, components):
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="result")

        workflow = WorkflowEntry(name="multi", steps=[
            WorkflowStep(step_id=0, type="system", params={"command": "help"}),
            WorkflowStep(step_id=1, type="llm", params={"text": "总结"}),
        ])
        ctx = await engine.execute(workflow)
        assert ctx.status == "completed"
        assert len(ctx.step_results) == 2

    @pytest.mark.asyncio
    async def test_execute_with_input_from(self, components):
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="search_result")

        workflow = WorkflowEntry(name="chain", steps=[
            WorkflowStep(step_id=0, type="skill", skill_name="web_search", params={"query": "test"}),
            WorkflowStep(step_id=1, type="llm", params={"text": "总结"}, input_from=0),
        ])
        ctx = await engine.execute(workflow)
        assert ctx.status == "completed"
        assert len(ctx.step_results) == 2

    @pytest.mark.asyncio
    async def test_execute_by_id(self, components):
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="ok")

        workflow = WorkflowEntry(name="by_id", steps=[
            WorkflowStep(step_id=0, type="system", params={"command": "help"}),
        ])
        store.add(workflow)

        ctx = await engine.execute_by_id(workflow.workflow_id)
        assert ctx is not None
        assert ctx.status == "completed"

    @pytest.mark.asyncio
    async def test_execute_by_name(self, components):
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="ok")

        workflow = WorkflowEntry(name="by_name_test", steps=[])
        store.add(workflow)

        ctx = await engine.execute_by_name("by_name_test")
        assert ctx is not None

    @pytest.mark.asyncio
    async def test_execute_not_found(self, components):
        engine, event_bus, router, executor, store = components
        ctx = await engine.execute_by_id("nonexistent")
        assert ctx is None

    @pytest.mark.asyncio
    async def test_execute_by_name_not_found(self, components):
        engine, event_bus, router, executor, store = components
        ctx = await engine.execute_by_name("no_such_name")
        assert ctx is None

    @pytest.mark.asyncio
    async def test_get_execution(self, components):
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="ok")

        workflow = WorkflowEntry(name="get_exec", steps=[
            WorkflowStep(step_id=0, type="system", params={"command": "help"}),
        ])
        ctx = await engine.execute(workflow)
        got = engine.get_execution(ctx.execution_id)
        assert got is not None
        assert got.execution_id == ctx.execution_id

    @pytest.mark.asyncio
    async def test_list_executions(self, components):
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="ok")

        workflow = WorkflowEntry(name="list_exec", steps=[])
        await engine.execute(workflow)
        await engine.execute(workflow)
        assert len(engine.list_executions()) == 2

    # ─── v0.3.4 新增测试 ──────────────────────────────────────

    @pytest.mark.asyncio
    async def test_execute_if_else_true_branch(self, components):
        """测试 if/else 分支 — 条件为真走 then"""
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="ok")

        workflow = WorkflowEntry(name="if_test", steps=[
            WorkflowStep(step_id=0, type="system", params={"command": "help"}),
            WorkflowStep(step_id=1, type="if", if_condition="step_0.success",
                         then_steps=[
                             WorkflowStep(step_id=10, type="system", params={"command": "then_cmd"}),
                         ],
                         else_steps=[
                             WorkflowStep(step_id=11, type="system", params={"command": "else_cmd"}),
                         ]),
        ])
        ctx = await engine.execute(workflow)
        assert ctx.status == "completed"
        assert ctx.step_results[1]["branch"] == "then"

    @pytest.mark.asyncio
    async def test_execute_if_else_false_branch(self, components):
        """测试 if/else 分支 — 条件为假走 else"""
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="ok")

        workflow = WorkflowEntry(name="if_false_test", steps=[
            WorkflowStep(step_id=0, type="system", params={"command": "help"}),
            WorkflowStep(step_id=1, type="if", if_condition="step_0.error",
                         then_steps=[
                             WorkflowStep(step_id=10, type="system", params={"command": "then_cmd"}),
                         ],
                         else_steps=[
                             WorkflowStep(step_id=11, type="system", params={"command": "else_cmd"}),
                         ]),
        ])
        ctx = await engine.execute(workflow)
        assert ctx.status == "completed"
        assert ctx.step_results[1]["branch"] == "else"

    @pytest.mark.asyncio
    async def test_execute_loop(self, components):
        """测试循环步骤"""
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="item_result")

        # 先设置 step_0 的结果作为循环数据源
        workflow = WorkflowEntry(name="loop_test", steps=[
            WorkflowStep(step_id=0, type="system", params={"command": "help"}),
            WorkflowStep(step_id=1, type="loop", loop_var="item", loop_over="step_0.result.items",
                         loop_steps=[
                             WorkflowStep(step_id=10, type="skill", skill_name="echo", params={"text": "{item}"}),
                         ]),
        ])
        ctx = await engine.execute(workflow)
        assert ctx.status == "completed"
        # step_0 没有返回 items 列表，所以循环 0 次
        assert ctx.step_results[1]["iterations"] == 0

    @pytest.mark.asyncio
    async def test_execute_parallel(self, components):
        """测试并行步骤"""
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="parallel_result")

        workflow = WorkflowEntry(name="parallel_test", steps=[
            WorkflowStep(step_id=0, type="parallel",
                         parallel_steps=[
                             WorkflowStep(step_id=10, type="skill", skill_name="web_search", params={"query": "a"}),
                             WorkflowStep(step_id=11, type="skill", skill_name="web_search", params={"query": "b"}),
                         ]),
        ])
        ctx = await engine.execute(workflow)
        assert ctx.status == "completed"
        assert len(ctx.step_results[0]["parallel_result"]) == 2

    @pytest.mark.asyncio
    async def test_execution_state_persisted(self, components):
        """测试执行中间状态持久化"""
        engine, event_bus, router, executor, store = components
        executor.handle_route_result = AsyncMock(return_value="ok")

        workflow = WorkflowEntry(name="persist_exec", steps=[
            WorkflowStep(step_id=0, type="system", params={"command": "help"}),
        ])
        ctx = await engine.execute(workflow)

        # 验证执行状态已持久化
        state = store.get_execution_state(ctx.execution_id)
        assert state is not None
        assert state["status"] == "completed"


# ─── Planner 测试 ────────────────────────────────────────────

class TestPlanner:
    @pytest.mark.asyncio
    async def test_plan_skill(self):
        from src.types import RouteResult, RouteType
        planner = Planner()
        route = RouteResult(route_type=RouteType.SKILL, skill_name="echo", params={"text": "hi"})
        plan = await planner.plan(route)
        assert plan.is_single_step()
        assert plan.steps[0]["type"] == "skill"
        assert plan.steps[0]["skill_name"] == "echo"

    @pytest.mark.asyncio
    async def test_plan_system(self):
        from src.types import RouteResult, RouteType
        planner = Planner()
        route = RouteResult(route_type=RouteType.SYSTEM, params={"command": "help"})
        plan = await planner.plan(route)
        assert plan.is_single_step()
        assert plan.steps[0]["type"] == "system"

    @pytest.mark.asyncio
    async def test_plan_llm(self):
        from src.types import RouteResult, RouteType
        planner = Planner()
        route = RouteResult(route_type=RouteType.LLM, params={"original_text": "hello"})
        plan = await planner.plan(route)
        assert plan.is_single_step()
        assert plan.steps[0]["type"] == "llm"

    def test_plan_to_workflow_steps(self):
        plan = Plan(steps=[
            {"type": "skill", "skill_name": "echo", "params": {"text": "hi"}},
            {"type": "llm", "params": {"text": "summarize"}, "input_from": 0},
        ])
        ws = plan.to_workflow_steps()
        assert len(ws) == 2
        assert ws[0].step_id == 0
        assert ws[0].skill_name == "echo"
        assert ws[1].input_from == 0

    @pytest.mark.asyncio
    async def test_decompose_mock(self):
        planner = Planner(RaccoonConfig(llm_provider="mock"))
        plan = await planner.decompose("搜索天气并总结")
        assert len(plan.steps) >= 1

    def test_parse_decomposition_json_array(self):
        planner = Planner()
        reply = '[{"type": "skill", "skill_name": "web_search", "params": {"query": "天气"}, "input_from": null, "condition": null}]'
        plan = planner._parse_decomposition(reply, "test")
        assert len(plan.steps) == 1
        assert plan.steps[0]["skill_name"] == "web_search"

    def test_parse_decomposition_json_block(self):
        planner = Planner()
        reply = '```json\n[{"type": "skill", "skill_name": "echo", "params": {}}]\n```'
        plan = planner._parse_decomposition(reply, "test")
        assert len(plan.steps) == 1

    def test_parse_decomposition_invalid(self):
        planner = Planner()
        plan = planner._parse_decomposition("not json at all", "original text")
        assert len(plan.steps) == 1
        assert plan.steps[0]["type"] == "llm"

    def test_parse_decomposition_dict_with_steps(self):
        planner = Planner()
        reply = '{"steps": [{"type": "skill", "skill_name": "echo", "params": {}}]}'
        plan = planner._parse_decomposition(reply, "test")
        assert len(plan.steps) == 1


# ─── WorkflowStep 类型测试 ────────────────────────────────────

class TestWorkflowStep:
    def test_basic_step(self):
        step = WorkflowStep(step_id=0, type="skill", skill_name="echo")
        assert step.step_id == 0
        assert step.type == "skill"
        assert step.input_from is None
        assert step.condition is None

    def test_step_with_dependencies(self):
        step = WorkflowStep(step_id=1, type="llm", params={"text": "总结"}, input_from=0, condition="on_success")
        assert step.input_from == 0
        assert step.condition == "on_success"

    def test_if_step(self):
        step = WorkflowStep(
            step_id=1, type="if",
            if_condition="step_0.success",
            then_steps=[WorkflowStep(step_id=10, type="system", params={"command": "then"})],
            else_steps=[WorkflowStep(step_id=11, type="system", params={"command": "else"})],
        )
        assert step.type == "if"
        assert step.if_condition == "step_0.success"
        assert len(step.then_steps) == 1
        assert len(step.else_steps) == 1

    def test_loop_step(self):
        step = WorkflowStep(
            step_id=1, type="loop",
            loop_var="item",
            loop_over="step_0.result.items",
            loop_steps=[WorkflowStep(step_id=10, type="skill", skill_name="echo")],
            max_iterations=50,
        )
        assert step.type == "loop"
        assert step.loop_var == "item"
        assert step.max_iterations == 50

    def test_parallel_step(self):
        step = WorkflowStep(
            step_id=1, type="parallel",
            parallel_steps=[
                WorkflowStep(step_id=10, type="skill", skill_name="web_search"),
                WorkflowStep(step_id=11, type="skill", skill_name="image_gen"),
            ],
        )
        assert step.type == "parallel"
        assert len(step.parallel_steps) == 2


class TestWorkflowEntry:
    def test_basic_entry(self):
        entry = WorkflowEntry(name="test", steps=[])
        assert entry.name == "test"
        assert entry.workflow_id
        assert entry.description == ""

    def test_entry_with_steps(self):
        steps = [
            WorkflowStep(step_id=0, type="skill", skill_name="echo"),
            WorkflowStep(step_id=1, type="llm", params={"text": "总结"}, input_from=0),
        ]
        entry = WorkflowEntry(name="two_step", steps=steps)
        assert len(entry.steps) == 2


# ─── WorkflowTemplateMarket 测试 ──────────────────────────────

class TestWorkflowTemplateMarket:
    def test_list_templates(self):
        market = WorkflowTemplateMarket()
        templates = market.list_templates()
        assert len(templates) >= 5  # 至少 5 个预置模板

    def test_get_template(self):
        market = WorkflowTemplateMarket()
        t = market.get_template("写作辅助")
        assert t is not None
        assert t.name == "写作辅助"
        assert len(t.steps) == 4

    def test_search_templates(self):
        market = WorkflowTemplateMarket()
        results = market.search_templates("日报")
        assert len(results) >= 1
        assert any("日报" in t.name for t in results)

    def test_get_template_not_found(self):
        market = WorkflowTemplateMarket()
        assert market.get_template("不存在的模板") is None
