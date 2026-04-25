from __future__ import annotations

from src.brain.core_scenario_playbook import CoreScenarioPlaybook


def test_playbook_price_monitor_requires_target():
    playbook = CoreScenarioPlaybook()
    decision = playbook.decide(
        scenario_id="price_monitor",
        message="帮我盯这个商品，降价提醒。",
        available_content_skills=[],
        has_monitor_target=False,
        has_form_target=False,
        has_shell_command=False,
    )
    assert decision is not None
    assert decision.handling_outcome == "clarified"
    assert decision.clarification_reason == "missing_price_target"


def test_playbook_daily_brief_prefers_reuse():
    playbook = CoreScenarioPlaybook()
    decision = playbook.decide(
        scenario_id="daily_brief",
        message="给我来一份今日简报",
        available_content_skills=["ai_daily_report", "weibo_hot"],
        has_monitor_target=False,
        has_form_target=False,
        has_shell_command=False,
    )
    assert decision is not None
    assert decision.handling_outcome == "reused"
    assert decision.skill_name == "content_skill_bundle"


def test_playbook_remote_exec_requires_command():
    playbook = CoreScenarioPlaybook()
    decision = playbook.decide(
        scenario_id="remote_exec",
        message="先审批，再帮我处理日志。",
        available_content_skills=[],
        has_monitor_target=False,
        has_form_target=False,
        has_shell_command=False,
    )
    assert decision is not None
    assert decision.handling_outcome == "clarified"
    assert decision.skill_name == "shell_exec"


def test_playbook_reminder_goes_scheduler_when_target_complete():
    playbook = CoreScenarioPlaybook()
    decision = playbook.decide(
        scenario_id="reminder_schedule",
        message="每个工作日 09:30 提醒我复盘客户跟进",
        available_content_skills=[],
        has_monitor_target=False,
        has_form_target=False,
        has_shell_command=False,
    )
    assert decision is not None
    assert decision.handling_outcome == "executed"
    assert decision.requires_scheduler is True
