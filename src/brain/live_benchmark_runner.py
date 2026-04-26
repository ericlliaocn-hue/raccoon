"""真实外站压测 runner（稳定包 + 扰动包）。"""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from src.brain.core_benchmark import CORE_BENCHMARK_THRESHOLDS
from src.brain.learning_engine import LearningEngine
from src.brain.learning_store import LearningRunStore
from src.config import RaccoonConfig, load_config
from src.eventbus.bus import EventBus
from src.llm import LLMFactory
from src.scheduler.schedule_store import ScheduleStore
from src.scheduler.scheduler import Scheduler
from src.skill_vault.vault_manager import VaultManager
from src.supervisor.approval_engine import ApprovalEngine
from src.types import Task


STABLE_CASES: tuple[tuple[str, str], ...] = (
    ("daily_brief", "帮我做一份今天AI与半导体简报，列出来源和时间。"),
    ("price_monitor", "请监控 https://item.jd.com/100012043978.html 降价到4299提醒我。"),
    ("login_form_chain", "后台模式 登录 https://the-internet.herokuapp.com/login ，账号 tomsmith 密码 SuperSecretPassword! 登录后截图"),
    ("material_collection", "收集本周AI热点素材并去重，按来源输出。"),
    ("reminder_schedule", "每个工作日 09:30 提醒我检查客户回复。"),
    ("remote_exec", "先审批，再执行 `pwd` 并返回结果。"),
)

PERTURB_CASES: dict[str, tuple[str, ...]] = {
    "daily_brief": (
        "今天开盘前给我AI+半导体简报，附来源时间",
        "整理今日技术热点摘要（AI方向），按来源列出",
        "帮我做个早报，AI热点+链接",
    ),
    "price_monitor": (
        "盯一下 https://item.jd.com/100012043978.html ，降到4299提醒",
        "监控 sku=100012043978 库存恢复通知我",
        "监控京东这个商品价格波动，链接 https://item.jd.com/100012043978.html",
    ),
    "login_form_chain": (
        "后台模式 登录 https://the-internet.herokuapp.com/login 账号 tomsmith 密码 SuperSecretPassword! 登录后截图",
        "请在后台浏览器里 signin https://the-internet.herokuapp.com/login user tomsmith password SuperSecretPassword!",
        "打开 https://the-internet.herokuapp.com/login 完成登录流程并截图（账号 tomsmith 密码 SuperSecretPassword!）",
    ),
    "material_collection": (
        "收集本周AIGC热点素材并去重",
        "抓取多源热门内容做素材池，按平台汇总",
        "整理爆款选题素材，去重后输出",
    ),
    "reminder_schedule": (
        "每个工作日 09:30 提醒我检查客户回复",
        "每周五18:00 提醒我复盘失败案例",
        "cron 30 9 * * 1-5 提醒我看盘前消息",
    ),
    "remote_exec": (
        "先审批，执行 `pwd` 并返回结果",
        "请 run 命令 `ls -la`，高风险先确认",
        "执行脚本 /bin/pwd 前先审批",
    ),
}

CASE_TIMEOUT_SECONDS = 180.0


def _pct(success: int, total: int) -> float:
    return (success / total) if total else 0.0


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


def _summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    attempted = [r for r in records if r.get("execution_attempted")]
    succeeded = [r for r in attempted if r.get("execution_success")]
    browser_attempted = [r for r in attempted if r.get("scenario_id") == "login_form_chain"]
    browser_succeeded = [r for r in browser_attempted if r.get("execution_success")]
    decision_success = sum(1 for r in records if r.get("decision_success"))

    by_scenario: dict[str, Any] = {}
    for scenario_id, _ in STABLE_CASES:
        rows = [r for r in records if r.get("scenario_id") == scenario_id]
        row_attempted = [r for r in rows if r.get("execution_attempted")]
        row_succeeded = [r for r in row_attempted if r.get("execution_success")]
        by_scenario[scenario_id] = {
            "runs": len(rows),
            "decision_success_rate": _pct(sum(1 for r in rows if r.get("decision_success")), len(rows)),
            "execution_attempt_rate": _pct(len(row_attempted), len(rows)),
            "execution_success_rate": _pct(len(row_succeeded), len(row_attempted)),
            "failure_codes": dict(Counter(r.get("failure_code") for r in rows if r.get("failure_code"))),
        }

    return {
        "total_runs": len(records),
        "decision_success_rate": _pct(decision_success, len(records)),
        "execution_attempt_rate": _pct(len(attempted), len(records)),
        "execution_success_rate": _pct(len(succeeded), len(attempted)),
        "browser_chain_execution_success_rate": _pct(len(browser_succeeded), len(browser_attempted)),
        "failure_code_topn": [
            {"code": code, "count": count}
            for code, count in Counter(
                r.get("failure_code") for r in records if r.get("failure_code")
            ).most_common(10)
        ],
        "by_scenario": by_scenario,
    }


