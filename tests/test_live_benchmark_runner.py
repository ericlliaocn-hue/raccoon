from __future__ import annotations

from src.brain.live_benchmark_runner import _build_gate, _summarize_records


def test_live_benchmark_summary_and_gate():
    records = [
        {
            "scenario_id": "daily_brief",
            "decision_success": True,
            "execution_attempted": True,
            "execution_success": True,
            "failure_code": None,
        },
        {
            "scenario_id": "price_monitor",
            "decision_success": True,
            "execution_attempted": True,
            "execution_success": True,
            "failure_code": None,
        },
        {
            "scenario_id": "login_form_chain",
            "decision_success": True,
            "execution_attempted": True,
            "execution_success": True,
            "failure_code": None,
        },
        {
            "scenario_id": "material_collection",
            "decision_success": True,
            "execution_attempted": True,
            "execution_success": True,
            "failure_code": None,
        },
        {
            "scenario_id": "reminder_schedule",
            "decision_success": True,
            "execution_attempted": True,
            "execution_success": True,
            "failure_code": None,
        },
        {
            "scenario_id": "remote_exec",
            "decision_success": True,
            "execution_attempted": True,
            "execution_success": False,
            "failure_code": "execution_contract_failed",
        },
    ]
    summary = _summarize_records(records)
    gate = _build_gate(summary)

    assert summary["total_runs"] == 6
    assert summary["decision_success_rate"] == 1.0
    assert summary["execution_success_rate"] == 5 / 6
    assert summary["browser_chain_execution_success_rate"] == 1.0
    assert summary["failure_code_topn"][0]["code"] == "execution_contract_failed"
    assert gate["gates"]["execution_success_rate_ok"] is False
    assert gate["pass"] is False

