"""Skill 沙箱隔离执行（subprocess + 资源限制）

MVP 实现：
- 使用 subprocess 运行 Skill
- 设置超时
- 预留 resource 限制（Linux: rlimit / macOS: 软限制）
"""

from __future__ import annotations

import asyncio
import importlib.util
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

        if self._skill_dir.name == "web_automate" and self._entry == "main.py":
            return await self._run_web_automate_in_process(input_data)

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

    async def _run_web_automate_in_process(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """内置 web_automate 走父进程会话池，避免 subprocess 退出后丢浏览器会话。"""
        entry_path = self._skill_dir / self._entry
        if not entry_path.exists():
            raise FileNotFoundError(f"Skill entry not found: {entry_path}")

        inserted = False
        skill_dir_str = str(self._skill_dir)
        if skill_dir_str not in sys.path:
            sys.path.insert(0, skill_dir_str)
            inserted = True

        try:
            spec = importlib.util.spec_from_file_location(
                "_raccoon_web_automate_main",
                entry_path,
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot import web_automate entry: {entry_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            run_browser_skill = getattr(module, "run_browser_skill")
            return await run_browser_skill(input_data)
        except Exception as e:
            logger.error("web_automate_in_process_error", skill_dir=str(self._skill_dir), error=str(e))
            raise
        finally:
            if inserted:
                try:
                    sys.path.remove(skill_dir_str)
                except ValueError:
                    pass
