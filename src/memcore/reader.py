"""MemCore Reader - 读接口（可并发）

WAL 模式下读不阻塞写，无需锁。

v0.3.4 增强：
- 访问追踪：每次读取更新 access_count + last_accessed_at
- 复用率统计：高频经验提升权重
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite
import structlog

from src.config import RaccoonConfig
from src.types import MemoryEntry

logger = structlog.get_logger(__name__)


class MemCoreReader:
    """MemCore 读取器：可并发读取，带访问追踪"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._db_path = self._config.db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """初始化数据库连接"""
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL;")
        # 确保新字段存在（v0.3.4 迁移）
        await self._ensure_columns()
        logger.info("memcore_reader_initialized")

    async def _ensure_columns(self) -> None:
        """确保 access_count / last_accessed_at 字段存在"""
        try:
            await self._db.execute("ALTER TABLE memories ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0")
            await self._db.commit()
        except Exception:
            pass  # 字段已存在
        try:
            await self._db.execute("ALTER TABLE memories ADD COLUMN last_accessed_at TEXT")
            await self._db.commit()
        except Exception:
            pass

    async def close(self) -> None:
        """关闭数据库"""
        if self._db:
            await self._db.close()
            self._db = None

    async def get(self, user_id: str, key: str) -> MemoryEntry | None:
        """获取用户某条记忆（带访问追踪）"""
        cursor = await self._db.execute(
            "SELECT * FROM memories WHERE user_id = ? AND key = ? AND is_archived = 0 "
            "ORDER BY updated_at DESC LIMIT 1",
            (user_id, key),
        )
        row = await cursor.fetchone()
        if row:
            entry = self._row_to_entry(row)
            await self._track_access(entry.memory_id)
            return entry
        return None

    async def get_all(self, user_id: str, *, include_archived: bool = False) -> list[MemoryEntry]:
        """获取用户所有记忆"""
        if include_archived:
            cursor = await self._db.execute(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM memories WHERE user_id = ? AND is_archived = 0 ORDER BY updated_at DESC",
                (user_id,),
            )
        rows = await cursor.fetchall()
        return [self._row_to_entry(r) for r in rows]

    async def search(self, user_id: str, query: str) -> list[MemoryEntry]:
        """搜索记忆（简单 LIKE 搜索，带访问加权排序）"""
        cursor = await self._db.execute(
            "SELECT * FROM memories WHERE user_id = ? AND is_archived = 0 "
            "AND (key LIKE ? OR value LIKE ?) "
            "ORDER BY (confidence * (1 + access_count * 0.1)) DESC, updated_at DESC",
            (user_id, f"%{query}%", f"%{query}%"),
        )
        rows = await cursor.fetchall()
        return [self._row_to_entry(r) for r in rows]

    # ─── 复用率统计（#6）───────────────────────────────────────

    async def get_top_accessed(self, user_id: str, limit: int = 10) -> list[MemoryEntry]:
        """获取高频访问的记忆（经验复用率统计）"""
        cursor = await self._db.execute(
            "SELECT * FROM memories WHERE user_id = ? AND is_archived = 0 "
            "ORDER BY access_count DESC, confidence DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_entry(r) for r in rows]

    async def get_access_stats(self, user_id: str) -> dict[str, Any]:
        """获取记忆访问统计"""
        async with aiosqlite.connect(str(self._db_path)) as db:
            # 总记忆数
            cursor = await db.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id = ? AND is_archived = 0",
                (user_id,),
            )
            total = (await cursor.fetchone())[0]

            # 总访问次数
            cursor = await db.execute(
                "SELECT COALESCE(SUM(access_count), 0) FROM memories WHERE user_id = ? AND is_archived = 0",
                (user_id,),
            )
            total_accesses = (await cursor.fetchone())[0]

            # 平均置信度
            cursor = await db.execute(
                "SELECT COALESCE(AVG(confidence), 0) FROM memories WHERE user_id = ? AND is_archived = 0",
                (user_id,),
            )
            avg_confidence = (await cursor.fetchone())[0]

            # 高频记忆数（access_count > 5）
            cursor = await db.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id = ? AND is_archived = 0 AND access_count > 5",
                (user_id,),
            )
            high_freq = (await cursor.fetchone())[0]

        return {
            "total_memories": total,
            "total_accesses": total_accesses,
            "avg_confidence": round(avg_confidence, 3),
            "high_freq_memories": high_freq,
            "reuse_rate": round(total_accesses / max(total, 1), 2),
        }

    # ─── 访问追踪 ─────────────────────────────────────────────

    async def _track_access(self, memory_id: str) -> None:
        """更新访问计数和最后访问时间"""
        now = datetime.now(timezone.utc).isoformat()
        try:
            await self._db.execute(
                "UPDATE memories SET access_count = access_count + 1, last_accessed_at = ? "
                "WHERE memory_id = ?",
                (now, memory_id),
            )
            await self._db.commit()
        except Exception:
            # 访问追踪失败不应影响读取
            logger.debug("track_access_failed", memory_id=memory_id)

    @staticmethod
    def _row_to_entry(row: Any) -> MemoryEntry:
        """数据库行 → MemoryEntry"""
        from datetime import datetime
        # 兼容有无 access_count / last_accessed_at 字段
        access_count = 0
        last_accessed_at = None

        # row 可能是 tuple，长度取决于表结构
        # memories 表字段: memory_id, user_id, key, value, confidence, source, is_archived, created_at, updated_at, access_count, last_accessed_at
        if len(row) > 9:
            access_count = row[9] or 0
        if len(row) > 10:
            last_accessed_at = datetime.fromisoformat(row[10]) if row[10] else None

        return MemoryEntry(
            memory_id=row[0],
            user_id=row[1],
            key=row[2],
            value=row[3],
            confidence=row[4],
            source=row[5],
            is_archived=bool(row[6]),
            access_count=access_count,
            last_accessed_at=last_accessed_at,
            created_at=datetime.fromisoformat(row[7]),
            updated_at=datetime.fromisoformat(row[8]),
        )