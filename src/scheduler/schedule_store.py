"""定时任务持久化

参考 TaskQueue 的持久化模式：
- 内存中维护 schedule 列表
- 每条 schedule 持久化到 schedules/ 目录（JSON）
- 启动时从目录恢复
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import structlog
from src.config import RaccoonConfig
from src.types import ScheduleEntry

logger = structlog.get_logger(__name__)


class ScheduleStore:
    """定时任务存储：内存 + 持久化"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._schedules: dict[str, ScheduleEntry] = {}
        self._dir = self._config.schedules_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def add(self, entry: ScheduleEntry) -> None:
        """添加定时任务"""
        self._schedules[entry.schedule_id] = entry
        self._persist(entry)
        logger.info(
            "schedule_added",
            schedule_id=entry.schedule_id,
            name=entry.name,
            cron=entry.cron,
        )

    def get(self, schedule_id: str) -> ScheduleEntry | None:
        """按 ID 查找"""
        return self._schedules.get(schedule_id)

    def get_by_conversation(self, conversation_id: str) -> list[ScheduleEntry]:
        """按 conversation_id 查找"""
        return [
            s for s in self._schedules.values()
            if s.conversation_id == conversation_id
        ]

    def get_enabled(self) -> list[ScheduleEntry]:
        """获取所有启用的定时任务"""
        return [s for s in self._schedules.values() if s.enabled]

    def update(self, entry: ScheduleEntry) -> None:
        """更新定时任务"""
        entry.updated_at = datetime.now(timezone.utc)
        self._schedules[entry.schedule_id] = entry
        self._persist(entry)
        logger.debug("schedule_updated", schedule_id=entry.schedule_id)

    def remove(self, schedule_id: str) -> ScheduleEntry | None:
        """删除定时任务，返回被删除的条目"""
        entry = self._schedules.pop(schedule_id, None)
        if entry:
            self._delete_persisted(schedule_id)
            logger.info("schedule_removed", schedule_id=schedule_id, name=entry.name)
        return entry

    def all_schedules(self) -> Iterator[ScheduleEntry]:
        """迭代所有定时任务"""
        yield from self._schedules.values()

    def count(self) -> int:
        return len(self._schedules)

    # ─── 持久化 ────────────────────────────────────────────────

    def _persist(self, entry: ScheduleEntry) -> None:
        """持久化到 JSON 文件"""
        path = self._dir / f"{entry.schedule_id}.json"
        path.write_text(entry.model_dump_json(indent=2), encoding="utf-8")

    def _delete_persisted(self, schedule_id: str) -> None:
        """删除持久化文件"""
        path = self._dir / f"{schedule_id}.json"
        if path.exists():
            path.unlink()

    def recover(self) -> int:
        """从持久化目录恢复定时任务（重启后调用）"""
        count = 0
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                entry = ScheduleEntry.model_validate(data)
                self._schedules[entry.schedule_id] = entry
                count += 1
            except Exception:
                logger.warning("schedule_recover_failed", path=str(path))
        if count:
            logger.info("schedules_recovered", count=count)
        return count
