"""事件类型定义与工厂函数

事件是 EventBus 中流转的核心数据单元。每个事件携带：
- event: 事件类型
- event_id: 唯一标识
- conversation_id: 对话上下文绑定
- user_id: 用户标识
- task_id / skill_name / status: 任务相关字段
- payload: 自定义数据
- timestamp: 事件时间
"""

from src.types import Event, EventType, TaskStatus, make_event

__all__ = ["Event", "EventType", "TaskStatus", "make_event"]
