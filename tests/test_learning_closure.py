"""Focused tests for the 0.5.0 learning closure changes."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.brain.learning_engine import LearningEngine
from src.brain.learning_store import LearningRunStore
from src.config import RaccoonConfig
from src.eventbus.events import EventType
from src.executor.agent import Executor, SkillCandidate
from src.memcore.writer import MemCoreWriter
from src.types import Event, LearningRun, LearningRunStatus, Task


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
