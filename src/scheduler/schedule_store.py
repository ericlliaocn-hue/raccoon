"""定时任务持久化（SQLite）

从 JSON 文件迁移到 SQLite，与 MemCore 共享 data/memcore.db：
- schedules 表：存储定时任务配置
- schedule_runs 表：存储调度执行记录
- 启动时自动从旧 JSON 目录迁移（兼容升级）
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterator

import structlog
from src.config import RaccoonConfig
from src.types import ScheduleEntry, ScheduleStatus

logger = structlog.get_logger(__name__)

# DDL
_CREATE_SCHEDULES = """
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    cron TEXT NOT NULL,
    message TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'scheduler',
    status TEXT NOT NULL DEFAULT 'enabled',
    retry_policy TEXT NOT NULL DEFAULT '{}',
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_run TEXT,
    next_run TEXT,
    last_run_result TEXT,
    last_run_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_CREATE_SCHEDULE_RUNS = """
CREATE TABLE IF NOT EXISTS schedule_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    finished_at TEXT,
    result TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    duration_ms INTEGER,
    payload TEXT DEFAULT '{}',
    FOREIGN KEY (schedule_id) REFERENCES schedules(schedule_id)
);
"""

_CREATE_RUNS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_schedule_runs_sid
ON schedule_runs(schedule_id, triggered_at DESC);
"""

_CREATE_PID_LOCK = """
CREATE TABLE IF NOT EXISTS schedule_pid_lock (
    schedule_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    locked_at TEXT NOT NULL
);
"""


class ScheduleStore:
    """定时任务存储：SQLite 持久化"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._db_path = self._config.db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schedules: dict[str, ScheduleEntry] = {}
        self._init_db()
        self._load_all()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        """初始化数据库表"""
        with self._connect() as conn:
            conn.executescript(
                _CREATE_SCHEDULES + _CREATE_SCHEDULE_RUNS + _CREATE_RUNS_INDEX + _CREATE_PID_LOCK
            )
            conn.commit()
        logger.info("schedule_store_initialized", db=str(self._db_path))

    def _load_all(self) -> None:
        """从 SQLite 加载所有定时任务到内存"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM schedules").fetchall()

        for row in rows:
            try:
                entry = self._row_to_entry(row)
                self._schedules[entry.schedule_id] = entry
            except Exception:
                logger.warning("schedule_load_failed", schedule_id=row["schedule_id"])

        # 兼容：如果 SQLite 为空但旧 JSON 目录存在，自动迁移
        if not self._schedules:
            self._migrate_from_json()

        logger.info("schedules_loaded", count=len(self._schedules))

    def _row_to_entry(self, row: sqlite3.Row) -> ScheduleEntry:
        """将数据库行转换为 ScheduleEntry"""
        retry_policy = {}
        if row["retry_policy"]:
            try:
                retry_policy = json.loads(row["retry_policy"])
            except (json.JSONDecodeError, TypeError):
                pass

        return ScheduleEntry(
            schedule_id=row["schedule_id"],
            name=row["name"],
            cron=row["cron"],
            message=row["message"],
            conversation_id=row["conversation_id"],
            user_id=row["user_id"],
            status=ScheduleStatus(row["status"]),
            retry_policy=retry_policy,
            retry_count=row["retry_count"] or 0,
            last_run=datetime.fromisoformat(row["last_run"]) if row["last_run"] else None,
            next_run=datetime.fromisoformat(row["next_run"]) if row["next_run"] else None,
            last_run_result=row["last_run_result"],
            last_run_error=row["last_run_error"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else datetime.now(timezone.utc),
        )

    def _entry_to_params(self, entry: ScheduleEntry) -> tuple:
        """将 ScheduleEntry 转换为数据库参数"""
        return (
            entry.schedule_id,
            entry.name,
            entry.cron,
            entry.message,
            entry.conversation_id,
            entry.user_id,
            entry.status.value,
            entry.retry_policy.model_dump_json() if hasattr(entry.retry_policy, 'model_dump_json') else json.dumps(entry.retry_policy),
            entry.retry_count,
            entry.last_run.isoformat() if entry.last_run else None,
            entry.next_run.isoformat() if entry.next_run else None,
            entry.last_run_result,
            entry.last_run_error,
            entry.created_at.isoformat(),
            entry.updated_at.isoformat(),
        )

    # ─── 兼容迁移 ─────────────────────────────────────────────

    def _migrate_from_json(self) -> None:
        """从旧版 JSON 目录迁移到 SQLite"""
        old_dir = self._config.schedules_dir
        if not old_dir.exists():
            return

        count = 0
        for path in old_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                # 旧版 enabled bool → 新版 status
                if "enabled" in data and "status" not in data:
                    data["status"] = "enabled" if data["enabled"] else "disabled"
                    data.pop("enabled", None)
                entry = ScheduleEntry.model_validate(data)
                self._schedules[entry.schedule_id] = entry
                self._upsert_to_db(entry)
                count += 1
            except Exception:
                logger.warning("schedule_migrate_failed", path=str(path))

        if count:
            logger.info("schedules_migrated_from_json", count=count)
            # 迁移完成后重命名旧目录
            try:
                old_dir.rename(old_dir.with_name("schedules.migrated"))
            except Exception:
                pass

    # ─── CRUD ──────────────────────────────────────────────────

    def add(self, entry: ScheduleEntry) -> None:
        """添加定时任务"""
        self._schedules[entry.schedule_id] = entry
        self._upsert_to_db(entry)
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
        self._upsert_to_db(entry)
        logger.debug("schedule_updated", schedule_id=entry.schedule_id)

    def remove(self, schedule_id: str) -> ScheduleEntry | None:
        """删除定时任务，返回被删除的条目"""
        entry = self._schedules.pop(schedule_id, None)
        if entry:
            self._delete_from_db(schedule_id)
            logger.info("schedule_removed", schedule_id=schedule_id, name=entry.name)
        return entry

    def all_schedules(self) -> Iterator[ScheduleEntry]:
        """迭代所有定时任务"""
        yield from self._schedules.values()

    def count(self) -> int:
        return len(self._schedules)

    # ─── 执行记录 ──────────────────────────────────────────────

    def record_run_start(self, schedule_id: str) -> int:
        """记录执行开始，返回 run_id"""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO schedule_runs (schedule_id, triggered_at, result) VALUES (?, ?, 'running')",
                (schedule_id, now),
            )
            conn.commit()
            return cursor.lastrowid

    def record_run_finish(self, run_id: int, result: str, error: str | None = None, duration_ms: int | None = None) -> None:
        """记录执行结束"""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE schedule_runs SET finished_at = ?, result = ?, error = ?, duration_ms = ? WHERE run_id = ?",
                (now, result, error, duration_ms, run_id),
            )
            conn.commit()

    def get_recent_runs(self, schedule_id: str, limit: int = 10) -> list[dict]:
        """获取最近执行记录"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM schedule_runs WHERE schedule_id = ? ORDER BY triggered_at DESC LIMIT ?",
                (schedule_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ─── 跨进程 PID 锁 ────────────────────────────────────────

    def try_acquire_lock(self, schedule_id: str, pid: int, timeout_seconds: int = 120) -> bool:
        """尝试获取调度锁（跨进程防重复触发）

        返回 True 表示获取成功，可以执行。
        如果已有锁且未超时，返回 False（其他进程正在执行）。
        如果已有锁但已超时，自动释放后重新获取。
        """
        import os
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        with self._connect() as conn:
            row = conn.execute(
                "SELECT pid, locked_at FROM schedule_pid_lock WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()

            if row:
                locked_at = datetime.fromisoformat(row["locked_at"])
                locked_pid = row["pid"]

                # 检查锁是否超时
                elapsed = (now - locked_at).total_seconds()
                if elapsed < timeout_seconds:
                    # 锁未超时，检查进程是否还活着
                    if locked_pid != pid:
                        try:
                            os.kill(locked_pid, 0)  # 检查进程是否存在
                            return False  # 进程还活着，锁有效
                        except (ProcessLookupError, PermissionError):
                            pass  # 进程已死，释放锁

                # 锁超时或进程已死，释放旧锁
                conn.execute(
                    "DELETE FROM schedule_pid_lock WHERE schedule_id = ?",
                    (schedule_id,),
                )

            # 获取锁
            conn.execute(
                "INSERT INTO schedule_pid_lock (schedule_id, pid, locked_at) VALUES (?, ?, ?)",
                (schedule_id, pid, now_iso),
            )
            conn.commit()
            return True

    def release_lock(self, schedule_id: str, pid: int) -> None:
        """释放调度锁"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM schedule_pid_lock WHERE schedule_id = ? AND pid = ?",
                (schedule_id, pid),
            )
            conn.commit()

    # ─── 持久化内部方法 ────────────────────────────────────────

    def _upsert_to_db(self, entry: ScheduleEntry) -> None:
        """写入或更新到 SQLite"""
        params = self._entry_to_params(entry)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO schedules (
                    schedule_id, name, cron, message, conversation_id, user_id,
                    status, retry_policy, retry_count, last_run, next_run,
                    last_run_result, last_run_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(schedule_id) DO UPDATE SET
                    name=excluded.name, cron=excluded.cron, message=excluded.message,
                    conversation_id=excluded.conversation_id, user_id=excluded.user_id,
                    status=excluded.status, retry_policy=excluded.retry_policy,
                    retry_count=excluded.retry_count, last_run=excluded.last_run,
                    next_run=excluded.next_run, last_run_result=excluded.last_run_result,
                    last_run_error=excluded.last_run_error, updated_at=excluded.updated_at
                """,
                params,
            )
            conn.commit()

    def _delete_from_db(self, schedule_id: str) -> None:
        """从 SQLite 删除"""
        with self._connect() as conn:
            conn.execute("DELETE FROM schedules WHERE schedule_id = ?", (schedule_id,))
            conn.execute("DELETE FROM schedule_pid_lock WHERE schedule_id = ?", (schedule_id,))
            conn.commit()

    # ─── 兼容旧接口 ────────────────────────────────────────────

    def recover(self) -> int:
        """兼容旧接口：从 SQLite 加载时已在 _load_all 中完成"""
        return len(self._schedules)
