"""Skill 沙箱隔离执行（subprocess + 资源限制）

MVP 实现：
- 使用 subprocess 运行 Skill
- 设置超时
- 预留 resource 限制（Linux: rlimit / macOS: 软限制）
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import structlog
from src.config import RaccoonConfig
from src.types import Task

logger = structlog.get_logger(__name__)


class SkillRunner:
    """Skill 运行器：沙箱隔离执行"""

    def __init__(self, skill_dir: Path, entry: str, config: RaccoonConfig | None = None) -> None:
        self._skill_dir = skill_dir
        self._entry = entry
        self._config = config or RaccoonConfig()

    async def run(self, task: Task, params: dict[str, Any]) -> dict[str, Any]:
        """执行 Skill

        通过 subprocess 调用 Skill 入口文件，传入 task 和 params 作为 JSON 参数。
        Skill 入口文件应读取 stdin 的 JSON，将结果写入 stdout 的 JSON。

        params 中可包含：
        - action: Skill 内部动作标识
        - state: FlowEngine 传递的会话状态（多轮交互时）
        - step_id / step_name: 当前流程步骤信息
        """
        input_data = {
            "task_id": task.task_id,
            "conversation_id": task.conversation_id,
            "user_id": task.user_id,
            "origin_message": task.origin_message,
            "params": params,
        }

        # 如果 params 中有 state，提升到顶层方便 Skill 读取
        if "state" in params:
            input_data["state"] = params["state"]

        entry_path = self._skill_dir / self._entry
        if not entry_path.exists():
            raise FileNotFoundError(f"Skill entry not found: {entry_path}")

        # 构建执行命令
        cmd = [sys.executable, str(entry_path)]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._skill_dir),
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=json.dumps(input_data).encode()),
                timeout=self._config.task_timeout_seconds,
            )

            if process.returncode != 0:
                error_msg = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Skill exited with code {process.returncode}: {error_msg}")

            # 解析输出
            output = stdout.decode("utf-8", errors="replace").strip()
            if output:
                try:
                    return json.loads(output)
                except json.JSONDecodeError:
                    return {"output": output}
            return {"status": "completed"}

        except asyncio.TimeoutError:
            raise
        except Exception as e:
            logger.error("skill_run_error", skill_dir=str(self._skill_dir), error=str(e))
            raise
