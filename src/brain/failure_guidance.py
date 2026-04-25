"""Failure code -> remediation guidance mapping used across learning/doctor/benchmark."""

from __future__ import annotations

from typing import Any


_FAILURE_GUIDANCE: dict[str, dict[str, str]] = {
    "parse_failed": {
        "hint": "确保 stdout 只输出 JSON，并至少包含 reply 字段；不要把调试日志混入协议输出。",
        "action": "检查 Skill 输出协议与 JSON 序列化逻辑。",
    },
    "auth_failed": {
        "hint": "目标站点/接口需要鉴权。优先补登录态或 token，再执行采集/提交。",
        "action": "改用可复用登录态（CDP/profile）或在配置中注入凭据。",
    },
    "data_hollow": {
        "hint": "当前抓取逻辑拿到的是空壳数据。需要修正选择器或 API 字段映射。",
        "action": "补充字段存在性校验和空数据兜底提示。",
    },
    "dependency_missing": {
        "hint": "运行缺依赖。优先使用仓库已有依赖，新增依赖需显式审批安装。",
        "action": "补全 requirements/依赖声明并验证安装流程。",
    },
    "network_failed": {
        "hint": "网络链路不稳定或域名不可达。要有超时、重试和降级反馈。",
        "action": "增加指数退避重试并输出可解释错误。",
    },
    "timeout": {
        "hint": "执行超时。应拆分步骤、缩短单步耗时并加强 checkpoint 恢复。",
        "action": "优化等待策略与超时时间配置。",
    },
    "quality_gate_failed": {
        "hint": "质量门未达标。结果缺结构化字段、来源说明或新鲜度信息。",
        "action": "按场景质量规则补齐输出模板。",
    },
    "execution_contract_price_target_missing": {
        "hint": "价格监控执行证据缺失，未体现目标信息或监控创建信号。",
        "action": "补齐 target/url/sku/snapshot_id，并在回复中明确“监控已创建/快照已创建”。",
    },
    "execution_contract_login_submit_missing": {
        "hint": "登录链路缺少明确的登录/提交完成信号。",
        "action": "增加登录/提交成功断言并输出结构化步骤结果。",
    },
    "execution_contract_checkpoint_missing": {
        "hint": "长链路缺少 checkpoint 证据，失败不可恢复。",
        "action": "每个关键步骤记录 checkpoint，失败时从最近检查点续跑。",
    },
    "execution_contract_trace_missing": {
        "hint": "远程执行缺少可追踪证据（command/trace/task/exit）。",
        "action": "返回结构化 artifacts，至少包含 command 和 trace/task_id/exit_code 之一。",
    },
    "execution_contract_schedule_missing": {
        "hint": "提醒场景缺少调度创建信号，无法证明任务已落库。",
        "action": "返回 schedule_created/cron/name，并在回复中显示创建结果。",
    },
    "playbook_skill_missing": {
        "hint": "命中稳定编排但没有可执行 skill_name。",
        "action": "修正 playbook 的场景→skill 映射，缺失时先澄清再执行。",
    },
    "skill_runner_missing": {
        "hint": "技能元数据存在，但执行 Runner 未注册。",
        "action": "检查 Skill 安装与 Vault 注册状态，恢复 runner 后再执行。",
    },
    "vault_unavailable": {
        "hint": "Vault 不可用，学习链路无法进入执行态。",
        "action": "先恢复 VaultManager，再重试学习执行。",
    },
    "dependency_requires_approval": {
        "hint": "依赖安装需要审批，当前被策略阻断。",
        "action": "先进入审批确认依赖清单，批准后再安装。",
    },
    "compile_failed": {
        "hint": "代码编译失败。先清理 Markdown 围栏/无效文本，再做语法修复。",
        "action": "运行 compile 检查并修复语法错误。",
    },
    "metadata_invalid": {
        "hint": "metadata 不符合 schema，字段缺失或类型错误。",
        "action": "按 SkillMetadata schema 补齐并校验 metadata.json。",
    },
    "approval_rejected": {
        "hint": "审批未通过。需要更清晰说明执行目的、风险与回滚路径。",
        "action": "降低权限范围并补审批备注。",
    },
    "unknown_error": {
        "hint": "未知错误，需要先补充日志证据再定位。",
        "action": "采集 stderr/traceback 与输入上下文后重试。",
    },
}


def failure_hint(failure_code: str | None) -> str:
    code = str(failure_code or "unknown_error")
    return _FAILURE_GUIDANCE.get(code, _FAILURE_GUIDANCE["unknown_error"])["hint"]


def failure_action(failure_code: str | None) -> str:
    code = str(failure_code or "unknown_error")
    return _FAILURE_GUIDANCE.get(code, _FAILURE_GUIDANCE["unknown_error"])["action"]


def guidance_for_failure(failure_code: str | None) -> dict[str, Any]:
    code = str(failure_code or "unknown_error")
    payload = dict(_FAILURE_GUIDANCE.get(code, _FAILURE_GUIDANCE["unknown_error"]))
    payload["failure_code"] = code
    return payload
