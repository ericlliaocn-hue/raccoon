"""LearningEngine：自主能力获取引擎（L3 核心）

当 Router 未命中任何 Skill 时，LearningEngine 接管：
1. 检索 MemCore 经验 → 有经验则复用
2. 无经验 → LLM 推理需要什么工具
3. 自动 pip install 缺失依赖
4. LLM 生成 Skill 代码
5. 注册到 VaultManager（并同步到 Router）
6. 执行并验证
7. 经验写入 MemCore
8. 识别定时需求 → 自动创建 Schedule

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
from typing import TYPE_CHECKING, Any

import structlog

from src.config import RaccoonConfig
from src.skill_vault.vault_manager import VaultManager
from src.types import ScheduleEntry, SkillMetadata, Task

if TYPE_CHECKING:
    from src.scheduler.scheduler import Scheduler
    from src.router.router import Router

logger = structlog.get_logger(__name__)

# import 名 → pip 包名映射（用于 ModuleNotFoundError 自动安装）
_IMPORT_TO_PIP: dict[str, str] = {
    "requests": "requests",
    "httpx": "httpx",
    "feedparser": "feedparser",
    "bs4": "beautifulsoup4",
    "beautifulsoup4": "beautifulsoup4",
    "playwright": "playwright",
    "openai": "openai",
    "PIL": "Pillow",
    "pillow": "Pillow",
    "yaml": "PyYAML",
    "lxml": "lxml",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "flask": "flask",
    "fastapi": "fastapi",
    "aiohttp": "aiohttp",
    "selenium": "selenium",
}

# skill-standard.md 核心规范摘要（注入到 LLM prompt 中）
_SKILL_STANDARD_CORE = """\
Raccoon Skill 通信协议：
- 输入(stdin JSON): { "task_id", "conversation_id", "user_id", "origin_message", "params" }
- 输出(stdout JSON): { "task_id": "...", "reply": "...", "files": [] }
- 参数读取优先级: params.xxx > state 中查找 > origin_message 提取
- ⚠️ 不要只依赖 origin_message 提取参数，Flow 模式下它可能是 "flow_step:xxx"
- files 元素格式: { "type": "image", "path": "/path/to/file", "name": "显示名" }
- 如需多轮交互: 返回 need_login=True + login_step + pending_prompt + pending_params
- metadata.json 必需字段: name, version, description, trigger_words, intent_tags, risk_level, requires_approval, timeout_seconds, entry, permissions
"""


class LearningEngine:
    """L3 对话自学引擎"""

    def __init__(
        self,
        config: RaccoonConfig | None = None,
        vault_manager: VaultManager | None = None,
        memcore_writer=None,
        llm_client=None,
        scheduler: Scheduler | None = None,
        router: Router | None = None,
    ) -> None:
        self._config = config or RaccoonConfig()
        self._vault = vault_manager
        self._memcore_writer = memcore_writer
        self._llm = llm_client
        self._scheduler = scheduler
        self._router = router
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

        # 6. 执行验证 + 自动修复循环
        exec_result = await self._execute_learned_skill(task, skill_name, user_message)

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            if exec_result.get("success"):
                break

            error_msg = exec_result.get("error", "未知错误")
            logger.warning(
                "skill_exec_failed_retry",
                skill=skill_name,
                attempt=attempt,
                error=error_msg,
            )

            # 尝试自动修复
            fixed_code = await self._repair_skill(
                skill_name, error_msg, user_message, analysis
            )
            if not fixed_code:
                logger.warning("skill_repair_failed", skill=skill_name, attempt=attempt)
                break

            # 更新 Skill 代码
            update_ok = await self._update_skill_code(skill_name, fixed_code)
            if not update_ok:
                break

            # 重新执行
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
            error_msg = exec_result.get("error", "未知")
            reply = f"⚠️ Skill 已生成但执行失败：{error_msg}"
            reply += "\n\n💡 你可以告诉我具体错误，我会尝试修复"

        return {
            "reply": reply,
            "files": exec_result.get("files", []),
            "learned": True,
        }

    async def learn_and_schedule(
        self,
        task: Task,
        user_message: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        """学习 + 尝试创建定时任务（由 Executor._handle_learn_confirm 调用）

        Returns:
            {"reply": "...", "files": [], "learned": True/False, "schedule_created": str|None}
        """
        # 先走 learn 主流程
        result = await self.learn(task, user_message)

        # 如果学习成功，尝试识别定时需求并创建 Schedule
        schedule_info = None
        if result.get("learned") and self._scheduler:
            schedule_info = await self._try_create_schedule(
                user_message, result.get("skill_name", ""), conversation_id,
            )
            if schedule_info:
                result["schedule_created"] = schedule_info

        return result

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

        # 注入已有 Skill 清单，让 LLM 知道可以组合哪些现有 Skill
        skill_catalog = self._build_skill_catalog()

        prompt = f"""分析用户需求，推理需要什么工具和 Skill 来实现。

