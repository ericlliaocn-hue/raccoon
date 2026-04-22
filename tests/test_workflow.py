"""工作流编排测试"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.types import WorkflowEntry, WorkflowStep
from src.workflow.workflow_store import WorkflowStore
from src.workflow.workflow_engine import WorkflowEngine, WorkflowExecution
from src.brain.planner import Plan, Planner


# ─── WorkflowStore 测试 ──────────────────────────────────────

class TestWorkflowStore:
    @pytest.fixture
    def store(self, tmp_path):
        cfg = RaccoonConfig(workflows_dir=tmp_path / "workflows")
        s = WorkflowStore(cfg)
        s.recover()
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
        cfg = RaccoonConfig(workflows_dir=tmp_path / "workflows")
        store1 = WorkflowStore(cfg)
        store1.recover()
        entry = WorkflowEntry(name="persist_test", steps=[
            WorkflowStep(step_id=0, type="skill", skill_name="echo"),
        ])
        store1.add(entry)

        # 新 store 实例应能恢复
        store2 = WorkflowStore(cfg)
        store2.recover()
        got = store2.get(entry.workflow_id)
        assert got is not None
        assert got.name == "persist_test"


# ─── WorkflowEngine 测试 ─────────────────────────────────────

class TestWorkflowEngine:
    @pytest.fixture
    def components(self):
        config = RaccoonConfig()
        event_bus = EventBus(config)
        router = MagicMock()
        executor = AsyncMock()
        store = WorkflowStore(config)
        store.recover()
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


class TestWorkflowEntry:
    def test_basic_entry(self):
        entry = WorkflowEntry(name="test", steps=[])
        assert entry.name == "test"
        assert entry.workflow_id  # 自动生成
        assert entry.description == ""

    def test_entry_with_steps(self):
        steps = [
            WorkflowStep(step_id=0, type="skill", skill_name="echo"),
            WorkflowStep(step_id=1, type="llm", params={"text": "总结"}, input_from=0),
        ]
        entry = WorkflowEntry(name="two_step", steps=steps)
        assert len(entry.steps) == 2
