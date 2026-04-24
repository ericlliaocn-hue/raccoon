"""核心场景定义与学习链路基准聚合。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


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
    "first_pass_rate": 0.70,
    "final_success_rate": 0.85,
    "browser_chain_success_rate": 0.90,
    "stuck_rate_max": 0.01,
}


def normalize_intent_phrase(text: str) -> str:
    value = str(text or "").strip().lower()
    value = re.sub(r"[，。！？,.!?；;：:（）()【】\\[\\]{}\"'`]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    synonyms = {
        "截屏": "截图",
        "截个图": "截图",
        "看一下": "查看",
        "瞅一下": "查看",
        "获取": "抓取",
        "拿一下": "抓取",
        "写个": "生成",
        "弄个": "生成",
        "监测": "监控",
        "跟踪": "监控",
    }
    for src, target in synonyms.items():
        value = value.replace(src, target)
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


def build_core_scenario_report(aggregated_rows: list[dict[str, object]]) -> dict[str, Any]:
    row_map = {str(row["scenario_id"]): row for row in aggregated_rows}
    scenarios: list[dict[str, Any]] = []

    total_runs = 0
    total_first_pass = 0.0
    total_final_success = 0.0

    for spec in CORE_SCENARIOS:
        row = row_map.get(spec.scenario_id, {})
        runs = int(row.get("runs", 0) or 0)
        first_pass_rate = float(row.get("first_pass_rate", 0.0) or 0.0)
        final_success_rate = float(row.get("final_success_rate", 0.0) or 0.0)
        avg_repair_count = float(row.get("avg_repair_count", 0.0) or 0.0)
        avg_quality_score = float(row.get("avg_quality_score", 0.0) or 0.0)

        total_runs += runs
        total_first_pass += first_pass_rate * runs
        total_final_success += final_success_rate * runs

        scenarios.append(
            {
                "scenario_id": spec.scenario_id,
                "name": spec.name,
                "runs": runs,
                "first_pass_rate": first_pass_rate,
                "final_success_rate": final_success_rate,
                "avg_repair_count": avg_repair_count,
                "avg_quality_score": avg_quality_score,
            }
        )

    overall_first_pass = (total_first_pass / total_runs) if total_runs else 0.0
    overall_final_success = (total_final_success / total_runs) if total_runs else 0.0
    browser_row = row_map.get("login_form_chain", {})
    browser_chain_success = float(browser_row.get("final_success_rate", 0.0) or 0.0)

    gates = {
        "first_pass_rate_ok": overall_first_pass >= CORE_BENCHMARK_THRESHOLDS["first_pass_rate"],
        "final_success_rate_ok": overall_final_success >= CORE_BENCHMARK_THRESHOLDS["final_success_rate"],
        "browser_chain_success_ok": browser_chain_success >= CORE_BENCHMARK_THRESHOLDS["browser_chain_success_rate"],
    }

    return {
        "thresholds": CORE_BENCHMARK_THRESHOLDS,
        "overall": {
            "runs": total_runs,
            "first_pass_rate": overall_first_pass,
            "final_success_rate": overall_final_success,
            "browser_chain_success_rate": browser_chain_success,
            "pass": all(gates.values()),
            "gates": gates,
        },
        "scenarios": scenarios,
    }
