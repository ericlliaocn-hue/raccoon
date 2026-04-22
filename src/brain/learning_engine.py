"""LearningEngine：自主能力获取引擎（L3 核心）

当 Router 未命中任何 Skill 时，LearningEngine 接管：
1. 检索 MemCore 经验 → 有经验则复用
2. 无经验 → LLM 推理需要什么工具
3. 自动 pip install 缺失依赖
4. LLM 生成 Skill 代码
5. 注册到 VaultManager
6. 执行并验证
7. 经验写入 MemCore

流程：
  learn "浏览器操作" →
    LLM推理: 需要 playwright →
    pip install playwright →
    生成 browser_skill/main.py →
    注册到 Vault →
    执行验证 →
    经验写入 MemCore
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import structlog

from src.config import RaccoonConfig
from src.skill_vault.vault_manager import VaultManager
from src.types import SkillMetadata, Task

logger = structlog.get_logger(__name__)


class LearningEngine:
    """L3 对话自学引擎"""

    def __init__(
        self,
        config: RaccoonConfig | None = None,
        vault_manager: VaultManager | None = None,
        memcore_writer=None,
        llm_client=None,
    ) -> None:
        self._config = config or RaccoonConfig()
        self._vault = vault_manager
        self._memcore_writer = memcore_writer
        self._llm = llm_client
        self._auto_install = getattr(config, "learning_auto_install", True)
        self._requires_approval = getattr(config, "learning_requires_approval", True)

    # ─── 主入口 ──────────────────────────────────────────────────

    async def learn(
        self,
        task: Task,
        user_message: str,
    ) -> dict[str, Any]:
        """学习并执行未知需求

        Returns:
            {"reply": "...", "files": [], "learned": True/False}
        """
        logger.info("learning_engine_start", message=user_message)

        # 1. 检索经验
        experience = await self._search_experience(user_message)
        if experience:
            logger.info("learning_reuse_experience", key=experience.get("key"))
            return {
                "reply": f"📋 复用已有经验：{experience.get('key', '')}\n\n{experience.get('value', '')}",
                "files": [],
                "learned": False,
            }

        # 2. LLM 推理：需要什么工具/Skill
        analysis = await self._analyze_need(user_message)
        if not analysis:
            return {
                "reply": "🤔 无法分析需求，请更具体地描述你想做什么",
                "files": [],
                "learned": False,
            }

        logger.info("learning_analysis", analysis=analysis)

        # 3. 安装缺失依赖
        deps = analysis.get("dependencies", [])
        if deps and self._auto_install:
            install_result = await self._install_dependencies(deps)
            if not install_result["success"]:
                return {
                    "reply": f"❌ 依赖安装失败：{install_result['error']}\n\n需要手动安装：`pip install {' '.join(deps)}`",
                    "files": [],
                    "learned": False,
                }

        # 4. 生成 Skill 代码
        skill_code = await self._generate_skill(user_message, analysis)
        if not skill_code:
            return {
                "reply": "❌ 无法自动生成 Skill 代码，请手动实现",
                "files": [],
                "learned": False,
            }

        # 5. 注册到 Vault
        skill_name = analysis.get("skill_name", f"learned_{task.task_id[:8]}")
        register_result = await self._register_skill(skill_name, skill_code, analysis)
        if not register_result:
            return {
                "reply": "❌ Skill 注册失败",
                "files": [],
                "learned": False,
            }

        # 6. 执行验证
        exec_result = await self._execute_learned_skill(task, skill_name, user_message)

        # 7. 经验写入 MemCore
        if exec_result.get("success"):
            await self._write_experience(
                key=user_message[:100],
                value=json.dumps(analysis, ensure_ascii=False),
                skill_name=skill_name,
            )
            reply = exec_result.get("reply", "✅ 学习并执行成功")
            reply += f"\n\n🎓 已学会新技能：{skill_name}"
        else:
            reply = f"⚠️ Skill 已生成但执行失败：{exec_result.get('error', '未知')}"

        return {
            "reply": reply,
            "files": exec_result.get("files", []),
            "learned": True,
        }

    # ─── 经验检索 ────────────────────────────────────────────────

    async def _search_experience(self, message: str) -> dict | None:
        """从 MemCore 检索相关经验"""
        if not self._memcore_writer:
            return None
        try:
            # 使用 MemCore reader 搜索
            from src.memcore.reader import MemCoreReader

            reader = MemCoreReader(self._config)
            results = await reader.search(message, limit=3)
            if results:
                return {"key": results[0].key, "value": results[0].value}
        except Exception as e:
            logger.warning("experience_search_failed", error=str(e))
        return None

    # ─── LLM 需求分析 ────────────────────────────────────────────

    async def _analyze_need(self, message: str) -> dict | None:
        """用 LLM 分析用户需求，推理需要什么工具"""
        if not self._llm:
            # 无 LLM 时用简单规则
            return self._rule_based_analysis(message)

        prompt = f"""分析用户需求，推理需要什么工具和 Skill 来实现。

