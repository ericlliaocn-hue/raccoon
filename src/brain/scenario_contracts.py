"""Core-scenario contracts for request precheck and execution validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.brain.core_benchmark import normalize_intent_phrase


@dataclass(frozen=True)
class ScenarioContract:
    """Minimal contract used by LearningEngine to keep core scenarios deterministic."""

    scenario_id: str
    missing_reason: str | None = None
    required_description: str = ""
    execution_description: str = ""


_CONTRACTS: dict[str, ScenarioContract] = {
    "price_monitor": ScenarioContract(
        scenario_id="price_monitor",
        missing_reason="missing_price_target",
        required_description="需要商品链接、SKU 或平台商品标识",
        execution_description="执行结果需包含监控创建信号或目标信息",
    ),
    "login_form_chain": ScenarioContract(
        scenario_id="login_form_chain",
        missing_reason="missing_form_target",
        required_description="需要真实站点 URL 与登录/提交上下文",
        execution_description="执行结果需体现登录/提交，并包含 checkpoint 信号",
    ),
    "remote_exec": ScenarioContract(
        scenario_id="remote_exec",
        missing_reason="missing_shell_command",
        required_description="需要具体 shell 命令或脚本路径",
        execution_description="执行结果需包含审批/trace/task 等可追踪信号",
    ),
    "reminder_schedule": ScenarioContract(
        scenario_id="reminder_schedule",
        missing_reason="missing_schedule_target",
        required_description="需要时间表达和提醒内容",
        execution_description="执行结果需包含调度创建信号",
    ),
}


def contract_for(scenario_id: str | None) -> ScenarioContract | None:
    if not scenario_id:
        return None
    return _CONTRACTS.get(str(scenario_id))


def evaluate_request_contract(
    scenario_id: str | None,
    *,
    has_monitor_target: bool,
    has_form_target: bool,
    has_shell_command: bool,
    has_schedule_target: bool,
) -> str | None:
    """Return missing-reason when a core scenario does not satisfy entry contract."""
    if scenario_id == "price_monitor" and not has_monitor_target:
        return "missing_price_target"
    if scenario_id == "login_form_chain" and not has_form_target:
        return "missing_form_target"
    if scenario_id == "remote_exec" and not has_shell_command:
        return "missing_shell_command"
    if scenario_id == "reminder_schedule" and not has_schedule_target:
        return "missing_schedule_target"
    return None


def evaluate_execution_contract(
    scenario_id: str | None,
    *,
    reply: str,
    execution_result: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Validate scenario-specific execution evidence.

    This is intentionally strict for risky/long-chain scenarios and lightweight for
    content/reuse scenarios.
    """
    if not scenario_id:
        return True, None

    execution_result = execution_result or {}
    artifacts = execution_result.get("artifacts") if isinstance(execution_result, dict) else {}
    if not isinstance(artifacts, dict):
        artifacts = {}

    text = normalize_intent_phrase(reply)
    text_lower = text.lower()

    if scenario_id == "price_monitor":
        has_structured_target = bool(
            artifacts.get("target")
            or artifacts.get("url")
            or artifacts.get("path")
            or artifacts.get("sku")
            or artifacts.get("snapshot_id")
            or artifacts.get("content_hash")
        )
        has_monitor_signal = any(
            token in text for token in ("监控已创建", "快照已创建", "快照已更新", "提醒", "降价", "库存")
        )
        subcmd = str(artifacts.get("subcmd") or "").lower()
        subcmd_ok = subcmd in {"snapshot", "check", "check_all"}
        if not (has_monitor_signal or (has_structured_target and subcmd_ok)):
            return False, "execution_contract_price_target_missing"
        return True, None

    if scenario_id == "login_form_chain":
        checkpoints = artifacts.get("checkpoints")
        checkpoint_rows = checkpoints if isinstance(checkpoints, list) else []
        has_submit_checkpoint = any(
            isinstance(row, dict)
            and row.get("stage") == "after"
            and row.get("success") is True
            and (
                "submit" in str(row.get("checkpoint_key") or "").lower()
                or (
                    str(row.get("action") or "").lower() in {"click", "press_key"}
                    and "login" in str(row.get("url") or "").lower()
                )
            )
            for row in checkpoint_rows
        )
        has_submit_signal = has_submit_checkpoint or any(
            token in text for token in ("登录成功", "提交成功", "上传成功", "submit", "login")
        )
        has_checkpoint = bool(checkpoints) or any(
            token in text for token in ("checkpoint", "恢复", "续跑", "resume")
        )
        if not has_submit_signal:
            return False, "execution_contract_login_submit_missing"
        if not has_checkpoint:
            return False, "execution_contract_checkpoint_missing"
        return True, None

    if scenario_id == "remote_exec":
        has_command = bool(artifacts.get("command"))
        has_structured_trace = bool(
            artifacts.get("trace")
            or artifacts.get("task_id")
            or (artifacts.get("exit_code") is not None)
        )
        text_traceable = any(
            token in text_lower for token in ("approval", "审批", "trace", "task", "run_id", "执行结果")
        )
        if not ((has_command and has_structured_trace) or text_traceable):
            return False, "execution_contract_trace_missing"
        return True, None

    if scenario_id == "reminder_schedule":
        has_schedule_signal = bool(
            artifacts.get("schedule_created")
            or any(token in text for token in ("已创建任务", "定时", "cron", "schedule"))
        )
        if not has_schedule_signal:
            return False, "execution_contract_schedule_missing"
        return True, None

    return True, None
