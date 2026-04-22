"""MemCore Reader - 读接口（可并发）

WAL 模式下读不阻塞写，无需锁。
"""

from __future__ import annotations

from typing import Any

import aiosqlite
import structlog

from src.config import RaccoonConfig
from src.types import MemoryEntry

logger = structlog.get_logger(__name__)


class MemCoreReader:
    """MemCore 读取器：可并发读取"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._db_path = self._config.db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """初始化数据库连接"""
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL;")
        logger.info("memcore_reader_initialized")

    async def close(self) -> None:
        """关闭数据库"""
        if self._db:
            await self._db.close()
            self._db = None

    async def get(self, user_id: str, key: str) -> MemoryEntry | None:
        """获取用户某条记忆"""
        cursor = await self._db.execute(
            "SELECT * FROM memories WHERE user_id = ? AND key = ? AND is_archived = 0 "
            "ORDER BY updated_at DESC LIMIT 1",
            (user_id, key),
        )
        row = await cursor.fetchone()
        if row:
            return self._row_to_entry(row)
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
        """搜索记忆（简单 LIKE 搜索）"""
        cursor = await self._db.execute(
            "SELECT * FROM memories WHERE user_id = ? AND is_archived = 0 "
            "AND (key LIKE ? OR value LIKE ?) ORDER BY updated_at DESC",
            (user_id, f"%{query}%", f"%{query}%"),
        )
        rows = await cursor.fetchall()
        return [self._row_to_entry(r) for r in rows]

    @staticmethod
    def _row_to_entry(row: Any) -> MemoryEntry:
        """数据库行 → MemoryEntry"""
        from datetime import datetime
        return MemoryEntry(
            memory_id=row[0],
            user_id=row[1],
            key=row[2],
            value=row[3],
            confidence=row[4],
            source=row[5],
            is_archived=bool(row[6]),
            created_at=datetime.fromisoformat(row[7]),
            updated_at=datetime.fromisoformat(row[8]),
        )
