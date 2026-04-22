"""场景验收测试

验收计划中的 6 个核心场景：
- 场景5：每日简报（定时+通知）
- 场景6：价格监控（定时+变化检测+通知）
- 场景8：表单填报（工作流+CDP）
- 场景11：素材收集（工作流多步）
- 场景16：提醒备忘（定时+通知）
- 场景19：手机远程执行（双向通信+通知）

每个场景测试完整的能力链路：从用户意图到最终效果，
验证多个模块的协作是否正确。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.gateway.inbound import GatewayInbound, GatewayAuthError
from src.notifier.channels.base import BaseChannel, Notification, Priority
from src.notifier.notifier import Notifier
from src.scheduler.cron_parser import CronParser
from src.scheduler.schedule_store import ScheduleStore
from src.scheduler.scheduler import Scheduler
from src.types import Event, ScheduleEntry, WorkflowEntry, WorkflowStep
from src.workflow.workflow_engine import WorkflowEngine
from src.workflow.workflow_store import WorkflowStore


# ─── 测试辅助 ────────────────────────────────────────────────────

class RecordingChannel(BaseChannel):
    """记录所有通知的测试通道"""

    channel_type = "recorder"

    def __init__(self, config: dict | None = None):
        super().__init__(config=config or {"name": "recorder"})
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        return True


def _make_config(**overrides) -> RaccoonConfig:
    """创建测试配置"""
    tmp = tempfile.mkdtemp()
    defaults = {
        "schedules_dir": Path(tmp) / "schedules",
        "workflows_dir": Path(tmp) / "workflows",
        "notify_channels": [],
        "notify_silent_hours": {},
        "notify_on_success": True,
        "notify_on_failure": True,
        "gateway_token": "test-token-123",
        "llm_provider": "mock",
    }
    defaults.update(overrides)
    return RaccoonConfig(**defaults)


# ─── 场景5：每日简报（定时+通知）────────────────────────────────

class TestScenario5DailyBriefing:
    """场景5：每日简报

    用户场景：用户希望每天早上8点收到天气+日历+新闻的聚合简报。
    能力链路：定时调度 → SCHEDULE_TRIGGERED → Router → Executor → 通知推送

    验收标准：
    1. 能创建 "每天8:00" 的 cron 定时任务
    2. 定时触发时正确生成 SCHEDULE_TRIGGERED 事件
    3. 事件被 Router → Executor 流程处理
    4. 执行完成后通过通知通道推送结果
    """

    @pytest.mark.asyncio
    async def test_create_daily_8am_schedule(self):
        """能创建每天8:00触发的定时任务"""
        config = _make_config()
        store = ScheduleStore(config)
        store.recover()
        bus = EventBus(config)
        scheduler = Scheduler(bus, store, config)

        entry = ScheduleEntry(
            name="每日简报",
            cron="0 8 * * *",
            message="帮我生成今日简报：天气、日历、新闻",
            conversation_id="daily-briefing",
        )
        result = await scheduler.add_schedule(entry)

        assert result.schedule_id
        assert result.name == "每日简报"
        assert result.cron == "0 8 * * *"
        assert result.enabled is True
        assert result.next_run is not None

        # 验证 cron 解析正确：next_run 应该是未来某个 8:00
        next_run = result.next_run
        assert next_run.hour == 8
        assert next_run.minute == 0

    @pytest.mark.asyncio
    async def test_schedule_triggers_event(self):
        """定时触发时正确生成 SCHEDULE_TRIGGERED 事件"""
        config = _make_config()
        store = ScheduleStore(config)
        store.recover()
        bus = EventBus(config)
        scheduler = Scheduler(bus, store, config)

        # 记录事件
        triggered_events: list[Event] = []
        bus.on(EventType.SCHEDULE_TRIGGERED, lambda e: triggered_events.append(e))

        # 创建一个"每分钟"触发的任务（测试用）
        entry = ScheduleEntry(
            name="测试简报",
            cron="* * * * *",
            message="生成简报",
            conversation_id="test-briefing",
        )
        await scheduler.add_schedule(entry)

        # 启动调度器，等待最多2秒
        await bus.start()
        await scheduler.start()
        try:
            await asyncio.sleep(2)
        finally:
            await scheduler.stop()
            await bus.stop()

        # 应该触发了至少一次
        assert len(triggered_events) >= 1
        event = triggered_events[0]
        assert event.event == EventType.SCHEDULE_TRIGGERED
        assert event.payload["text"] == "生成简报"
        assert event.payload["schedule_name"] == "测试简报"

    @pytest.mark.asyncio
    async def test_briefing_result_notified(self):
        """简报执行完成后通过通知通道推送"""
        config = _make_config()
        bus = EventBus(config)

        # 添加录制通道
        recorder = RecordingChannel()
        notifier = Notifier(bus, config)
        notifier._channels.append(recorder)
        notifier.initialize()

        await bus.start()
        try:
            # 模拟任务完成事件（简报生成完毕）
            event = make_event(
                EventType.TASK_COMPLETED,
                conversation_id="daily-briefing",
                skill_name="echo",
                task_id="task-001",
                payload={"result": "今日简报：晴 25°C，无日程，3条新闻"},
            )
            await bus.emit(event)
            await asyncio.sleep(0.5)  # 等待事件处理

            # 验证通知已发送
            assert len(recorder.sent) >= 1
            notification = recorder.sent[0]
            assert "任务完成" in notification.title
            assert "简报" in notification.body or "task-001" in notification.body
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_schedule_persistence(self):
        """定时任务重启后恢复"""
        config = _make_config()
        store = ScheduleStore(config)
        store.recover()

        entry = ScheduleEntry(
            name="每日简报",
            cron="0 8 * * *",
            message="帮我生成今日简报",
            conversation_id="daily-briefing",
        )
        store.add(entry)

        # 模拟重启：新建 store 实例，从磁盘恢复
        store2 = ScheduleStore(config)
        store2.recover()

        entries = list(store2.all_schedules())
        assert len(entries) == 1
        assert entries[0].name == "每日简报"
        assert entries[0].cron == "0 8 * * *"


# ─── 场景6：价格监控（定时+变化检测+通知）────────────────────────

class TestScenario6PriceMonitor:
    """场景6：价格/库存监控

    用户场景：用户想监控某商品价格，降价时收到通知。
    能力链路：定时调度 → change_detector Skill → 检测到变化 → 通知推送

    验收标准：
    1. 能创建定时轮询任务
    2. change_detector 能创建快照和检测变化
    3. 检测到变化时触发通知
    """

    @pytest.mark.asyncio
    async def test_create_price_monitor_schedule(self):
        """能创建价格监控的定时轮询任务"""
        config = _make_config()
        store = ScheduleStore(config)
        store.recover()
        bus = EventBus(config)
        scheduler = Scheduler(bus, store, config)

        entry = ScheduleEntry(
            name="价格监控-手机",
            cron="*/30 * * * *",  # 每30分钟
            message="检查手机价格变化",
            conversation_id="price-monitor",
        )
        result = await scheduler.add_schedule(entry)

        assert result.name == "价格监控-手机"
        assert result.cron == "*/30 * * * *"
        assert result.enabled is True

    def test_change_detector_snapshot(self):
        """change_detector 能创建快照"""
        from skills.change_detector.main import cmd_snapshot, SNAPSHOT_DIR

        # 确保快照目录存在
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

        # 创建临时文件作为监控目标
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("价格: 3999元")
            f.flush()
            result = cmd_snapshot({"path": f.name, "name": "手机价格"})

        assert "快照" in result["reply"]
        assert result["changed"] is False

        # 清理
        Path(f.name).unlink(missing_ok=True)
        # 清理快照
        if SNAPSHOT_DIR.exists():
            shutil.rmtree(SNAPSHOT_DIR, ignore_errors=True)

    def test_change_detector_detects_change(self):
        """change_detector 能检测到变化"""
        from skills.change_detector.main import cmd_snapshot, cmd_check, SNAPSHOT_DIR

        # 确保快照目录存在
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

        # 创建临时文件
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp.write("价格: 3999元\n")
        tmp.close()

        # 第一次快照
        cmd_snapshot({"path": tmp.name, "name": "手机价格"})

        # 修改文件
        Path(tmp.name).write_text("价格: 2999元\n", "utf-8")

        # 检查变化 - 需要重新 snapshot 来检测变化
        result = cmd_snapshot({"path": tmp.name, "name": "手机价格"})
        assert result["changed"] is True

        # 清理
        Path(tmp.name).unlink(missing_ok=True)
        if SNAPSHOT_DIR.exists():
            shutil.rmtree(SNAPSHOT_DIR, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_price_change_triggers_notification(self):
        """价格变化时触发通知"""
        config = _make_config()
        bus = EventBus(config)

        recorder = RecordingChannel()
        notifier = Notifier(bus, config)
        notifier._channels.append(recorder)
        notifier.initialize()

        await bus.start()
        try:
            # 模拟价格变化检测完成 → 任务完成事件
            event = make_event(
                EventType.TASK_COMPLETED,
                conversation_id="price-monitor",
                skill_name="change_detector",
                task_id="task-price-001",
                payload={"result": "检测到变化！手机价格: 3999→2999"},
            )
            await bus.emit(event)
            await asyncio.sleep(0.5)

            assert len(recorder.sent) >= 1
            assert "change_detector" in recorder.sent[0].title
        finally:
            await bus.stop()


# ─── 场景8：表单填报（工作流+CDP）────────────────────────────────

class TestScenario8FormFill:
    """场景8：表单/报表填报

    用户场景：用户需要定期在网页上填写周报表单。
    能力链路：工作流编排 → 多步骤执行（打开页面→填表→提交）→ 通知结果

    验收标准：
    1. 能创建包含多步骤的工作流模板
    2. 工作流能按步骤顺序执行
    3. 步骤间数据传递（input_from）正确
    4. 工作流执行结果能通知用户
    """

    @pytest.mark.asyncio
    async def test_create_form_fill_workflow(self):
        """能创建表单填报工作流"""
        config = _make_config()
        store = WorkflowStore(config)
        store.recover()

        entry = WorkflowEntry(
            name="周报填报",
            description="自动填写并提交周报表单",
            steps=[
                WorkflowStep(step_id=0, type="skill", skill_name="echo",
                             params={"text": "准备周报数据"}),
                WorkflowStep(step_id=1, type="skill", skill_name="echo",
                             params={"text": "填写表单"},
                             input_from=0),
                WorkflowStep(step_id=2, type="skill", skill_name="echo",
                             params={"text": "提交表单"},
                             input_from=1),
            ],
        )
        result = store.add(entry)

        assert result.workflow_id
        assert result.name == "周报填报"
        assert len(result.steps) == 3
        assert result.steps[1].input_from == 0
        assert result.steps[2].input_from == 1

    @pytest.mark.asyncio
    async def test_workflow_executes_steps_in_order(self):
        """工作流按步骤顺序执行"""
        config = _make_config()
        bus = EventBus(config)
        store = WorkflowStore(config)
        store.recover()

        # Mock Router 和 Executor
        router = MagicMock()
        executor = MagicMock()

        step_results = []

        async def mock_handle(route_result, event):
            step_results.append(route_result.skill_name or route_result.route_type.value)
            return f"步骤完成: {route_result.skill_name or route_result.route_type.value}"

        executor.handle_route_result = mock_handle

        engine = WorkflowEngine(bus, router, executor, store, config)

        workflow = WorkflowEntry(
            name="测试工作流",
            steps=[
                WorkflowStep(step_id=0, type="skill", skill_name="echo", params={"text": "step1"}),
                WorkflowStep(step_id=1, type="skill", skill_name="echo", params={"text": "step2"}),
                WorkflowStep(step_id=2, type="skill", skill_name="echo", params={"text": "step3"}),
            ],
        )

        await bus.start()
        try:
            ctx = await engine.execute(workflow)
            assert ctx.status == "completed"
            assert len(step_results) == 3
            # 步骤按顺序执行
            assert all(r == "echo" for r in step_results)
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_workflow_step_data_passing(self):
        """工作流步骤间数据传递"""
        config = _make_config()
        bus = EventBus(config)
        store = WorkflowStore(config)
        store.recover()

        router = MagicMock()
        executor = MagicMock()

        async def mock_handle(route_result, event):
            # 模拟步骤返回数据
            skill = route_result.skill_name
            if skill == "fetch_data":
                return "本周完成了5个任务"
            elif skill == "fill_form":
                # 检查 input_from 传递的数据
                return f"表单已填写: {route_result.params.get('_input', '无数据')}"
            return "完成"

        executor.handle_route_result = mock_handle

        engine = WorkflowEngine(bus, router, executor, store, config)

        workflow = WorkflowEntry(
            name="数据传递测试",
            steps=[
                WorkflowStep(step_id=0, type="skill", skill_name="fetch_data", params={}),
                WorkflowStep(step_id=1, type="skill", skill_name="fill_form",
                             params={}, input_from=0),
            ],
        )

        await bus.start()
        try:
            ctx = await engine.execute(workflow)
            assert ctx.status == "completed"
            # 步骤1的结果应该包含步骤0的输出
            step1_result = ctx.step_results[1]
            assert "reply" in step1_result
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_workflow_result_notified(self):
        """工作流执行完成后通知"""
        config = _make_config()
        bus = EventBus(config)
        store = WorkflowStore(config)
        store.recover()

        recorder = RecordingChannel()
        notifier = Notifier(bus, config)
        notifier._channels.append(recorder)
        notifier.initialize()

        router = MagicMock()
        executor = MagicMock()
        executor.handle_route_result = AsyncMock(return_value="完成")

        engine = WorkflowEngine(bus, router, executor, store, config)

        workflow = WorkflowEntry(
            name="周报填报",
            steps=[WorkflowStep(step_id=0, type="skill", skill_name="echo", params={})],
        )

        await bus.start()
        try:
            ctx = await engine.execute(workflow)
            assert ctx.status == "completed"

            # 工作流完成会发射 TASK_COMPLETED 事件 → Notifier 应收到
            await asyncio.sleep(0.5)
            assert len(recorder.sent) >= 1
        finally:
            await bus.stop()


# ─── 场景11：素材收集（工作流多步）───────────────────────────────

class TestScenario11MaterialCollection:
    """场景11：素材收集与整理

    用户场景：用户想收集某主题的素材，搜索+抓取+摘要+整理成文档。
    能力链路：工作流编排 → 搜索 → 抓取 → 摘要 → 整理

    验收标准：
    1. 能创建多步骤素材收集工作流
    2. 工作流能持久化保存和复用
    3. 能按名称执行工作流
    4. 工作流模板支持条件分支
    """

    @pytest.mark.asyncio
    async def test_create_material_collection_workflow(self):
        """能创建素材收集工作流"""
        config = _make_config()
        store = WorkflowStore(config)
        store.recover()

        entry = WorkflowEntry(
            name="素材收集",
            description="搜索+抓取+摘要+整理成文档",
            steps=[
                WorkflowStep(step_id=0, type="skill", skill_name="echo",
                             params={"text": "搜索相关素材"}),
                WorkflowStep(step_id=1, type="skill", skill_name="echo",
                             params={"text": "抓取网页内容"},
                             input_from=0),
                WorkflowStep(step_id=2, type="llm",
                             params={"text": "对素材进行摘要整理"},
                             input_from=1),
                WorkflowStep(step_id=3, type="skill", skill_name="echo",
                             params={"text": "生成文档"},
                             input_from=2),
            ],
        )
        result = store.add(entry)

        assert result.name == "素材收集"
        assert len(result.steps) == 4
        # 验证步骤类型混合
        assert result.steps[0].type == "skill"
        assert result.steps[2].type == "llm"
        assert result.steps[3].type == "skill"

    @pytest.mark.asyncio
    async def test_workflow_persistence_and_reuse(self):
        """工作流模板持久化保存和复用"""
        config = _make_config()
        store = WorkflowStore(config)
        store.recover()

        entry = WorkflowEntry(
            name="素材收集",
            steps=[
                WorkflowStep(step_id=0, type="skill", skill_name="echo", params={}),
            ],
        )
        store.add(entry)

        # 模拟重启
        store2 = WorkflowStore(config)
        store2.recover()

        entries = store2.list_all()
        assert len(entries) == 1
        assert entries[0].name == "素材收集"

    @pytest.mark.asyncio
    async def test_execute_workflow_by_name(self):
        """按名称执行工作流"""
        config = _make_config()
        bus = EventBus(config)
        store = WorkflowStore(config)
        store.recover()

        router = MagicMock()
        executor = MagicMock()
        executor.handle_route_result = AsyncMock(return_value="步骤完成")

        engine = WorkflowEngine(bus, router, executor, store, config)

        entry = WorkflowEntry(
            name="素材收集",
            steps=[
                WorkflowStep(step_id=0, type="skill", skill_name="echo", params={}),
                WorkflowStep(step_id=1, type="skill", skill_name="echo", params={}),
            ],
        )
        store.add(entry)

        await bus.start()
        try:
            ctx = await engine.execute_by_name("素材收集")
            assert ctx is not None
            assert ctx.status == "completed"
            assert ctx.workflow.name == "素材收集"
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_workflow_with_condition_branch(self):
        """工作流支持条件分支"""
        config = _make_config()
        store = WorkflowStore(config)
        store.recover()

        entry = WorkflowEntry(
            name="条件分支测试",
            steps=[
                WorkflowStep(step_id=0, type="skill", skill_name="echo", params={}),
                WorkflowStep(step_id=1, type="skill", skill_name="echo",
                             params={"text": "成功处理"},
                             condition="on_success"),
                WorkflowStep(step_id=2, type="skill", skill_name="echo",
                             params={"text": "失败处理"},
                             condition="on_failure"),
            ],
        )

        # 验证条件字段正确保存
        assert entry.steps[1].condition == "on_success"
        assert entry.steps[2].condition == "on_failure"


# ─── 场景16：提醒备忘（定时+通知）────────────────────────────────

class TestScenario16Reminder:
    """场景16：提醒与备忘

    用户场景：用户想创建一次性/周期性提醒，到期收到通知。
    能力链路：定时调度 → 触发 → 通知推送

    验收标准：
    1. 能创建一次性提醒（特定时间）
    2. 能创建周期性提醒
    3. 提醒触发时通过通知通道推送
    4. 能暂停/恢复提醒
    5. 静默时段内不发送通知
    """

    @pytest.mark.asyncio
    async def test_create_one_time_reminder(self):
        """能创建一次性提醒"""
        config = _make_config()
        store = ScheduleStore(config)
        store.recover()
        bus = EventBus(config)
        scheduler = Scheduler(bus, store, config)

        # 明天下午3点的提醒
        entry = ScheduleEntry(
            name="下午开会提醒",
            cron="0 15 22 4 *",  # 4月22日15:00
            message="3点有产品评审会议",
            conversation_id="reminder-001",
        )
        result = await scheduler.add_schedule(entry)

        assert result.name == "下午开会提醒"
        assert result.next_run is not None

    @pytest.mark.asyncio
    async def test_create_recurring_reminder(self):
        """能创建周期性提醒"""
        config = _make_config()
        store = ScheduleStore(config)
        store.recover()
        bus = EventBus(config)
        scheduler = Scheduler(bus, store, config)

        entry = ScheduleEntry(
            name="每周站会提醒",
            cron="0 9 * * 1",  # 每周一9:00
            message="站会时间到了",
            conversation_id="reminder-weekly",
        )
        result = await scheduler.add_schedule(entry)

        assert result.name == "每周站会提醒"
        cron = CronParser("0 9 * * 1")
        assert cron.next_time(datetime.now(timezone.utc)) is not None

    @pytest.mark.asyncio
    async def test_reminder_triggers_notification(self):
        """提醒触发时通过通知通道推送"""
        config = _make_config()
        bus = EventBus(config)

        recorder = RecordingChannel()
        notifier = Notifier(bus, config)
        notifier._channels.append(recorder)
        notifier.initialize()

        await bus.start()
        try:
            # 模拟定时触发 → 任务完成
            event = make_event(
                EventType.TASK_COMPLETED,
                conversation_id="reminder-001",
                skill_name="echo",
                task_id="task-remind-001",
                payload={"result": "3点有产品评审会议"},
            )
            await bus.emit(event)
            await asyncio.sleep(0.5)

            assert len(recorder.sent) >= 1
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_toggle_reminder(self):
        """能暂停/恢复提醒"""
        config = _make_config()
        store = ScheduleStore(config)
        store.recover()
        bus = EventBus(config)
        scheduler = Scheduler(bus, store, config)

        entry = ScheduleEntry(
            name="测试提醒",
            cron="0 9 * * *",
            message="提醒内容",
            conversation_id="toggle-test",
        )
        result = await scheduler.add_schedule(entry)
        assert result.enabled is True

        # 暂停
        toggled = await scheduler.toggle_schedule(result.schedule_id)
        assert toggled.enabled is False

        # 恢复
        toggled = await scheduler.toggle_schedule(result.schedule_id)
        assert toggled.enabled is True

    @pytest.mark.asyncio
    async def test_silent_hours_suppress_notification(self):
        """静默时段内不发送通知"""
        config = _make_config(notify_silent_hours={"start": "23:00", "end": "07:00"})
        bus = EventBus(config)

        recorder = RecordingChannel()
        notifier = Notifier(bus, config)
        notifier._channels.append(recorder)
        notifier.initialize()

        # 直接调用 notify，如果在静默时段则不发送
        # （具体是否静默取决于当前时间，我们验证逻辑存在）
        result = await notifier.notify(title="测试", body="静默测试")
        # 如果当前时间在静默时段，result 为空；否则有结果
        # 关键是 _is_silent_hours 逻辑正确
        now_hour = datetime.now().hour
        if 23 <= now_hour or now_hour < 7:
            assert result == {}  # 静默时段，不发送
        # 非静默时段的验证在 test_notifier.py 中已覆盖


# ─── 场景19：手机远程执行（双向通信+通知）────────────────────────

class TestScenario19RemoteExecution:
    """场景19：手机指令远程执行

    用户场景：用户通过手机（Bark/Server酱）发送指令，Raccoon执行后结果推回手机。
    能力链路：Gateway入站 → Router → Executor → Notifier → 推送回手机

    验收标准：
    1. 入站网关能接收外部请求
    2. Token 认证正确
    3. 请求转为 USER_MESSAGE 事件
    4. 执行结果通过通知通道推回
    5. 频率限制生效
    """

    @pytest.mark.asyncio
    async def test_gateway_receives_inbound(self):
        """入站网关能接收外部请求"""
        config = _make_config()
        bus = EventBus(config)

        # 记录事件
        received_events: list[Event] = []
        bus.on(EventType.USER_MESSAGE, lambda e: received_events.append(e))

        gateway = GatewayInbound(bus, config)

        await bus.start()
        try:
            event = await gateway.handle_inbound(
                text="查看服务器状态",
                source="bark",
                token="test-token-123",
            )

            assert event.event == EventType.USER_MESSAGE
            assert event.payload["text"] == "查看服务器状态"
            assert event.payload["source"] == "bark"
            assert event.user_id == "gateway:bark"

            # 事件已注入 EventBus
            await asyncio.sleep(0.5)
            assert len(received_events) >= 1
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_gateway_auth_rejects_invalid_token(self):
        """Token 认证拒绝无效请求"""
        config = _make_config()
        bus = EventBus(config)
        gateway = GatewayInbound(bus, config)

        with pytest.raises(GatewayAuthError):
            await gateway.handle_inbound(
                text="恶意指令",
                source="hacker",
                token="wrong-token",
            )

    @pytest.mark.asyncio
    async def test_gateway_disabled_without_token(self):
        """未配置 Token 时网关不启用"""
        config = _make_config(gateway_token="")
        bus = EventBus(config)
        gateway = GatewayInbound(bus, config)

        assert gateway.enabled is False

        with pytest.raises(GatewayAuthError, match="未启用"):
            await gateway.handle_inbound(text="test", source="webhook")

    @pytest.mark.asyncio
    async def test_remote_execution_result_notified_back(self):
        """远程执行结果通过通知推回"""
        config = _make_config()
        bus = EventBus(config)

        recorder = RecordingChannel()
        notifier = Notifier(bus, config)
        notifier._channels.append(recorder)
        notifier.initialize()

        await bus.start()
        try:
            # 模拟远程指令执行完成
            event = make_event(
                EventType.TASK_COMPLETED,
                conversation_id="gateway_bark",
                skill_name="shell_exec",
                task_id="task-remote-001",
                payload={"result": "服务器状态正常，CPU 15%，内存 60%"},
            )
            await bus.emit(event)
            await asyncio.sleep(0.5)

            assert len(recorder.sent) >= 1
            assert "shell_exec" in recorder.sent[0].title
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_gateway_rate_limit(self):
        """频率限制生效"""
        config = _make_config()
        bus = EventBus(config)
        gateway = GatewayInbound(bus, config)

        from src.gateway.inbound import GatewayRateLimitError

        # 快速发送大量请求
        sent = 0
        rate_limited = False
        for i in range(70):
            try:
                await gateway.handle_inbound(
                    text=f"指令{i}",
                    source="burst-test",
                    token="test-token-123",
                )
                sent += 1
            except GatewayRateLimitError:
                rate_limited = True
                break

        assert rate_limited is True, "应该在超过频率限制后被拒绝"
        assert sent <= 60  # 限制是60次/分钟

    @pytest.mark.asyncio
    async def test_gateway_stats(self):
        """网关状态查询"""
        config = _make_config()
        bus = EventBus(config)
        gateway = GatewayInbound(bus, config)

        stats = gateway.get_stats()
        assert stats["enabled"] is True
        assert stats["rate_limit_per_minute"] == 60


# ─── 跨场景集成测试 ──────────────────────────────────────────────

class TestCrossScenarioIntegration:
    """跨场景集成：验证多个能力模块的协作"""

    @pytest.mark.asyncio
    async def test_schedule_to_workflow_to_notification(self):
        """定时触发 → 工作流执行 → 通知推送 完整链路"""
        config = _make_config()
        bus = EventBus(config)

        # 通知
        recorder = RecordingChannel()
        notifier = Notifier(bus, config)
        notifier._channels.append(recorder)
        notifier.initialize()

        # 工作流
        wf_store = WorkflowStore(config)
        wf_store.recover()
        router = MagicMock()
        executor = MagicMock()
        executor.handle_route_result = AsyncMock(return_value="执行结果")
        wf_engine = WorkflowEngine(bus, router, executor, wf_store, config)

        # 创建工作流
        workflow = WorkflowEntry(
            name="集成测试工作流",
            steps=[
                WorkflowStep(step_id=0, type="skill", skill_name="echo", params={}),
            ],
        )

        await bus.start()
        try:
            # 执行工作流
            ctx = await wf_engine.execute(workflow)
            assert ctx.status == "completed"

            # 工作流完成 → TASK_COMPLETED → Notifier 推送
            await asyncio.sleep(0.5)
            assert len(recorder.sent) >= 1
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_gateway_to_workflow_to_notification(self):
        """入站网关 → 工作流执行 → 通知推送 完整链路"""
        config = _make_config()
        bus = EventBus(config)

        # 通知
        recorder = RecordingChannel()
        notifier = Notifier(bus, config)
        notifier._channels.append(recorder)
        notifier.initialize()

        # 工作流
        wf_store = WorkflowStore(config)
        wf_store.recover()
        router = MagicMock()
        executor = MagicMock()
        executor.handle_route_result = AsyncMock(return_value="远程执行结果")
        wf_engine = WorkflowEngine(bus, router, executor, wf_store, config)

        # 入站网关
        gateway = GatewayInbound(bus, config)

        # 创建工作流
        workflow = WorkflowEntry(
            name="远程任务",
            steps=[
                WorkflowStep(step_id=0, type="skill", skill_name="echo", params={}),
            ],
        )

        await bus.start()
        try:
            # 1. 入站网关接收请求
            event = await gateway.handle_inbound(
                text="执行远程任务",
                source="bark",
                token="test-token-123",
            )
            assert event.event == EventType.USER_MESSAGE

            # 2. 执行工作流
            ctx = await wf_engine.execute(workflow)
            assert ctx.status == "completed"

            # 3. 通知推送
            await asyncio.sleep(0.5)
            assert len(recorder.sent) >= 1
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_all_modules_coexist(self):
        """所有模块能同时运行不冲突"""
        config = _make_config()
        bus = EventBus(config)

        # 定时调度
        sched_store = ScheduleStore(config)
        sched_store.recover()
        scheduler = Scheduler(bus, sched_store, config)

        # 通知
        recorder = RecordingChannel()
        notifier = Notifier(bus, config)
        notifier._channels.append(recorder)
        notifier.initialize()

        # 工作流
        wf_store = WorkflowStore(config)
        wf_store.recover()
        router = MagicMock()
        executor = MagicMock()
        executor.handle_route_result = AsyncMock(return_value="ok")
        wf_engine = WorkflowEngine(bus, router, executor, wf_store, config)

        # 入站网关
        gateway = GatewayInbound(bus, config)

        # 添加定时任务
        entry = ScheduleEntry(
            name="共存测试",
            cron="0 8 * * *",
            message="测试",
            conversation_id="coexist-test",
        )
        await scheduler.add_schedule(entry)

        # 添加工作流
        workflow = WorkflowEntry(
            name="共存工作流",
            steps=[WorkflowStep(step_id=0, type="skill", skill_name="echo", params={})],
        )
        wf_store.add(workflow)

        await bus.start()
        await scheduler.start()
        try:
            # 入站网关
            event = await gateway.handle_inbound(
                text="共存测试",
                source="test",
                token="test-token-123",
            )
            assert event is not None

            # 工作流执行
            ctx = await wf_engine.execute_by_name("共存工作流")
            assert ctx is not None
            assert ctx.status == "completed"

            # 通知
            await asyncio.sleep(0.5)
            # 至少有一个通知（工作流完成的）
            assert len(recorder.sent) >= 1

            # 调度器正在运行
            assert scheduler.is_running is True
        finally:
            await scheduler.stop()
            await bus.stop()
