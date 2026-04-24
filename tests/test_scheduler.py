"""定时调度模块单元测试

覆盖：
- CronParser：5位 cron 表达式解析
- ScheduleStore：SQLite 持久化、执行记录、PID 锁
- Scheduler：暂停/恢复/重试/触发
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.scheduler.cron_parser import CronParser, CronParseError
from src.scheduler.schedule_store import ScheduleStore
from src.scheduler.scheduler import Scheduler
from src.types import Event, EventType, ScheduleEntry, ScheduleStatus, RetryPolicy, make_event


# ─── CronParser 测试 ─────────────────────────────────────────


class TestCronParser:
    """5位 cron 表达式解析器测试"""

    def test_every_minute(self):
        """* * * * * 匹配任意时间"""
        cron = CronParser("* * * * *")
        dt = datetime(2026, 4, 21, 8, 30, tzinfo=timezone.utc)
        assert cron.matches(dt)

    def test_specific_minute(self):
        """0 8 * * * 每天早上8:00"""
        cron = CronParser("0 8 * * *")
        assert cron.matches(datetime(2026, 4, 21, 8, 0, tzinfo=timezone.utc))
        assert not cron.matches(datetime(2026, 4, 21, 8, 1, tzinfo=timezone.utc))
        assert not cron.matches(datetime(2026, 4, 21, 9, 0, tzinfo=timezone.utc))

    def test_step(self):
        """*/15 * * * * 每15分钟"""
        cron = CronParser("*/15 * * * *")
        assert cron.matches(datetime(2026, 4, 21, 8, 0, tzinfo=timezone.utc))
        assert cron.matches(datetime(2026, 4, 21, 8, 15, tzinfo=timezone.utc))
        assert cron.matches(datetime(2026, 4, 21, 8, 30, tzinfo=timezone.utc))
        assert not cron.matches(datetime(2026, 4, 21, 8, 7, tzinfo=timezone.utc))

    def test_range(self):
        """0 9 * * 1-5 周一到周五9:00"""
        cron = CronParser("0 9 * * 1-5")
        # 2026-04-20 是周一
        assert cron.matches(datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc))
        # 2026-04-25 是周六
        assert not cron.matches(datetime(2026, 4, 25, 9, 0, tzinfo=timezone.utc))

    def test_list(self):
        """0 8,12,18 * * * 每天8/12/18点"""
        cron = CronParser("0 8,12,18 * * *")
        assert cron.matches(datetime(2026, 4, 21, 8, 0, tzinfo=timezone.utc))
        assert cron.matches(datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc))
        assert cron.matches(datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc))
        assert not cron.matches(datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc))

    def test_aliases(self):
        """特殊别名"""
        hourly = CronParser("@hourly")
        assert hourly.matches(datetime(2026, 4, 21, 8, 0, tzinfo=timezone.utc))
        assert not hourly.matches(datetime(2026, 4, 21, 8, 30, tzinfo=timezone.utc))

        daily = CronParser("@daily")
        assert daily.matches(datetime(2026, 4, 21, 0, 0, tzinfo=timezone.utc))
        assert not daily.matches(datetime(2026, 4, 21, 1, 0, tzinfo=timezone.utc))

    def test_invalid_expression(self):
        """无效表达式抛出 CronParseError"""
        with pytest.raises(CronParseError):
            CronParser("0 8 *")  # 只有3个字段

        with pytest.raises(CronParseError):
            CronParser("60 8 * * *")  # 分钟超出范围

    def test_next_time(self):
        """next_time 计算下次触发时间"""
        cron = CronParser("0 8 * * *")  # 每天8:00
        after = datetime(2026, 4, 21, 7, 30, tzinfo=timezone.utc)
        nxt = cron.next_time(after)
        assert nxt.hour == 8
        assert nxt.minute == 0

        # 如果当前已过8:00，下次是明天8:00
        after2 = datetime(2026, 4, 21, 8, 1, tzinfo=timezone.utc)
        nxt2 = cron.next_time(after2)
        assert nxt2.day == 22
        assert nxt2.hour == 8

    def test_next_time_hourly(self):
        """@hourly 的 next_time"""
        cron = CronParser("@hourly")
        after = datetime(2026, 4, 21, 8, 30, tzinfo=timezone.utc)
        nxt = cron.next_time(after)
        assert nxt.hour == 9
        assert nxt.minute == 0


# ─── ScheduleStore 测试 ──────────────────────────────────────


class TestScheduleStore:
    """定时任务 SQLite 持久化测试"""

    @pytest.fixture
    def store(self, tmp_path):
        """使用临时目录的 ScheduleStore"""
        config = RaccoonConfig(db_path=tmp_path / "data" / "test.db")
        return ScheduleStore(config)

    def test_add_and_get(self, store):
        """添加和查询"""
        entry = ScheduleEntry(
            name="测试任务",
            cron="0 8 * * *",
            message="早上好",
            conversation_id="conv-1",
        )
        store.add(entry)

        got = store.get(entry.schedule_id)
        assert got is not None
        assert got.name == "测试任务"
        assert got.cron == "0 8 * * *"

    def test_remove(self, store):
        """删除"""
        entry = ScheduleEntry(
            name="待删除",
            cron="@daily",
            message="test",
            conversation_id="conv-2",
        )
        store.add(entry)
        removed = store.remove(entry.schedule_id)
        assert removed is not None
        assert removed.name == "待删除"
        assert store.get(entry.schedule_id) is None

    def test_get_enabled(self, store):
        """获取启用的任务"""
        e1 = ScheduleEntry(name="启用", cron="@daily", message="1", conversation_id="c1", status=ScheduleStatus.ENABLED)
        e2 = ScheduleEntry(name="禁用", cron="@daily", message="2", conversation_id="c2", status=ScheduleStatus.DISABLED)
        e3 = ScheduleEntry(name="暂停", cron="@daily", message="3", conversation_id="c3", status=ScheduleStatus.PAUSED)
        store.add(e1)
        store.add(e2)
        store.add(e3)

        enabled = store.get_enabled()
        assert len(enabled) == 1
        assert enabled[0].name == "启用"

    def test_persist_and_recover(self, tmp_path):
        """持久化和恢复"""
        db_path = tmp_path / "data" / "test.db"
        config = RaccoonConfig(db_path=db_path)
        store1 = ScheduleStore(config)

        entry = ScheduleEntry(
            name="持久化测试",
            cron="*/30 * * * *",
            message="每30分钟",
            conversation_id="conv-p",
        )
        store1.add(entry)

        # 新 store 从同一数据库恢复
        store2 = ScheduleStore(config)
        got = store2.get(entry.schedule_id)
        assert got is not None
        assert got.name == "持久化测试"
        assert got.cron == "*/30 * * * *"

    def test_update(self, store):
        """更新"""
        entry = ScheduleEntry(
            name="原始",
            cron="@daily",
            message="hi",
            conversation_id="c1",
        )
        store.add(entry)

        entry.name = "更新后"
        store.update(entry)

        got = store.get(entry.schedule_id)
        assert got.name == "更新后"

    def test_count(self, store):
        """计数"""
        assert store.count() == 0
        store.add(ScheduleEntry(name="a", cron="@daily", message="1", conversation_id="c1"))
        assert store.count() == 1
        store.add(ScheduleEntry(name="b", cron="@daily", message="2", conversation_id="c2"))
        assert store.count() == 2

    def test_schedule_status(self, store):
        """测试 ScheduleStatus 三态"""
        entry = ScheduleEntry(
            name="状态测试",
            cron="@daily",
            message="test",
            conversation_id="c1",
            status=ScheduleStatus.PAUSED,
        )
        store.add(entry)

        got = store.get(entry.schedule_id)
        assert got.status == ScheduleStatus.PAUSED
        assert not got.enabled  # paused 不是 enabled

    def test_retry_policy(self, store):
        """测试 RetryPolicy 持久化"""
        policy = RetryPolicy(max_retries=5, retry_interval_seconds=120, retry_on_failure=True)
        entry = ScheduleEntry(
            name="重试测试",
            cron="@daily",
            message="test",
            conversation_id="c1",
            retry_policy=policy,
        )
        store.add(entry)

        got = store.get(entry.schedule_id)
        assert got.retry_policy.max_retries == 5
        assert got.retry_policy.retry_interval_seconds == 120

    def test_record_run(self, store):
        """测试执行记录"""
        entry = ScheduleEntry(name="记录测试", cron="@daily", message="test", conversation_id="c1")
        store.add(entry)

        # 记录开始
        run_id = store.record_run_start(entry.schedule_id)
        assert run_id > 0

        # 记录完成
        store.record_run_finish(run_id, "success", duration_ms=1500)

        # 查询记录
        runs = store.get_recent_runs(entry.schedule_id)
        assert len(runs) == 1
        assert runs[0]["result"] == "success"
        assert runs[0]["duration_ms"] == 1500

    def test_pid_lock(self, store):
        """测试跨进程 PID 锁"""
        entry = ScheduleEntry(name="锁测试", cron="@daily", message="test", conversation_id="c1")
        store.add(entry)

        pid = os.getpid()
        # 首次获取锁应该成功
        assert store.try_acquire_lock(entry.schedule_id, pid) is True

        # 同一进程再次获取也应该成功（upsert）
        assert store.try_acquire_lock(entry.schedule_id, pid) is True

        # 释放锁
        store.release_lock(entry.schedule_id, pid)

        # 释放后可以重新获取
        assert store.try_acquire_lock(entry.schedule_id, pid) is True
        store.release_lock(entry.schedule_id, pid)

    def test_pid_lock_different_process(self, store):
        """不同进程的 PID 锁"""
        entry = ScheduleEntry(name="跨进程锁", cron="@daily", message="test", conversation_id="c1")
        store.add(entry)

        pid1 = os.getpid()
        pid2 = 999999  # 不存在的 PID

        # pid1 获取锁
        assert store.try_acquire_lock(entry.schedule_id, pid1) is True

        # pid2 尝试获取（pid1 还活着，应该失败）
        assert store.try_acquire_lock(entry.schedule_id, pid2) is False

        # 释放锁
        store.release_lock(entry.schedule_id, pid1)

        # 现在 pid2 可以获取
        assert store.try_acquire_lock(entry.schedule_id, pid2) is True
        store.release_lock(entry.schedule_id, pid2)

    def test_enabled_compat(self):
        """测试 enabled 属性兼容性"""
        e1 = ScheduleEntry(name="t", cron="@daily", message="t", conversation_id="c1")
        assert e1.enabled is True
        assert e1.status == ScheduleStatus.ENABLED

        e1.set_enabled(False)
        assert e1.enabled is False
        assert e1.status == ScheduleStatus.DISABLED

        e1.set_enabled(True)
        assert e1.enabled is True
        assert e1.status == ScheduleStatus.ENABLED


# ─── Scheduler 集成测试 ─────────────────────────────────────


class TestScheduler:
    """调度引擎集成测试"""

    @pytest.fixture
    def components(self, tmp_path):
        """创建 EventBus + ScheduleStore + Scheduler"""
        config = RaccoonConfig(
            event_queue_size=100,
            db_path=tmp_path / "data" / "test.db",
        )
        event_bus = EventBus(config)
        store = ScheduleStore(config)
        scheduler = Scheduler(event_bus, store, config)
        return event_bus, store, scheduler

    @pytest.mark.asyncio
    async def test_add_and_list(self, components):
        """添加和列出定时任务"""
        event_bus, store, scheduler = components
        entry = ScheduleEntry(
            name="测试",
            cron="0 8 * * *",
            message="早上好",
            conversation_id="conv-1",
        )
        result = await scheduler.add_schedule(entry)
        assert result.next_run is not None

        schedules = scheduler.list_schedules()
        assert len(schedules) == 1
        assert schedules[0].name == "测试"

    @pytest.mark.asyncio
    async def test_remove_schedule(self, components):
        """删除定时任务"""
        event_bus, store, scheduler = components
        entry = ScheduleEntry(
            name="待删",
            cron="@daily",
            message="bye",
            conversation_id="conv-2",
        )
        await scheduler.add_schedule(entry)
        removed = await scheduler.remove_schedule(entry.schedule_id)
        assert removed is not None
        assert len(scheduler.list_schedules()) == 0

    @pytest.mark.asyncio
    async def test_toggle_schedule(self, components):
        """启用/禁用"""
        event_bus, store, scheduler = components
        entry = ScheduleEntry(
            name="切换",
            cron="@daily",
            message="toggle",
            conversation_id="conv-3",
            status=ScheduleStatus.ENABLED,
        )
        await scheduler.add_schedule(entry)

        toggled = await scheduler.toggle_schedule(entry.schedule_id)
        assert toggled.enabled is False
        assert toggled.status == ScheduleStatus.DISABLED

        toggled2 = await scheduler.toggle_schedule(entry.schedule_id)
        assert toggled2.enabled is True
        assert toggled2.status == ScheduleStatus.ENABLED

    @pytest.mark.asyncio
    async def test_pause_and_resume(self, components):
        """暂停和恢复"""
        event_bus, store, scheduler = components
        entry = ScheduleEntry(
            name="暂停测试",
            cron="@daily",
            message="pause",
            conversation_id="conv-pause",
        )
        await scheduler.add_schedule(entry)

        # 暂停
        paused = await scheduler.pause_schedule(entry.schedule_id)
        assert paused.status == ScheduleStatus.PAUSED
        assert not paused.enabled  # paused 不算 enabled

        # 恢复
        resumed = await scheduler.resume_schedule(entry.schedule_id)
        assert resumed.status == ScheduleStatus.ENABLED
        assert resumed.enabled

    @pytest.mark.asyncio
    async def test_pause_not_resumable_if_disabled(self, components):
        """禁用状态的任务不能恢复"""
        event_bus, store, scheduler = components
        entry = ScheduleEntry(
            name="禁用测试",
            cron="@daily",
            message="disabled",
            conversation_id="conv-dis",
            status=ScheduleStatus.DISABLED,
        )
        await scheduler.add_schedule(entry)

        # 禁用状态不能 resume
        result = await scheduler.resume_schedule(entry.schedule_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_schedule_triggered_event(self, components):
        """定时触发时向 EventBus 发送 SCHEDULE_TRIGGERED 事件"""
        event_bus, store, scheduler = components
        received = []

        async def on_triggered(event: Event):
            received.append(event)

        event_bus.on(EventType.SCHEDULE_TRIGGERED, on_triggered)
        await event_bus.start()
        await scheduler.start()

        # 创建一个"每分钟"触发的 schedule
        entry = ScheduleEntry(
            name="每分钟",
            cron="* * * * *",
            message="tick",
            conversation_id="conv-tick",
        )
        await scheduler.add_schedule(entry)

        # 等待最多 2 秒让调度器触发
        await asyncio.sleep(2)

        await scheduler.stop()
        await event_bus.stop()

        # 应该收到至少一个 SCHEDULE_TRIGGERED 事件
        assert len(received) >= 1
        assert received[0].event == EventType.SCHEDULE_TRIGGERED
        assert received[0].payload["text"] == "tick"
        assert received[0].payload["schedule_name"] == "每分钟"

    @pytest.mark.asyncio
    async def test_no_duplicate_trigger(self, components):
        """同一分钟内不重复触发"""
        event_bus, store, scheduler = components
        received = []

        async def on_triggered(event: Event):
            received.append(event)

        event_bus.on(EventType.SCHEDULE_TRIGGERED, on_triggered)
        await event_bus.start()
        await scheduler.start()

        entry = ScheduleEntry(
            name="防重复",
            cron="* * * * *",
            message="once",
            conversation_id="conv-dedup",
        )
        await scheduler.add_schedule(entry)

        # 等待超过1秒但不到1分钟
        await asyncio.sleep(1.5)

        await scheduler.stop()
        await event_bus.stop()

        # 同一分钟内应该只触发一次
        assert len(received) <= 1

    @pytest.mark.asyncio
    async def test_disabled_schedule_not_triggered(self, components):
        """禁用的 schedule 不会触发"""
        event_bus, store, scheduler = components
        received = []

        async def on_triggered(event: Event):
            received.append(event)

        event_bus.on(EventType.SCHEDULE_TRIGGERED, on_triggered)
        await event_bus.start()
        await scheduler.start()

        entry = ScheduleEntry(
            name="已禁用",
            cron="* * * * *",
            message="should not fire",
            conversation_id="conv-disabled",
            status=ScheduleStatus.DISABLED,
        )
        await scheduler.add_schedule(entry)

        await asyncio.sleep(2)

        await scheduler.stop()
        await event_bus.stop()

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_paused_schedule_not_triggered(self, components):
        """暂停的 schedule 不会触发"""
        event_bus, store, scheduler = components
        received = []

        async def on_triggered(event: Event):
            received.append(event)

        event_bus.on(EventType.SCHEDULE_TRIGGERED, on_triggered)
        await event_bus.start()
        await scheduler.start()

        entry = ScheduleEntry(
            name="已暂停",
            cron="* * * * *",
            message="should not fire",
            conversation_id="conv-paused",
            status=ScheduleStatus.PAUSED,
        )
        await scheduler.add_schedule(entry)

        await asyncio.sleep(2)

        await scheduler.stop()
        await event_bus.stop()

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_record_run_result(self, components):
        """测试记录执行结果"""
        event_bus, store, scheduler = components
        entry = ScheduleEntry(
            name="结果记录",
            cron="@daily",
            message="test",
            conversation_id="c1",
        )
        await scheduler.add_schedule(entry)

        # 模拟执行成功
        run_id = store.record_run_start(entry.schedule_id)
        await scheduler.record_run_result(entry.schedule_id, run_id, success=True, duration_ms=500)

        got = store.get(entry.schedule_id)
        assert got.last_run_result == "success"
        assert got.retry_count == 0

    @pytest.mark.asyncio
    async def test_record_run_result_failure_triggers_retry(self, components):
        """测试失败触发重试"""
        event_bus, store, scheduler = components
        entry = ScheduleEntry(
            name="重试测试",
            cron="@daily",
            message="test",
            conversation_id="c1",
            retry_policy=RetryPolicy(max_retries=2, retry_interval_seconds=1),
        )
        await scheduler.add_schedule(entry)

        # 模拟执行失败
        run_id = store.record_run_start(entry.schedule_id)
        await scheduler.record_run_result(entry.schedule_id, run_id, success=False, error="timeout")

        got = store.get(entry.schedule_id)
        assert got.last_run_result == "retrying"
        assert got.retry_count == 1

    @pytest.mark.asyncio
    async def test_get_schedule_runs(self, components):
        """测试获取执行记录"""
        event_bus, store, scheduler = components
        entry = ScheduleEntry(name="记录测试", cron="@daily", message="test", conversation_id="c1")
        await scheduler.add_schedule(entry)

        # 创建几条执行记录
        run_id1 = store.record_run_start(entry.schedule_id)
        store.record_run_finish(run_id1, "success", duration_ms=100)

        run_id2 = store.record_run_start(entry.schedule_id)
        store.record_run_finish(run_id2, "failed", error="timeout", duration_ms=5000)

        runs = scheduler.get_schedule_runs(entry.schedule_id)
        assert len(runs) == 2
        # 最新的在前
        assert runs[0]["result"] == "failed"
        assert runs[1]["result"] == "success"
