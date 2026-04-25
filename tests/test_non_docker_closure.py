"""非 Docker 闭环修复回归测试。"""

from __future__ import annotations

import asyncio

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