def _build_gate(summary: dict[str, Any]) -> dict[str, Any]:
    decision = float(summary.get("decision_success_rate", 0.0))
    execution = float(summary.get("execution_success_rate", 0.0))
    browser = float(summary.get("browser_chain_execution_success_rate", 0.0))
    gates = {
        "decision_success_rate_ok": decision >= CORE_BENCHMARK_THRESHOLDS["decision_success_rate"],
        "execution_success_rate_ok": execution >= CORE_BENCHMARK_THRESHOLDS["execution_success_rate"],
        "browser_chain_execution_success_rate_ok": (
            browser >= CORE_BENCHMARK_THRESHOLDS["browser_chain_execution_success_rate"]
        ),
    }
    return {
        "thresholds": {
            "decision_success_rate": CORE_BENCHMARK_THRESHOLDS["decision_success_rate"],
            "execution_success_rate": CORE_BENCHMARK_THRESHOLDS["execution_success_rate"],
            "browser_chain_execution_success_rate": CORE_BENCHMARK_THRESHOLDS[
                "browser_chain_execution_success_rate"
            ],
        },
        "gates": gates,
        "pass": all(gates.values()),
    }


async def run_live_benchmark(
    config: RaccoonConfig | None = None,
    *,
    stable_rounds: int = 5,
    perturb_rounds: int = 3,
) -> dict[str, Any]:
    started_at = time.time()
    base = config or load_config()
    if getattr(base, "llm_provider", "mock") == "mock":
        return {
            "error": "live_benchmark_requires_real_llm",
            "message": "当前 llm_provider=mock，无法进行真实外站压测。",
        }

    with tempfile.TemporaryDirectory(prefix="raccoon_live_benchmark_") as tmp:
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

        stable_records: list[dict[str, Any]] = []
        perturb_records: list[dict[str, Any]] = []

        await event_bus.start()
        await approval.start()
        try:
            seq = 0
            for round_no in range(1, max(1, stable_rounds) + 1):
                for scenario_id, prompt in STABLE_CASES:
                    seq += 1
                    task = Task(
                        conversation_id=f"live_stable_r{round_no}_{scenario_id}_{seq}",
                        user_id="live_benchmark",
                        origin_message=prompt,
                        skill_name="learning",
                        context={"scenario_id": scenario_id, "round": round_no, "pack": "stable"},
                    )
                    result, fallback_failure = await _run_case_with_timeout(engine, task, prompt)
                    if scenario_id == "login_form_chain" and fallback_failure == "benchmark_case_timeout":
                        retry_task = Task(
                            conversation_id=f"live_stable_r{round_no}_{scenario_id}_{seq}_retry1",
                            user_id="live_benchmark",
                            origin_message=prompt,
                            skill_name="learning",
                            context={"scenario_id": scenario_id, "round": round_no, "pack": "stable", "retry": 1},
                        )
                        result, fallback_failure = await _run_case_with_timeout(engine, retry_task, prompt)
                    run_id = str(result.get("learning_run_id") or "")
                    run = learning_store.get(run_id) if run_id else None
                    failure_code = str(getattr(run, "failure_code", "") or fallback_failure)
                    handling_outcome = str(getattr(run, "handling_outcome", "") or ("failed" if failure_code else ""))
                    stable_records.append(
                        {
                            "pack": "stable",
                            "round": round_no,
                            "scenario_id": scenario_id,
                            "run_id": run_id,
                            "decision_success": bool(getattr(run, "decision_success", False)),
                            "execution_attempted": bool(getattr(run, "execution_attempted", False) or bool(fallback_failure)),
                            "execution_success": bool(getattr(run, "execution_success", False)),
                            "handling_outcome": handling_outcome,
                            "failure_code": failure_code or None,
                            "quality_score": float(getattr(run, "quality_score", 0.0) or 0.0),
                            "reply_preview": str(result.get("reply", ""))[:200],
                        }
                    )

            for round_no in range(1, max(1, perturb_rounds) + 1):
                for scenario_id in PERTURB_CASES:
                    seq += 1
                    prompts = PERTURB_CASES[scenario_id]
                    prompt = prompts[(round_no - 1) % len(prompts)]
                    task = Task(
                        conversation_id=f"live_perturb_r{round_no}_{scenario_id}_{seq}",
                        user_id="live_benchmark",
                        origin_message=prompt,
                        skill_name="learning",
                        context={"round": round_no, "pack": "perturb"},
                    )
                    result, fallback_failure = await _run_case_with_timeout(engine, task, prompt)
                    if scenario_id == "login_form_chain" and fallback_failure == "benchmark_case_timeout":
                        retry_task = Task(
                            conversation_id=f"live_perturb_r{round_no}_{scenario_id}_{seq}_retry1",
                            user_id="live_benchmark",
                            origin_message=prompt,
                            skill_name="learning",
                            context={"round": round_no, "pack": "perturb", "retry": 1},
                        )
                        result, fallback_failure = await _run_case_with_timeout(engine, retry_task, prompt)
                    run_id = str(result.get("learning_run_id") or "")
                    run = learning_store.get(run_id) if run_id else None
                    detected_scenario = str(getattr(run, "scenario_id", "") or "")
                    failure_code = str(getattr(run, "failure_code", "") or fallback_failure)
                    handling_outcome = str(getattr(run, "handling_outcome", "") or ("failed" if failure_code else ""))
                    perturb_records.append(
                        {
                            "pack": "perturb",
                            "round": round_no,
                            "expected_scenario": scenario_id,
                            "detected_scenario": detected_scenario,
                            "run_id": run_id,
                            "decision_success": bool(getattr(run, "decision_success", False)),
                            "execution_attempted": bool(getattr(run, "execution_attempted", False) or bool(fallback_failure)),
                            "execution_success": bool(getattr(run, "execution_success", False)),
                            "handling_outcome": handling_outcome,
                            "failure_code": failure_code or None,
                            "quality_score": float(getattr(run, "quality_score", 0.0) or 0.0),
                            "reply_preview": str(result.get("reply", ""))[:200],
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

    stable_summary = _summarize_records(stable_records)
    perturb_detect_ok = sum(
        1 for r in perturb_records if r.get("expected_scenario") == r.get("detected_scenario")
    )
    perturb_attempted = [r for r in perturb_records if r.get("execution_attempted")]
    perturb_succeeded = [r for r in perturb_attempted if r.get("execution_success")]
    perturb_summary = {
        "total_runs": len(perturb_records),
        "scenario_detect_accuracy": _pct(perturb_detect_ok, len(perturb_records)),
        "decision_success_rate": _pct(
            sum(1 for r in perturb_records if r.get("decision_success")),
            len(perturb_records),
        ),
        "execution_success_rate": _pct(len(perturb_succeeded), len(perturb_attempted)),
    }

    gate = _build_gate(stable_summary)
    overall = {
        "total_runs": len(stable_records) + len(perturb_records),
        "decision_success_rate": stable_summary["decision_success_rate"],
        "execution_success_rate": stable_summary["execution_success_rate"],
        "browser_chain_execution_success_rate": stable_summary["browser_chain_execution_success_rate"],
        "scenario_detect_accuracy": perturb_summary["scenario_detect_accuracy"],
        "failure_code_topn": stable_summary["failure_code_topn"],
        "pass": gate["pass"],
        "gates": gate["gates"],
    }

    mismatches = [
        {
            "expected_scenario": r.get("expected_scenario"),
            "detected_scenario": r.get("detected_scenario"),
            "reply_preview": r.get("reply_preview", ""),
        }
        for r in perturb_records
        if r.get("expected_scenario") != r.get("detected_scenario")
    ]
    failed_samples = [
        {
            "scenario_id": r.get("scenario_id"),
            "failure_code": r.get("failure_code"),
            "reply_preview": r.get("reply_preview", ""),
        }
        for r in stable_records
        if r.get("execution_attempted") and not r.get("execution_success")
    ]

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_seconds": round(time.time() - started_at, 3),
        "mode": "live_external",
        "rounds": {"stable": stable_rounds, "perturb": perturb_rounds},
        "thresholds": gate["thresholds"],
        "overall": overall,
        "stable_summary": stable_summary,
        "perturb_summary": perturb_summary,
        "mismatch_samples": mismatches[:20],
        "failed_samples": failed_samples[:20],
    }


def run_live_benchmark_sync(
    config: RaccoonConfig | None = None,
    *,
    stable_rounds: int = 5,
    perturb_rounds: int = 3,
) -> dict[str, Any]:
    return asyncio.run(
        run_live_benchmark(
            config=config,
            stable_rounds=stable_rounds,
            perturb_rounds=perturb_rounds,
        )
    )