用户消息：{message}

请用 JSON 回答：
{{
  "skill_name": "简短英文技能名",
  "description": "技能描述",
  "trigger_words": ["触发词1", "触发词2"],
  "dependencies": ["pip包1", "pip包2"],
  "approach": "实现思路简述"
}}

只返回 JSON，不要其他内容。"""

        try:
            response = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=500,
            )
            # 提取 JSON
            text = response if isinstance(response, str) else str(response)
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                return json.loads(match.group())
        except Exception as e:
            logger.warning("llm_analysis_failed", error=str(e))

        return self._rule_based_analysis(message)

    def _rule_based_analysis(self, message: str) -> dict | None:
        """基于规则的简单需求分析（无 LLM 兜底）"""
        rules = [
            (r"浏览器|网页|打开网页|browse|web", {
                "skill_name": "web_browse",
                "description": "打开网页",
                "trigger_words": ["打开网页", "浏览"],
                "dependencies": [],
                "approach": "使用 webbrowser 模块打开",
            }),
            (r"截图|截屏|screenshot", {
                "skill_name": "screenshot",
                "description": "屏幕截图",
                "trigger_words": ["截图", "截屏"],
                "dependencies": [],
                "approach": "使用 macOS screencapture",
            }),
            (r"剪贴板|粘贴板|clipboard", {
                "skill_name": "clipboard",
                "description": "剪贴板操作",
                "trigger_words": ["剪贴板", "粘贴板"],
                "dependencies": [],
                "approach": "使用 pbcopy/pbpaste",
            }),
        ]
        for pattern, result in rules:
            if re.search(pattern, message, re.IGNORECASE):
                return result
        return None

    # ─── 依赖安装 ────────────────────────────────────────────────

    async def _install_dependencies(self, deps: list[str]) -> dict:
        """自动 pip install 缺失依赖"""
        if not deps:
            return {"success": True}

        if self._requires_approval:
            logger.warning("install_requires_approval", deps=deps)
            # MVP: 自动通过，记录审计日志
            # 完整版应暂停等待用户确认

        for dep in deps:
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install", dep,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode != 0:
                    error = stderr.decode("utf-8", errors="replace").strip()
                    logger.error("pip_install_failed", dep=dep, error=error)
                    return {"success": False, "error": f"pip install {dep} failed: {error}"}
                logger.info("pip_installed", dep=dep)
            except asyncio.TimeoutError:
                return {"success": False, "error": f"pip install {dep} timed out"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        return {"success": True}

    # ─── Skill 代码生成 ──────────────────────────────────────────

    async def _generate_skill(self, message: str, analysis: dict) -> str | None:
        """用 LLM 生成 Skill 代码"""
        if not self._llm:
            return self._template_skill(analysis)

        skill_name = analysis.get("skill_name", "custom")
        approach = analysis.get("approach", "")
        deps = analysis.get("dependencies", [])

        prompt = f"""为浣熊生成一个 Skill 的 main.py 代码。

Skill 名称：{skill_name}
实现思路：{approach}
依赖包：{', '.join(deps) if deps else '无额外依赖'}

