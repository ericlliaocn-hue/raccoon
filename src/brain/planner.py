"""任务规划器

支持：
- 单步计划（MVP：直接映射路由结果）
- 多步计划（LLM 驱动的任务分解）
- 步骤间数据传递
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from src.config import RaccoonConfig
from src.types import RouteResult, RouteType, WorkflowStep

logger = structlog.get_logger(__name__)


class Plan:
    """执行计划"""

    def __init__(self, steps: list[dict]) -> None:
        self.steps = steps
        self.current_step = 0

    def is_single_step(self) -> bool:
        return len(self.steps) == 1

    def next_step(self) -> dict | None:
        if self.current_step < len(self.steps):
            step = self.steps[self.current_step]
            self.current_step += 1
            return step
        return None

    def to_workflow_steps(self) -> list[WorkflowStep]:
        """转换为 WorkflowStep 列表"""
        result = []
        for i, step in enumerate(self.steps):
            ws = WorkflowStep(
                step_id=i,
                type=step.get("type", "skill"),
                skill_name=step.get("skill_name"),
                params=step.get("params", {}),
                input_from=step.get("input_from"),
                condition=step.get("condition"),
            )
            result.append(ws)
        return result


class Planner:
    """任务规划器"""

    def __init__(self, config: RaccoonConfig | None = None) -> None:
        self._config = config or RaccoonConfig()
        self._llm = None

    def _get_llm(self):
        """延迟初始化 LLM"""
        if self._llm is None:
            from src.llm import LLMFactory
            self._llm = LLMFactory.create(self._config)
        return self._llm

    async def plan(self, route: RouteResult) -> Plan:
        """根据路由结果生成执行计划

        MVP: 单步计划，直接执行路由结果
        """
        if route.route_type == RouteType.SKILL:
            step = {
                "type": "skill",
                "skill_name": route.skill_name,
                "params": route.params,
            }
            return Plan(steps=[step])

        elif route.route_type == RouteType.SYSTEM:
            step = {
                "type": "system",
                "command": route.params.get("command", ""),
                "args": route.params.get("args", ""),
            }
            return Plan(steps=[step])

        elif route.route_type == RouteType.TASK_STATUS:
            step = {
                "type": "status_query",
                "task_id": route.params.get("task_id"),
            }
            return Plan(steps=[step])

        elif route.route_type == RouteType.TASK_OPERATION:
            step = {
                "type": "task_operation",
                "operation": route.params.get("operation", ""),
                "task_id": route.params.get("task_id"),
            }
            return Plan(steps=[step])

        else:
            # LLM 兜底
            step = {
                "type": "llm",
                "text": route.params.get("original_text", ""),
            }
            return Plan(steps=[step])

    async def decompose(self, text: str, max_steps: int = 5) -> Plan:
        """LLM 驱动的多步任务分解

        将复杂需求分解为多个步骤，返回多步 Plan。
        """
        if self._config.llm_provider == "mock":
            # Mock 模式：返回单步
            return Plan(steps=[{"type": "llm", "text": text}])

        try:
            llm = self._get_llm()
            prompt = _DECOMPOSE_PROMPT.format(text=text, max_steps=max_steps)
            messages = [{"role": "user", "content": prompt}]
            reply = await llm.chat(
                messages,
                temperature=0.3,
                max_tokens=2048,
            )
            return self._parse_decomposition(reply, text)
        except Exception as e:
            logger.error("planner_decompose_failed", error=str(e))
            # 降级为单步 LLM
            return Plan(steps=[{"type": "llm", "text": text}])

    def _parse_decomposition(self, reply: str, original_text: str) -> Plan:
        """解析 LLM 分解结果"""
        steps = []

        # 尝试解析 JSON
        try:
            # 提取 JSON 块
            json_str = reply
            if "```json" in reply:
                json_str = reply.split("```json")[1].split("```")[0]
            elif "```" in reply:
                json_str = reply.split("```")[1].split("```")[0]

            data = json.loads(json_str.strip())
            if isinstance(data, list):
                for i, item in enumerate(data):
                    step = {
                        "step_id": i,
                        "type": item.get("type", "skill"),
                        "skill_name": item.get("skill_name"),
                        "params": item.get("params", {}),
                        "input_from": item.get("input_from"),
                        "condition": item.get("condition"),
                    }
                    steps.append(step)
            elif isinstance(data, dict) and "steps" in data:
                for i, item in enumerate(data["steps"]):
                    step = {
                        "step_id": i,
                        "type": item.get("type", "skill"),
                        "skill_name": item.get("skill_name"),
                        "params": item.get("params", {}),
                        "input_from": item.get("input_from"),
                        "condition": item.get("condition"),
                    }
                    steps.append(step)
        except (json.JSONDecodeError, IndexError):
            logger.warning("planner_parse_failed", reply=reply[:200])
            # 降级为单步
            steps = [{"type": "llm", "text": original_text}]

        if not steps:
            steps = [{"type": "llm", "text": original_text}]

        return Plan(steps=steps)


_DECOMPOSE_PROMPT = """你是一个任务分解专家。请将以下用户需求分解为多个可执行的步骤。

用户需求: {text}

请将需求分解为最多 {max_steps} 个步骤，每步包含：
- type: 步骤类型 ("skill" | "llm" | "system")
- skill_name: 如果 type=skill，填写 skill 名称
- params: 步骤参数 (dict)
- input_from: 从第几步的输出取输入 (null 表示不依赖前序步骤)
- condition: 条件 ("on_success" | "on_failure" | null)

请以 JSON 数组格式返回，例如:
```json
[
  {{"type": "skill", "skill_name": "web_search", "params": {{"query": "关键词"}}, "input_from": null, "condition": null}},
  {{"type": "llm", "params": {{"text": "总结搜索结果"}}, "input_from": 0, "condition": "on_success"}}
]
```

只返回 JSON，不要其他内容。"""
