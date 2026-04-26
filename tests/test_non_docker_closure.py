"""非 Docker 闭环修复回归测试。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.adapters.http_adapter import create_app
from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.executor.agent import Executor
from src.skill_vault.sandbox_runner import SkillRunner
from src.supervisor.approval_engine import ApprovalEngine
from src.types import LearningRun, LearningRunStatus, RouteResult, RouteType, SkillMetadata, Task, TaskStatus


def _test_config(tmp_path, **overrides) -> RaccoonConfig:
    data = {
        "skills_dir": tmp_path / "skills",
        "db_path": tmp_path / "data" / "memcore.db",
        "schedules_dir": tmp_path / "schedules",
        "workflows_dir": tmp_path / "workflows",
        "llm_provider": "mock",
        "notify_channels": [],
    }
    data.update(overrides)
    return RaccoonConfig(**data)


def test_http_auth_and_health_public(tmp_path):
    app = create_app(_test_config(tmp_path, http_auth_token="secret"))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/message", json={"text": "你好"}).status_code == 401
        authed = client.post(
            "/message",
            headers={"Authorization": "Bearer secret"},
            json={"text": "你好"},
        )
        assert authed.status_code == 200


def test_upload_rejects_traversal_and_accepts_safe_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(_test_config(tmp_path, http_auth_token="secret"))
    headers = {"Authorization": "Bearer secret"}

    with TestClient(app) as client:
        bad = client.post(
            "/upload/file",
            headers=headers,
            files={"file": ("../config.json", b"x", "text/plain")},
        )
        assert bad.status_code == 400

        ok = client.post(
            "/upload/file",
            headers=headers,
            files={"file": ("report 1.txt", b"hello", "text/plain")},
        )
        assert ok.status_code == 200
        assert ok.json()["name"] == "report_1.txt"

        download = client.get("/files/uploads/report_1.txt", headers=headers)
        assert download.status_code == 200
        assert download.content == b"hello"

        traversal = client.get("/files/uploads/..%2Fconfig.json", headers=headers)
        assert traversal.status_code == 400


def test_sse_query_token_auth_and_rejects_missing_token(tmp_path):
    from fastapi import Request

    from src.adapters.auth import request_has_auth_token

    app = create_app(_test_config(tmp_path, http_auth_token="secret"))

    with TestClient(app) as client:
        assert client.get("/events").status_code == 401

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/events",
            "headers": [],
            "query_string": b"token=secret",
        }
    )
    assert request_has_auth_token(request, "secret") is True


def test_learning_runs_filter_and_core_benchmark_endpoint(tmp_path):
    app = create_app(_test_config(tmp_path, http_auth_token="secret"))
    headers = {"Authorization": "Bearer secret"}

    run = LearningRun(
        conversation_id="c1",
        user_id="u1",
        request_text="生成简报",
        scenario_id="daily_brief",
        status=LearningRunStatus.SUCCEEDED,
        first_pass=True,
        final_success=True,
        decision_success=True,
        execution_attempted=True,
        execution_success=True,
        handling_outcome="executed",
        quality_score=0.9,
    )
    app.state.learning_store.add(run)
    for i in range(6):
        app.state.learning_store.add(
            LearningRun(
                conversation_id=f"c_fail_{i % 3}",
                user_id="u1",
                request_text="采集素材并去重",
                scenario_id="material_collection",
                status=LearningRunStatus.FAILED,
                first_pass=False,
                final_success=False,
                decision_success=True,
                execution_attempted=True,
                execution_success=False,
                handling_outcome="failed",
                failure_code="data_hollow",
                quality_score=0.2,
            )
        )

    with TestClient(app) as client:
        unauth = client.get("/benchmarks/core-scenarios/latest")
        assert unauth.status_code == 401
        assert client.get("/learning/candidates").status_code == 401

        res = client.get("/learning/runs?scenario_id=daily_brief&first_pass=true", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert len(data) >= 1
        assert data[0]["scenario_id"] == "daily_brief"

        by_execution = client.get("/learning/runs?execution_success=true", headers=headers)
        assert by_execution.status_code == 200
        assert any(item["execution_success"] for item in by_execution.json())

        failed = client.get("/learning/runs?handling_outcome=failed", headers=headers)
        assert failed.status_code == 200
        assert all(item["handling_outcome"] == "failed" for item in failed.json())

        bench = client.get("/benchmarks/core-scenarios/latest", headers=headers)
        assert bench.status_code == 200
        payload = bench.json()
        assert "overall" in payload
        assert "scenarios" in payload
        assert "decision_success_rate" in payload["overall"]
        assert "execution_success_rate" in payload["overall"]
        assert "failure_code_topn" in payload

        candidates = client.get(
            "/learning/candidates?days=7&min_failures=5&min_conversations=2",
            headers=headers,
        )
        assert candidates.status_code == 200
        candidate_payload = candidates.json()
        assert len(candidate_payload) >= 1
        assert candidate_payload[0]["failure_code"] == "data_hollow"
        assert candidate_payload[0]["triggered"] is True


@pytest.mark.asyncio
async def test_executor_waits_for_high_risk_approval(tmp_path):
    class Runner:
        def __init__(self) -> None:
            self.ran = False

        async def run(self, task, params):
            self.ran = True
            return {"reply": "done"}

    class Vault:
        def __init__(self) -> None:
            self.runner = Runner()
            self.meta = SkillMetadata(
                name="danger",
                risk_level="high",
                requires_approval=True,
                permissions=["subprocess"],
            )

        def get_skill(self, name):
            return self.meta if name == "danger" else None

        def get_skill_runner(self, name):
            return self.runner if name == "danger" else None

    config = _test_config(tmp_path, auto_approve=False)
    bus = EventBus(config)
    vault = Vault()
    approval = ApprovalEngine(auto_approve=False, event_bus=bus)
    executor = Executor(bus, vault, config, approval_engine=approval)

    await bus.start()
    try:
        event = make_event(
            EventType.USER_MESSAGE,
            conversation_id="approval-test",
            payload={"text": "run danger"},
        )
        route = RouteResult(route_type=RouteType.SKILL, skill_name="danger")
        reply = await executor.handle_route_result(route, event)
        task = next(iter(executor.task_queue.all_tasks()))

        assert "等待审批" in reply
        assert task.status == TaskStatus.PENDING_APPROVAL
        assert vault.runner.ran is False

        await approval.approve(task.context["approval_id"])
        await asyncio.sleep(0.2)

        assert vault.runner.ran is True
        assert task.status == TaskStatus.SUCCESS
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_web_automate_runner_uses_in_process_entry(tmp_path):
    skill_dir = tmp_path / "web_automate"
    skill_dir.mkdir()
    (skill_dir / "main.py").write_text(
        "async def run_browser_skill(data):\n"
        "    return {\n"
        "        'runner': 'in_process',\n"
        "        'task_id': data['task_id'],\n"
        "        'params': data['params'],\n"
        "    }\n",
        encoding="utf-8",
    )

    runner = SkillRunner(skill_dir, "main.py", _test_config(tmp_path))
    task = Task(
        task_id="browser-task",
        conversation_id="browser-conv",
        origin_message="打开淘宝",
        skill_name="web_automate",
    )

    result = await runner.run(task, {"foo": "bar"})

    assert result == {
        "runner": "in_process",
        "task_id": "browser-task",
        "params": {"foo": "bar"},
    }


def test_web_automate_without_url_preserves_current_page():
    from skills.web_automate.main import _parse_actions

    assert _parse_actions("浏览器截图", None) == [{"action": "screenshot"}]


def test_web_automate_auto_resume_from_failed_step():
    from types import SimpleNamespace

    from skills.web_automate.main import _resolve_resume_plan

    session = SimpleNamespace(
        last_run={
            "owner": "conv-1",
            "action_list": [
                {"action": "open", "url": "https://example.com"},
                {"action": "click", "selector": "#login"},
                {"action": "type", "selector": "#kw", "text": "raccoon"},
            ],
            "failed_step": 1,
        }
    )
    actions, resume_from, resumed = _resolve_resume_plan(
        origin="继续",
        params={},
        action_list=[{"action": "screenshot"}],
        session=session,
        owner="conv-1",
    )

    assert resumed is True
    assert resume_from == 1
    assert actions == session.last_run["action_list"][1:]


def test_web_automate_auto_resume_from_persisted_state():
    from types import SimpleNamespace

    from skills.web_automate.main import _resolve_resume_plan

    session = SimpleNamespace(last_run={})
    persisted = {
        "owner": "conv-2",
        "action_list": [
            {"action": "open", "url": "https://fixture.local/login"},
            {"action": "type", "selector": "#username", "text": "demo"},
            {"action": "click", "selector": "#submit"},
        ],
        "failed_step": 2,
    }

    actions, resume_from, resumed = _resolve_resume_plan(
        origin="继续",
        params={},
        action_list=[{"action": "screenshot"}],
        session=session,
        owner="conv-2",
        persisted_last_run=persisted,
    )

    assert resumed is True
    assert resume_from == 2
    assert actions == persisted["action_list"][2:]


def test_web_automate_persist_artifacts_manifest(tmp_path):
    from skills.web_automate.main import _persist_run_artifacts

    manifest = _persist_run_artifacts(
        task_id="task-1",
        owner="conv-1",
        mode="cdp",
        action_list=[{"action": "open", "url": "https://fixture.local/login"}],
        details=[{"action": "open", "success": True, "message": "ok"}],
        runtime_artifacts={"checkpoints": [{"step_index": 0}]},
        output_dir=tmp_path,
    )

    path = tmp_path / Path(manifest).name
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["task_id"] == "task-1"
    assert payload["mode"] == "cdp"
    assert payload["action_count"] == 1


def test_web_automate_persist_failure_evidence_manifest(tmp_path):
    from skills.web_automate.main import _persist_failure_evidence

    manifest = _persist_failure_evidence(
        task_id="task-2",
        owner="conv-2",
        mode="cdp",
        details=[
            {"action": "open", "success": True, "message": "ok"},
            {"action": "click", "success": False, "message": "selector missing"},
        ],
        runtime_artifacts={
            "checkpoints": [{"step_index": 1}],
            "domain_health": {"fixture.local": "suspected_expired"},
            "artifacts": [{"screenshot": "/tmp/a.png", "dom_snapshot": "/tmp/a.html"}],
        },
        output_dir=tmp_path,
    )

    assert manifest is not None
    path = tmp_path / Path(str(manifest)).name
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["task_id"] == "task-2"
    assert len(payload["failed_steps"]) == 1
    assert payload["failed_steps"][0]["action"] == "click"


@pytest.mark.asyncio
async def test_browser_engine_execute_fail_fast_stops_on_first_failure():
    from skills.web_automate.actions import ActionResult
    from skills.web_automate.browser_engine import BrowserEngine

    engine = BrowserEngine("headless")

    async def _fake_execute(index, act, action_type):
        if action_type == "click":
            return ActionResult(success=False, message="boom")
        return ActionResult(success=True, message="ok")

    engine._execute_action_reliable = _fake_execute  # type: ignore[assignment]
    results = await engine.execute(
        [
            {"action": "open", "url": "https://fixture.local/login"},
            {"action": "click", "selector": "#submit"},
            {"action": "screenshot"},
        ]
    )

    assert len(results) == 2
    assert results[-1]["action"] == "click"
    assert results[-1]["success"] is False


@pytest.mark.asyncio
async def test_browser_engine_execute_continue_on_error_runs_all_steps():
    from skills.web_automate.actions import ActionResult
    from skills.web_automate.browser_engine import BrowserEngine

    engine = BrowserEngine("headless")

    async def _fake_execute(index, act, action_type):
        if action_type == "click":
            return ActionResult(success=False, message="boom")
        return ActionResult(success=True, message="ok")

    engine._execute_action_reliable = _fake_execute  # type: ignore[assignment]
    results = await engine.execute(
        [
            {"action": "open", "url": "https://fixture.local/login"},
            {"action": "click", "selector": "#submit"},
            {"action": "screenshot"},
        ],
        continue_on_error=True,
    )

    assert len(results) == 3
    assert results[1]["success"] is False
    assert results[2]["action"] == "screenshot"


@pytest.mark.asyncio
async def test_browser_engine_custom_url_assertion():
    from skills.web_automate.actions import ActionResult
    from skills.web_automate.browser_engine import BrowserEngine

    class _Page:
        def __init__(self):
            self.url = "https://fixture.local/dashboard"

        async def wait_for_selector(self, selector, timeout=1000, state="visible"):
            return None

        async def inner_text(self, selector):
            return "dashboard ok"

    engine = BrowserEngine("headless")
    engine._page = _Page()

    ok, reason = await engine._assert_action_result(  # type: ignore[attr-defined]
        "open",
        {"expected_url_contains": ["dashboard", "fixture.local"]},
        ActionResult(success=True, message="ok", data={"url": "https://fixture.local/dashboard"}),
    )
    assert ok is True
    assert reason == ""

    ok2, reason2 = await engine._assert_action_result(  # type: ignore[attr-defined]
        "open",
        {"expected_url_contains": "not-found"},
        ActionResult(success=True, message="ok", data={"url": "https://fixture.local/dashboard"}),
    )
    assert ok2 is False
    assert "url_assert_failed" in reason2


@pytest.mark.asyncio
async def test_browser_engine_custom_text_assertion():
    from skills.web_automate.actions import ActionResult
    from skills.web_automate.browser_engine import BrowserEngine

    class _Page:
        url = "https://fixture.local/report"

        async def wait_for_selector(self, selector, timeout=1000, state="visible"):
            return None

        async def inner_text(self, selector):
            return "report contains summary and timestamp"

    engine = BrowserEngine("headless")
    engine._page = _Page()

    ok, reason = await engine._assert_action_result(  # type: ignore[attr-defined]
        "read_page",
        {"assert_text_contains": ["summary", "timestamp"]},
        ActionResult(success=True, message="ok", data={}),
    )
    assert ok is True
    assert reason == ""

    ok2, reason2 = await engine._assert_action_result(  # type: ignore[attr-defined]
        "read_page",
        {"assert_text_contains": "absent-token"},
        ActionResult(success=True, message="ok", data={}),
    )
    assert ok2 is False
    assert "text_assert_failed" in reason2


def test_web_automate_resume_falls_back_to_last_success_checkpoint():
    from types import SimpleNamespace

    from skills.web_automate.main import _resolve_resume_plan

    session = SimpleNamespace(
        last_run={
            "owner": "conv-3",
            "action_list": [
                {"action": "open", "url": "https://fixture.local/login"},
                {"action": "type", "selector": "#username", "text": "demo"},
                {"action": "click", "selector": "#submit"},
                {"action": "screenshot"},
            ],
            "failed_step": None,
            "runtime": {
                "checkpoints": [
                    {"step_index": 0, "stage": "after", "success": True},
                    {"step_index": 1, "stage": "after", "success": True},
                    {"step_index": 2, "stage": "after", "success": False},
                ]
            },
        }
    )

    actions, resume_from, resumed = _resolve_resume_plan(
        origin="继续",
        params={},
        action_list=[{"action": "screenshot"}],
        session=session,
        owner="conv-3",
    )

    assert resumed is True
    assert resume_from == 2
    assert actions == session.last_run["action_list"][2:]


@pytest.mark.asyncio
async def test_browser_engine_blocks_on_recent_auth_failures():
    from skills.web_automate.browser_engine import BrowserEngine

    class _Page:
        url = "https://fixture.local/orders"

    engine = BrowserEngine("headless")
    engine._page = _Page()
    engine._recent_network_events.append(
        {
            "timestamp": time.time(),
            "domain": "fixture.local",
            "url": "https://fixture.local/api/orders",
            "status": 401,
            "kind": "response",
        }
    )

    blocked, reason = await engine._should_block_for_login("read_page")

    assert blocked is True
    assert "认证失败" in reason


@pytest.mark.asyncio
async def test_browser_engine_reliable_execution_includes_trace():
    from skills.web_automate.actions import ActionResult
    from skills.web_automate.browser_engine import BrowserEngine

    class _Page:
        url = "https://fixture.local/report"

        async def wait_for_selector(self, selector, timeout=1000, state="visible"):
            return None

        async def wait_for_load_state(self, state="domcontentloaded", timeout=1000):
            return None

        async def wait_for_timeout(self, ms):
            return None

    engine = BrowserEngine("headless")
    engine._page = _Page()

    async def _fake_action(act, action_type):
        return ActionResult(success=True, message="ok", data={"text": "done"})

    engine._do_action = _fake_action  # type: ignore[assignment]

    result = await engine._execute_action_reliable(
        0,
        {"action": "read_page", "retries": 1, "timeout_ms": 2000},
        "read_page",
    )

    assert result.success is True
    trace = result.data.get("execution_trace", {})
    assert trace.get("attempt_count") == 1
    assert trace.get("retries") == 1
    assert len(trace.get("attempts", [])) == 1


@pytest.mark.asyncio
async def test_actions_dynamic_selector_fallback_for_plain_text(tmp_path):
    from skills.web_automate.actions import Actions

    class _Page:
        async def wait_for_selector(self, selector, timeout=1000, state="visible"):
            # 仅 text=xxx 视为可命中，模拟陌生站点动态 selector 场景
            if selector.startswith("text="):
                return None
            raise RuntimeError("selector_not_found")

    actions = Actions(_Page(), tmp_path)
    selected = await actions._try_selectors("登录")
    assert selected == "text=登录"


@pytest.mark.asyncio
async def test_browser_engine_wait_request_signal_uses_recent_network_events():
    from skills.web_automate.browser_engine import BrowserEngine

    engine = BrowserEngine("headless")
    engine._recent_network_events.append(
        {
            "timestamp": time.time(),
            "domain": "fixture.local",
            "url": "https://fixture.local/api/submit",
            "status": 200,
            "kind": "response",
        }
    )
    await engine._wait_request_signal({"wait_request_contains": "api/submit"}, timeout_ms=400)


@pytest.mark.asyncio
async def test_browser_engine_allows_wait_for_selector_on_login_page():
    from skills.web_automate.browser_engine import BrowserEngine

    class _Page:
        url = "https://fixture.local/login"

    engine = BrowserEngine("headless")
    engine._page = _Page()

    blocked, reason = await engine._should_block_for_login("wait_for_selector")

    assert blocked is False
    assert reason == ""


@pytest.mark.asyncio
async def test_browser_engine_wait_request_signal_checked_after_action():
    from skills.web_automate.actions import ActionResult
    from skills.web_automate.browser_engine import BrowserEngine

    class _Page:
        url = "https://fixture.local/form"

        async def wait_for_selector(self, selector, timeout=1000, state="visible"):
            return None

        async def wait_for_load_state(self, state="domcontentloaded", timeout=1000):
            return None

        async def wait_for_timeout(self, ms):
            return None

    engine = BrowserEngine("headless")
    engine._page = _Page()

    async def _fake_action(act, action_type):
        engine._recent_network_events.append(
            {
                "timestamp": time.time(),
                "domain": "fixture.local",
                "url": "https://fixture.local/api/submit",
                "status": 200,
                "kind": "response",
            }
        )
        return ActionResult(success=True, message="ok", data={})

    engine._do_action = _fake_action  # type: ignore[assignment]

    result = await engine._execute_action_reliable(
        1,
        {
            "action": "click",
            "selector": "#submit",
            "wait_request_contains": ["/api/submit"],
            "timeout_ms": 800,
            "retries": 0,
        },
        "click",
    )

    assert result.success is True


def test_web_automate_resume_uses_checkpoint_fingerprint_alignment():
    from types import SimpleNamespace

    from skills.web_automate.main import _action_fingerprint, _resolve_resume_plan

    previous_actions = [
        {"action": "open", "url": "https://fixture.local/login"},
        {"action": "type", "selector": "#username", "text": "demo"},
        {"action": "click", "selector": "#submit", "checkpoint_key": "submit"},
        {"action": "screenshot"},
    ]
    current_actions = [
        {"action": "open", "url": "https://fixture.local/login"},
        {"action": "type", "selector": "#username", "text": "demo"},
        {"action": "wait", "ms": 500},
        {"action": "click", "selector": "#submit", "checkpoint_key": "submit"},
        {"action": "screenshot"},
    ]

    session = SimpleNamespace(
        last_run={
            "owner": "conv-fp",
            "action_list": previous_actions,
            "runtime": {
                "checkpoints": [
                    {
                        "step_index": 0,
                        "stage": "after",
                        "success": True,
                        "action_fingerprint": _action_fingerprint(previous_actions[0]),
                    },
                    {
                        "step_index": 1,
                        "stage": "after",
                        "success": True,
                        "action_fingerprint": _action_fingerprint(previous_actions[1]),
                    },
                    {
                        "step_index": 2,
                        "stage": "after",
                        "success": True,
                        "action_fingerprint": _action_fingerprint(previous_actions[2]),
                        "checkpoint_key": "submit",
                    },
                ]
            },
        }
    )

    actions, resume_from, resumed = _resolve_resume_plan(
        origin="继续",
        params={"actions": current_actions},
        action_list=current_actions,
        session=session,
        owner="conv-fp",
    )

    assert resumed is True
    assert resume_from == 4
    assert actions == current_actions[4:]