{_SKILL_STANDARD_CORE}

已有 Skill 清单：
{skill_catalog}

用户消息：{message}

请用 JSON 回答：
{{
  "skill_name": "简短英文技能名",
  "description": "技能描述",
  "trigger_words": ["触发词1", "触发词2"],
  "dependencies": ["pip包1", "pip包2"],
  "approach": "实现思路简述"
}}

注意：
- 如果已有 Skill 能组合实现，在 approach 中说明如何组合
- skill_name 用小写+下划线，如 web_monitor
- 只返回 JSON，不要其他内容。"""

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

        # 注入已有 Skill 清单
        skill_catalog = self._build_skill_catalog()

        prompt = f"""为浣熊生成一个 Skill 的 main.py 代码。

{_SKILL_STANDARD_CORE}

已有 Skill 清单（可参考或组合）：
{skill_catalog}

Skill 名称：{skill_name}
实现思路：{approach}
依赖包：{', '.join(deps) if deps else '无额外依赖'}

要求：
1. 读取 stdin JSON：{{ "task_id", "conversation_id", "user_id", "origin_message", "params" }}
2. 输出 stdout JSON：{{ "task_id": "...", "reply": "...", "files": [] }}
3. 参数读取优先级: params.xxx > origin_message 提取
4. reply 使用中文
5. 包含错误处理
6. 只输出 Python 代码，不要其他内容

