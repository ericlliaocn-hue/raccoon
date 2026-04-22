"""审计日志（JSONL 格式）

每条审计记录包含：
- timestamp: 时间戳
- action: 操作类型
- actor: 执行者
- target: 目标
- detail: 详情
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import structlog
from src.config import RaccoonConfig

logger = structlog.get_logger(__name__)


class AuditLogger:
    """审计日志器：JSONL 格式写入"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._log_dir = self._config.skills_dir.parent / "logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._current_file: Path | None = None

    def _get_log_file(self) -> Path:
        """获取当天日志文件"""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._log_dir / f"audit_{today}.jsonl"

    def log(
        self,
        action: str,
        actor: str = "system",
        target: str = "",
        detail: dict | None = None,
    ) -> None:
        """写入审计日志"""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "actor": actor,
            "target": target,
            "detail": detail or {},
        }

        log_file = self._get_log_file()
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.debug("audit_logged", action=action, target=target)

    def log_task_event(
        self, task_id: str, action: str, user_id: str = "", detail: dict | None = None
    ) -> None:
        """记录任务相关审计事件"""
        self.log(
            action=action,
            actor=user_id or "system",
            target=task_id,
            detail=detail or {},
        )

    def log_skill_event(
        self, skill_name: str, action: str, user_id: str = "", detail: dict | None = None
    ) -> None:
        """记录 Skill 相关审计事件"""
        self.log(
            action=action,
            actor=user_id or "system",
            target=skill_name,
            detail=detail or {},
        )
