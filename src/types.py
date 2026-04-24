"""共享类型定义：Event / Task / RouteResult / Skill / TaskStatus 等"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ─── Task Status ───────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING = "pending"
    PENDING_APPROVAL = "pending_approval"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


# 合法状态转换表
VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {
        TaskStatus.PENDING_APPROVAL,
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.PENDING_APPROVAL: {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLING},
    TaskStatus.CANCELLING: {TaskStatus.CANCELLED},
    TaskStatus.SUCCESS: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


# ─── Event Types ───────────────────────────────────────────────

class EventType(str, Enum):
    USER_MESSAGE = "user_message"
    TASK_SUBMITTED = "task_submitted"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_PROGRESS = "task_progress"
    SCHEDULE_TRIGGERED = "schedule_triggered"
    SYSTEM = "system"
    SESSION_STARTED = "session_started"     # Skill 会话开始
    SESSION_STEP = "session_step"           # 会话步骤结果推送
    SESSION_ENDED = "session_ended"         # Skill 会话结束
    APPROVAL_RESOLVED = "approval_resolved" # 审批完成（批准/拒绝/超时）
    SKILL_INSTALLING = "skill_installing"   # Skill 安装中
    SKILL_INSTALLED = "skill_installed"     # Skill 安装完成
    SKILL_INSTALL_FAILED = "skill_install_failed"  # Skill 安装失败


# ─── Event ─────────────────────────────────────────────────────

class Event(BaseModel):
    """事件总线中流转的核心事件"""
    event: EventType
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str
    user_id: str = "anonymous"
    task_id: str | None = None
    skill_name: str | None = None
    status: TaskStatus | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def make_event(
    event_type: EventType,
    conversation_id: str,
    *,
    user_id: str = "anonymous",
    task_id: str | None = None,
    skill_name: str | None = None,
    status: TaskStatus | None = None,
    payload: dict[str, Any] | None = None,
) -> Event:
    """事件工厂函数"""
    return Event(
        event=event_type,
        conversation_id=conversation_id,
        user_id=user_id,
        task_id=task_id,
        skill_name=skill_name,
        status=status,
        payload=payload or {},
    )


# ─── Task ──────────────────────────────────────────────────────

class Task(BaseModel):
    """执行器中的任务实体"""
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str
    user_id: str = "anonymous"
    origin_message: str
    skill_name: str
    status: TaskStatus = TaskStatus.PENDING
    cancel_token: bool = False
    result: dict[str, Any] | None = None
    error: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─── RouteResult ───────────────────────────────────────────────

class RouteType(str, Enum):
    SKILL = "skill"           # 命中 Skill，交给 Executor
    SKILL_SESSION = "skill_session"  # 活跃的 Skill 会话（多轮交互）
    TASK_STATUS = "task_status"  # 查询任务状态
    TASK_OPERATION = "task_operation"  # 任务操作（取消等）
    SYSTEM = "system"         # 系统指令
    LLM = "llm"               # LLM 兜底
    LEARN = "learn"           # 学习请求（用户确认创建新 Skill）
    NONE = "none"             # 无匹配


class RouteResult(BaseModel):
    """路由结果"""
    route_type: RouteType
    skill_name: str | None = None
    task_id: str | None = None
    intent: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0


# ─── Skill Metadata ────────────────────────────────────────────

class SkillMetadata(BaseModel):
    """Skill 元数据（metadata.json 的 Schema）"""
    name: str
    version: str = "0.1.0"
    description: str = ""
    trigger_words: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)  # @别名 调用，如 ["图片生成", "AI绘画"]
    intent_tags: list[str] = Field(default_factory=list)
    risk_level: str = "low"  # low / medium / high
    requires_approval: bool = False
    timeout_seconds: int = 300
    entry: str = "main.py"  # Skill 入口文件
    schema_def: dict[str, Any] | None = None  # Schema 定义（预留）
    permissions: list[str] = Field(default_factory=list)  # network / filesystem / subprocess
    interactive: bool = False  # 是否需要多轮交互
    flow: FlowDefinition | None = None  # 流程定义（interactive=True 时必填）
    enabled: bool = True  # 是否启用（默认启用，用户可手动关闭）
    starred: bool = False  # 是否收藏
    installed_from: str | None = None  # 安装来源（Git URL / "builtin"）
    installed_at: str | None = None    # 安装时间（ISO 8601）


# ─── Memory ────────────────────────────────────────────────────

class ScheduleStatus(str, Enum):
    """定时任务状态"""
    ENABLED = "enabled"       # 正常运行
    PAUSED = "paused"         # 暂停（不触发，但保留配置）
    DISABLED = "disabled"     # 禁用

class RetryPolicy(BaseModel):
    """重试策略"""
    max_retries: int = 3          # 最大重试次数
    retry_interval_seconds: int = 60  # 重试间隔（秒）
    retry_on_failure: bool = True     # 失败时是否重试

class ScheduleEntry(BaseModel):
    """定时任务条目"""
    schedule_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str                              # 人类可读名称
    cron: str                              # 5位 cron 表达式 "分 时 日 月 周"
    message: str                           # 触发时发送的消息文本
    conversation_id: str                   # 绑定的对话 ID
    user_id: str = "scheduler"
    status: ScheduleStatus = ScheduleStatus.ENABLED  # 替代 enabled bool
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    retry_count: int = 0                   # 当前重试次数
    last_run: datetime | None = None
    next_run: datetime | None = None
    last_run_result: str | None = None     # 上次执行结果：success / failed / retrying
    last_run_error: str | None = None      # 上次执行错误信息
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def enabled(self) -> bool:
        """兼容旧代码：status == ENABLED 时视为 enabled"""
        return self.status == ScheduleStatus.ENABLED

    def set_enabled(self, val: bool) -> None:
        """兼容旧代码：设置 enabled/disabled"""
        self.status = ScheduleStatus.ENABLED if val else ScheduleStatus.DISABLED


class WorkflowStep(BaseModel):
    """工作流步骤"""
    step_id: int
    type: str                    # "skill" | "system" | "llm" | "if" | "loop" | "parallel"
    skill_name: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    input_from: int | None = None  # 从第几步的输出取输入（None=不依赖前序步骤）
    condition: str | None = None    # "on_success" | "on_failure" | None
    # ─── v0.3.4 条件分支增强 ───
    if_condition: str | None = None   # 表达式，如 "step_0.success == true"
    then_steps: list[WorkflowStep] | None = None   # if 分支子步骤（递归）
    else_steps: list[WorkflowStep] | None = None   # else 分支子步骤
    loop_var: str | None = None       # 循环变量名，如 "item"
    loop_over: str | None = None      # 循环来源，如 "step_0.result.items"
    loop_steps: list[WorkflowStep] | None = None   # 循环体子步骤
    parallel_steps: list[WorkflowStep] | None = None  # 并行子步骤
    max_iterations: int = 100         # 循环最大迭代次数（防死循环）


class WorkflowEntry(BaseModel):
    """工作流模板条目"""
    workflow_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str                              # 人类可读名称
    description: str = ""
    steps: list[WorkflowStep] = Field(default_factory=list)
    conversation_id: str = ""
    user_id: str = "workflow"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryEntry(BaseModel):
    """MemCore 中的记忆条目"""
    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    key: str
    value: Any
    confidence: float = 1.0
    source: str = "user"  # user / skill / system
    is_archived: bool = False
    # ─── v0.3.4 访问追踪 ───
    access_count: int = 0
    last_accessed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Flow & Session ────────────────────────────────────────────

class FlowStepType(str, Enum):
    """流程步骤类型"""
    LLM = "llm"                # LLM 参与步骤（如润色提示词）
    USER_CHOICE = "user_choice" # 等待用户选择
    SKILL_EXEC = "skill_exec"  # 执行 Skill 代码
    USER_INPUT = "user_input"  # 等待用户自由输入


class FlowStep(BaseModel):
    """流程编排中的一个步骤"""
    step_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str                           # 步骤名称，如 "选择风格"
    type: FlowStepType                  # 步骤类型
    prompt: str = ""                    # 给用户/LLM 的提示
    options: list[str] = Field(default_factory=list)  # USER_CHOICE 的选项
    skill_action: str = ""              # SKILL_EXEC 的 action 名称
    llm_system_prompt: str = ""         # LLM 步骤的系统提示词
    background: bool = False            # 是否后台执行（释放会话）
    next_step: str | None = None        # 显式指定下一步（None 则顺序）


class FlowDefinition(BaseModel):
    """Skill 的流程定义（在 metadata.json 中声明）"""
    steps: list[FlowStep] = Field(default_factory=list)
    on_complete: str = ""               # 完成后的消息模板
    on_cancel: str = "流程已取消"        # 取消时的消息


class SessionStatus(str, Enum):
    """会话状态"""
    ACTIVE = "active"           # 活跃，等待用户输入
    PROCESSING = "processing"   # 正在处理（LLM/Skill 执行中）
    BACKGROUND = "background"   # 后台执行中（会话已释放）
    COMPLETED = "completed"     # 已完成
    CANCELLED = "cancelled"     # 已取消
    TIMEOUT = "timeout"         # 超时


class SkillSession(BaseModel):
    """Skill 多轮交互会话"""
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str
    skill_name: str
    flow: FlowDefinition = Field(default_factory=FlowDefinition)
    current_step_index: int = 0
    status: SessionStatus = SessionStatus.ACTIVE
    state: dict[str, Any] = Field(default_factory=dict)  # 会话状态数据
    task_id: str | None = None           # 关联的后台任务 ID
    login_step: str = ""                 # 登录阶段："" / "input_phone" / "input_code"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    timeout_seconds: int = 1800          # 默认 30 分钟超时


class MessageSource(str, Enum):
    """消息来源标识"""
    LLM = "llm"                  # LLM 自由回复
    SKILL = "skill"              # Skill 步骤输出
    FLOW_LLM = "flow_llm"       # 流程中 LLM 步骤输出
    BACKGROUND = "background"    # 后台任务完成推送
    SYSTEM = "system"            # 系统消息
