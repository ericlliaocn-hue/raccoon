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
import ast
import json
import py_compile
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from src.brain.core_benchmark import detect_core_scenario_id, evaluate_quality, normalize_intent_phrase
from src.brain.core_scenario_playbook import CoreScenarioPlaybook
from src.config import RaccoonConfig
from src.eventbus.events import EventType, make_event
from src.memcore.reader import MemCoreReader
from src.skill_vault.vault_manager import VaultManager
from src.skill_vault.sandbox_runner import SkillRunner
from src.types import LearningRun, LearningRunStatus, MemoryEntry, ScheduleEntry, SkillMetadata, Task

if TYPE_CHECKING:
    from src.brain.learning_store import LearningRunStore
    from src.eventbus.bus import EventBus
    from src.scheduler.scheduler import Scheduler
    from src.router.router import Router
    from src.supervisor.approval_engine import ApprovalEngine

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
        event_bus: EventBus | None = None,
        approval_engine: ApprovalEngine | None = None,
        learning_store: LearningRunStore | None = None,
    ) -> None:
        self._config = config or RaccoonConfig()
        self._vault = vault_manager
        self._memcore_writer = memcore_writer
        self._llm = llm_client
        self._scheduler = scheduler
        self._router = router
        self._event_bus = event_bus
        self._approval_engine = approval_engine
        self._learning_store = learning_store
        self._playbook = CoreScenarioPlaybook()
        self._auto_install = getattr(
            self._config,
            "learning_auto_install_dependencies",
            getattr(self._config, "learning_auto_install", False),
        )
        self._requires_approval = getattr(
            self._config,
            "learning_require_approval",
            getattr(self._config, "learning_requires_approval", True),
        )
        self._pending_approval_runs: dict[str, str] = {}
        if self._event_bus:
            self._event_bus.on(EventType.APPROVAL_RESOLVED, self._on_approval_resolved)

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
        run = self._create_run(task, user_message)
        await self._emit_learning_event(
            EventType.LEARNING_STARTED,
            run,
            {"request_text": user_message},
        )

        try:
            preflight = await self._preflight_learning_request(task, user_message, run)
            if preflight:
                return preflight

            experience = await self._search_experience(user_message)
            if experience:
                reused = await self._try_reuse_experience(task, user_message, run, experience)
                if reused:
                    return reused

            analysis = await self._analyze_need(user_message)
            if not analysis:
                return await self._fail_run(
                    run,
                    "🤔 无法分析需求，请更具体地描述你想做什么",
                    "analysis_failed",
                )
            blocker = await self._block_invalid_or_incomplete_plan(run, user_message, analysis)
            if blocker:
                return blocker

            analysis = await self._decide_data_strategy(analysis)
            blocker = await self._block_invalid_or_incomplete_plan(run, user_message, analysis)
            if blocker:
                return blocker
            skill_name = analysis.get("skill_name", f"learned_{task.task_id[:8]}")
            run.skill_name = skill_name
            run.analysis = analysis
            run.dependencies = list(analysis.get("dependencies", []))
            self._save_run(run, status=LearningRunStatus.GENERATING)

            probe_data = await self._probe_api(analysis)
            skill_code = await self._generate_skill(user_message, analysis, probe_data=probe_data)
            if not skill_code:
                return await self._fail_run(
                    run,
                    "❌ 无法自动生成 Skill 代码，请手动实现",
                    "code_generation_failed",
                )

            current_analysis = analysis
            current_code = skill_code
            validation: dict[str, Any] | None = None
            error_history: list[str] = []
            max_repairs = max(0, int(getattr(self._config, "learning_max_repair_attempts", 2)))
            replan_used = False

            while True:
                stage_dir = self._prepare_stage_dir(run.run_id, skill_name)
                run.staging_dir = str(stage_dir)
                metadata = self._build_generated_metadata(skill_name, current_analysis)
                self._write_staged_skill(stage_dir, current_code, metadata)
                self._save_run(run, status=LearningRunStatus.VALIDATING)

                validation = await self._validate_staged_skill(
                    task,
                    user_message,
                    stage_dir,
                    metadata,
                    dependencies=run.dependencies,
                )
                quality_score, quality_detail = evaluate_quality(
                    run.scenario_id,
                    analysis=current_analysis,
                    validation=validation,
                    execution_result={"reply": validation.get("reply", "")},
                )
                validation["quality"] = quality_detail
                validation["quality_score"] = quality_score
                run.quality_score = quality_score
                quality_threshold = self._quality_threshold(run.scenario_id, stage="validating")
                if validation.get("success") and quality_score < quality_threshold:
                    validation["success"] = False
                    validation["error"] = (
                        f"quality_gate_failed: score={quality_score:.2f} < threshold={quality_threshold:.2f}"
                    )
                run.validation = validation
                if validation.get("debug_info"):
                    run.artifacts["validation_debug"] = validation.get("debug_info")
                if validation.get("result_preview"):
                    run.artifacts["validation_preview"] = validation.get("result_preview")
                self._save_run(run)

                if validation.get("success"):
                    break

                error_msg = str(validation.get("error") or "validation_failed")
                run.failure_code = self._classify_failure_code(error_msg)
                error_history.append(error_msg)
                run.attempt_log.append({
                    "phase": "validation",
                    "attempt": run.repair_count + 1,
                    "approach": current_analysis.get("approach", ""),
                    "error": error_msg,
                    "failure_code": run.failure_code,
                })

                if run.repair_count < max_repairs:
                    repair_hint = self._repair_hint_for_failure(run.failure_code)
                    fixed_code = await self._repair_skill(
                        skill_name,
                        f"[{run.failure_code}] {error_msg}\n{repair_hint}".strip(),
                        user_message,
                        current_analysis,
                        actual_reply=validation.get("reply", ""),
                        debug_info=validation.get("debug_info"),
                        current_code=current_code,
                    )
                    if fixed_code:
                        current_code = fixed_code
                        run.repair_count += 1
                        self._save_run(run)
                        continue

                if not replan_used:
                    new_analysis = await self._replan_approach(
                        user_message,
                        current_analysis,
                        error_history,
                        last_reply=validation.get("reply", ""),
                        debug_info=validation.get("debug_info"),
                    )
                    if new_analysis:
                        blocker = await self._block_invalid_or_incomplete_plan(
                            run,
                            user_message,
                            new_analysis,
                        )
                        if blocker:
                            return blocker
                        replan_used = True
                        current_analysis = await self._decide_data_strategy(new_analysis)
                        blocker = await self._block_invalid_or_incomplete_plan(
                            run,
                            user_message,
                            current_analysis,
                        )
                        if blocker:
                            return blocker
                        skill_name = current_analysis.get("skill_name", skill_name)
                        run.skill_name = skill_name
                        run.analysis = current_analysis
                        run.dependencies = list(current_analysis.get("dependencies", []))
                        run.attempt_log.append({
                            "phase": "replan",
                            "old_approach": analysis.get("approach", ""),
                            "new_approach": current_analysis.get("approach", ""),
                            "reason": current_analysis.get("replan_reason", ""),
                        })
                        new_probe = await self._probe_api(current_analysis)
                        regenerated = await self._generate_skill(
                            user_message,
                            current_analysis,
                            probe_data=new_probe,
                        )
                        if regenerated:
                            current_code = regenerated
                            continue

                break

            if not validation or not validation.get("success"):
                reply = self._build_failure_report(
                    user_message,
                    run.attempt_log,
                    validation or {"error": "validation_failed"},
                )
                return await self._fail_run(
                    run,
                    reply,
                    str((validation or {}).get("error") or "validation_failed"),
                )

            await self._emit_learning_event(
                EventType.LEARNING_VALIDATED,
                run,
                {
                    "skill_name": skill_name,
                    "validation": validation,
                    "dependencies": run.dependencies,
                },
            )

            approval_reply = await self._request_install_approval(run, task)
            if approval_reply:
                return {
                    "reply": approval_reply,
                    "files": [],
                    "learned": False,
                    "skill_name": skill_name,
                    "learning_run_id": run.run_id,
                }

            return await self._install_and_execute_learning_run(run, task, user_message)

        except Exception as e:
            logger.exception("learning_engine_run_failed", run_id=run.run_id)
            return await self._fail_run(
                run,
                f"❌ 学习过程出错：{e}",
                str(e),
            )

    def _create_run(self, task: Task, user_message: str) -> LearningRun:
        scenario_id = (
            str(task.context.get("scenario_id")) if isinstance(task.context, dict) and task.context.get("scenario_id")
            else detect_core_scenario_id(user_message)
        )
        run = LearningRun(
            conversation_id=task.conversation_id,
            user_id=task.user_id,
            request_text=user_message,
            source_task_id=task.task_id,
            scenario_id=scenario_id,
        )
        store = getattr(self, "_learning_store", None)
        if store:
            store.add(run)
        return run

    def _save_run(
        self,
        run: LearningRun,
        *,
        status: LearningRunStatus | None = None,
        reply: str | None = None,
        error: str | None = None,
    ) -> LearningRun:
        if status is not None:
            run.status = status
        if reply is not None:
            run.reply = reply
        if error is not None:
            run.error = error
        store = getattr(self, "_learning_store", None)
        if store:
            store.update(run)
        return run

    async def _emit_learning_event(
        self,
        event_type: EventType,
        run: LearningRun,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event_bus = getattr(self, "_event_bus", None)
        if not event_bus:
            return
        data = {
            "learning_run_id": run.run_id,
            "status": run.status.value,
            "skill_name": run.skill_name,
            "scenario_id": run.scenario_id,
            "first_pass": run.first_pass,
            "final_success": run.final_success,
            "decision_success": run.decision_success,
            "execution_attempted": run.execution_attempted,
            "execution_success": run.execution_success,
            "handling_outcome": run.handling_outcome,
            "clarification_reason": run.clarification_reason,
            "failure_code": run.failure_code,
            "quality_score": run.quality_score,
            "artifacts": run.artifacts,
        }
        if payload:
            data.update(payload)
        await event_bus.emit(
            make_event(
                event_type,
                conversation_id=run.conversation_id,
                user_id=run.user_id,
                task_id=run.source_task_id,
                skill_name=run.skill_name,
                payload=data,
            )
        )

    async def _fail_run(self, run: LearningRun, reply: str, error: str) -> dict[str, Any]:
        failure_code = self._classify_failure_code(error)
        previous_outcome = run.handling_outcome
        if previous_outcome == "executed" and run.status == LearningRunStatus.EXECUTING:
            run.execution_attempted = True
        run.handling_outcome = "failed"
        run.execution_success = False
        if not run.decision_success and previous_outcome in {"clarified", "reused", "executed"}:
            run.decision_success = True
        run.final_success = False
        run.failure_code = failure_code
        candidate = self._maybe_mark_new_skill_candidate(run, failure_code)
        if candidate:
            run.artifacts["new_skill_candidate"] = candidate
            if "可以考虑新增通用 Skill" not in reply:
                reply = (
                    f"{reply}\n\n📌 失败聚类提示：最近 7 天同类失败达到阈值，"
                    f"可考虑新增通用 Skill（failure_code={failure_code}）。"
                )
        self._save_run(
            run,
            status=LearningRunStatus.FAILED,
            reply=reply,
            error=str(error),
        )
        await self._emit_learning_event(
            EventType.LEARNING_FAILED,
            run,
            {"error": str(error), "reply": reply, "failure_code": failure_code},
        )
        return {
            "reply": reply,
            "files": [],
            "learned": False,
            "skill_name": run.skill_name,
            "learning_run_id": run.run_id,
        }

    async def _succeed_without_learning(
        self,
        run: LearningRun,
        reply: str,
        *,
        skill_name: str | None = None,
        artifacts: dict[str, Any] | None = None,
        schedule_created: str | None = None,
        handling_outcome: str = "reused",
        execution_attempted: bool = False,
        execution_success: bool = False,
        clarification_reason: str | None = None,
    ) -> dict[str, Any]:
        """Mark the learning run as handled without generating a new Skill."""
        run.skill_name = skill_name or run.skill_name
        run.handling_outcome = handling_outcome
        run.clarification_reason = clarification_reason
        run.decision_success = True
        run.execution_attempted = execution_attempted
        run.execution_success = execution_success
        run.first_pass = True
        run.final_success = True
        run.failure_code = None
        run.quality_score = max(run.quality_score, 0.9)
        run.schedule_created = schedule_created
        if artifacts:
            run.artifacts.update(artifacts)
        self._save_run(run, status=LearningRunStatus.SUCCEEDED, reply=reply, error=None)
        return {
            "reply": reply,
            "files": [],
            "learned": False,
            "skill_name": run.skill_name,
            "learning_run_id": run.run_id,
            "schedule_created": schedule_created,
        }

    async def _preflight_learning_request(
        self,
        task: Task,
        user_message: str,
        run: LearningRun,
    ) -> dict[str, Any] | None:
        scenario_id = self._infer_scenario_id(run, user_message)
        text = normalize_intent_phrase(user_message)

        if "example.com" in text or "oa.internal" in text:
            return await self._clarify_run(
                run,
                "检测到占位或内部示例地址。请提供真实可访问地址后再继续，我不会把假入口写进 Skill。",
                reason="placeholder_endpoint",
            )

        decision = self._playbook.decide(
            scenario_id=scenario_id,
            message=user_message,
            available_content_skills=self._available_content_skills(),
            has_monitor_target=self._has_monitor_target(user_message),
            has_form_target=self._has_form_target(user_message),
            has_shell_command=self._has_concrete_shell_command(user_message),
        )
        if not decision:
            return None

        if decision.requires_scheduler:
            if self._scheduler:
                schedule_info = await self._try_create_schedule(user_message, "scheduler", task.conversation_id)
                if schedule_info:
                    return await self._succeed_without_learning(
                        run,
                        f"{decision.reply} 已创建任务：{schedule_info}",
                        skill_name=decision.skill_name,
                        artifacts=decision.artifacts,
                        schedule_created=schedule_info,
                        handling_outcome="executed",
                        execution_attempted=True,
                        execution_success=True,
                    )
            return await self._clarify_run(
                run,
                "提醒场景走 Scheduler 稳定路径，请补充可执行时间表达式和提醒内容。",
                reason=decision.clarification_reason or "scheduler_unavailable_or_incomplete",
                capability=decision.skill_name,
            )

        if decision.handling_outcome == "clarified":
            return await self._clarify_run(
                run,
                decision.reply,
                reason=decision.clarification_reason or "clarification_required",
                capability=decision.skill_name,
            )

        return await self._succeed_without_learning(
            run,
            decision.reply,
            skill_name=decision.skill_name,
            artifacts=decision.artifacts,
            handling_outcome=decision.handling_outcome,
            execution_attempted=decision.handling_outcome == "executed",
            execution_success=decision.handling_outcome == "executed",
        )

    async def _block_invalid_or_incomplete_plan(
        self,
        run: LearningRun,
        user_message: str,
        analysis: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self._analysis_has_fake_endpoint(analysis):
            return await self._clarify_run(
                run,
                "学习方案里出现了占位地址、假 API 或模板变量。请提供真实 URL、SKU、账号流程或脚本路径后再继续。",
                reason="placeholder_endpoint",
                analysis=analysis,
            )

        scenario_id = self._infer_scenario_id(run, user_message)
        if scenario_id == "price_monitor" and not self._has_monitor_target(user_message):
            return await self._clarify_run(
                run,
                "价格监控缺少商品链接/SKU，当前只做追问，不生成不可执行的监控 Skill。",
                reason="missing_price_target",
                analysis=analysis,
            )
        if scenario_id == "login_form_chain" and not self._has_form_target(user_message):
            return await self._clarify_run(
                run,
                "登录表单链路缺少真实网址、账号状态和表单字段，当前只做追问，不生成占位自动化 Skill。",
                reason="missing_form_target",
                analysis=analysis,
            )
        if scenario_id == "remote_exec" and not self._has_concrete_shell_command(user_message):
            return await self._clarify_run(
                run,
                "远程/命令执行缺少具体命令或脚本路径，当前只做追问，不生成审批系统占位 Skill。",
                reason="missing_shell_command",
                analysis=analysis,
            )
        return None

    async def _clarify_run(
        self,
        run: LearningRun,
        reply: str,
        *,
        reason: str,
        capability: str | None = None,
        analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifacts = {
            "clarification_required": True,
            "reason": reason,
        }
        if capability:
            artifacts["reused_capability"] = capability
        if analysis:
            artifacts["blocked_analysis"] = analysis
            run.analysis = analysis
        return await self._succeed_without_learning(
            run,
            f"需要补充信息：{reply}",
            skill_name=capability,
            artifacts=artifacts,
            handling_outcome="clarified",
            execution_attempted=False,
            execution_success=False,
            clarification_reason=reason,
        )

    def _infer_scenario_id(self, run: LearningRun, user_message: str) -> str | None:
        scenario_id = run.scenario_id or detect_core_scenario_id(user_message)
        text = normalize_intent_phrase(user_message)
        if any(token in text for token in ("价格", "降到", "降价", "商品", "库存")) and any(
            token in text for token in ("监控", "盯", "提醒", "通知")
        ):
            scenario_id = "price_monitor"
        elif any(token in text for token in ("登录", "表单", "提交", "oa", "后台")):
            scenario_id = "login_form_chain"
        elif any(token in text for token in ("执行", "命令", "脚本", "shell", "审批")):
            scenario_id = "remote_exec"
        elif any(token in text for token in ("素材", "爆款", "热搜", "热门", "采集", "去重")):
            scenario_id = "material_collection"
        elif any(token in text for token in ("提醒", "每天", "每周", "每个工作日", "定时")):
            scenario_id = "reminder_schedule"
        if scenario_id and run.scenario_id != scenario_id:
            run.scenario_id = scenario_id
            self._save_run(run)
        return scenario_id

    def _has_monitor_target(self, text: str) -> bool:
        value = str(text or "")
        if re.search(r"https?://|sku\s*[:：=]?\s*\w+|\b\d{6,}\b", value, re.IGNORECASE):
            return True
        if any(platform in value.lower() for platform in ("jd.com", "taobao", "tmall", "amazon", "pdd")):
            return True
        return False

    def _has_form_target(self, text: str) -> bool:
        value = str(text or "").lower()
        if re.search(r"https?://", value):
            return True
        if any(token in value for token in ("oa.example", "example.com", "oa.internal")):
            return False
        return False

    def _has_concrete_shell_command(self, text: str) -> bool:
        value = str(text or "").strip()
        concrete_patterns = (
            r"\b(ls|pwd|df|du|cat|tail|grep|find|python|python3|bash|sh|git|npm|pnpm|uv|pytest|ruff)\b",
            r"\.sh\b",
            r"/[\w./-]+",
            r"`[^`]+`",
        )
        return any(re.search(pattern, value, re.IGNORECASE) for pattern in concrete_patterns)

    def _can_reuse_content_skills(self, text: str) -> bool:
        value = normalize_intent_phrase(text)
        if not any(token in value for token in ("素材", "爆款", "热门", "热搜", "日报", "采集")):
            return False
        return bool(self._available_content_skills())

    def _available_content_skills(self) -> list[str]:
        if not self._vault:
            return []
        preferred = {
            "weibo_hot",
            "bilibili_hot",
            "baidu_hot",
            "36kr_hot",
            "ai_daily_report",
            "xiaohongshu_daily_report",
            "juejin_hot",
            "csdn_hot",
            "cnblogs_hot",
        }
        return sorted(
            skill.name
            for skill in self._vault.list_skills()
            if getattr(skill, "name", "") in preferred
        )

    def _analysis_has_fake_endpoint(self, analysis: dict[str, Any]) -> bool:
        haystack = json.dumps(analysis, ensure_ascii=False).lower()
        fake_tokens = (
            "example.com",
            "oa.internal",
            "internal.company",
            "internal.corp",
            "your_",
            "todo",
            "{sku_id}",
            "{sku}",
            "{商品",
            "<url",
            "<your",
            "替换为",
            "示例",
        )
        return any(token in haystack for token in fake_tokens)

    def _prepare_stage_dir(self, run_id: str, skill_name: str) -> Path:
        safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", skill_name or "learned_skill").strip("_") or "learned_skill"
        stage_dir = self._config.learning_staging_dir / run_id / safe_name
        if stage_dir.exists():
            shutil.rmtree(stage_dir)
        stage_dir.mkdir(parents=True, exist_ok=True)
        return stage_dir

    def _build_generated_metadata(self, skill_name: str, analysis: dict) -> dict[str, Any]:
        permissions = self._infer_permissions(analysis)
        risk_level = "medium" if permissions else "low"
        if "subprocess" in permissions:
            risk_level = "high"
        return {
            "name": skill_name,
            "version": "0.1.0",
            "description": analysis.get("description", f"Auto-learned: {skill_name}"),
            "trigger_words": analysis.get("trigger_words", [skill_name]),
            "aliases": analysis.get("aliases", []),
            "intent_tags": list(dict.fromkeys(["learned", *analysis.get("intent_tags", [])])),
            "risk_level": risk_level,
            "requires_approval": False,
            "timeout_seconds": 60,
            "entry": "main.py",
            "permissions": permissions,
        }

    def _infer_permissions(self, analysis: dict) -> list[str]:
        permissions: list[str] = []
        text = " ".join(
            str(v) for v in (
                analysis.get("approach", ""),
                analysis.get("description", ""),
                analysis.get("skill_name", ""),
            )
        ).lower()
        data_strategy = str(analysis.get("data_strategy", "")).lower()

        if data_strategy in {"api", "html", "cdp"}:
            permissions.append("network")
        if any(word in text for word in ("file", "文件", "保存", "下载", "写入")):
            permissions.append("filesystem")
        if any(word in text for word in ("shell", "命令", "终端", "subprocess")):
            permissions.append("subprocess")
        return list(dict.fromkeys(permissions))

    def _write_staged_skill(self, stage_dir: Path, code: str, metadata: dict[str, Any]) -> None:
        (stage_dir / "main.py").write_text(code, encoding="utf-8")
        (stage_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _validate_staged_skill(
        self,
        task: Task,
        user_message: str,
        stage_dir: Path,
        metadata: dict[str, Any],
        *,
        dependencies: list[str] | None = None,
        strict_smoke: bool = False,
    ) -> dict[str, Any]:
        validation: dict[str, Any] = {
            "success": False,
            "metadata_ok": False,
            "compile_ok": False,
            "smoke_ok": False,
            "smoke_deferred": False,
            "risk_warnings": [],
            "warnings": [],
        }

        try:
            parsed = SkillMetadata.model_validate(metadata)
            validation["metadata_ok"] = True
        except Exception as e:
            validation["error"] = f"metadata_invalid: {e}"
            return validation

        main_path = stage_dir / parsed.entry
        if not main_path.exists():
            validation["error"] = f"entry_not_found: {parsed.entry}"
            return validation

        try:
            py_compile.compile(str(main_path), doraise=True)
            validation["compile_ok"] = True
        except Exception as e:
            validation["error"] = f"compile_failed: {e}"
            return validation

        code = main_path.read_text(encoding="utf-8")
        validation["risk_warnings"] = self._scan_skill_risks(code, parsed)

        smoke_task = Task(
            conversation_id=task.conversation_id,
            user_id=task.user_id,
            origin_message=f"validation::{user_message}",
            skill_name=parsed.name,
        )
        try:
            runner = SkillRunner(stage_dir, parsed.entry, self._config)
            smoke_result = await asyncio.wait_for(
                runner.run(smoke_task, {"prompt": user_message, "validation_mode": True}),
                timeout=min(parsed.timeout_seconds, self._config.task_timeout_seconds, 15),
            )
            if isinstance(smoke_result, dict) and "reply" in smoke_result:
                validation["smoke_ok"] = True
                validation["result_preview"] = str(smoke_result.get("reply", ""))[:200]
                validation["reply"] = smoke_result.get("reply", "")
                validation["debug_info"] = smoke_result.get("_debug")
            else:
                validation["error"] = "protocol_smoke_failed: output missing reply"
                return validation
        except Exception as e:
            validation["reply"] = ""
            validation["debug_info"] = {}
            missing_match = re.search(r"No module named '([^']+)'", str(e))
            declared_deps = {
                str(dep).strip().lower()
                for dep in (dependencies or [])
                if str(dep).strip()
            }
            missing_module = missing_match.group(1).lower() if missing_match else ""
            missing_pip = _IMPORT_TO_PIP.get(missing_module, missing_module)
            if missing_match and missing_pip in declared_deps and not strict_smoke:
                validation["smoke_deferred"] = True
                validation["warnings"].append("protocol_smoke_deferred_until_dependency_install")
                validation["missing_module"] = missing_module
            else:
                validation["error"] = f"protocol_smoke_failed: {e}"
                return validation

        validation["success"] = (
            validation["metadata_ok"]
            and validation["compile_ok"]
            and (validation["smoke_ok"] or validation["smoke_deferred"])
        )
        if not validation["success"] and "error" not in validation:
            validation["error"] = "validation_failed"
        return validation

    def _scan_skill_risks(self, code: str, metadata: SkillMetadata) -> list[str]:
        warnings: list[str] = []
        lowered = code.lower()

        try:
            tree = ast.parse(code)
        except SyntaxError:
            return warnings

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = getattr(node.func, "attr", "") or getattr(node.func, "id", "")
                if func in {"system", "popen", "run", "call"}:
                    warnings.append("uses_subprocess_calls")
                if func == "open":
                    mode = ""
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                        mode = str(node.args[1].value)
                    for kw in node.keywords:
                        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                            mode = str(kw.value.value)
                    if any(flag in mode for flag in ("w", "a", "+")):
                        warnings.append("writes_files")

        if any(pkg in lowered for pkg in ("httpx", "requests", "aiohttp", "feedparser")):
            warnings.append("uses_network_requests")
        if any(pkg in lowered for pkg in ("playwright", "selenium")):
            warnings.append("controls_browser")
        if "# requires:" in lowered:
            warnings.append("declares_extra_dependencies")
        for perm in metadata.permissions:
            warnings.append(f"permission:{perm}")

        return list(dict.fromkeys(warnings))

    async def _request_install_approval(self, run: LearningRun, task: Task) -> str | None:
        if not getattr(self, "_approval_engine", None) or not getattr(self, "_requires_approval", True):
            return None

        approval_task = Task(
            conversation_id=task.conversation_id,
            user_id=task.user_id,
            origin_message=task.origin_message,
            skill_name=run.skill_name or "learning_install",
            context={"learning_run_id": run.run_id},
        )
        meta = SkillMetadata(
            name=run.skill_name or "learning_install",
            description="LearningEngine staged install approval",
            trigger_words=[],
            intent_tags=["learning"],
            risk_level="high",
            requires_approval=True,
            timeout_seconds=60,
            entry="main.py",
            permissions=["filesystem", "subprocess"] if run.dependencies else ["filesystem"],
        )
        result = await self._approval_engine.review(approval_task, metadata=meta, risk_level="high")
        run.approval_id = result.approval_id
        run.approval_status = result.status.value
        run.decision_success = True
        run.handling_outcome = "executed"
        run.execution_attempted = False
        run.execution_success = False

        if result.approved:
            self._save_run(run)
            return None

        if result.approval_id:
            self._pending_approval_runs[result.approval_id] = run.run_id
        self._save_run(run, status=LearningRunStatus.PENDING_APPROVAL)
        await self._emit_learning_event(
            EventType.LEARNING_APPROVAL_REQUIRED,
            run,
            {
                "approval_id": result.approval_id,
                "dependencies": run.dependencies,
                "validation": run.validation,
            },
        )
        return (
            f"学习运行 [{run.run_id[:8]}] 已通过验证，等待审批后安装。"
            f"{' 需要安装依赖: ' + ', '.join(run.dependencies) if run.dependencies else ''}"
        )

    async def _install_and_execute_learning_run(
        self,
        run: LearningRun,
        task: Task,
        user_message: str,
        *,
        resumed: bool = False,
    ) -> dict[str, Any]:
        if not run.staging_dir:
            return await self._fail_run(run, "❌ 找不到 staging Skill，无法继续安装", "staging_missing")

        stage_dir = Path(run.staging_dir)
        meta_path = stage_dir / "metadata.json"
        if not meta_path.exists():
            return await self._fail_run(run, "❌ staging Skill 元数据缺失", "metadata_missing")

        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        skill_name = str(metadata.get("name") or run.skill_name or "")
        run.skill_name = skill_name

        if self._vault and self._vault.get_skill(skill_name):
            return await self._fail_run(
                run,
                f"❌ Skill「{skill_name}」已存在，当前学习流程不会直接覆盖同名 Skill。",
                "skill_already_exists",
            )

        self._save_run(run, status=LearningRunStatus.INSTALLING)

        if run.dependencies:
            install_result = await self._install_dependencies(run.dependencies, force=True)
            if not install_result["success"]:
                return await self._fail_run(
                    run,
                    f"❌ 依赖安装失败：{install_result['error']}",
                    str(install_result["error"]),
                )

        validation = await self._validate_staged_skill(
            task,
            user_message,
            stage_dir,
            metadata,
            dependencies=run.dependencies,
            strict_smoke=True,
        )
        quality_score, quality_detail = evaluate_quality(
            run.scenario_id,
            analysis=run.analysis,
            validation=validation,
            execution_result={"reply": validation.get("reply", "")},
        )
        validation["quality"] = quality_detail
        validation["quality_score"] = quality_score
        run.quality_score = quality_score
        threshold = self._quality_threshold(run.scenario_id, stage="validating")
        if validation.get("success") and quality_score < threshold:
            validation["success"] = False
            validation["error"] = f"quality_gate_failed: score={quality_score:.2f} < threshold={threshold:.2f}"
        run.validation = validation
        if validation.get("debug_info"):
            run.artifacts["validation_debug"] = validation.get("debug_info")
        if validation.get("result_preview"):
            run.artifacts["validation_preview"] = validation.get("result_preview")
        if not validation.get("success"):
            return await self._fail_run(
                run,
                self._build_failure_report(user_message, run.attempt_log, validation),
                str(validation.get("error") or "post_approval_validation_failed"),
            )

        if not self._vault:
            return await self._fail_run(run, "❌ VaultManager 未初始化", "vault_unavailable")

        try:
            installed_meta = self._vault._install_from_local(str(stage_dir))
            if self._router and installed_meta:
                self._router.register_skill(installed_meta)
        except Exception as e:
            return await self._fail_run(run, f"❌ Skill 安装失败：{e}", str(e))

        run.handling_outcome = "executed"
        run.decision_success = True
        run.execution_attempted = True
        run.execution_success = False
        self._save_run(run, status=LearningRunStatus.EXECUTING)
        exec_result = await self._execute_learned_skill(task, skill_name, user_message)
        if not exec_result.get("success"):
            return await self._fail_run(
                run,
                self._build_failure_report(user_message, run.attempt_log, exec_result),
                str(exec_result.get("error") or "execute_failed"),
            )
        final_quality, final_detail = evaluate_quality(
            run.scenario_id,
            analysis=run.analysis,
            validation=run.validation,
            execution_result=exec_result,
        )
        run.quality_score = max(run.quality_score, final_quality)
        run.artifacts["quality"] = final_detail
        exec_threshold = self._quality_threshold(run.scenario_id, stage="executing")
        if final_quality < exec_threshold:
            return await self._fail_run(
                run,
                self._build_failure_report(
                    user_message,
                    run.attempt_log,
                    {
                        "error": f"quality_gate_failed: score={final_quality:.2f} < threshold={exec_threshold:.2f}",
                        "reply": exec_result.get("reply", ""),
                    },
                ),
                "quality_gate_failed",
            )

        await self._write_experience(
            key=user_message[:100],
            value=json.dumps(
                {
                    **run.analysis,
                    "skill_name": skill_name,
                    "scenario_id": run.scenario_id,
                    "request_text": user_message,
                    "dependencies": run.dependencies,
                    "repair_count": run.repair_count,
                    "attempt_log": run.attempt_log,
                    "validation": run.validation,
                },
                ensure_ascii=False,
            ),
            skill_name=skill_name,
        )

        if self._scheduler:
            schedule_info = await self._try_create_schedule(
                user_message,
                skill_name,
                task.conversation_id,
            )
            if schedule_info:
                run.schedule_created = schedule_info

        reply = exec_result.get("reply", "✅ 学习并执行成功")
        if resumed:
            reply = f"✅ 审批通过，已安装并执行技能「{skill_name}」。\n\n{reply}"
        else:
            reply = f"{reply}\n\n🎓 已学会新技能：{skill_name}"
        if run.schedule_created:
            reply += f"\n⏰ 已创建定时任务：{run.schedule_created}"

        run.execution_result = exec_result
        run.decision_success = True
        run.execution_attempted = True
        run.execution_success = True
        run.clarification_reason = None
        run.first_pass = run.repair_count == 0
        run.final_success = True
        run.failure_code = None
        run.artifacts["files"] = exec_result.get("files", [])
        run.artifacts["debug_info"] = exec_result.get("debug_info", {})
        if exec_result.get("artifacts") is not None:
            run.artifacts["execution_artifacts"] = exec_result.get("artifacts")
        self._save_run(
            run,
            status=LearningRunStatus.SUCCEEDED,
            reply=reply,
            error=None,
        )
        await self._emit_learning_event(
            EventType.LEARNING_INSTALLED,
            run,
            {
                "name": skill_name,
                "version": metadata.get("version", "0.1.0"),
                "reply": reply,
                "files": exec_result.get("files", []),
                "resumed": resumed,
            },
        )

        return {
            "reply": reply,
            "files": exec_result.get("files", []),
            "learned": True,
            "skill_name": skill_name,
            "learning_run_id": run.run_id,
            "schedule_created": run.schedule_created,
        }

    async def _on_approval_resolved(self, event) -> None:
        approval_id = event.payload.get("approval_id")
        if not approval_id:
            return
        pending_runs = getattr(self, "_pending_approval_runs", {})
        run_id = pending_runs.pop(approval_id, None)
        store = getattr(self, "_learning_store", None)
        if not run_id and store:
            run = store.find_by_approval(approval_id)
            run_id = run.run_id if run else None
        if not run_id or not store:
            return

        run = store.get(run_id)
        if not run:
            return

        run.approval_status = str(event.payload.get("status") or "")
        if not event.payload.get("approved"):
            await self._fail_run(
                run,
                f"❌ 学习安装审批未通过：{event.payload.get('reason') or run.approval_status}",
                str(event.payload.get("reason") or run.approval_status or "approval_rejected"),
            )
            return

        task = Task(
            conversation_id=run.conversation_id,
            user_id=run.user_id,
            origin_message=run.request_text,
            skill_name=run.skill_name or "learning",
            context={"learning_run_id": run.run_id},
        )
        asyncio.create_task(self._install_and_execute_learning_run(run, task, run.request_text, resumed=True))

    async def _try_reuse_experience(
        self,
        task: Task,
        user_message: str,
        run: LearningRun,
        experience: dict[str, Any],
    ) -> dict[str, Any] | None:
        skill_name = str(experience.get("skill_name") or "")
        if not skill_name or not self._vault or not self._vault.get_skill_runner(skill_name):
            return None

        run.skill_name = skill_name
        run.handling_outcome = "reused"
        run.decision_success = True
        run.execution_attempted = True
        run.execution_success = False
        self._save_run(run, status=LearningRunStatus.EXECUTING)
        exec_result = await self._execute_learned_skill(task, skill_name, user_message)
        if not exec_result.get("success"):
            run.failure_code = self._classify_failure_code(str(exec_result.get("error") or "experience_execute_failed"))
            return None

        reply = f"📋 复用已有学习经验，直接使用技能「{skill_name}」。\n\n{exec_result.get('reply', '')}"
        run.execution_result = exec_result
        run.execution_success = True
        run.clarification_reason = None
        score, detail = evaluate_quality(
            run.scenario_id,
            analysis=run.analysis,
            validation=run.validation,
            execution_result=exec_result,
        )
        run.quality_score = score
        run.artifacts["quality"] = detail
        run.first_pass = True
        run.final_success = True
        run.failure_code = None
        self._save_run(run, status=LearningRunStatus.SUCCEEDED, reply=reply, error=None)
        return {
            "reply": reply,
            "files": exec_result.get("files", []),
            "learned": False,
            "skill_name": skill_name,
            "learning_run_id": run.run_id,
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
        if result.get("learned") and self._scheduler and not result.get("schedule_created"):
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
        normalized = normalize_intent_phrase(message)
        reader: MemCoreReader | None = None
        try:
            reader = MemCoreReader(self._config)
            await reader.init()
            results = await reader.search("learning_engine", normalized or message)
            if results:
                top = results[0]
                payload = top.value
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        payload = {"raw": top.value}
                return {"key": top.key, "value": top.value, **(payload if isinstance(payload, dict) else {})}
        except Exception as e:
            logger.warning("experience_search_failed", error=str(e))
        finally:
            if reader is not None:
                await reader.close()
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

    async def _install_dependencies(self, deps: list[str], *, force: bool = False) -> dict:
        """自动 pip install 缺失依赖"""
        if not deps:
            return {"success": True}

        if not force and not self._auto_install:
            return {
                "success": False,
                "error": "dependency_install_requires_post_approval",
            }

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
            code = self._extract_python_code(text)
            if code:
                return code
        except Exception as e:
            logger.warning("skill_generation_failed", error=str(e))

        return self._template_skill(analysis)

    @staticmethod
    def _extract_python_code(text: str) -> str | None:
        value = str(text or "").strip()
        if not value:
            return None
        match = re.search(r"```(?:python|py)?\s*\n([\s\S]*?)```", value, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        value = re.sub(r"^```(?:python|py)?\s*", "", value, flags=re.IGNORECASE).strip()
        value = re.sub(r"\s*```$", "", value).strip()
        if "def main" in value or "if __name__" in value or value.startswith("import "):
            return value
        return None

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
            artifacts = result.get("artifacts", {})

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
                    "artifacts": artifacts,
                }

            return {
                "success": True,
                "reply": reply,
                "files": files,
                "debug_info": debug_info,
                "artifacts": artifacts,
            }

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
        current_code: str | None = None,
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
        current_code = current_code or self._read_skill_code(skill_name)
        if not current_code:
            return None

        # 常见错误的快速修复（不依赖 LLM）
        quick_fix = await self._quick_fix(error_msg, current_code)
        if quick_fix:
            logger.info("skill_quick_fixed", skill=skill_name, error=error_msg)
            return quick_fix

        actual_reply_section = ""
        if actual_reply:
            actual_reply_section = f"""\
Skill 实际输出（reply）：
{actual_reply[:500]}

⚠️ 注意：Skill 代码没有抛异常，但输出内容表明执行结果不正确。
请分析实际输出，找出根因（如 API 返回了错误状态、接口已废弃、数据解析逻辑有误等），然后修复代码。
"""

        debug_section = ""
        if debug_info:
            debug_section = f"""\
🔍 Skill _debug 诊断信息（API 原始返回）：
{json.dumps(debug_info, ensure_ascii=False, indent=2)[:800]}

⚠️ 这是 Skill 代码记录的 API 原始响应，是诊断根因的关键线索！
请根据此信息判断：API 是否已废弃/变更？是否需要换接口？数据解析逻辑是否正确？
"""

        prompt = f"""以下 Skill 代码执行出错，请修复。

Skill 名称：{skill_name}
用户需求：{user_message}

当前代码：
```python
{current_code}
```

执行错误：
{error_msg}
{actual_reply_section}
{debug_section}
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
            code = self._extract_python_code(text)
            if code:
                return code
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

        last_reply_section = ""
        if last_reply:
            last_reply_section = f"""\
- Skill 实际输出（reply）：
{last_reply[:500]}

⚠️ 请仔细分析实际输出，判断根因：
  - 如果输出是"未能获取/暂无数据"等 → 大概率是 API 接口已失效/废弃，需要换一个不同的 API
  - 如果输出包含鉴权/签名错误 → 该 API 需要认证，换不需要认证的公开接口
  - 如果输出包含风控/验证码 → 该站点有反爬，考虑换数据源或用浏览器方式
"""

        debug_section = ""
        if debug_info:
            debug_section = f"""\
- 🔍 Skill _debug 诊断信息（API 原始返回）：
{json.dumps(debug_info, ensure_ascii=False, indent=2)[:800]}

⚠️ 这是关键诊断线索！根据 API 原始返回判断：
  - 如果 API 返回错误码/异常状态 → 该接口可能已废弃或变更，必须换不同的 API
  - 如果 API 返回空数据 → 可能是接口参数变更或需要鉴权
  - 如果 API 返回正常但解析失败 → 可以只修代码，不必换方案
"""

        prompt = f"""之前的方案执行失败，需要重新选择实现方案。

用户需求：{user_message}

之前尝试的方案：
- Skill 名称：{old_skill_name}
- 实现思路：{old_approach}
- 失败记录：
{errors_summary}
{last_reply_section}
{debug_section}
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

            # 策略1: requests → httpx（项目已有依赖，API 更优）
            if missing_pkg == "requests":
                logger.info("quick_fix_requests_to_httpx")
                return self._replace_requests_with_httpx(code)

            logger.info("quick_fix_dependency_deferred", package=pip_name)

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
            normalized_key = normalize_intent_phrase(key) or key
            await self._memcore_writer.write(
                MemoryEntry(
                    user_id="learning_engine",
                    key=f"learned:{skill_name}:{normalized_key}",
                    value=value,
                    confidence=0.8,
                    source="learning_engine",
                )
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

    def _quality_threshold(self, scenario_id: str | None, *, stage: str) -> float:
        base = 0.55 if stage == "validating" else 0.60
        if scenario_id == "login_form_chain":
            return base + 0.08
        if scenario_id == "remote_exec":
            return base + 0.05
        return base

    def _classify_failure_code(self, error: str) -> str:
        msg = str(error or "").lower()
        if not msg:
            return "unknown_error"
        if "quality_gate_failed" in msg:
            return "quality_gate_failed"
        if "metadata_invalid" in msg:
            return "metadata_invalid"
        if "compile_failed" in msg or "syntaxerror" in msg:
            return "compile_failed"
        if "no module named" in msg or "dependency" in msg or "pip" in msg:
            return "dependency_missing"
        if "timeout" in msg or "timed out" in msg:
            return "timeout"
        if any(tag in msg for tag in ("401", "403", "unauthorized", "forbidden", "鉴权", "登录")):
            return "auth_failed"
        if any(tag in msg for tag in ("空壳输出", "暂无数据", "未获取到", "hollow")):
            return "data_hollow"
        if any(tag in msg for tag in ("protocol_smoke_failed", "json", "parse", "解析")):
            return "parse_failed"
        if any(tag in msg for tag in ("network", "connect", "dns", "connection")):
            return "network_failed"
        if "approval" in msg or "审批" in msg:
            return "approval_rejected"
        return "unknown_error"

    def _repair_hint_for_failure(self, failure_code: str | None) -> str:
        hints = {
            "parse_failed": "修复方向：确保 stdout 只输出 JSON 且包含 reply 字段，避免混入日志文本。",
            "auth_failed": "修复方向：目标站点可能需要登录态/鉴权，优先改成 CDP 登录态或可公开访问数据源。",
            "data_hollow": "修复方向：当前解析逻辑只拿到默认值，检查选择器/API 字段并增加空数据兜底。",
            "dependency_missing": "修复方向：尽量使用项目已有依赖（httpx、bs4 等），并在代码注释里声明 # requires。",
            "network_failed": "修复方向：增加重试和超时保护，失败时返回可解释错误而不是空结果。",
            "timeout": "修复方向：降低单次请求耗时、拆分步骤、必要时调整等待策略。",
            "quality_gate_failed": "修复方向：补齐结构化字段、来源说明、数据新鲜度信息，确保结果可解释。",
        }
        return hints.get(str(failure_code or ""), "")

    def _maybe_mark_new_skill_candidate(self, run: LearningRun, failure_code: str | None) -> dict[str, Any] | None:
        store = getattr(self, "_learning_store", None)
        if not store or not failure_code:
            return None
        clusters = store.top_failure_clusters(days=7, limit=20)
        hit = next((item for item in clusters if item.get("failure_code") == failure_code), None)
        if not hit:
            return None
        failures = int(hit.get("failures", 0)) + 1  # + 当前 run
        conversations = int(hit.get("conversations", 0))
        if failures < 5 or conversations < 2:
            return None
        return {
            "failure_code": failure_code,
            "failures_7d": failures,
            "conversations_7d": conversations,
            "triggered": True,
            "reason": "同类失败在 7 天内达到新增 Skill 候选阈值（>=5 且跨>=2会话）",
            "suggested_skill_name": run.skill_name or f"candidate_{failure_code}",
        }

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
                lines.append("   代码生成失败")
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
