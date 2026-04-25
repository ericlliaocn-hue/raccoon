"""核心 6 场景稳定编排策略（优先稳定路径，失败再进入自由学习）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.brain.core_benchmark import normalize_intent_phrase


@dataclass(frozen=True)
class PlaybookDecision:
    scenario_id: str
    handling_outcome: str  # clarified / reused / executed
    reply: str
    skill_name: str | None = None
    clarification_reason: str | None = None
    requires_scheduler: bool = False
    artifacts: dict[str, Any] = field(default_factory=dict)


class CoreScenarioPlaybook:
    """命中核心场景时，给出稳定处理路径。"""

    def decide(
        self,
        *,
        scenario_id: str | None,
        message: str,
        available_content_skills: list[str],
        has_monitor_target: bool,
        has_form_target: bool,
        has_shell_command: bool,
    ) -> PlaybookDecision | None:
        if not scenario_id:
            return None

        if scenario_id == "daily_brief":
            if not available_content_skills:
                return None
            return PlaybookDecision(
                scenario_id=scenario_id,
                handling_outcome="reused",
                reply="这个日报需求优先走现有热榜/日报 Skill 编排，不先新造 Skill。",
                skill_name="content_skill_bundle",
                artifacts={
                    "reused_capability": "skill_bundle",
                    "skills": available_content_skills,
                },
            )

        if scenario_id == "price_monitor":
            if not has_monitor_target:
                return PlaybookDecision(
                    scenario_id=scenario_id,
                    handling_outcome="clarified",
                    reply="价格监控需要商品链接、SKU 或平台商品标识。请补充目标后我再执行。",
                    skill_name="change_detector",
                    clarification_reason="missing_price_target",
                    artifacts={"reused_capability": "change_detector"},
                )
            return PlaybookDecision(
                scenario_id=scenario_id,
                handling_outcome="executed",
                reply="这个需求命中价格监控稳定路径，将直接执行监控能力。",
                skill_name="change_detector",
                artifacts={"reused_capability": "change_detector"},
            )

        if scenario_id == "login_form_chain":
            if not has_form_target:
                return PlaybookDecision(
                    scenario_id=scenario_id,
                    handling_outcome="clarified",
                    reply="登录长链路必须有真实站点和字段信息；请补充后再执行。",
                    skill_name="web_automate",
                    clarification_reason="missing_form_target",
                    artifacts={"reused_capability": "web_automate"},
                )
            return PlaybookDecision(
                scenario_id=scenario_id,
                handling_outcome="executed",
                reply="这个需求命中浏览器长链路稳定编排，将直接执行 web_automate。",
                skill_name="web_automate",
                artifacts={"reused_capability": "web_automate"},
            )

        if scenario_id == "material_collection":
            if not available_content_skills:
                return None
            return PlaybookDecision(
                scenario_id=scenario_id,
                handling_outcome="reused",
                reply="素材采集优先复用已有多源热榜 Skill 组合与去重，不先造新爬虫。",
                skill_name="content_skill_bundle",
                artifacts={
                    "reused_capability": "skill_bundle",
                    "skills": available_content_skills,
                },
            )

        if scenario_id == "reminder_schedule":
            if not self._has_schedule_target(message):
                return PlaybookDecision(
                    scenario_id=scenario_id,
                    handling_outcome="clarified",
                    reply="提醒场景需要时间和提醒内容，例如「每个工作日 09:30 提醒我复盘」。",
                    skill_name="scheduler",
                    clarification_reason="missing_schedule_target",
                    artifacts={"reused_capability": "scheduler"},
                )
            return PlaybookDecision(
                scenario_id=scenario_id,
                handling_outcome="executed",
                reply="提醒场景命中 Scheduler 稳定路径，直接创建调度，不走新 Skill 学习。",
                skill_name="scheduler",
                requires_scheduler=True,
                artifacts={"reused_capability": "scheduler"},
            )

        if scenario_id == "remote_exec":
            if not has_shell_command:
                return PlaybookDecision(
                    scenario_id=scenario_id,
                    handling_outcome="clarified",
                    reply="远程执行需要具体命令或脚本路径。请给出可执行命令后我再走审批执行。",
                    skill_name="shell_exec",
                    clarification_reason="missing_shell_command",
                    artifacts={"reused_capability": "shell_exec", "requires_approval": True},
                )
            return PlaybookDecision(
                scenario_id=scenario_id,
                handling_outcome="executed",
                reply="这个需求命中 shell_exec 稳定路径，将直接执行 shell_exec + 审批链路。",
                skill_name="shell_exec",
                artifacts={"reused_capability": "shell_exec", "requires_approval": True},
            )

        return None

    @staticmethod
    def _has_schedule_target(message: str) -> bool:
        text = normalize_intent_phrase(message)
        has_time = bool(
            re.search(r"\b\d{1,2}:\d{2}\b", text)
            or any(token in text for token in ("每天", "每周", "每月", "工作日", "cron"))
        )
        has_content = any(token in text for token in ("提醒", "通知", "复盘", "检查", "记得"))
        return has_time and has_content
