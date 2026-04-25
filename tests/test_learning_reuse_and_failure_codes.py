from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.brain.core_benchmark import normalize_intent_phrase
from src.brain.learning_engine import LearningEngine
from src.config import RaccoonConfig
from src.memcore.writer import MemCoreWriter
from src.types import LearningRun, Task


def _make_config(tmp_path: Path) -> RaccoonConfig:
    return RaccoonConfig(
        db_path=tmp_path / "data" / "memcore.db",
        skills_dir=tmp_path / "skills",
        learning_staging_dir=tmp_path / "data" / "learning" / "staging",
        task_timeout_seconds=5,
    )


def test_normalize_intent_phrase_maps_english_synonyms():
    normalized = normalize_intent_phrase(
        "Please summarize today's news, take a screenshot, and notify me."
    )
    assert "简报" in normalized
    assert "截图" in normalized
    assert "提醒" in normalized


@pytest.mark.asyncio
async def test_search_experience_uses_variants_and_returns_confident_match(tmp_path: Path):
    config = _make_config(tmp_path)
    writer = MemCoreWriter(config)
    await writer.init()

    try:
        engine = LearningEngine.__new__(LearningEngine)
        engine._config = config
        engine._memcore_writer = writer

        payload = {
            "skill_name": "ai_daily_report",
            "scenario_id": "daily_brief",
            "trigger_words": ["日报", "简报", "热点"],
            "data_strategy": "api",
        }
        await LearningEngine._write_experience(
            engine,
            "给我一份今日简报",
            json.dumps(payload, ensure_ascii=False),
            "ai_daily_report",
        )

        result = await LearningEngine._search_experience(
            engine,
            "please summarize today news",
            user_id="u1",
        )
        assert result is not None
        assert result["skill_name"] == "ai_daily_report"
        assert float(result.get("reuse_confidence", 0.0)) >= 0.45
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_try_reuse_experience_skips_scenario_mismatch(tmp_path: Path):
    class _Runner:
        async def run(self, task, params):
            raise AssertionError("scenario mismatch should skip runner execution")

    class _Vault:
        def get_skill_runner(self, name):
            return _Runner() if name == "change_detector" else None

    engine = LearningEngine(config=_make_config(tmp_path), vault_manager=_Vault())
    run = LearningRun(
        conversation_id="c1",
        user_id="u1",
        request_text="先审批，再执行命令",
        scenario_id="remote_exec",
    )
    task = Task(
        conversation_id="c1",
        user_id="u1",
        origin_message="监控 https://item.jd.com/10086.html，降价提醒。",
        skill_name="learning",
    )
    result = await engine._try_reuse_experience(
        task,
        "监控 https://item.jd.com/10086.html，降价提醒。",
        run,
        {
            "skill_name": "change_detector",
            "scenario_id": "price_monitor",
            "reuse_confidence": 0.92,
        },
    )
    assert result is None


def test_classify_failure_code_preserves_execution_contract_detail(tmp_path: Path):
    engine = LearningEngine(config=_make_config(tmp_path))
    assert (
        engine._classify_failure_code("execution_contract_trace_missing")
        == "execution_contract_trace_missing"
    )
    assert (
        engine._classify_failure_code("execution_contract_checkpoint_missing")
        == "execution_contract_checkpoint_missing"
    )
    assert (
        engine._classify_failure_code("dependency_install_requires_post_approval")
        == "dependency_requires_approval"
    )
