"""Focused tests for the 0.5.0 learning closure changes."""

from __future__ import annotations

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
