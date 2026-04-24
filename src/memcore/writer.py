"""MemCore Writer - 单例写锁队列（asyncio.Lock + WAL）

写操作通过 asyncio.Lock 保证串行化。
WAL 模式下读不阻塞写。
偏好更新 = 新增 confidence=1.0 + 冲突旧记忆 is_archived=1

v0.3.4 增强：
- 偏好学习：从用户行为中提取偏好
- 访问追踪字段迁移
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiosqlite
import structlog

from src.config import RaccoonConfig
from src.types import MemoryEntry

logger = structlog.get_logger(__name__)

# DDL
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL DEFAULT 'user',
    is_archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_memories_user_key
ON memories(user_id, key, is_archived);
"""

_ENABLE_WAL = "PRAGMA journal_mode=WAL;"


class MemCoreWriter:
    """MemCore 写入器：单例写锁 + WAL"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._db_path = self._config.db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """初始化数据库"""
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute(_ENABLE_WAL)
        await self._db.executescript(_CREATE_TABLE + _CREATE_INDEX)
        await self._db.commit()
        # 确保新字段存在（v0.3.4 迁移）
        await self._ensure_columns()
        logger.info("memcore_writer_initialized", db=str(self._db_path))

    async def _ensure_columns(self) -> None:
        """确保 access_count / last_accessed_at 字段存在"""
        try:
            await self._db.execute("ALTER TABLE memories ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0")
            await self._db.commit()
        except Exception:
            pass
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

    async def write(self, entry: MemoryEntry) -> None:
        """写入记忆条目（带写锁）"""
        async with self._lock:
            await self._write_internal(entry)

    async def write_preference(self, user_id: str, key: str, value: str, source: str = "user") -> MemoryEntry:
        """写入偏好（冲突处理：新偏好 confidence=1.0 + 旧偏好归档）"""
        async with self._lock:
            # 归档冲突的旧记忆
            await self._db.execute(
                "UPDATE memories SET is_archived = 1, updated_at = ? "
                "WHERE user_id = ? AND key = ? AND is_archived = 0",
                (datetime.now(timezone.utc).isoformat(), user_id, key),
            )

            # 写入新偏好
            entry = MemoryEntry(
                user_id=user_id,
                key=key,
                value=value,
                confidence=1.0,
                source=source,
            )
            await self._write_internal(entry)
            return entry

    # ─── 偏好学习（#8）─────────────────────────────────────────

    async def learn_preference_from_behavior(
        self,
        user_id: str,
        action: str,
        target: str,
        context: str = "",
    ) -> MemoryEntry | None:
        """从用户行为中提取偏好

        常见行为模式：
        - action="skill_used", target="web_search" → 偏好: 常用工具=web_search
        - action="model_switched", target="gpt-4o" → 偏好: 首选模型=gpt-4o
        - action="schedule_created", target="daily_report" → 偏好: 定时偏好=daily_report
        - action="language_used", target="zh-CN" → 偏好: 语言=zh-CN
        """
        # 行为 → 偏好 key 映射
        behavior_key_map = {
            "skill_used": "frequent_skill",
            "model_switched": "preferred_model",
            "schedule_created": "schedule_preference",
            "language_used": "language",
            "notification_channel": "notify_channel",
            "output_format": "output_format",
        }

        key = behavior_key_map.get(action)
        if not key:
            logger.debug("behavior_no_preference_mapping", action=action)
            return None

        # 检查是否已有该偏好
        cursor = await self._db.execute(
            "SELECT memory_id, value, access_count FROM memories "
            "WHERE user_id = ? AND key = ? AND is_archived = 0",
            (user_id, key),
        )
        existing = await cursor.fetchone()

        if existing:
            existing_value = existing[1]
            if existing_value == target:
                # 相同偏好，增加 access_count
                await self._db.execute(
                    "UPDATE memories SET access_count = access_count + 1, "
                    "updated_at = ? WHERE memory_id = ?",
                    (datetime.now(timezone.utc).isoformat(), existing[0]),
                )
                await self._db.commit()
                return None
            else:
                # 偏好变化，用 write_preference 处理冲突
                return await self.write_preference(user_id, key, target, source="behavior")
        else:
            # 新偏好
            return await self.write_preference(user_id, key, target, source="behavior")

    async def _write_internal(self, entry: MemoryEntry) -> None:
        """内部写入（调用者需持有锁）"""
        await self._db.execute(
            "INSERT OR REPLACE INTO memories "
            "(memory_id, user_id, key, value, confidence, source, is_archived, "
            "access_count, last_accessed_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.memory_id,
                entry.user_id,
                entry.key,
                str(entry.value),
                entry.confidence,
                entry.source,
                int(entry.is_archived),
                entry.access_count,
                entry.last_accessed_at.isoformat() if entry.last_accessed_at else None,
                entry.created_at.isoformat(),
                entry.updated_at.isoformat(),
            ),
        )
        await self._db.commit()
        logger.debug("memory_written", memory_id=entry.memory_id, key=entry.key)