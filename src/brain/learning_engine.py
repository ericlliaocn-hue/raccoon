"""LearningEngine：自主能力获取引擎（L3 核心）

当 Router 未命中任何 Skill 时，LearningEngine 接管：
1. 检索 MemCore 经验 → 有经验则复用
2. 无经验 → LLM 推理需要什么工具
3. 数据获取策略决策：API > HTML > CDP，逐个探测选最优
4. 自动 pip install 缺失依赖
5. API 探针：探测真实数据结构
6. LLM 生成 Skill 代码
7. 注册到 VaultManager（并同步到 Router）
8. 执行并验证（含自动修复 + 方案重选）
9. 经验写入 MemCore（含数据获取策略）
10. 识别定时需求 → 自动创建 Schedule

流程：
  learn "获取博客园热榜" →
    LLM推理: 候选数据源=[API, HTML, CDP] →
    策略探测: API不可用 → HTML可用 →
    选择: HTML爬取方案 →
    pip install beautifulsoup4 →
    生成 cnblogs_hot/main.py →
    注册到 Vault →
    执行验证 →
    经验写入 MemCore（记录：博客园无API，用HTML爬取）
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

        # 3. 数据获取策略决策：探测候选数据源，选最优方案（API > HTML > CDP）
        analysis = await self._decide_data_strategy(analysis)
        logger.info(
            "data_strategy_decided",
            strategy=analysis.get("data_strategy"),
            approach=analysis.get("approach", "")[:80],
        )

        # 4. 安装缺失依赖
        deps = analysis.get("dependencies", [])
        if deps and self._auto_install:
            install_result = await self._install_dependencies(deps)
            if not install_result["success"]:
                return {
                    "reply": f"❌ 依赖安装失败：{install_result['error']}\n\n需要手动安装：`pip install {' '.join(deps)}`",
                    "files": [],
                    "learned": False,
                }

        # 5. API 探针：探测真实数据结构（基于策略决策选中的数据源）
        probe_data = await self._probe_api(analysis)
        if probe_data:
            logger.info("api_probe_data_obtained", url=probe_data.get("api_url"))

        # 6. 生成 Skill 代码（注入探针数据）
        skill_code = await self._generate_skill(user_message, analysis, probe_data=probe_data)
        if not skill_code:
            return {
                "reply": "❌ 无法自动生成 Skill 代码，请手动实现",
                "files": [],
                "learned": False,
            }

        # 7. 注册到 Vault
        skill_name = analysis.get("skill_name", f"learned_{task.task_id[:8]}")
        register_result = await self._register_skill(skill_name, skill_code, analysis)
        if not register_result:
            return {
                "reply": "❌ Skill 注册失败",
                "files": [],
                "learned": False,
            }

        # 8. 执行验证 + 自动修复循环（含方案重选）
        #    流程：执行 → 语义验证 → 代码修复(最多3次) → 方案重选(最多2次) → 详尽反馈
        attempt_log: list[dict] = []  # 记录所有尝试，用于最终反馈
        current_analysis = analysis
        current_skill_name = skill_name

        max_replans = 2
        for replan_round in range(max_replans + 1):
            # 6a. 执行 + 代码修复循环
            exec_result = await self._execute_learned_skill(
                task, current_skill_name, user_message
            )

            error_history: list[str] = []
            max_code_retries = 3
            for code_attempt in range(1, max_code_retries + 1):
                if exec_result.get("success"):
                    break

                error_msg = exec_result.get("error", "未知错误")
                error_history.append(error_msg)
                attempt_log.append({
                    "phase": "code_fix",
                    "replan_round": replan_round,
                    "attempt": code_attempt,
                    "approach": current_analysis.get("approach", ""),
                    "error": error_msg,
                })
                logger.warning(
                    "skill_exec_failed_retry",
                    skill=current_skill_name,
                    replan_round=replan_round,
                    attempt=code_attempt,
                    error=error_msg,
                )

                # 尝试自动修复代码
                fixed_code = await self._repair_skill(
                    current_skill_name, error_msg, user_message, current_analysis,
                    actual_reply=exec_result.get("reply", ""),
                    debug_info=exec_result.get("debug_info"),
                )
                if not fixed_code:
                    logger.warning("skill_repair_failed", skill=current_skill_name, attempt=code_attempt)
                    break

                # 更新 Skill 代码
                update_ok = await self._update_skill_code(current_skill_name, fixed_code)
                if not update_ok:
                    break

                # 重新执行
                exec_result = await self._execute_learned_skill(
                    task, current_skill_name, user_message
                )

            # 6b. 如果代码修复后成功了，跳出方案重选循环
            if exec_result.get("success"):
                break

            # 6c. 代码修复失败 → 尝试方案重选
            if replan_round < max_replans:
                logger.info(
                    "approach_replan_start",
                    replan_round=replan_round + 1,
                    failed_approach=current_analysis.get("approach", ""),
                )
                new_analysis = await self._replan_approach(
                    user_message, current_analysis, error_history,
                    last_reply=exec_result.get("reply", ""),
                    debug_info=exec_result.get("debug_info"),
                )
                if not new_analysis:
                    logger.warning("approach_replan_no_alternative")
                    break

                attempt_log.append({
                    "phase": "replan",
                    "replan_round": replan_round + 1,
                    "old_approach": current_analysis.get("approach", ""),
                    "new_approach": new_analysis.get("approach", ""),
                    "reason": new_analysis.get("replan_reason", ""),
                })

                # 安装新方案可能需要的依赖
                new_deps = new_analysis.get("dependencies", [])
                if new_deps and self._auto_install:
                    install_result = await self._install_dependencies(new_deps)
                    if not install_result["success"]:
                        attempt_log.append({
                            "phase": "dep_install_failed",
                            "deps": new_deps,
                            "error": install_result["error"],
                        })
                        continue

                # 生成新方案的 Skill 代码（重新探测 API）
                new_probe = await self._probe_api(new_analysis)
                new_code = await self._generate_skill(user_message, new_analysis, probe_data=new_probe)
                if not new_code:
                    attempt_log.append({
                        "phase": "code_gen_failed",
                        "approach": new_analysis.get("approach", ""),
                    })
                    continue

                # 注册新 Skill（如果 skill_name 不同则创建新 Skill，否则更新）
                new_skill_name = new_analysis.get("skill_name", current_skill_name)
                if new_skill_name != current_skill_name:
                    # 新方案用新 Skill 名 → 注册新 Skill
                    register_ok = await self._register_skill(new_skill_name, new_code, new_analysis)
                    if not register_ok:
                        attempt_log.append({
                            "phase": "register_failed",
                            "skill": new_skill_name,
                        })
                        continue
                else:
                    # 同名 Skill → 更新代码
                    update_ok = await self._update_skill_code(new_skill_name, new_code)
                    if not update_ok:
                        continue

                current_analysis = new_analysis
                current_skill_name = new_skill_name
            # else: 已达最大方案重选次数，退出循环

        # 9. 结果处理
        if exec_result.get("success"):
            # 经验写入 MemCore（包含数据获取策略，方便下次复用）
            experience_data = {
                **current_analysis,
                "data_strategy": current_analysis.get("data_strategy", "unknown"),
                "chosen_source": current_analysis.get("chosen_source"),
            }
            await self._write_experience(
                key=user_message[:100],
                value=json.dumps(experience_data, ensure_ascii=False),
                skill_name=current_skill_name,
            )
            reply = exec_result.get("reply", "✅ 学习并执行成功")
            reply += f"\n\n🎓 已学会新技能：{current_skill_name}"

            # 如果经历过方案重选，告知用户
            replan_entries = [a for a in attempt_log if a["phase"] == "replan"]
            if replan_entries:
                reply += f"\n💡 （经过 {len(replan_entries)} 次方案调整后成功）"
        else:
            # 详尽失败反馈
            reply = self._build_failure_report(user_message, attempt_log, exec_result)

        return {
            "reply": reply,
            "files": exec_result.get("files", []),
            "learned": True,
            "skill_name": current_skill_name,
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
            # skill_name 可能在 learn() 内部因方案重选而变化
            # 从 reply 中提取，或使用空字符串让 _try_create_schedule 自行处理
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
        """用 LLM 分析用户需求，推理需要什么工具，并列出候选数据源

        核心改进：不再让 LLM 直接给一个 approach，而是让它列出候选数据源清单，
        按 API > HTML > CDP 优先级排序，后续由 _decide_data_strategy 逐个探测选最优。
        """
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
  "data_sources": [
    {{
      "type": "api",
      "url": "完整的 API URL（如果已知）",
      "description": "数据源说明",
      "priority": 1
    }},
    {{
      "type": "html",
      "url": "页面 URL（如果 API 不可用）",
      "description": "数据源说明",
      "priority": 2
    }},
    {{
      "type": "cdp",
      "url": "页面 URL（如果需要浏览器渲染）",
      "description": "数据源说明",
      "priority": 3
    }}
  ]
}}

注意：
- data_sources 按优先级排序：API（结构化 JSON）> HTML 爬取 > CDP 浏览器
- API 类型：目标平台是否有公开/半公开的 JSON API？优先列出
- HTML 类型：如果没有 API，哪个页面包含所需数据？
- CDP 类型：如果页面需要 JS 渲染或登录，才需要浏览器方式
- 如果已有 Skill 能组合实现，在 description 中说明如何组合
- skill_name 用小写+下划线，如 web_monitor
- 不确定的 URL 也要列出，后续会自动探测可用性
- 只返回 JSON，不要其他内容。"""

        try:
            response = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=600,
            )
            # 提取 JSON
            text = response if isinstance(response, str) else str(response)
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                result = json.loads(match.group())
                # 兼容：如果 LLM 仍然返回 approach 而非 data_sources，自动转换
                if "approach" in result and "data_sources" not in result:
                    result["data_sources"] = [
                        {"type": "auto", "url": "", "description": result["approach"], "priority": 1}
                    ]
                return result
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

    # ─── 数据获取策略决策 ──────────────────────────────────────────

    async def _decide_data_strategy(self, analysis: dict) -> dict:
        """根据候选数据源清单，逐个探测，选择最优数据获取方案

        策略优先级：API（结构化 JSON）> HTML 爬取 > CDP 浏览器

        对每个候选数据源发一个轻量请求，判断：
        1. 是否可达（HTTP 200）
        2. 返回是否是结构化 JSON（Content-Type + 内容检测）
        3. 数据是否有实质内容（非空、非错误页）

        Returns:
            更新后的 analysis dict，新增以下字段：
            - chosen_source: 选中的数据源信息
            - approach: 基于探测结果的实现思路
            - data_strategy: 策略类型（api / html / cdp）
        """
        data_sources = analysis.get("data_sources", [])

        # 如果没有候选数据源，让 LLM 补充
        if not data_sources:
            logger.info("no_data_sources_fallback")
            analysis["approach"] = analysis.get("approach", "自动分析实现")
            analysis["data_strategy"] = "unknown"
            analysis["chosen_source"] = None
            return analysis

        # 按 priority 排序（数字越小优先级越高）
        data_sources.sort(key=lambda s: s.get("priority", 99))

        chosen_source = None
        probe_results: list[dict] = []

        for source in data_sources:
            source_type = source.get("type", "unknown")
            source_url = source.get("url", "")
            source_desc = source.get("description", "")

            logger.info(
                "probing_data_source",
                type=source_type,
                url=source_url[:100] if source_url else "",
                description=source_desc[:50],
            )

            if not source_url:
                # 无 URL 的候选，跳过探测，保留为备选
                probe_results.append({
                    "type": source_type,
                    "url": "",
                    "reachable": False,
                    "reason": "无 URL",
                })
                continue

            # 发探测请求
            probe = await self._probe_source(source_url, source_type)
            probe_results.append(probe)

            if probe.get("usable"):
                chosen_source = source
                logger.info(
                    "data_source_chosen",
                    type=source_type,
                    url=source_url[:100],
                    reason=probe.get("reason", ""),
                )
                break  # 找到可用的最高优先级数据源，停止探测

        # 根据选中结果更新 analysis
        if chosen_source:
            strategy_type = chosen_source.get("type", "unknown")
            source_url = chosen_source.get("url", "")
            source_desc = chosen_source.get("description", "")

            # 构建明确的 approach 描述
            if strategy_type == "api":
                approach = f"调用 JSON API 获取数据：{source_url}（{source_desc}）"
            elif strategy_type == "html":
                approach = f"爬取 HTML 页面提取数据：{source_url}（{source_desc}）"
            elif strategy_type == "cdp":
                approach = f"使用 CDP 浏览器抓取数据：{source_url}（{source_desc}）"
            else:
                approach = source_desc

            analysis["approach"] = approach
            analysis["data_strategy"] = strategy_type
            analysis["chosen_source"] = chosen_source
        else:
            # 所有候选都不可用，使用第一个候选作为兜底（让后续流程去尝试）
            fallback = data_sources[0]
            analysis["approach"] = fallback.get("description", "尝试获取数据")
            analysis["data_strategy"] = fallback.get("type", "unknown")
            analysis["chosen_source"] = fallback
            logger.warning(
                "no_usable_data_source",
                fallback_type=fallback.get("type"),
                probe_results=probe_results,
            )

        # 保存探测结果供后续参考
        analysis["_probe_results"] = probe_results

        return analysis

    async def _probe_source(self, url: str, expected_type: str) -> dict:
        """探测单个数据源的可用性

        Args:
            url: 数据源 URL
            expected_type: 期望的数据类型（api / html / cdp）

        Returns:
            {"type", "url", "usable", "reason", "content_type", "is_json", "sample"}
        """
        result = {
            "type": expected_type,
            "url": url,
            "usable": False,
            "reason": "",
            "content_type": "",
            "is_json": False,
            "sample": "",
        }

        try:
            import httpx
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept": "application/json, text/html, */*",
            }
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)

            result["status_code"] = resp.status_code
            result["content_type"] = resp.headers.get("content-type", "")

            # 判断返回内容类型
            resp_text = resp.text[:2000] if resp.text else ""
            result["sample"] = resp_text

            is_json = (
                "application/json" in result["content_type"]
                or resp_text.strip().startswith("{")
                or resp_text.strip().startswith("[")
            )
            result["is_json"] = is_json

            if resp.status_code != 200:
                result["reason"] = f"HTTP {resp.status_code}"
                return result

            if not resp_text.strip():
                result["reason"] = "响应为空"
                return result

            # 根据期望类型判断可用性
            if expected_type == "api":
                if is_json:
                    # 检查 JSON 是否有实质数据（非错误响应）
                    try:
                        data = json.loads(resp_text)
                        # 常见错误模式：{"code": -1, "message": "..."} 或 {"code": 403}
                        if isinstance(data, dict):
                            code = data.get("code")
                            if isinstance(code, int) and code < 0:
                                result["reason"] = f"API 返回错误码: {code}"
                                return result
                            if data.get("code") == 403 or data.get("code") == 401:
                                result["reason"] = f"API 需要鉴权: code={code}"
                                return result
                        result["usable"] = True
                        result["reason"] = "JSON API 可用"
                        return result
                    except json.JSONDecodeError:
                        pass
                # 期望 API 但返回了 HTML → 标记不可用，让 HTML 候选去处理
                result["reason"] = "期望 JSON API 但返回了 HTML"
                return result

            elif expected_type == "html":
                if "text/html" in result["content_type"] or (resp_text and not is_json):
                    # 检查 HTML 是否有实质内容（非 404 页面、非空白页）
                    if len(resp_text) > 500:  # 简单启发式：有效 HTML 通常 >500 字符
                        result["usable"] = True
                        result["reason"] = "HTML 页面可爬取"
                        return result
                    result["reason"] = "HTML 内容过短，可能是错误页"
                    return result
                result["reason"] = "期望 HTML 但返回了其他格式"
                return result

            elif expected_type == "cdp":
                # CDP 类型总是标记为可用（需要浏览器渲染的页面无法通过简单 HTTP 判断）
                result["usable"] = True
                result["reason"] = "CDP 方案需浏览器验证"
                return result

            else:
                # 未知类型，有响应就算可用
                result["usable"] = True
                result["reason"] = "有响应数据"
                return result

        except Exception as e:
            result["reason"] = f"请求失败: {str(e)[:100]}"
            return result

    # ─── API 探针 ──────────────────────────────────────────────

    async def _probe_api(self, analysis: dict) -> dict | None:
        """在生成 Skill 代码前，先探测 API 返回的真实数据结构

        LLM 生成代码时经常猜错 API 返回的字段路径（如掘金 item_info 嵌套），
        探针机制先发一个真实请求，把返回结构喂给 LLM，让它基于真实数据写代码。

        Returns:
            {"api_url": "...", "sample_response": "截断的JSON", "status_code": 200}
            或 None（探测失败时不阻塞，只是没有参考数据）
        """
        approach = analysis.get("approach", "")
        deps = analysis.get("dependencies", [])

        # 让 LLM 推理需要探测的 API URL 和参数
        if not self._llm:
            return None

        prompt = f"""根据以下实现思路，给出需要探测的 API 请求信息。

实现思路：{approach}
依赖包：{', '.join(deps) if deps else '无'}

请用 JSON 回答：
{{
  "api_url": "完整的API URL",
  "method": "GET 或 POST",
  "headers": {{"key": "value"}},
  "body": {{}},  // POST 时的请求体，GET 时为空
  "api_note": "简要说明这个API返回什么数据"
}}

注意：
- 只返回 JSON，不要其他内容
- URL 必须是可直接请求的完整地址
- headers 中包含必要的 User-Agent 等"""

        try:
            response = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=400,
            )
            text = response if isinstance(response, str) else str(response)
            match = re.search(r"\{[\s\S]*\}", text)
            if not match:
                return None

            probe_config = json.loads(match.group())
            api_url = probe_config.get("api_url", "")
            if not api_url:
                return None

            # 执行探测请求
            method = probe_config.get("method", "GET").upper()
            headers = probe_config.get("headers", {})
            if "User-Agent" not in {k for k in headers}:
                headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            body = probe_config.get("body", {})

            import httpx
            async with httpx.AsyncClient(timeout=15.0) as client:
                if method == "POST":
                    resp = await client.post(api_url, json=body, headers=headers)
                else:
                    resp = await client.get(api_url, headers=headers)

            # 截断响应，保留结构但不要太大
            resp_text = resp.text[:3000] if resp.text else ""

            probe_result = {
                "api_url": api_url,
                "status_code": resp.status_code,
                "sample_response": resp_text,
                "api_note": probe_config.get("api_note", ""),
            }
            logger.info(
                "api_probe_success",
                url=api_url,
                status=resp.status_code,
                response_len=len(resp_text),
            )
            return probe_result

        except Exception as e:
            logger.warning("api_probe_failed", error=str(e))
            return None

    # ─── Skill 代码生成 ──────────────────────────────────────────

    async def _generate_skill(
        self, message: str, analysis: dict, probe_data: dict | None = None,
    ) -> str | None:
        """用 LLM 生成 Skill 代码

        Args:
            probe_data: API 探针返回的真实数据结构（可选，有则大幅提高代码准确性）
        """
        if not self._llm:
            return self._template_skill(analysis)

        skill_name = analysis.get("skill_name", "custom")
        approach = analysis.get("approach", "")
        deps = analysis.get("dependencies", [])

        # 注入已有 Skill 清单
        skill_catalog = self._build_skill_catalog()

        # 构建探针数据提示（如果有）
        probe_section = ""
        if probe_data:
            probe_section = f"""
🔍 API 探针返回的真实数据（关键！必须基于此结构写解析代码）：
- API URL: {probe_data.get('api_url', '')}
- 状态码: {probe_data.get('status_code', '')}
- 原始响应（前3000字）：
```
{probe_data.get('sample_response', '')}
```

⚠️⚠️⚠️ 你必须基于上面的真实 API 返回结构来写数据解析代码！
仔细观察 JSON 的嵌套层级，字段名是什么，数据在哪个 key 下面。
不要凭猜测写字段路径，必须和上面的真实数据完全对应！
"""

        prompt = f"""为浣熊生成一个 Skill 的 main.py 代码。

{_SKILL_STANDARD_CORE}

已有 Skill 清单（可参考或组合）：
{skill_catalog}

Skill 名称：{skill_name}
实现思路：{approach}
依赖包：{', '.join(deps) if deps else '无额外依赖'}
{probe_section}
要求：
1. 读取 stdin JSON：{{ "task_id", "conversation_id", "user_id", "origin_message", "params" }}
2. 输出 stdout JSON：{{ "task_id": "...", "reply": "...", "files": [], "_debug": {{}} }}
3. 参数读取优先级: params.xxx > origin_message 提取
4. reply 使用中文
5. 包含错误处理
6. 只输出 Python 代码，不要其他内容
7. ⚠️ _debug 字段（关键！）：当执行结果不理想时（API返回异常、数据为空、解析失败等），
   必须在 _debug 中写入诊断信息，格式：{{ "api_url": "请求的URL", "api_status": 状态码, "api_response": "API原始返回前500字", "error_detail": "具体错误描述" }}
   _debug 不展示给用户，仅供引擎诊断和自动修复使用。成功时 _debug 可为空字典。

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

    # 语义验证：reply 中出现这些信号词 → 视为执行失败
    _FAILURE_SIGNALS = (
        "失败", "错误", "error", "failed", "exception", "traceback",
        "无法获取", "无法访问", "无法连接", "请求被拒",
        "未能获取", "未能访问", "未能连接", "获取不到", "未获取到",
        "timeout", "超时", "拒绝访问", "风控", "签名校验失败",
        "验证失败", "access denied", "forbidden", "unauthorized",
        "rate limit", "限流", "captcha", "验证码",
        "暂不可用", "暂无数据", "接口已废弃", "服务不可用",
    )

    # 空壳输出检测：这些默认值大量出现 → 数据解析失败
    _HOLLOW_PATTERNS = (
        "无标题", "未知作者", "未知", "暂无", "N/A", "n/a", "-",
    )

    # API 错误码模式（如 B站 -352、-403 等）
    _API_ERROR_CODE_PATTERN = re.compile(r"[（(]-?\d+[）)]|code['\"]?\s*[:=]\s*[-]?\d+")

    async def _execute_learned_skill(
        self, task: Task, skill_name: str, user_message: str
    ) -> dict:
        """执行刚学会的 Skill，并进行语义验证

        Returns:
            dict 包含 success, reply, files, debug_info 等字段
        """
        if not self._vault:
            return {"success": False, "error": "No vault manager"}

        try:
            runner = self._vault.get_skill_runner(skill_name)
            if not runner:
                return {"success": False, "error": "Skill runner not found"}

            result = await runner.run(task, {"rest": user_message})
            reply = result.get("reply", "")
            files = result.get("files", [])
            # 提取 _debug 诊断信息（Skill 代码在失败时应写入此字段）
            debug_info = result.get("_debug", {})

            # 语义验证：即使没抛异常，也检查 reply 是否表示失败
            validation = self._validate_result(reply, result)
            if not validation["success"]:
                logger.warning(
                    "skill_result_semantic_failure",
                    skill=skill_name,
                    reason=validation.get("error", "语义验证失败"),
                    reply_preview=reply[:200],
                    debug_info=debug_info,
                )
                return {
                    "success": False,
                    "error": validation.get("error", "执行结果语义验证失败"),
                    "reply": reply,
                    "files": files,
                    "debug_info": debug_info,
                }

            return {"success": True, "reply": reply, "files": files, "debug_info": debug_info}

        except Exception as e:
            logger.error("learned_skill_exec_failed", skill=skill_name, error=str(e))
            return {"success": False, "error": str(e)}

    def _validate_result(self, reply: str, result: dict | None = None) -> dict:
        """语义验证：检查 reply 内容是否真正表示成功

        不仅仅看有没有异常，还要看 reply 里是否包含失败信号。
        比如 B站 API 返回 -352，Skill 代码把错误写进了 reply，
        但没有抛异常 → 之前会被误判为成功。

        Returns:
            {"success": True} 或 {"success": False, "error": "原因"}
        """
        if not reply or not reply.strip():
            return {"success": False, "error": "执行结果为空"}

        reply_lower = reply.lower()

        # 检查失败信号词
        detected_signals = []
        for signal in self._FAILURE_SIGNALS:
            if signal in reply_lower:
                detected_signals.append(signal)

        # 检查 API 错误码模式
        if self._API_ERROR_CODE_PATTERN.search(reply):
            detected_signals.append("API错误码")

        # 空壳输出检测：大量默认值 → 数据解析失败（如"无标题+未知作者+0阅读"）
        hollow_count = 0
        for pattern in self._HOLLOW_PATTERNS:
            hollow_count += reply_lower.count(pattern.lower())
        # 启发式：如果默认值出现次数 >= 3 且占行数比例高，判定为空壳输出
        line_count = reply.count("\n") + 1
        if hollow_count >= 3 and hollow_count / max(line_count, 1) >= 0.15:
            return {
                "success": False,
                "error": f"空壳输出检测：回复中大量默认值（{hollow_count}处），数据解析可能失败",
            }

        # 如果检测到失败信号，判断是否为"结果中提及失败"而非"成功描述中提到失败"
        # 启发式：如果 reply 很短（<100字）且包含失败信号 → 很可能是失败
        # 如果 reply 较长且包含实际数据 → 可能是成功但描述中提到某些失败
        if detected_signals:
            # 粗略判断：reply 中是否有实质性的成功内容
            # 如果 reply 包含列表、数据、排名等 → 可能是成功的
            success_indicators = (
                "排名", "第1", "第2", "第3", "1.", "2.", "3.",
                "📊", "📋", "✅", "结果如下", "数据如下", "获取成功",
                "top", "rank", "list",
            )
            has_success_content = any(ind in reply_lower for ind in success_indicators)

            if not has_success_content:
                signal_str = "、".join(detected_signals[:3])
                return {
                    "success": False,
                    "error": f"结果包含失败信号（{signal_str}）：{reply[:150]}",
                }

        return {"success": True}

    # ─── 自动修复 ──────────────────────────────────────────────

    async def _repair_skill(
        self,
        skill_name: str,
        error_msg: str,
        user_message: str,
        analysis: dict,
        actual_reply: str = "",
        debug_info: dict | None = None,
    ) -> str | None:
        """根据执行错误信息，让 LLM 修复 Skill 代码

        Args:
            actual_reply: Skill 的实际输出内容（语义验证失败时提供，帮助 LLM 诊断根因）
            debug_info: Skill 输出中的 _debug 诊断信息（API原始返回等）

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
{f"""
Skill 实际输出（reply）：
{actual_reply[:500]}

⚠️ 注意：Skill 代码没有抛异常，但输出内容表明执行结果不正确。
请分析实际输出，找出根因（如 API 返回了错误状态、接口已废弃、数据解析逻辑有误等），然后修复代码。
""" if actual_reply else ""}
{f"""
🔍 Skill _debug 诊断信息（API 原始返回）：
{json.dumps(debug_info, ensure_ascii=False, indent=2)[:800]}

⚠️ 这是 Skill 代码记录的 API 原始响应，是诊断根因的关键线索！
请根据此信息判断：API 是否已废弃/变更？是否需要换接口？数据解析逻辑是否正确？
""" if debug_info else ""}
{_SKILL_STANDARD_CORE}

修复要求：
1. 只输出修复后的完整 Python 代码，不要其他内容
2. 推荐使用 httpx（项目已安装，API 与 requests 兼容且更现代）
3. 如果需要新的第三方包，在代码开头用注释声明：# requires: pip包名
4. 保持 stdin/stdout JSON 通信协议不变
5. 如果是 API 接口失效，请更换为可用的替代 API

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

    # ─── 方案重选 ──────────────────────────────────────────────

    async def _replan_approach(
        self,
        user_message: str,
        failed_analysis: dict,
        error_history: list[str],
        last_reply: str = "",
        debug_info: dict | None = None,
    ) -> dict | None:
        """代码修复多次失败后，让 LLM 重新分析方案

        与 _repair_skill 不同：repair 只修代码 bug，replan 是换实现方案。
        例如：API 需要签名 → 换不需要签名的 API → 或换 CDP 方案。

        Args:
            user_message: 用户原始需求
            failed_analysis: 之前失败的分析结果
            error_history: 历次执行错误信息列表
            last_reply: Skill 最后一次的实际输出（帮助诊断根因）
            debug_info: Skill 输出中的 _debug 诊断信息

        Returns:
            新的 analysis dict（含新方案），或 None 表示无法重选
        """
        if not self._llm:
            return None

        skill_catalog = self._build_skill_catalog()
        old_approach = failed_analysis.get("approach", "未知")
        old_skill_name = failed_analysis.get("skill_name", "未知")

        errors_summary = "\n".join(
            f"  第{i+1}次: {err}" for i, err in enumerate(error_history)
        )

        prompt = f"""之前的方案执行失败，需要重新选择实现方案。

用户需求：{user_message}

之前尝试的方案：
- Skill 名称：{old_skill_name}
- 实现思路：{old_approach}
- 失败记录：
{errors_summary}
{f"""
- Skill 实际输出（reply）：
{last_reply[:500]}

⚠️ 请仔细分析实际输出，判断根因：
  - 如果输出是"未能获取/暂无数据"等 → 大概率是 API 接口已失效/废弃，需要换一个不同的 API
  - 如果输出包含鉴权/签名错误 → 该 API 需要认证，换不需要认证的公开接口
  - 如果输出包含风控/验证码 → 该站点有反爬，考虑换数据源或用浏览器方式
""" if last_reply else ""}
{f"""
- 🔍 Skill _debug 诊断信息（API 原始返回）：
{json.dumps(debug_info, ensure_ascii=False, indent=2)[:800]}

⚠️ 这是关键诊断线索！根据 API 原始返回判断：
  - 如果 API 返回错误码/异常状态 → 该接口可能已废弃或变更，必须换不同的 API
  - 如果 API 返回空数据 → 可能是接口参数变更或需要鉴权
  - 如果 API 返回正常但解析失败 → 可以只修代码，不必换方案
""" if debug_info else ""}
已有 Skill 清单（可参考或组合）：
{skill_catalog}

请重新分析需求，选择一个**不同的实现方案**。例如：
- 如果 API 需要签名/鉴权 → 换不需要签名的公开 API
- 如果 API 不可用 → 改用 CDP 浏览器抓取（web_automate Skill）
- 如果某个数据源不可用 → 换其他数据源
- 如果需要组合多个已有 Skill → 说明组合方式

{_SKILL_STANDARD_CORE}

请用 JSON 回答：
{{
  "skill_name": "新的简短英文技能名（如果方案完全不同则改名）",
  "description": "新方案描述",
  "trigger_words": ["触发词1", "触发词2"],
  "dependencies": ["pip包1", "pip包2"],
  "approach": "新实现思路（必须与之前不同）",
  "replan_reason": "为什么换方案"
}}

注意：
- approach 必须与之前的方案本质不同，不能只是微调
- 如果已有 Skill 能组合实现，优先组合
- skill_name 用小写+下划线
- 只返回 JSON，不要其他内容。"""

        try:
            response = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=600,
            )
            text = response if isinstance(response, str) else str(response)
            match = re.search(r"\{[\s\S]*\}", text)
            if match:
                new_analysis = json.loads(match.group())
                # 确保新方案与旧方案不同
                if new_analysis.get("approach") != old_approach:
                    logger.info(
                        "approach_replanned",
                        old=old_approach,
                        new=new_analysis.get("approach"),
                        reason=new_analysis.get("replan_reason", ""),
                    )
                    return new_analysis
        except Exception as e:
            logger.warning("approach_replan_failed", error=str(e))

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

    def _build_failure_report(
        self, user_message: str, attempt_log: list[dict], final_result: dict
    ) -> str:
        """构建详尽的失败报告：试过什么方案、失败原因、建议

        不再简单说"执行失败请手动修复"，而是给用户完整的诊断信息。
        """
        lines = [f"⚠️ 尝试完成「{user_message[:50]}」失败，以下是完整诊断："]

        # 按方案分组展示
        current_approach = ""
        approach_num = 0
        for entry in attempt_log:
            approach = entry.get("approach", entry.get("old_approach", ""))
            if approach != current_approach:
                approach_num += 1
                current_approach = approach
                if entry["phase"] == "replan":
                    lines.append(f"\n🔄 方案 {approach_num}（调整后）：{entry.get('new_approach', approach)}")
                    if entry.get("reason"):
                        lines.append(f"   调整原因：{entry['reason']}")
                else:
                    lines.append(f"\n📌 方案 {approach_num}：{approach}")

            if entry["phase"] == "code_fix":
                lines.append(f"   代码修复第{entry['attempt']}次失败：{entry['error'][:100]}")
            elif entry["phase"] == "dep_install_failed":
                lines.append(f"   依赖安装失败（{', '.join(entry.get('deps', []))}）：{entry.get('error', '')[:80]}")
            elif entry["phase"] == "code_gen_failed":
                lines.append(f"   代码生成失败")
            elif entry["phase"] == "register_failed":
                lines.append(f"   Skill 注册失败（{entry.get('skill', '')}）")

        # 最终错误
        final_error = final_result.get("error", "未知错误")
        lines.append(f"\n❌ 最终错误：{final_error[:150]}")

        # 建议
        lines.append("\n💡 建议：")
        # 根据错误类型给出针对性建议
        error_lower = final_error.lower()
        if any(kw in error_lower for kw in ("风控", "签名", "-352", "wbi", "鉴权", "验证", "forbidden")):
            lines.append("  - 该接口可能需要鉴权/签名，建议改用浏览器方式（web_automate Skill）抓取")
            lines.append("  - 或换一个不需要鉴权的公开 API")
        elif any(kw in error_lower for kw in ("timeout", "超时", "连接")):
            lines.append("  - 网络连接问题，稍后重试或检查网络设置")
        elif any(kw in error_lower for kw in ("module", "import", "no module")):
            lines.append("  - 缺少依赖包，尝试手动安装：pip install <包名>")
        else:
            lines.append("  - 可以告诉我更具体的需求，我会重新尝试")
            lines.append("  - 或手动实现后用 /install 安装")

        return "\n".join(lines)

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
