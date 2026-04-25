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
