from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.brain.core_benchmark import build_core_scenario_report
from src.brain.learning_store import LearningRunStore
from src.config import RaccoonConfig
from src.types import LearningRun, LearningRunStatus


def _config(tmp_path: Path) -> RaccoonConfig:
    return RaccoonConfig(
        db_path=tmp_path / "data" / "memcore.db",
        skills_dir=tmp_path / "skills",
        learning_staging_dir=tmp_path / "data" / "learning" / "staging",
    )


def _run(
    scenario_id: str,
    *,
    first_pass: bool,
    final_success: bool,
    failure_code: str | None = None,
    quality_score: float = 0.0,
) -> LearningRun:
    now = datetime.now(timezone.utc)
    return LearningRun(
        conversation_id="conv",
        user_id="u1",
        request_text=f"request::{scenario_id}",
        scenario_id=scenario_id,
        status=LearningRunStatus.SUCCEEDED if final_success else LearningRunStatus.FAILED,
        first_pass=first_pass,
        final_success=final_success,
        failure_code=failure_code,
        quality_score=quality_score,
        created_at=now,
        updated_at=now,
    )


def test_learning_store_filters_and_benchmark_report(tmp_path: Path):
    store = LearningRunStore(_config(tmp_path))
    store.add(_run("daily_brief", first_pass=True, final_success=True, quality_score=0.82))
    store.add(_run("daily_brief", first_pass=False, final_success=True, quality_score=0.78))
    store.add(_run("login_form_chain", first_pass=False, final_success=False, failure_code="auth_failed", quality_score=0.31))

    filtered = store.list_recent(limit=10, scenario_id="daily_brief")
    assert len(filtered) == 2
    assert all(item.scenario_id == "daily_brief" for item in filtered)

    failed = store.list_recent(limit=10, failure_code="auth_failed")
    assert len(failed) == 1
    assert failed[0].final_success is False

    first_pass = store.list_recent(limit=10, first_pass=True)
    assert len(first_pass) == 1
    assert first_pass[0].scenario_id == "daily_brief"

    rows = store.aggregate_core_scenarios()
    report = build_core_scenario_report(rows)
    daily = next(item for item in report["scenarios"] if item["scenario_id"] == "daily_brief")
    assert daily["runs"] == 2
    assert daily["final_success_rate"] == 1.0


def test_failure_cluster_counts(tmp_path: Path):
    store = LearningRunStore(_config(tmp_path))
    for idx in range(6):
        run = _run(
            "material_collection",
            first_pass=False,
            final_success=False,
            failure_code="data_hollow",
            quality_score=0.2,
        )
        run.conversation_id = f"conv-{idx % 3}"
        store.add(run)

    clusters = store.top_failure_clusters(days=7, limit=5)
    assert clusters
    assert clusters[0]["failure_code"] == "data_hollow"
    assert clusters[0]["failures"] >= 6
    assert clusters[0]["conversations"] >= 2

    candidates = store.list_new_skill_candidates(days=7, min_failures=5, min_conversations=2, limit=5)
    assert candidates
    assert candidates[0]["failure_code"] == "data_hollow"
    assert candidates[0]["triggered"] is True