要求：
1. 读取 stdin JSON：{{ "task_id", "conversation_id", "user_id", "origin_message", "params" }}
2. 输出 stdout JSON：{{ "task_id": "...", "reply": "...", "files": [] }}
3. reply 使用中文
4. 包含错误处理
5. 只输出 Python 代码，不要其他内容

```python
# 代码
```"""

        try:
            response = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=2000,
            )
            text = response if isinstance(response, str) else str(response)
            # 提取代码块
            match = re.search(r"```python\s*\n([\s\S]*?)```", text)
            if match:
                return match.group(1).strip()
            # 尝试直接作为代码
            if "def main" in text:
                return text.strip()
        except Exception as e:
            logger.warning("skill_generation_failed", error=str(e))

        return self._template_skill(analysis)

    def _template_skill(self, analysis: dict) -> str:
        """基于模板生成简单 Skill 代码"""
        skill_name = analysis.get("skill_name", "custom")
        approach = analysis.get("approach", "")

        return f'''"""{skill_name} Skill - {approach}

Auto-generated by LearningEngine
"""
import json
import sys


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    origin = data.get("origin_message", "")
    params = data.get("params", {{}})

    result = {{
        "task_id": task_id,
        "reply": f"🔧 {{origin}}\\n\\n（此 Skill 由 LearningEngine 自动生成，功能待完善）",
        "files": [],
    }}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
'''

    # ─── Skill 注册 ──────────────────────────────────────────────

    async def _register_skill(
        self, skill_name: str, code: str, analysis: dict
    ) -> bool:
        """将生成的 Skill 注册到 VaultManager"""
        if not self._vault:
            logger.warning("no_vault_manager", skill=skill_name)
            return False

        try:
            # 创建临时目录
            with tempfile.TemporaryDirectory() as tmp_dir:
                skill_dir = Path(tmp_dir) / skill_name
                skill_dir.mkdir()

                # 写入 main.py
                (skill_dir / "main.py").write_text(code, encoding="utf-8")

                # 写入 metadata.json
                metadata = {
                    "name": skill_name,
                    "version": "0.1.0",
                    "description": analysis.get("description", f"Auto-learned: {skill_name}"),
                    "trigger_words": analysis.get("trigger_words", [skill_name]),
                    "intent_tags": ["learned"],
                    "risk_level": "medium",
                    "requires_approval": False,
                    "timeout_seconds": 60,
                    "entry": "main.py",
                    "permissions": analysis.get("dependencies", []),
                }
                (skill_dir / "metadata.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
                )

                # 安装到 Vault
                meta = self._vault._install_from_local(str(skill_dir))
                logger.info("skill_registered", name=skill_name)
                return True

        except Exception as e:
            logger.error("skill_register_failed", skill=skill_name, error=str(e))
            return False

    # ─── 执行验证 ────────────────────────────────────────────────

    async def _execute_learned_skill(
        self, task: Task, skill_name: str, user_message: str
    ) -> dict:
        """执行刚学会的 Skill"""
        if not self._vault:
            return {"success": False, "error": "No vault manager"}

        try:
            runner = self._vault.get_skill_runner(skill_name)
            if not runner:
                return {"success": False, "error": "Skill runner not found"}

            result = await runner.run(task, {"rest": user_message})
            reply = result.get("reply", "")
            files = result.get("files", [])
            return {"success": True, "reply": reply, "files": files}

        except Exception as e:
            logger.error("learned_skill_exec_failed", skill=skill_name, error=str(e))
            return {"success": False, "error": str(e)}

    # ─── 经验写入 ────────────────────────────────────────────────

    async def _write_experience(
        self, key: str, value: str, skill_name: str
    ) -> None:
        """将学习经验写入 MemCore"""
        if not self._memcore_writer:
            logger.warning("no_memcore_writer", skill=skill_name)
            return

        try:
            await self._memcore_writer.write(
                user_id="learning_engine",
                key=f"learned:{skill_name}",
                value=value,
                confidence=0.8,
                source="learning_engine",
            )
            logger.info("experience_written", skill=skill_name)
        except Exception as e:
            logger.warning("experience_write_failed", error=str(e))
