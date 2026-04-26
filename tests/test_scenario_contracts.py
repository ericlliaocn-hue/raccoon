from __future__ import annotations

from src.brain.scenario_contracts import evaluate_execution_contract


def test_price_monitor_contract_accepts_structured_snapshot_artifacts():
    ok, reason = evaluate_execution_contract(
        "price_monitor",
        reply="快照已创建",
        execution_result={
            "artifacts": {
                "subcmd": "snapshot",
                "target": "https://item.jd.com/100012043978.html",
                "snapshot_id": "abc123",
                "content_hash": "f" * 64,
            }
        },
    )
    assert ok is True
    assert reason is None


def test_price_monitor_contract_rejects_without_target_evidence():
    ok, reason = evaluate_execution_contract(
        "price_monitor",
        reply="处理完成",
        execution_result={"artifacts": {"subcmd": "snapshot"}},
    )
    assert ok is False
    assert reason == "execution_contract_price_target_missing"


def test_remote_exec_contract_accepts_structured_trace_artifacts():
    ok, reason = evaluate_execution_contract(
        "remote_exec",
        reply="命令执行成功",
        execution_result={
            "artifacts": {
                "command": "pwd",
                "exit_code": 0,
                "task_id": "task-001",
            }
        },
    )
    assert ok is True
    assert reason is None


def test_remote_exec_contract_rejects_without_trace_or_command():
    ok, reason = evaluate_execution_contract(
        "remote_exec",
        reply="已完成",
        execution_result={"artifacts": {}},
    )
    assert ok is False
    assert reason == "execution_contract_trace_missing"


def test_login_form_chain_contract_keeps_checkpoint_requirement():
    ok, reason = evaluate_execution_contract(
        "login_form_chain",
        reply="登录成功，提交成功",
        execution_result={"artifacts": {}},
    )
    assert ok is False
    assert reason == "execution_contract_checkpoint_missing"


def test_login_form_chain_contract_accepts_submit_checkpoint_evidence():
    ok, reason = evaluate_execution_contract(
        "login_form_chain",
        reply="浏览器自动化完成",
        execution_result={
            "artifacts": {
                "checkpoints": [
                    {
                        "stage": "after",
                        "success": True,
                        "action": "click",
                        "checkpoint_key": "login_submit",
                        "url": "https://httpbin.org/forms/post",
                    }
                ]
            }
        },
    )
    assert ok is True
    assert reason is None
