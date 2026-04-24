"""会话持久化存储（SQLite）

将会话消息从内存 dict 迁移到 SQLite：
- 重启不丢失
- 支持大量会话
- 与 MemCore 共享 data/memcore.db
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import structlog
from src.config import RaccoonConfig

logger = structlog.get_logger(__name__)


class ConversationStore:
    """会话存储：SQLite 持久化"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._db_path = self._config.db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表"""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    messages TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, conversation_id: str, messages: list[dict]) -> None:
        """保存会话消息（upsert）"""
        now = datetime.now(timezone.utc).isoformat()
        msgs_json = json.dumps(messages, ensure_ascii=False)

        with self._connect() as conn:
            # 检查是否已存在
            row = conn.execute(
                "SELECT id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()

            if row:
                conn.execute(
                    "UPDATE conversations SET messages = ?, updated_at = ? WHERE id = ?",
                    (msgs_json, now, conversation_id),
                )
            else:
                conn.execute(
                    "INSERT INTO conversations (id, messages, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (conversation_id, msgs_json, now, now),
                )
            conn.commit()

    def get(self, conversation_id: str) -> list[dict]:
        """获取会话消息"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT messages FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()

        if not row:
            return []
        return json.loads(row["messages"])

    def list_all(self) -> list[dict]:
        """列出所有会话（不含消息内容，只含摘要）"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, messages, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
            ).fetchall()

        result = []
        for row in rows:
            msgs = json.loads(row["messages"])
            first_user = next((m for m in msgs if m.get("role") == "user"), None)
            result.append({
                "conversation_id": row["id"],
                "message_count": len(msgs),
                "preview": first_user["content"][:50] if first_user else "(空)",
                "last_time": msgs[-1].get("time", "") if msgs else "",
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return result

    def delete(self, conversation_id: str) -> bool:
        """删除会话，返回是否成功"""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def count(self) -> int:
        """会话总数"""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM conversations").fetchone()
            return row["cnt"]