⚠️ 依赖说明：
- 推荐使用 httpx 发送 HTTP 请求（项目已安装，API 与 requests 兼容且支持异步）
- 如果需要其他第三方包，在 dependencies 字段中声明，系统会自动 pip install
- 可以自由使用任何 pip 可安装的包，不必局限于标准库

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

    # 参数读取优先级: params > origin_message
    prompt = params.get("prompt") or origin.strip()

    result = {{
        "task_id": task_id,
        "reply": f"🔧 {{prompt}}\\n\\n（此 Skill 由 LearningEngine 自动生成，功能待完善）",
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
        """将生成的 Skill 注册到 VaultManager，并同步到 Router"""
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

                # 同步注册到 Router
                if self._router and meta:
                    self._router.register_skill(meta)
                    logger.info("skill_registered_to_router", name=skill_name)

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

    # ─── 自动修复 ──────────────────────────────────────────────

    async def _repair_skill(
        self,
        skill_name: str,
        error_msg: str,
        user_message: str,
        analysis: dict,
    ) -> str | None:
        """根据执行错误信息，让 LLM 修复 Skill 代码

        Returns:
            修复后的代码，或 None 表示无法修复
        """
        if not self._llm:
            return None

        # 读取当前（有 bug 的）代码
        current_code = self._read_skill_code(skill_name)
        if not current_code:
            return None

        # 常见错误的快速修复（不依赖 LLM）
        quick_fix = await self._quick_fix(error_msg, current_code)
        if quick_fix:
            logger.info("skill_quick_fixed", skill=skill_name, error=error_msg)
            return quick_fix

        prompt = f"""以下 Skill 代码执行出错，请修复。

Skill 名称：{skill_name}
用户需求：{user_message}

当前代码：
```python
{current_code}
```

执行错误：
{error_msg}

{_SKILL_STANDARD_CORE}

修复要求：
1. 只输出修复后的完整 Python 代码，不要其他内容
2. 推荐使用 httpx（项目已安装，API 与 requests 兼容且更现代）
3. 如果需要新的第三方包，在代码开头用注释声明：# requires: pip包名
4. 保持 stdin/stdout JSON 通信协议不变

```python
# 修复后的代码
```"""

        try:
            response = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=2000,
            )
            text = response if isinstance(response, str) else str(response)
            match = re.search(r"```python\s*\n([\s\S]*?)```", text)
            if match:
                return match.group(1).strip()
            if "def main" in text or "import " in text:
                return text.strip()
        except Exception as e:
            logger.warning("skill_repair_llm_failed", error=str(e))

        return None

    async def _quick_fix(self, error_msg: str, code: str) -> str | None:
        """常见错误的快速修复（无需 LLM）

        修复策略：
        1. ModuleNotFoundError → 先尝试 pip install 缺失的包
        2. pip install 失败 → 用项目已有的 httpx 替换 requests
        """
        match = re.search(r"No module named '(\w+)'", error_msg)
        if match:
            missing_pkg = match.group(1)
            # 映射 import 名 → pip 包名
            pip_name = _IMPORT_TO_PIP.get(missing_pkg, missing_pkg)

            # 策略1: 尝试 pip install
            install_result = await self._install_dependencies([pip_name])
            if install_result["success"]:
                logger.info("quick_fix_installed", package=pip_name)
                return code  # 代码不用改，装好包就行

            # 策略2: requests → httpx（项目已有依赖，API 更优）
            if missing_pkg == "requests":
                logger.info("quick_fix_requests_to_httpx")
                return self._replace_requests_with_httpx(code)

        return None

    @staticmethod
    def _replace_requests_with_httpx(code: str) -> str:
        """将 requests 库调用替换为 httpx（项目已有依赖，API 更现代）

        httpx 是 requests 的现代替代，API 几乎一致，且项目已安装。
        """
        # 替换 import
        code = code.replace("import requests\n", "import httpx\n")
        code = code.replace("import requests", "import httpx")

        # requests.get/post/put/delete → httpx.get/post/put/delete
        # API 兼容：httpx.get(url, headers=..., timeout=...) 用法一致
        code = re.sub(r"requests\.(get|post|put|delete|patch|head)\(", r"httpx.\1(", code)

        # resp.raise_for_status() → httpx 也支持，保留不变
        # resp.content → resp.content (httpx 也支持)
        # resp.json() → resp.json() (httpx 也支持)
        # resp.text → resp.text (httpx 也支持)
        # resp.status_code → resp.status_code (httpx 也支持)

        return code

    def _read_skill_code(self, skill_name: str) -> str | None:
        """读取已注册 Skill 的 main.py 代码"""
        if not self._vault:
            return None
        try:
            skill_dir = self._vault._skills_dir / skill_name
            main_py = skill_dir / "main.py"
            if main_py.exists():
                return main_py.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("read_skill_code_failed", skill=skill_name, error=str(e))
        return None

    async def _update_skill_code(self, skill_name: str, code: str) -> bool:
        """更新已注册 Skill 的 main.py 代码"""
        if not self._vault:
            return False
        try:
            skill_dir = self._vault._skills_dir / skill_name
            main_py = skill_dir / "main.py"
            if not skill_dir.exists():
                return False
            main_py.write_text(code, encoding="utf-8")
            logger.info("skill_code_updated", skill=skill_name)
            return True
        except Exception as e:
            logger.error("update_skill_code_failed", skill=skill_name, error=str(e))
            return False

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

    # ─── Schedule 创建 ──────────────────────────────────────────

    async def _try_create_schedule(
        self, user_message: str, skill_name: str, conversation_id: str
    ) -> str | None:
        """尝试从用户消息中识别定时需求，创建 Schedule

        Returns:
            创建的 Schedule 描述字符串，或 None
        """
        if not self._scheduler or not self._llm:
            return None

        # 快速检查：消息中是否包含定时信号词
        schedule_signals = ("每天", "每周", "每月", "定时", "定期", "自动", "监控", "轮询")
        has_schedule_intent = any(s in user_message for s in schedule_signals)
        if not has_schedule_intent:
            return None

        prompt = f"""从用户消息中提取定时任务信息。

用户消息：{user_message}

如果这是一个定时/周期性需求，用 JSON 回答：
{{
  "cron": "5位cron表达式（分 时 日 月 周）",
  "message": "触发时发送给 Raccoon 的消息",
  "name": "定时任务名称"
}}

如果不是定时需求，返回：{{"schedule": false}}

只返回 JSON，不要其他内容。
cron 示例：
- 每天8点: "0 8 * * *"
- 每小时: "0 * * * *"
- 每周一9点: "0 9 * * 1"
- 每30分钟: "*/30 * * * *"
"""

        try:
            response = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=200,
            )
            text = response if isinstance(response, str) else str(response)
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                return None

            data = json.loads(match.group())
            if not data.get("cron") or data.get("schedule") is False:
                return None

            cron = data["cron"]
            message = data.get("message", user_message)
            name = data.get("name", f"auto_{skill_name}")

            # 创建 Schedule
            entry = ScheduleEntry(
                name=name,
                cron=cron,
                message=message,
                conversation_id=conversation_id,
                user_id="learning_engine",
            )
            entry = await self._scheduler.add_schedule(entry)
            logger.info(
                "schedule_created_by_learning",
                schedule_id=entry.schedule_id,
                name=name,
                cron=cron,
            )
            return f"{name} ({cron})"

        except Exception as e:
            logger.warning("schedule_creation_failed", error=str(e))
            return None

    # ─── 辅助 ──────────────────────────────────────────────────

    def _build_skill_catalog(self) -> str:
        """构建已有 Skill 清单摘要，注入 LLM prompt"""
        if not self._vault:
            return "（无已安装 Skill）"
        skills = self._vault.list_skills()
        if not skills:
            return "（无已安装 Skill）"
        lines = []
        for s in skills:
            tags = ", ".join(s.intent_tags) if s.intent_tags else ""
            triggers = ", ".join(s.trigger_words[:3]) if s.trigger_words else ""
            lines.append(
                f"- {s.name}: {s.description} | 触发词: {triggers} | 标签: {tags}"
            )
        return "\n".join(lines)
