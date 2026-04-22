"""衰减/归档/快照（MVP 简化版）

- 衰减：confidence 随时间降低（MVP: 不实现，预留接口）
- 归档：标记 is_archived=1
- 快照：导出所有记忆为 JSON 文件
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import structlog

from src.config import RaccoonConfig

logger = structlog.get_logger(__name__)


class MemCoreLifecycle:
    """记忆生命周期管理"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._db_path = self._config.db_path
        self._backup_dir = self._config.db_path.parent.parent / "backups"
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    async def archive(self, memory_id: str) -> bool:
        """归档单条记忆"""
        async with aiosqlite.connect(str(self._db_path)) as db:
            cursor = await db.execute(
                "UPDATE memories SET is_archived = 1, updated_at = ? WHERE memory_id = ?",
                (datetime.now(timezone.utc).isoformat(), memory_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def snapshot(self, user_id: str | None = None) -> Path:
        """导出记忆快照"""
        async with aiosqlite.connect(str(self._db_path)) as db:
            if user_id:
                cursor = await db.execute(
                    "SELECT * FROM memories WHERE user_id = ?",
                    (user_id,),
                )
            else:
                cursor = await db.execute("SELECT * FROM memories")

            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            records = [dict(zip(columns, row)) for row in rows]

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        suffix = f"_{user_id}" if user_id else "_all"
        path = self._backup_dir / f"snapshot{suffix}_{timestamp}.json"
        path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")

        logger.info("snapshot_created", path=str(path), count=len(records))
        return path

    async def decay(self, days: int = 30, threshold: float = 0.1) -> int:
        """衰减旧记忆的 confidence（MVP: 简化实现）

        超过 days 天的记忆，confidence 降低 0.1；
        confidence 低于 threshold 的自动归档。
        """
        # MVP: 预留接口，暂不实现复杂衰减逻辑
        logger.info("decay_called", days=days, threshold=threshold, note="MVP_noop")
        return 0
