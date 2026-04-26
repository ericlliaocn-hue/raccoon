from __future__ import annotations

import asyncio

from src.brain import open_world_benchmark_runner as open_world_runner
from src.brain.open_world_benchmark_runner import (
    _build_open_world_gate,
    _run_case_with_timeout,
    _summarize_open_world_records,
)


def test_open_world_summary_metrics_are_computed_from_complete_inputs():
    records = [
        {
            "case_id": "c1",
            "expected_scenario": "price_monitor",
            "detected_scenario": "price_monitor",
            "input_complete": True,
            "decision_success": True,
            "execution_attempted": True,
            "execution_success": True,
            "handling_outcome": "executed",
            "failure_code": "",
        },
        {
            "case_id": "c2",
            "expected_scenario": "login_form_chain",
            "detected_scenario": "login_form_chain",
            "input_complete": True,
            "decision_success": True,
            "execution_attempted": True,
            "execution_success": False,
            "handling_outcome": "failed",
            "failure_code": "browser_action_failed",
        },
        {
            "case_id": "c3",
            "expected_scenario": "remote_exec",
            "detected_scenario": "remote_exec",
            "input_complete": True,
            "decision_success": True,
            "execution_attempted": False,
            "execution_success": False,
            "handling_outcome": "clarified",
            "failure_code": "",
        },
        {
            "case_id": "c4",
            "expected_scenario": "daily_brief",
            "detected_scenario": "material_collection",
            "input_complete": False,
            "decision_success": False,
            "execution_attempted": False,
            "execution_success": False,
            "handling_outcome": "clarified",
            "failure_code": "",
        },
    ]

    summary = _summarize_open_world_records(records)

    assert summary["total_runs"] == 4
    assert summary["complete_runs"] == 3
    assert summary["scenario_detect_accuracy"] == 0.75
    assert summary["decision_success_rate"] == 0.75
    assert summary["open_world_execution_success_rate"] == 1 / 3
    assert summary["complete_input_auto_execute_rate"] == 2 / 3
    assert summary["false_clarification_rate"] == 1 / 3
    assert summary["browser_long_chain_success_rate"] == 0.0
    assert summary["failure_code_topn"][0]["code"] == "browser_action_failed"


def test_open_world_gate_fails_when_false_clarification_or_auto_execute_not_met():
    summary = {
        "decision_success_rate": 0.96,
        "open_world_execution_success_rate": 0.81,
        "browser_long_chain_success_rate": 0.92,
        "false_clarification_rate": 0.12,
        "complete_input_auto_execute_rate": 0.75,
    }
    gate = _build_open_world_gate(summary)

    assert gate["gates"]["decision_success_rate_ok"] is True
    assert gate["gates"]["open_world_execution_success_rate_ok"] is True
    assert gate["gates"]["browser_long_chain_success_rate_ok"] is True
    assert gate["gates"]["false_clarification_rate_ok"] is False
    assert gate["gates"]["complete_input_auto_execute_rate_ok"] is False
    assert gate["pass"] is False


def test_open_world_runner_case_timeout_returns_timeout_failure(monkeypatch):
    class SlowEngine:
        async def learn(self, task, prompt):
            await asyncio.sleep(0.02)
            return {"learning_run_id": "x"}

    monkeypatch.setattr(open_world_runner, "CASE_TIMEOUT_SECONDS", 0.001)
    result, failure_code = asyncio.run(_run_case_with_timeout(SlowEngine(), object(), "hello"))

    assert failure_code == "benchmark_case_timeout"
    assert result["learning_run_id"] == ""
