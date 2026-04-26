"""开放世界压测 runner（真实外站样本 + 缺参扰动）。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from src.brain.core_benchmark import CORE_BENCHMARK_THRESHOLDS
from src.brain.learning_engine import LearningEngine
from src.brain.learning_store import LearningRunStore
from src.config import PROJECT_ROOT, RaccoonConfig, load_config
from src.eventbus.bus import EventBus
from src.llm import LLMFactory
from src.scheduler.schedule_store import ScheduleStore
from src.scheduler.scheduler import Scheduler
from src.skill_vault.vault_manager import VaultManager
from src.supervisor.approval_engine import ApprovalEngine
from src.types import Task

CASE_TIMEOUT_SECONDS = 180.0


def _pct(success: int, total: int) -> float:
    return (success / total) if total else 0.0


def _load_pack(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


async def _run_case_with_timeout(engine: LearningEngine, task: Task, prompt: str) -> tuple[dict[str, Any], str]:
    try:
        result = await asyncio.wait_for(engine.learn(task, prompt), timeout=CASE_TIMEOUT_SECONDS)
        return result, ""
    except asyncio.TimeoutError:
        return (
            {
                "learning_run_id": "",
                "reply": f"benchmark_case_timeout: execution exceeded {int(CASE_TIMEOUT_SECONDS)}s",
            },
            "benchmark_case_timeout",
        )
    except Exception as exc:  # pragma: no cover - defensive for external benchmark runner
        return (
            {
                "learning_run_id": "",
                "reply": f"benchmark_case_error: {exc}",
            },
            "benchmark_case_error",
        )


def _summarize_open_world_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    decision_success = sum(1 for row in records if row.get("decision_success"))
    detect_success = sum(
        1 for row in records if str(row.get("expected_scenario") or "") == str(row.get("detected_scenario") or "")
    )

    complete_rows = [row for row in records if row.get("input_complete")]
    complete_total = len(complete_rows)
    complete_attempted = [row for row in complete_rows if row.get("execution_attempted")]
    complete_succeeded = [row for row in complete_rows if row.get("execution_success")]
    complete_clarified = [row for row in complete_rows if row.get("handling_outcome") == "clarified"]

    browser_complete = [row for row in complete_rows if row.get("expected_scenario") == "login_form_chain"]
    browser_succeeded = [row for row in browser_complete if row.get("execution_success")]

    by_scenario: dict[str, dict[str, Any]] = {}
    for scenario_id in sorted({str(row.get("expected_scenario") or "") for row in records if row.get("expected_scenario")}):
        rows = [row for row in records if row.get("expected_scenario") == scenario_id]
        complete = [row for row in rows if row.get("input_complete")]
        complete_attempt = [row for row in complete if row.get("execution_attempted")]
        complete_success = [row for row in complete if row.get("execution_success")]
        false_clarified = [row for row in complete if row.get("handling_outcome") == "clarified"]
        by_scenario[scenario_id] = {
            "runs": len(rows),
            "complete_runs": len(complete),
            "decision_success_rate": _pct(sum(1 for row in rows if row.get("decision_success")), len(rows)),
            "complete_input_auto_execute_rate": _pct(len(complete_attempt), len(complete)),
            "open_world_execution_success_rate": _pct(len(complete_success), len(complete)),
            "false_clarification_rate": _pct(len(false_clarified), len(complete)),
            "failure_codes": dict(Counter(row.get("failure_code") for row in rows if row.get("failure_code"))),
        }

    return {
        "total_runs": total,
        "complete_runs": complete_total,
        "decision_success_rate": _pct(decision_success, total),
        "scenario_detect_accuracy": _pct(detect_success, total),
        "open_world_execution_success_rate": _pct(len(complete_succeeded), complete_total),
        "complete_input_auto_execute_rate": _pct(len(complete_attempted), complete_total),
        "false_clarification_rate": _pct(len(complete_clarified), complete_total),
        "browser_long_chain_success_rate": _pct(len(browser_succeeded), len(browser_complete)),
        "failure_code_topn": [
            {"code": code, "count": count}
            for code, count in Counter(row.get("failure_code") for row in records if row.get("failure_code")).most_common(10)
        ],
        "by_scenario": by_scenario,
    }


def _build_open_world_gate(summary: dict[str, Any]) -> dict[str, Any]:
    thresholds = {
        "decision_success_rate": CORE_BENCHMARK_THRESHOLDS["decision_success_rate"],
        "open_world_execution_success_rate": CORE_BENCHMARK_THRESHOLDS["open_world_execution_success_rate"],
        "browser_long_chain_success_rate": CORE_BENCHMARK_THRESHOLDS["browser_long_chain_success_rate"],
        "false_clarification_rate_max": CORE_BENCHMARK_THRESHOLDS["false_clarification_rate_max"],
        "complete_input_auto_execute_rate": CORE_BENCHMARK_THRESHOLDS["complete_input_auto_execute_rate"],
    }
    gates = {
        "decision_success_rate_ok": (
            float(summary.get("decision_success_rate", 0.0)) >= thresholds["decision_success_rate"]
        ),
        "open_world_execution_success_rate_ok": (
            float(summary.get("open_world_execution_success_rate", 0.0))
            >= thresholds["open_world_execution_success_rate"]
        ),
        "browser_long_chain_success_rate_ok": (
            float(summary.get("browser_long_chain_success_rate", 0.0))
            >= thresholds["browser_long_chain_success_rate"]
        ),
        "false_clarification_rate_ok": (
            float(summary.get("false_clarification_rate", 1.0))
            <= thresholds["false_clarification_rate_max"]
        ),
        "complete_input_auto_execute_rate_ok": (
            float(summary.get("complete_input_auto_execute_rate", 0.0))
            >= thresholds["complete_input_auto_execute_rate"]
        ),
    }
    return {"thresholds": thresholds, "gates": gates, "pass": all(gates.values())}


async def run_open_world_benchmark(
    config: RaccoonConfig | None = None,
    *,
    rounds: int = 1,
    pack_path: Path | None = None,
) -> dict[str, Any]:
    started_at = time.time()
    base = config or load_config()
    if getattr(base, "llm_provider", "mock") == "mock":
        return {
            "error": "open_world_benchmark_requires_real_llm",
            "message": "当前 llm_provider=mock，无法进行开放世界真实压测。",
        }

    pack = _load_pack(pack_path or (PROJECT_ROOT / "benchmarks" / "open_world" / "open_world_pack.json"))
    cases = list(pack.get("cases") or [])
    if not cases:
        return {"error": "open_world_pack_empty", "message": "open_world_pack 为空，无法执行压测。"}

    with tempfile.TemporaryDirectory(prefix="raccoon_open_world_benchmark_") as tmp:
        root = Path(tmp)
        run_config = base.model_copy(
            update={
                "db_path": root / "memcore.db",
                "schedules_dir": root / "schedules",
                "workflows_dir": root / "workflows",
                "auto_approve": True,
            }
        )

        event_bus = EventBus(run_config)
        schedule_store = ScheduleStore(run_config)
        schedule_store.recover()
        scheduler = Scheduler(event_bus, schedule_store, run_config)
        vault = VaultManager(run_config)
        llm = LLMFactory.create(run_config)
        learning_store = LearningRunStore(run_config)
        approval = ApprovalEngine(
            auto_approve=run_config.auto_approve,
            approval_timeout_seconds=run_config.approval_timeout_seconds,
            event_bus=event_bus,
        )
        engine = LearningEngine(
            config=run_config,
            vault_manager=vault,
            llm_client=llm,
            scheduler=scheduler,
            event_bus=event_bus,
            approval_engine=approval,
            learning_store=learning_store,
        )

        rows: list[dict[str, Any]] = []
        await event_bus.start()
        await approval.start()
        try:
            seq = 0
            for round_no in range(1, max(1, int(rounds)) + 1):
                for case in cases:
                    seq += 1
                    prompt = str(case.get("prompt") or "")
                    expected_scenario = str(case.get("scenario_id") or "")
                    input_complete = bool(case.get("input_complete"))
                    case_id = str(case.get("case_id") or seq)

                    task = Task(
                        conversation_id=f"open_world_r{round_no}_{case_id}_{seq}",
                        user_id="open_world_benchmark",
                        origin_message=prompt,
                        skill_name="learning",
                        context={
                            "scenario_id": expected_scenario,
                            "round": round_no,
                            "pack": pack.get("pack_id", "open_world"),
                            "benchmark_case_id": case_id,
                        },
                    )
                    result, fallback_failure = await _run_case_with_timeout(engine, task, prompt)
                    if expected_scenario == "login_form_chain" and fallback_failure == "benchmark_case_timeout":
                        retry_task = Task(
                            conversation_id=f"open_world_r{round_no}_{case_id}_{seq}_retry1",
                            user_id="open_world_benchmark",
                            origin_message=prompt,
                            skill_name="learning",
                            context={
                                "scenario_id": expected_scenario,
                                "round": round_no,
                                "pack": pack.get("pack_id", "open_world"),
                                "benchmark_case_id": case_id,
                                "retry": 1,
                            },
                        )
                        result, fallback_failure = await _run_case_with_timeout(engine, retry_task, prompt)
                    run_id = str(result.get("learning_run_id") or "")
                    run = learning_store.get(run_id) if run_id else None
                    failure_code = str(getattr(run, "failure_code", "") or fallback_failure)
                    handling_outcome = str(getattr(run, "handling_outcome", "") or ("failed" if failure_code else ""))
                    rows.append(
                        {
                            "round": round_no,
                            "case_id": case_id,
                            "input_complete": input_complete,
                            "expected_scenario": expected_scenario,
                            "detected_scenario": str(getattr(run, "scenario_id", "") or ""),
                            "decision_success": bool(getattr(run, "decision_success", False)),
                            "execution_attempted": bool(getattr(run, "execution_attempted", False) or bool(fallback_failure)),
                            "execution_success": bool(getattr(run, "execution_success", False)),
                            "handling_outcome": handling_outcome,
                            "clarification_reason": str(getattr(run, "clarification_reason", "") or ""),
                            "failure_code": failure_code,
                            "quality_score": float(getattr(run, "quality_score", 0.0) or 0.0),
                            "reply_preview": str(result.get("reply", ""))[:240],
                        }
                    )
        finally:
            await approval.stop()
            await event_bus.stop()
            try:
                from skills.web_automate.session_manager import shutdown_session_manager

                await shutdown_session_manager(reset_instance=True)
            except Exception:
                pass

    summary = _summarize_open_world_records(rows)
    gate = _build_open_world_gate(summary)

    mismatches = [
        {
            "case_id": row.get("case_id"),
            "expected_scenario": row.get("expected_scenario"),
            "detected_scenario": row.get("detected_scenario"),
            "input_complete": row.get("input_complete"),
            "handling_outcome": row.get("handling_outcome"),
            "reply_preview": row.get("reply_preview", ""),
        }
        for row in rows
        if str(row.get("expected_scenario") or "") != str(row.get("detected_scenario") or "")
    ]
    failed_samples = [
        {
            "case_id": row.get("case_id"),
            "scenario_id": row.get("expected_scenario"),
            "failure_code": row.get("failure_code") or "execution_not_success",
            "handling_outcome": row.get("handling_outcome"),
            "input_complete": row.get("input_complete"),
            "reply_preview": row.get("reply_preview", ""),
        }
        for row in rows
        if bool(row.get("input_complete")) and not bool(row.get("execution_success"))
    ]

    overall = {
        "total_runs": summary["total_runs"],
        "complete_runs": summary["complete_runs"],
        "decision_success_rate": summary["decision_success_rate"],
        "scenario_detect_accuracy": summary["scenario_detect_accuracy"],
        "open_world_execution_success_rate": summary["open_world_execution_success_rate"],
        "browser_long_chain_success_rate": summary["browser_long_chain_success_rate"],
        "false_clarification_rate": summary["false_clarification_rate"],
        "complete_input_auto_execute_rate": summary["complete_input_auto_execute_rate"],
        "failure_code_topn": summary["failure_code_topn"],
        "pass": gate["pass"],
        "gates": gate["gates"],
    }

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_seconds": round(time.time() - started_at, 3),
        "mode": "open_world_external",
        "pack_id": str(pack.get("pack_id") or "open_world"),
        "rounds": max(1, int(rounds)),
        "thresholds": gate["thresholds"],
        "overall": overall,
        "by_scenario": summary["by_scenario"],
        "mismatch_samples": mismatches[:20],
        "failed_samples": failed_samples[:20],
    }


def run_open_world_benchmark_sync(
    config: RaccoonConfig | None = None,
    *,
    rounds: int = 1,
    pack_path: Path | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        run_open_world_benchmark(
            config=config,
            rounds=rounds,
            pack_path=pack_path,
        )
    )
