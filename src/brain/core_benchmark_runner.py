"""Core scenario benchmark pack runner (clarification + execution)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.brain.core_benchmark import build_core_scenario_report
from src.brain.learning_engine import LearningEngine
from src.brain.learning_store import LearningRunStore
from src.config import PROJECT_ROOT, RaccoonConfig
from src.types import SkillMetadata, Task


class _FixtureRunner:
    def __init__(self, name: str) -> None:
        self._name = name

    async def run(self, task: Task, params: dict[str, Any]) -> dict[str, Any]:
        prompt = str(params.get("prompt") or params.get("rest") or task.origin_message or "")
        if self._name == "change_detector":
            target = str(params.get("url") or params.get("path") or params.get("target") or "")
            if not target:
                return {"reply": "❌ 缺少监控目标", "files": []}
            return {
                "reply": f"✅ 监控已创建: {target}\n更新时间: 2026-04-25 09:30",
                "files": [],
                "artifacts": {"target": target, "subcmd": params.get("subcmd", "snapshot")},
            }
        if self._name == "web_automate":
            if "fixture.local" not in prompt:
                return {"reply": "❌ 缺少可访问测试站点", "files": []}
            return {
                "reply": (
                    "✅ 登录成功，提交成功。\n"
                    "checkpoint 已保存，可恢复执行。\n"
                    "执行时间: 2026-04-25 10:00"
                ),
                "files": [],
                "artifacts": {
                    "checkpoints": [
                        {"step_index": 0, "action": "open"},
                        {"step_index": 1, "action": "type"},
                        {"step_index": 2, "action": "click"},
                    ],
                    "domain_health": {"fixture.local": "valid"},
                    "network_summary": [{"name": "/login", "duration": 128}],
                },
            }
        if self._name == "shell_exec":
            command = str(params.get("rest") or "").strip()
            if not command:
                return {"reply": "❌ 未提供命令", "files": []}
            return {
                "reply": (
                    "✅ 审批通过。\n"
                    f"执行结果: {command}\n"
                    "trace task=fixture run_id=fixture-shell-001"
                ),
                "files": [],
                "artifacts": {"command": command, "trace": "fixture-shell-001"},
            }

        # 内容类 skill（仅用于 list_skills 展示）
        return {"reply": "✅ fixture skill executed", "files": []}


class _FixtureVault:
    def __init__(self) -> None:
        self._skills = {
            "weibo_hot": SkillMetadata(name="weibo_hot", trigger_words=["微博", "热搜"]),
            "bilibili_hot": SkillMetadata(name="bilibili_hot", trigger_words=["b站", "热门"]),
            "ai_daily_report": SkillMetadata(name="ai_daily_report", trigger_words=["日报", "简报"]),
            "change_detector": SkillMetadata(
                name="change_detector",
                trigger_words=["监控", "变化"],
                risk_level="medium",
                permissions=["network", "filesystem"],
            ),
            "web_automate": SkillMetadata(
                name="web_automate",
                trigger_words=["登录", "表单", "浏览器"],
                risk_level="medium",
                permissions=["network", "browser"],
            ),
            "shell_exec": SkillMetadata(
                name="shell_exec",
                trigger_words=["执行", "命令", "shell"],
                risk_level="high",
                requires_approval=True,
                permissions=["subprocess"],
            ),
        }
        self._runners = {
            name: _FixtureRunner(name)
            for name in ("change_detector", "web_automate", "shell_exec")
        }

    def list_skills(self) -> list[SkillMetadata]:
        return list(self._skills.values())

    def get_skill(self, name: str) -> SkillMetadata | None:
        return self._skills.get(name)

    def get_skill_runner(self, name: str):
        return self._runners.get(name)


class _FixtureScheduler:
    async def add_schedule(self, entry):
        return entry


class _FixtureLLM:
    async def chat(self, messages, temperature: float = 0.1, max_tokens: int = 200):  # noqa: D401
        text = " ".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
        if "每个工作日 09:30" in text or "工作日 09:30" in text:
            return '{"cron":"30 9 * * 1-5","message":"提醒我检查客户回复","name":"weekday_followup"}'
        if "每周五 18:00" in text or "每周五" in text:
            return '{"cron":"0 18 * * 5","message":"提醒我汇总本周学习失败案例","name":"weekly_learning_review"}'
        return '{"schedule": false}'


def _load_pack(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _cleanup_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            candidate.unlink()


async def run_core_benchmark_packs(
    config: RaccoonConfig | None = None,
    *,
    clarification_pack: Path | None = None,
    execution_pack: Path | None = None,
) -> dict[str, Any]:
    base = config or RaccoonConfig()
    benchmark_db = PROJECT_ROOT / "data" / "benchmarks" / "core_fixture_latest.db"
    benchmark_db.parent.mkdir(parents=True, exist_ok=True)
    _cleanup_sqlite(benchmark_db)

    run_config = base.model_copy(update={"db_path": benchmark_db})
    store = LearningRunStore(run_config)
    engine = LearningEngine(
        config=run_config,
        vault_manager=_FixtureVault(),
        scheduler=_FixtureScheduler(),
        llm_client=_FixtureLLM(),
        learning_store=store,
    )

    clarification_pack = clarification_pack or (PROJECT_ROOT / "benchmarks" / "core" / "clarification_pack.json")
    execution_pack = execution_pack or (PROJECT_ROOT / "benchmarks" / "core" / "execution_pack.json")
    packs = [_load_pack(clarification_pack), _load_pack(execution_pack)]

    pack_summaries: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    total_cases = 0
    total_matched = 0

    for pack in packs:
        cases = list(pack.get("cases", []))
        matched = 0
        pack_mismatches: list[dict[str, Any]] = []
        for case in cases:
            prompt = str(case.get("prompt", ""))
            scenario_id = str(case.get("scenario_id", "") or "")
            expected_outcome = str(case.get("expected_outcome", "") or "")
            expected_reason = case.get("expected_reason")
            case_id = str(case.get("case_id", ""))

            task = Task(
                conversation_id=f"bench_{pack.get('pack_id', 'core')}_{case_id}",
                user_id="benchmark",
                origin_message=prompt,
                skill_name="learning",
                context={"scenario_id": scenario_id, "benchmark_case_id": case_id},
            )
            result = await engine.learn(task, prompt)
            run_id = str(result.get("learning_run_id", "") or "")
            run = store.get(run_id) if run_id else None

            actual_outcome = run.handling_outcome if run else ""
            actual_reason = run.clarification_reason if run else None
            outcome_ok = actual_outcome == expected_outcome
            reason_ok = (expected_reason is None) or (actual_reason == expected_reason)
            case_ok = outcome_ok and reason_ok
            if case_ok:
                matched += 1
            else:
                mismatch = {
                    "pack_id": pack.get("pack_id"),
                    "case_id": case_id,
                    "scenario_id": scenario_id,
                    "prompt": prompt,
                    "expected_outcome": expected_outcome,
                    "actual_outcome": actual_outcome,
                    "expected_reason": expected_reason,
                    "actual_reason": actual_reason,
                    "reply": result.get("reply", ""),
                    "learning_run_id": run_id,
                }
                pack_mismatches.append(mismatch)
                mismatches.append(mismatch)

        count = len(cases)
        total_cases += count
        total_matched += matched
        pack_summaries.append(
            {
                "pack_id": pack.get("pack_id"),
                "type": pack.get("type"),
                "total": count,
                "matched": matched,
                "match_rate": (matched / count) if count else 0.0,
                "mismatches": pack_mismatches,
            }
        )

    report = build_core_scenario_report(
        store.aggregate_core_scenarios(),
        store.top_failure_clusters(days=7, limit=10),
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(benchmark_db),
        "packs": pack_summaries,
        "overall": {
            "total_cases": total_cases,
            "matched_cases": total_matched,
            "pack_match_rate": (total_matched / total_cases) if total_cases else 0.0,
            "mismatch_count": len(mismatches),
        },
        "mismatches": mismatches,
        "benchmark_report": report,
    }


def run_core_benchmark_packs_sync(
    config: RaccoonConfig | None = None,
    *,
    clarification_pack: Path | None = None,
    execution_pack: Path | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        run_core_benchmark_packs(
            config=config,
            clarification_pack=clarification_pack,
            execution_pack=execution_pack,
        )
    )
