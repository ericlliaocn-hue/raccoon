"""Focused tests for the 0.5.0 learning closure changes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.brain.learning_engine import LearningEngine
from src.brain.learning_store import LearningRunStore
from src.config import RaccoonConfig
from src.eventbus.events import EventType
from src.executor.agent import Executor, SkillCandidate
from src.memcore.writer import MemCoreWriter
from src.types import SkillMetadata
from src.types import Event, LearningRun, LearningRunStatus, RouteResult, RouteType, Task


def _make_config(tmp_path: Path) -> RaccoonConfig:
    return RaccoonConfig(
        db_path=tmp_path / "data" / "memcore.db",
        skills_dir=tmp_path / "skills",
        learning_staging_dir=tmp_path / "data" / "learning" / "staging",
        task_timeout_seconds=5,
    )


@pytest.mark.asyncio
async def test_learning_run_store_round_trip(tmp_path: Path):
    config = _make_config(tmp_path)
    store = LearningRunStore(config)

    run = LearningRun(
        conversation_id="c1",
        user_id="u1",
        request_text="帮我监控网站变化",
        status=LearningRunStatus.VALIDATING,
        skill_name="change_detector",
        dependencies=["httpx"],
        validation={"success": True},
    )
    store.add(run)

    loaded = store.get(run.run_id)
    assert loaded is not None
    assert loaded.run_id == run.run_id
    assert loaded.skill_name == "change_detector"
    assert loaded.validation["success"] is True
    assert store.list_recent(limit=1)[0].run_id == run.run_id


@pytest.mark.asyncio
async def test_learning_engine_experience_round_trip(tmp_path: Path):
    config = _make_config(tmp_path)
    writer = MemCoreWriter(config)
    await writer.init()

    try:
        engine = LearningEngine.__new__(LearningEngine)
        engine._config = config
        engine._memcore_writer = writer

        payload = {"skill_name": "weather_query", "data_strategy": "api"}
        await LearningEngine._write_experience(
            engine,
            "帮我查天气",
            json.dumps(payload, ensure_ascii=False),
            "weather_query",
        )

        result = await LearningEngine._search_experience(engine, "天气")
        assert result is not None
        assert result["skill_name"] == "weather_query"
        assert result["data_strategy"] == "api"
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_validate_staged_skill_defers_missing_dependency(tmp_path: Path):
    config = _make_config(tmp_path)
    engine = LearningEngine.__new__(LearningEngine)
    engine._config = config

    skill_dir = tmp_path / "staged" / "missing_dep"
    skill_dir.mkdir(parents=True)
    (skill_dir / "main.py").write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "import definitely_missing_package",
                "",
                "def main():",
                "    data = json.loads(sys.stdin.read())",
                "    print(json.dumps({'task_id': data.get('task_id', ''), 'reply': 'ok'}))",
                "",
                "if __name__ == '__main__':",
                "    main()",
            ]
        ),
        encoding="utf-8",
    )

    validation = await LearningEngine._validate_staged_skill(
        engine,
        Task(
            conversation_id="c1",
            user_id="u1",
            origin_message="帮我处理",
            skill_name="missing_dep",
        ),
        "帮我处理",
        skill_dir,
        {
            "name": "missing_dep",
            "version": "0.1.0",
            "description": "test",
            "trigger_words": ["处理"],
            "intent_tags": ["learned"],
            "risk_level": "low",
            "requires_approval": False,
            "timeout_seconds": 5,
            "entry": "main.py",
            "permissions": ["network"],
        },
        dependencies=["definitely_missing_package"],
    )

    assert validation["success"] is True
    assert validation["smoke_deferred"] is True
    assert validation["missing_module"] == "definitely_missing_package"


@pytest.mark.asyncio
async def test_try_existing_skill_before_learning_reuses_candidate():
    executor = Executor.__new__(Executor)
    executor._config = RaccoonConfig()
    executor._vault_manager = MagicMock()
    executor._vault_manager.list_skills = MagicMock(return_value=[MagicMock(name="web_automate")])
    executor._find_skill_candidates = MagicMock(
        return_value=[SkillCandidate(skill_name="web_automate", confidence=0.91, matched_terms=["浏览器"])]
    )
    executor._handle_skill = AsyncMock(return_value="收到！任务已创建")

    reply = await Executor._try_existing_skill_before_learning(
        executor,
        "打开浏览器访问百度",
        Event(
            event=EventType.USER_MESSAGE,
            conversation_id="c1",
            user_id="u1",
            payload={"text": "要"},
        ),
    )

    assert reply is not None
    assert "复用已有技能" in reply
    executor._handle_skill.assert_awaited_once()


@pytest.mark.asyncio
async def test_learning_preflight_price_monitor_requires_target(tmp_path: Path):
    config = _make_config(tmp_path)
    store = LearningRunStore(config)
    engine = LearningEngine(config=config, learning_store=store)

    result = await engine.learn(
        Task(
            conversation_id="c1",
            user_id="u1",
            origin_message="帮我持续盯这个商品，降到 4299 直接提醒。",
            skill_name="learning",
        ),
        "帮我持续盯这个商品，降到 4299 直接提醒。",
    )

    run = store.get(result["learning_run_id"])
    assert run is not None
    assert run.status == LearningRunStatus.SUCCEEDED
    assert run.final_success is True
    assert run.decision_success is True
    assert run.execution_attempted is False
    assert run.execution_success is False
    assert run.handling_outcome == "clarified"
    assert run.clarification_reason == "missing_price_target"
    assert run.artifacts["clarification_required"] is True
    assert run.artifacts["reason"] == "missing_price_target"
    assert "商品链接" in result["reply"]


@pytest.mark.asyncio
async def test_learning_preflight_remote_exec_requires_concrete_command(tmp_path: Path):
    config = _make_config(tmp_path)
    store = LearningRunStore(config)
    engine = LearningEngine(config=config, learning_store=store)

    result = await engine.learn(
        Task(
            conversation_id="c1",
            user_id="u1",
            origin_message="先审批，再帮我执行日志归档脚本。",
            skill_name="learning",
        ),
        "先审批，再帮我执行日志归档脚本。",
    )

    run = store.get(result["learning_run_id"])
    assert run is not None
    assert run.status == LearningRunStatus.SUCCEEDED
    assert run.final_success is True
    assert run.decision_success is True
    assert run.execution_attempted is False
    assert run.execution_success is False
    assert run.handling_outcome == "clarified"
    assert run.clarification_reason == "missing_shell_command"
    assert run.artifacts["reason"] == "missing_shell_command"
    assert result["skill_name"] == "shell_exec"


def test_infer_scenario_does_not_confuse_downloads_with_oa(tmp_path: Path):
    config = _make_config(tmp_path)
    store = LearningRunStore(config)
    engine = LearningEngine(config=config, learning_store=store)
    run = LearningRun(
        conversation_id="c1",
        user_id="u1",
        request_text="先审批，再执行 `du -sh ~/Downloads` 并返回结果。",
        scenario_id="remote_exec",
        status=LearningRunStatus.ANALYZING,
    )

    scenario_id = engine._infer_scenario_id(run, "先审批，再执行 `du -sh ~/Downloads` 并返回结果。")

    assert scenario_id == "remote_exec"


@pytest.mark.asyncio
async def test_schedule_creation_recognizes_workday_signal(tmp_path: Path):
    class _FakeScheduler:
        async def add_schedule(self, entry):
            return entry

    class _FakeLLM:
        async def chat(self, messages, temperature=0.1, max_tokens=200):
            return '{"cron":"30 9 * * 1-5","message":"提醒我检查客户回复","name":"weekday_check"}'

    engine = LearningEngine(
        config=_make_config(tmp_path),
        scheduler=_FakeScheduler(),
        llm_client=_FakeLLM(),
    )

    created = await engine._try_create_schedule(
        "每个工作日 09:30 提醒我检查客户回复。",
        "scheduler",
        "conv-1",
    )

    assert created is not None
    assert "weekday_check" in created


@pytest.mark.asyncio
async def test_learning_preflight_reuses_content_skill_bundle(tmp_path: Path):
    config = _make_config(tmp_path)
    store = LearningRunStore(config)
    vault = MagicMock()
    vault.list_skills.return_value = [
        SimpleNamespace(name="weibo_hot"),
        SimpleNamespace(name="bilibili_hot"),
        SimpleNamespace(name="ai_daily_report"),
    ]
    engine = LearningEngine(config=config, vault_manager=vault, learning_store=store)

    result = await engine.learn(
        Task(
            conversation_id="c1",
            user_id="u1",
            origin_message="收集本周爆款内容素材，按平台去重后输出。",
            skill_name="learning",
        ),
        "收集本周爆款内容素材，按平台去重后输出。",
    )

    run = store.get(result["learning_run_id"])
    assert run is not None
    assert run.status == LearningRunStatus.SUCCEEDED
    assert run.final_success is True
    assert run.decision_success is True
    assert run.handling_outcome == "reused"
    assert run.execution_attempted is False
    assert run.artifacts["reused_capability"] == "skill_bundle"
    assert "weibo_hot" in run.artifacts["skills"]


@pytest.mark.asyncio
async def test_learning_preflight_reused_bundle_can_execute_when_runner_available(tmp_path: Path):
    config = _make_config(tmp_path)
    store = LearningRunStore(config)

    class _ReportRunner:
        async def run(self, task, params):
            return {"reply": "✅ 日报已生成，来源与时间已附带。", "files": []}

    class _Vault:
        def list_skills(self):
            return [
                SimpleNamespace(name="weibo_hot"),
                SimpleNamespace(name="ai_daily_report"),
            ]

        def get_skill_runner(self, name):
            return _ReportRunner() if name == "ai_daily_report" else None

    engine = LearningEngine(config=config, vault_manager=_Vault(), learning_store=store)
    result = await engine.learn(
        Task(
            conversation_id="c1",
            user_id="u1",
            origin_message="帮我整理一下今天重点。",
            skill_name="learning",
        ),
        "帮我整理一下今天重点。",
    )

    run = store.get(result["learning_run_id"])
    assert run is not None
    assert run.status == LearningRunStatus.SUCCEEDED
    assert run.handling_outcome == "reused"
    assert run.execution_attempted is True
    assert run.execution_success is True
    assert "ai_daily_report" in (run.artifacts.get("executed_bundle_skills") or [])


def test_learning_blocks_fake_endpoint_and_extracts_fenced_code(tmp_path: Path):
    engine = LearningEngine(config=_make_config(tmp_path))
    assert engine._analysis_has_fake_endpoint(
        {"data_sources": [{"url": "https://oa.example.com/api/flow/submit"}]}
    )

    code = LearningEngine._extract_python_code(
        "这里是代码：\n```python\nimport json\n\ndef main():\n    pass\n```"
    )
    assert code is not None
    assert code.startswith("import json")
    assert "```" not in code


@pytest.mark.asyncio
async def test_shell_exec_requires_concrete_command_before_approval():
    executor = Executor.__new__(Executor)
    executor._vault_manager = MagicMock()
    executor._vault_manager.get_skill = MagicMock(return_value=MagicMock(interactive=False, flow=None))

    reply = await Executor._handle_skill(
        executor,
        RouteResult(route_type=RouteType.SKILL, skill_name="shell_exec"),
        Event(
            event=EventType.USER_MESSAGE,
            conversation_id="c1",
            user_id="u1",
            payload={"text": "先审批，再帮我执行日志归档脚本。"},
        ),
    )

    assert "具体命令" in reply


def test_shell_exec_preflight_autofills_template_command():
    executor = Executor.__new__(Executor)
    reply, normalized = Executor._preflight_skill_request(
        executor,
        "shell_exec",
        "请先审批，再执行磁盘空间检查命令并返回结果。",
        {},
    )

    assert reply is None
    assert normalized["rest"] == "df -h"
    assert normalized["auto_command_template"] is True


@pytest.mark.asyncio
async def test_web_browse_blocks_placeholder_url_before_execution():
    executor = Executor.__new__(Executor)
    executor._vault_manager = MagicMock()
    executor._vault_manager.get_skill = MagicMock(return_value=MagicMock(interactive=False, flow=None))

    reply = await Executor._handle_skill(
        executor,
        RouteResult(route_type=RouteType.SKILL, skill_name="web_browse"),
        Event(
            event=EventType.USER_MESSAGE,
            conversation_id="c1",
            user_id="u1",
            payload={"text": "打开网页 https://example.com 试试看"},
        ),
    )

    assert "占位地址" in reply


@pytest.mark.asyncio
async def test_change_detector_requires_target_before_snapshot_execution():
    executor = Executor.__new__(Executor)
    executor._vault_manager = MagicMock()
    executor._vault_manager.get_skill = MagicMock(return_value=MagicMock(interactive=False, flow=None))

    reply = await Executor._handle_skill(
        executor,
        RouteResult(route_type=RouteType.SKILL, skill_name="change_detector"),
        Event(
            event=EventType.USER_MESSAGE,
            conversation_id="c1",
            user_id="u1",
            payload={"text": "帮我监控这个商品的价格变化，降价就提醒"},
        ),
    )

    assert "URL/SKU" in reply


def test_change_detector_preflight_infers_snapshot_url_params():
    executor = Executor.__new__(Executor)
    params, clarification = Executor._prepare_change_detector_params(
        executor,
        {},
        "帮我监控 https://www.jd.com/item/10086 价格变化",
    )

    assert clarification is None
    assert params["subcmd"] == "snapshot"
    assert params["url"] == "https://www.jd.com/item/10086"


@pytest.mark.asyncio
async def test_learning_preflight_executes_price_monitor_when_target_complete(tmp_path: Path):
    config = _make_config(tmp_path)
    store = LearningRunStore(config)

    class _Runner:
        async def run(self, task, params):
            assert params["subcmd"] == "snapshot"
            assert "url" in params
            return {"reply": "✅ 监控已创建", "files": [], "artifacts": {"target": params["url"]}}

    class _Vault:
        def __init__(self):
            self._runner = _Runner()

        def list_skills(self):
            return []

        def get_skill_runner(self, name):
            return self._runner if name == "change_detector" else None

        def get_skill(self, name):
            return SkillMetadata(name=name) if name == "change_detector" else None

    engine = LearningEngine(config=config, vault_manager=_Vault(), learning_store=store)
    result = await engine.learn(
        Task(
            conversation_id="c1",
            user_id="u1",
            origin_message="监控 https://item.jd.com/100012043978.html，降价提醒。",
            skill_name="learning",
        ),
        "监控 https://item.jd.com/100012043978.html，降价提醒。",
    )

    run = store.get(result["learning_run_id"])
    assert run is not None
    assert run.handling_outcome == "executed"
    assert run.execution_attempted is True
    assert run.execution_success is True
    assert run.skill_name == "change_detector"


@pytest.mark.asyncio
async def test_learning_preflight_executes_price_monitor_with_minimal_runner_artifacts(tmp_path: Path):
    config = _make_config(tmp_path)
    store = LearningRunStore(config)

    class _Runner:
        async def run(self, task, params):
            assert params["subcmd"] == "snapshot"
            assert "url" in params
            return {"reply": "监控已创建", "files": []}

    class _Vault:
        def list_skills(self):
            return []

        def get_skill_runner(self, name):
            return _Runner() if name == "change_detector" else None

        def get_skill(self, name):
            return SkillMetadata(name=name) if name == "change_detector" else None

    engine = LearningEngine(config=config, vault_manager=_Vault(), learning_store=store)
    result = await engine.learn(
        Task(
            conversation_id="c1b",
            user_id="u1",
            origin_message="监控 https://item.jd.com/100012043978.html，降价提醒。",
            skill_name="learning",
        ),
        "监控 https://item.jd.com/100012043978.html，降价提醒。",
    )

    run = store.get(result["learning_run_id"])
    assert run is not None
    assert run.handling_outcome == "executed"
    assert run.execution_success is True
    assert run.failure_code is None
    assert run.artifacts.get("execution_artifacts", {}).get("target")


@pytest.mark.asyncio
async def test_learning_preflight_executes_remote_exec_with_backtick_command(tmp_path: Path):
    config = _make_config(tmp_path)
    store = LearningRunStore(config)

    class _ShellRunner:
        async def run(self, task, params):
            assert params["rest"] == "du -sh ~/Downloads"
            return {"reply": "✅ 审批通过，执行结果 trace run_id=abc", "files": []}

    class _Vault:
        def list_skills(self):
            return []

        def get_skill_runner(self, name):
            return _ShellRunner() if name == "shell_exec" else None

        def get_skill(self, name):
            if name != "shell_exec":
                return None
            return SkillMetadata(name=name, risk_level="low")

    engine = LearningEngine(config=config, vault_manager=_Vault(), learning_store=store)
    result = await engine.learn(
        Task(
            conversation_id="c2",
            user_id="u2",
            origin_message="先审批，再执行 `du -sh ~/Downloads` 并返回结果。",
            skill_name="learning",
        ),
        "先审批，再执行 `du -sh ~/Downloads` 并返回结果。",
    )

    run = store.get(result["learning_run_id"])
    assert run is not None
    assert run.handling_outcome == "executed"
    assert run.execution_success is True
    assert run.skill_name == "shell_exec"


@pytest.mark.asyncio
async def test_learning_preflight_executes_remote_exec_with_normalized_trace_artifacts(tmp_path: Path):
    config = _make_config(tmp_path)
    store = LearningRunStore(config)

    class _ShellRunner:
        async def run(self, task, params):
            assert params["rest"] == "pwd"
            return {"reply": "✅ 命令执行成功 (exit 0)", "files": []}

    class _Vault:
        def list_skills(self):
            return []

        def get_skill_runner(self, name):
            return _ShellRunner() if name == "shell_exec" else None

        def get_skill(self, name):
            if name != "shell_exec":
                return None
            return SkillMetadata(name=name, risk_level="low")

    engine = LearningEngine(config=config, vault_manager=_Vault(), learning_store=store)
    result = await engine.learn(
        Task(
            conversation_id="c2b",
            user_id="u2",
            origin_message="先审批，再执行 `pwd` 并返回结果。",
            skill_name="learning",
        ),
        "先审批，再执行 `pwd` 并返回结果。",
    )

    run = store.get(result["learning_run_id"])
    assert run is not None
    assert run.handling_outcome == "executed"
    assert run.execution_success is True
    assert run.failure_code is None
    artifacts = run.artifacts.get("execution_artifacts", {})
    assert artifacts.get("command") == "pwd"
    assert artifacts.get("trace")


@pytest.mark.asyncio
async def test_learning_preflight_executes_login_chain_when_target_complete(tmp_path: Path):
    config = _make_config(tmp_path)
    store = LearningRunStore(config)

    class _BrowserRunner:
        async def run(self, task, params):
            assert "fixture.local" in params["prompt"]
            return {"reply": "✅ 登录成功，提交成功，checkpoint 已恢复", "files": []}

    class _Vault:
        def list_skills(self):
            return []

        def get_skill_runner(self, name):
            return _BrowserRunner() if name == "web_automate" else None

        def get_skill(self, name):
            return SkillMetadata(name=name) if name == "web_automate" else None

    engine = LearningEngine(config=config, vault_manager=_Vault(), learning_store=store)
    result = await engine.learn(
        Task(
            conversation_id="c3",
            user_id="u3",
            origin_message="登录 https://fixture.local/login 并提交采购申请。",
            skill_name="learning",
        ),
        "登录 https://fixture.local/login 并提交采购申请。",
    )

    run = store.get(result["learning_run_id"])
    assert run is not None
    assert run.handling_outcome == "executed"
    assert run.execution_success is True
    assert run.skill_name == "web_automate"


def test_oa_context_boundary_avoids_english_substring_noise(tmp_path: Path):
    engine = LearningEngine(config=_make_config(tmp_path), learning_store=LearningRunStore(_make_config(tmp_path)))
    assert engine._mentions_oa_context("please update roadmap for q2") is False
    assert engine._mentions_oa_context("检查 download path ~/Downloads") is False
    assert engine._mentions_oa_context("进入 oa 系统审批流程") is True


def test_shell_command_detection_handles_english_and_paths(tmp_path: Path):
    engine = LearningEngine(config=_make_config(tmp_path), learning_store=LearningRunStore(_make_config(tmp_path)))
    assert engine._has_concrete_shell_command("run df -h /var/log and send summary") is True
    assert engine._has_concrete_shell_command("帮我处理日志问题") is False
    assert engine._has_concrete_shell_command("请执行磁盘空间检查命令并返回结果") is True
    assert engine._extract_shell_command("please run `du -sh ~/Downloads` now") == "du -sh ~/Downloads"
    assert engine._extract_shell_command("run /usr/local/bin/backup_logs.sh --tail 20").startswith(
        "/usr/local/bin/backup_logs.sh"
    )
    assert engine._extract_shell_command("请执行磁盘空间检查命令并返回结果") == "df -h"


def test_login_form_target_requires_url_and_action_context(tmp_path: Path):
    engine = LearningEngine(config=_make_config(tmp_path), learning_store=LearningRunStore(_make_config(tmp_path)))
    assert engine._has_form_target("登录 https://fixture.local/login，账号 test_user，提交申请") is True
    assert engine._has_form_target("访问 https://fixture.local/home 看一下页面") is False
    assert engine._has_form_target("登录系统并提交流程单") is False


def test_build_playbook_params_for_web_automate_contains_stable_actions(tmp_path: Path):
    engine = LearningEngine(config=_make_config(tmp_path), learning_store=LearningRunStore(_make_config(tmp_path)))
    params, reason = engine._build_playbook_exec_params(
        skill_name="web_automate",
        user_message="登录 https://fixture.local/login，账号 test_user，上传 /tmp/demo.pdf 并提交申请。",
    )

    assert reason is None
    assert params["auto_resume"] is True
    assert params["continue_on_error"] is False
    actions = params["actions"]
    assert actions[0]["action"] == "open"
    assert "fixture.local" in actions[0]["url"]
    assert any(step["action"] == "type" for step in actions)
    assert any(step["action"] == "upload_file" for step in actions)
    assert actions[-1]["action"] == "screenshot"


def test_build_login_chain_actions_for_heroku_contains_recovery_assertions(tmp_path: Path):
    engine = LearningEngine(config=_make_config(tmp_path), learning_store=LearningRunStore(_make_config(tmp_path)))
    actions = engine._build_login_chain_actions(
        "后台模式 登录 https://the-internet.herokuapp.com/login ，账号 tomsmith 密码 SuperSecretPassword! 登录后截图。"
    )

    submit = next(step for step in actions if step.get("checkpoint_key") == "login_submit")
    assert "/authenticate" in submit["wait_request_contains"]
    assert "/login" in submit["expected_url_not_contains"]
    assert any("secure area" in token.lower() for token in submit["assert_text_contains"])
    assert actions[0]["url"] == "https://the-internet.herokuapp.com/login"
    assert not any(step.get("checkpoint_key") == "final_submit" for step in actions)


def test_extract_url_like_strips_trailing_chinese_punctuation(tmp_path: Path):
    engine = LearningEngine(config=_make_config(tmp_path), learning_store=LearningRunStore(_make_config(tmp_path)))
    message = "打开 https://the-internet.herokuapp.com/login，完成登录并继续提交流程，失败可从 checkpoint 续跑。"
    assert engine._extract_url_like(message) == "https://the-internet.herokuapp.com/login"


def test_extract_file_path_like_ignores_http_url_path(tmp_path: Path):
    engine = LearningEngine(config=_make_config(tmp_path), learning_store=LearningRunStore(_make_config(tmp_path)))
    message = "后台模式 登录 https://the-internet.herokuapp.com/login ，账号 tomsmith 密码 SuperSecretPassword! 登录后截图。"
    assert engine._extract_file_path_like(message) is None
    assert engine._extract_file_path_like("上传 /tmp/demo.pdf 并提交") == "/tmp/demo.pdf"


def test_httpbin_login_actions_inject_default_credentials_when_missing(tmp_path: Path):
    engine = LearningEngine(config=_make_config(tmp_path), learning_store=LearningRunStore(_make_config(tmp_path)))
    actions = engine._build_login_chain_actions("登录 https://httpbin.org/forms/post ，填表后提交并截图。")
    typed_values = [step.get("text") for step in actions if step.get("action") == "type"]
    assert "raccoon-benchmark" in typed_values
    assert "13800000000" in typed_values
    submit = next(step for step in actions if step.get("checkpoint_key") == "login_submit")
    assert "form button" in submit.get("selector", "")


@pytest.mark.asyncio
async def test_execute_learned_skill_honors_skill_timeout_seconds(tmp_path: Path):
    class _SlowRunner:
        async def run(self, task, params):
            await asyncio.sleep(1.2)
            return {"reply": "ok", "files": []}

    class _Vault:
        def get_skill_runner(self, name):
            return _SlowRunner()

        def get_skill(self, name):
            return SkillMetadata(name=name, timeout_seconds=1)

    config = _make_config(tmp_path)
    store = LearningRunStore(config)
    engine = LearningEngine(config=config, vault_manager=_Vault(), learning_store=store)
    result = await engine._execute_learned_skill(
        Task(
            conversation_id="c-timeout",
            user_id="u-timeout",
            origin_message="后台模式 登录 https://the-internet.herokuapp.com/login 并截图",
            skill_name="web_automate",
        ),
        "web_automate",
        "后台模式 登录 https://the-internet.herokuapp.com/login 并截图",
        params={"prompt": "后台模式 登录 https://the-internet.herokuapp.com/login 并截图"},
    )

    assert result["success"] is False
    assert result["error"] == "skill_timeout:web_automate:1s"


@pytest.mark.asyncio
async def test_login_playbook_auto_recovers_after_first_timeout(tmp_path: Path):
    class _Vault:
        def get_skill_runner(self, name):
            return object() if name == "web_automate" else None

        def get_skill(self, name):
            return SkillMetadata(name=name, timeout_seconds=60)

    config = _make_config(tmp_path)
    store = LearningRunStore(config)
    engine = LearningEngine(config=config, vault_manager=_Vault(), learning_store=store)
    engine._execute_learned_skill = AsyncMock(
        side_effect=[
            {"success": False, "error": "skill_timeout:web_automate:5s"},
            {
                "success": True,
                "reply": "✅ 登录成功，checkpoint 已恢复。",
                "files": [],
                "artifacts": {"checkpoints": [{"step_index": 2, "stage": "after", "success": True}]},
            },
        ]
    )

    message = "后台模式 登录 https://the-internet.herokuapp.com/login ，账号 tomsmith 密码 SuperSecretPassword! 登录后截图。"
    result = await engine.learn(
        Task(
            conversation_id="c-recover",
            user_id="u-recover",
            origin_message=message,
            skill_name="learning",
        ),
        message,
    )

    run = store.get(result["learning_run_id"])
    assert run is not None
    assert run.execution_success is True
    assert run.artifacts["playbook_recovery"]["attempted"] is True
    assert run.artifacts["playbook_recovery"]["retry_success"] is True
    assert engine._execute_learned_skill.await_count == 2
    assert "自动从 checkpoint 恢复" in result["reply"]


def test_build_playbook_params_for_change_detector_supports_query_target(tmp_path: Path):
    engine = LearningEngine(config=_make_config(tmp_path), learning_store=LearningRunStore(_make_config(tmp_path)))
    params, reason = engine._build_playbook_exec_params(
        skill_name="change_detector",
        user_message="盯一下 iPhone 16 Pro 256G 的价格变化，有波动提醒我。",
    )

    assert reason is None
    assert params["subcmd"] == "snapshot"
    assert params["target_hint"] == "query"
    assert "search.jd.com/Search?keyword=" in params["url"]
    assert "iPhone 16 Pro 256G" in params["target"]
