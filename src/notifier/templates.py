"""通知模板系统

不同场景使用不同格式模板：
- brief: 简报（富文本，适合每日摘要）
- alert: 告警（短文本，强调紧迫性）
- approval: 审批请求（包含操作链接）
- default: 默认格式
"""

from __future__ import annotations

from datetime import datetime, timezone
from string import Template
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class NotificationTemplate:
    """通知模板渲染器"""

    # 内置模板
    TEMPLATES: dict[str, dict[str, str]] = {
        "default": {
            "title": "${title}",
            "body": "${body}",
        },
        "brief": {
            "title": "📋 ${title}",
            "body": "${body}\n\n---\n⏰ ${timestamp}",
            "html_body": """
<div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto;">
  <h2 style="color: #333;">📋 ${title}</h2>
  <div style="color: #555; line-height: 1.6;">${body_html}</div>
  <hr style="border: none; border-top: 1px solid #eee; margin: 16px 0;">
  <p style="color: #999; font-size: 12px;">⏰ ${timestamp} · Raccoon 自动推送</p>
</div>
""",
        },
        "alert": {
            "title": "🚨 ${title}",
            "body": "⚠️ ${body}\n\n🕐 ${timestamp}",
        },
        "approval": {
            "title": "🔐 审批请求: ${title}",
            "body": "${body}\n\n⏳ 请在 ${timeout} 内处理，否则自动拒绝。\n\n✅ 批准: ${approve_url}\n❌ 拒绝: ${reject_url}",
        },
        "schedule_result": {
            "title": "⏰ ${schedule_name} - ${result}",
            "body": "${body}\n\n📅 Cron: ${cron}\n🕐 ${timestamp}",
        },
    }

    @classmethod
    def render(
        cls,
        template_name: str,
        **kwargs: Any,
    ) -> dict[str, str]:
        """渲染通知模板

        Args:
            template_name: 模板名称（default/brief/alert/approval/schedule_result）
            **kwargs: 模板变量

        Returns:
            {"title": str, "body": str, "html_body": str | None}
        """
        tmpl = cls.TEMPLATES.get(template_name, cls.TEMPLATES["default"])

        # 注入通用变量
        kwargs.setdefault("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        kwargs.setdefault("body_html", kwargs.get("body", "").replace("\n", "<br>"))

        result: dict[str, str] = {}
        for key, tmpl_str in tmpl.items():
            try:
                t = Template(tmpl_str)
                result[key] = t.safe_substitute(kwargs)
            except Exception:
                result[key] = tmpl_str

        return result

    @classmethod
    def register_template(cls, name: str, template: dict[str, str]) -> None:
        """注册自定义模板"""
        cls.TEMPLATES[name] = template
        logger.info("notification_template_registered", name=name)

    @classmethod
    def list_templates(cls) -> list[str]:
        """列出所有可用模板"""
        return list(cls.TEMPLATES.keys())
