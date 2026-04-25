"""核心场景定义与学习链路基准聚合。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.brain.failure_guidance import guidance_for_failure


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    name: str
    keywords: tuple[str, ...]


CORE_SCENARIOS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec("daily_brief", "每日简报", ("日报", "简报", "热点", "热搜", "汇总", "早报")),
    ScenarioSpec("price_monitor", "价格监控", ("价格", "降价", "监控商品", "比价", "电商", "库存")),
    ScenarioSpec("login_form_chain", "登录表单长链路", ("登录", "验证码", "表单", "提交", "多步", "长链路")),
    ScenarioSpec("material_collection", "素材采集", ("素材", "采集", "抓取", "多源", "去重", "整理")),
    ScenarioSpec("reminder_schedule", "提醒调度", ("提醒", "定时", "cron", "每天", "每周", "闹钟")),
    ScenarioSpec("remote_exec", "远程执行", ("执行命令", "shell", "远程", "审批", "终端", "脚本")),
)

CORE_SCENARIO_IDS = tuple(spec.scenario_id for spec in CORE_SCENARIOS)

CORE_BENCHMARK_THRESHOLDS = {
    "decision_success_rate": 0.95,
    "execution_success_rate": 0.85,
    "browser_chain_execution_success_rate": 0.90,
    "stuck_rate_max": 0.01,
    # 兼容旧报告字段
    "first_pass_rate": 0.70,
    "final_success_rate": 0.85,
    "browser_chain_success_rate": 0.90,
}


def normalize_intent_phrase(text: str) -> str:
    value = str(text or "").strip().lower()
    value = re.sub(r"[，。！？,.!?；;：:（）()【】\\[\\]{}\"'`]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    synonyms = {
        "截屏": "截图",
        "截个图": "截图",
        "屏幕截图": "截图",
        "看一下": "查看",
        "看下": "查看",
        "瞅一下": "查看",
        "查一下": "查看",
        "查下": "查看",
        "获取": "抓取",
        "拿一下": "抓取",
        "收集": "采集",
        "写个": "生成",
        "弄个": "生成",
        "监测": "监控",
        "跟踪": "监控",
        "追踪": "监控",
        "提醒一下": "提醒",
        "提醒下": "提醒",
    }
    for src, target in synonyms.items():
        value = value.replace(src, target)
    regex_synonyms = {
        r"\bscreenshot\b": "截图",
        r"\bscreen\s*shot\b": "截图",
        r"\bsummary\b": "简报",
        r"\bsummarize\b": "简报",
        r"\bdigest\b": "简报",
        r"\bmonitor(?:ing)?\b": "监控",
        r"\btrack(?:ing)?\b": "监控",
        r"\balert\b": "提醒",
        r"\bnotify\b": "提醒",
        r"\bcollect(?:ion)?\b": "采集",
        r"\bcrawl(?:ing)?\b": "抓取",
    }
    for pattern, target in regex_synonyms.items():
        value = re.sub(pattern, target, value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def detect_core_scenario_id(text: str) -> str | None:
    normalized = normalize_intent_phrase(text)
    if not normalized:
        return None
    for spec in CORE_SCENARIOS:
        if any(token in normalized for token in spec.keywords):
            return spec.scenario_id
    return None


def evaluate_quality(
    scenario_id: str | None,
    *,
    analysis: dict[str, Any] | None,
    validation: dict[str, Any] | None,
    execution_result: dict[str, Any] | None,
) -> tuple[float, dict[str, Any]]:
    analysis = analysis or {}
    validation = validation or {}
    execution_result = execution_result or {}
    reply = str(execution_result.get("reply") or validation.get("reply") or "")
    reply_lower = reply.lower()

    checks: dict[str, float] = {}

    checks["protocol"] = 1.0 if validation.get("success") else 0.0
    checks["fields_complete"] = 1.0 if (
        bool(reply.strip())
        and (
            len(reply.strip().splitlines()) >= 2
            or any(marker in reply for marker in ("1.", "2.", "-", "•", "：", ":"))
        )
    ) else 0.0
    checks["source_explainable"] = 1.0 if (
        bool(analysis.get("data_strategy"))
        or bool(analysis.get("approach"))
        or bool(analysis.get("source"))
        or bool(analysis.get("source_url"))
    ) else 0.0
    checks["freshness"] = 1.0 if (
        bool(re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", reply))
        or bool(re.search(r"\d{1,2}:\d{2}", reply))
        or any(word in reply_lower for word in ("最新", "刚刚", "today", "today's", "updated"))
    ) else 0.0

    if scenario_id == "login_form_chain":
        checks["chain_state"] = 1.0 if any(
            word in reply_lower
            for word in ("登录成功", "提交成功", "checkpoint", "恢复", "已继续", "resume")
        ) else 0.0
    elif scenario_id == "remote_exec":
        checks["traceable"] = 1.0 if any(
            word in reply_lower
            for word in ("task", "approval", "审批", "执行结果", "trace", "run_id")
        ) else 0.0

    score = sum(checks.values()) / max(len(checks), 1)
    return score, {"checks": checks, "scenario_id": scenario_id}


def build_core_scenario_report(
    aggregated_rows: list[dict[str, object]],
    top_failures: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    row_map = {str(row["scenario_id"]): row for row in aggregated_rows}
    scenarios: list[dict[str, Any]] = []

    total_runs = 0
    total_first_pass = 0.0
    total_final_success = 0.0
    total_decision_success = 0.0
    total_execution_attempted = 0
    total_execution_success = 0.0
    total_stuck = 0
    handling_totals = {"clarified": 0, "reused": 0, "executed": 0, "failed": 0}

    for spec in CORE_SCENARIOS:
        row = row_map.get(spec.scenario_id, {})
        runs = int(row.get("runs", 0) or 0)
        first_pass_rate = float(row.get("first_pass_rate", 0.0) or 0.0)
        final_success_rate = float(row.get("final_success_rate", 0.0) or 0.0)
        decision_success_rate = float(row.get("decision_success_rate", first_pass_rate) or 0.0)
        execution_attempt_rate = float(row.get("execution_attempt_rate", 0.0) or 0.0)
        execution_success_rate = float(row.get("execution_success_rate", final_success_rate) or 0.0)
        stuck_rate = float(row.get("stuck_rate", 0.0) or 0.0)
        avg_repair_count = float(row.get("avg_repair_count", 0.0) or 0.0)
        avg_quality_score = float(row.get("avg_quality_score", 0.0) or 0.0)
        handling_outcomes = row.get("handling_outcomes", {}) or {}
        clarified = int(handling_outcomes.get("clarified", 0) or 0)
        reused = int(handling_outcomes.get("reused", 0) or 0)
        executed = int(handling_outcomes.get("executed", 0) or 0)
        failed = int(handling_outcomes.get("failed", 0) or 0)

        total_runs += runs
        total_first_pass += first_pass_rate * runs
        total_final_success += final_success_rate * runs
        total_decision_success += decision_success_rate * runs
        attempted_runs = int(row.get("execution_attempted_runs", 0) or round(execution_attempt_rate * runs))
        success_runs = int(row.get("execution_success_runs", 0) or round(execution_success_rate * attempted_runs))
        total_execution_attempted += attempted_runs
        total_execution_success += success_runs
        total_stuck += int(row.get("stuck_runs", 0) or round(stuck_rate * runs))
        handling_totals["clarified"] += clarified
        handling_totals["reused"] += reused
        handling_totals["executed"] += executed
        handling_totals["failed"] += failed

        scenarios.append(
            {
                "scenario_id": spec.scenario_id,
                "name": spec.name,
                "runs": runs,
                "first_pass_rate": first_pass_rate,
                "final_success_rate": final_success_rate,
                "decision_success_rate": decision_success_rate,
                "execution_attempt_rate": execution_attempt_rate,
                "execution_success_rate": execution_success_rate,
                "stuck_rate": stuck_rate,
                "avg_repair_count": avg_repair_count,
                "avg_quality_score": avg_quality_score,
                "handling_outcomes": {
                    "clarified": clarified,
                    "reused": reused,
                    "executed": executed,
                    "failed": failed,
                },
            }
        )

    overall_first_pass = (total_first_pass / total_runs) if total_runs else 0.0
    overall_final_success = (total_final_success / total_runs) if total_runs else 0.0
    overall_decision_success = (total_decision_success / total_runs) if total_runs else 0.0
    overall_execution_success = (
        (total_execution_success / total_execution_attempted)
        if total_execution_attempted
        else 0.0
    )
    overall_stuck_rate = (total_stuck / total_runs) if total_runs else 0.0
    browser_row = row_map.get("login_form_chain", {})
    browser_chain_execution_success = float(
        browser_row.get(
            "execution_success_rate",
            browser_row.get("final_success_rate", 0.0),
        )
        or 0.0
    )

    gates = {
        "decision_success_rate_ok": (
            overall_decision_success >= CORE_BENCHMARK_THRESHOLDS["decision_success_rate"]
        ),
        "execution_success_rate_ok": (
            overall_execution_success >= CORE_BENCHMARK_THRESHOLDS["execution_success_rate"]
        ),
        "browser_chain_execution_success_rate_ok": (
            browser_chain_execution_success
            >= CORE_BENCHMARK_THRESHOLDS["browser_chain_execution_success_rate"]
        ),
        "stuck_rate_ok": overall_stuck_rate <= CORE_BENCHMARK_THRESHOLDS["stuck_rate_max"],
    }
    legacy_gates = {
        "first_pass_rate_ok": overall_first_pass >= CORE_BENCHMARK_THRESHOLDS["first_pass_rate"],
        "final_success_rate_ok": overall_final_success >= CORE_BENCHMARK_THRESHOLDS["final_success_rate"],
        "browser_chain_success_ok": browser_chain_execution_success >= CORE_BENCHMARK_THRESHOLDS["browser_chain_success_rate"],
    }
    handling_total_count = sum(handling_totals.values())
    handling_rates = {
        key: (value / handling_total_count if handling_total_count else 0.0)
        for key, value in handling_totals.items()
    }

    enriched_failures = []
    for item in top_failures or []:
        code = str(item.get("failure_code") or "unknown_error")
        guidance = guidance_for_failure(code)
        enriched_failures.append(
            {
                **item,
                "hint": guidance.get("hint", ""),
                "action": guidance.get("action", ""),
            }
        )

    return {
        "thresholds": CORE_BENCHMARK_THRESHOLDS,
        "overall": {
            "runs": total_runs,
            "decision_success_rate": overall_decision_success,
            "execution_attempts": total_execution_attempted,
            "execution_success_rate": overall_execution_success,
            "browser_chain_execution_success_rate": browser_chain_execution_success,
            "stuck_rate": overall_stuck_rate,
            "first_pass_rate": overall_first_pass,
            "final_success_rate": overall_final_success,
            "browser_chain_success_rate": browser_chain_execution_success,
            "handling_outcomes": handling_totals,
            "handling_outcome_rates": handling_rates,
            "pass": all(gates.values()),
            "gates": gates,
            "legacy_gates": legacy_gates,
        },
        "scenarios": scenarios,
        "failure_code_topn": enriched_failures,
    }
