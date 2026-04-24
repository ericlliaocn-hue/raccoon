"""衰减/归档/快照

- 衰减：confidence 随时间降低（指数衰减 + 访问频率加权）
- 归档：标记 is_archived=1（超过 days 天未使用且 confidence 低于阈值）
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
        """衰减旧记忆的 confidence（#5: 指数衰减 + 访问加权）

        算法：
        1. 超过 days 天的记忆，confidence 按指数衰减
        2. 高频访问（access_count > 5）的记忆衰减更慢
        3. confidence 低于 threshold 的自动归档

        返回归档的记忆数量。
        """
        now = datetime.now(timezone.utc)
        cutoff = now - __import__("datetime").timedelta(days=days)
        cutoff_iso = cutoff.isoformat()

        archived_count = 0

        async with aiosqlite.connect(str(self._db_path)) as db:
            # 查找需要衰减的记忆（未归档 + 超过 days 天）
            cursor = await db.execute(
                "SELECT memory_id, confidence, access_count, updated_at "
                "FROM memories WHERE is_archived = 0 AND updated_at < ?",
                (cutoff_iso,),
            )
            rows = await cursor.fetchall()

            for row in rows:
                memory_id, confidence, access_count, updated_at = row
                try:
                    updated = datetime.fromisoformat(updated_at)
                except (ValueError, TypeError):
                    continue

                # 计算天数差
                age_days = (now - updated).days
                if age_days <= 0:
                    continue

                # 指数衰减: confidence *= e^(-lambda * age_days)
                # lambda = 0.01 (约 70 天衰减到 50%)
                import math
                decay_lambda = 0.01

                # 访问频率加权：高频访问衰减更慢
                if access_count and access_count > 5:
                    decay_lambda *= 0.5  # 高频记忆衰减速度减半
                elif access_count and access_count > 2:
                    decay_lambda *= 0.75

                new_confidence = confidence * math.exp(-decay_lambda * age_days)
                new_confidence = max(0.0, min(1.0, new_confidence))  # clamp [0, 1]

                if new_confidence < threshold:
                    # 低于阈值 → 归档
                    await db.execute(
                        "UPDATE memories SET is_archived = 1, confidence = ?, updated_at = ? WHERE memory_id = ?",
                        (new_confidence, now.isoformat(), memory_id),
                    )
                    archived_count += 1
                else:
                    # 仅更新 confidence
                    await db.execute(
                        "UPDATE memories SET confidence = ?, updated_at = ? WHERE memory_id = ?",
                        (new_confidence, now.isoformat(), memory_id),
                    )

            await db.commit()

        logger.info(
            "decay_completed",
            days=days,
            threshold=threshold,
            evaluated=len(rows),
            archived=archived_count,
        )
        return archived_count

    async def archive_unused(self, days: int = 30, confidence_threshold: float = 0.3) -> int:
        """归档长时间未使用且置信度低的记忆（#7）

        条件：超过 days 天未访问 + confidence < confidence_threshold
        """
        now = datetime.now(timezone.utc)
        cutoff = now - __import__("datetime").timedelta(days=days)
        cutoff_iso = cutoff.isoformat()

        async with aiosqlite.connect(str(self._db_path)) as db:
            cursor = await db.execute(
                "UPDATE memories SET is_archived = 1, updated_at = ? "
                "WHERE is_archived = 0 AND confidence < ? "
                "AND (last_accessed_at IS NULL OR last_accessed_at < ?) "
                "AND updated_at < ?",
                (now.isoformat(), confidence_threshold, cutoff_iso, cutoff_iso),
            )
            await db.commit()
            archived = cursor.rowcount

        logger.info("archive_unused_completed", days=days, threshold=confidence_threshold, archived=archived)
        return archived
