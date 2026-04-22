"""Schema 驱动 Skill 步骤执行

完整版：
- 读取 Skill 的 schema_def
- 按 Schema 定义的步骤逐步执行
- 每步结果校验、中间结果存储到 MemCore

MVP：直接调用 Skill 入口，不做步骤拆分。
"""

from __future__ import annotations

import structlog
from src.types import Task

logger = structlog.get_logger(__name__)


class SchemaEngine:
    """Schema 驱动执行引擎"""

    async def execute(self, task: Task, skill_runner, params: dict) -> dict:
        """执行 Skill（MVP: 直接调用，不做步骤拆分）

        未来版本：
        1. 读取 skill.schema_def
        2. 按 steps 定义逐步执行
        3. 每步结果校验 + 存 MemCore
        4. LLM 填充 Schema 参数
        """
        result = await skill_runner.run(task, params)
        logger.info(
            "schema_engine_executed",
            task_id=task.task_id,
            skill=task.skill_name,
        )
        return result
