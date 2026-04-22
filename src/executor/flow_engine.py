"""FlowEngine：流程编排引擎

核心职责：
1. 按定义的 Flow 步骤顺序执行
2. LLM 作为受控步骤参与（不是自由回复）
3. 每步结果推送到对话，用户可查看中间结果
4. background 步骤释放会话，后台完成后推送结果
5. 步骤间通过 session.state 传递数据

执行模式：
- llm: 调用 LLM 生成内容（如润色提示词），结果写入 state
- user_choice: 展示选项，等待用户选择，结果写入 state
- user_input: 等待用户自由输入，结果写入 state
- skill_exec: 执行 Skill 代码，结果写入 state；background=True 时释放会话
"""

from __future__ import annotations

import asyncio
from typing import Any, TYPE_CHECKING

import structlog

from src.config import RaccoonConfig
from src.eventbus.bus import EventBus
from src.executor.media_pusher import MediaPusher
from src.executor.file_manager import FileManager
from src.executor.skill_session_manager import SkillSessionManager
from src.executor.task_queue import TaskQueue
from src.executor.task_state_machine import TaskStateMachine
from src.llm import LLMFactory, LLMClient
from src.types import (
    Event,
    EventType,
    FlowDefinition,
    FlowStep,
    FlowStepType,
    MessageSource,
    RouteResult,
    RouteType,
    SessionStatus,
    SkillSession,
    Task,
    TaskStatus,
    make_event,
)

if TYPE_CHECKING:
    from src.skill_vault.vault_manager import VaultManager

logger = structlog.get_logger(__name__)


class FlowEngine:
    """流程编排引擎：按 Flow 定义执行步骤，LLM 是受控步骤"""

    def __init__(
        self,
        event_bus: EventBus,
        session_manager: SkillSessionManager,
        vault_manager: "VaultManager",
        config: RaccoonConfig | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._session_manager = session_manager
        self._vault_manager = vault_manager
        self._config = config or RaccoonConfig()
        self._llm: LLMClient | None = None
        self._file_manager = FileManager()
        self._media_pusher = MediaPusher(self._file_manager)
        self._task_queue = TaskQueue(self._config)
        self._state_machine = TaskStateMachine()
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent_tasks)

    def _get_llm(self) -> LLMClient:
        """延迟初始化 LLM 客户端"""
        if self._llm is None:
            self._llm = LLMFactory.create(self._config)
        return self._llm

    # ─── 启动流程 ─────────────────────────────────────────────

    async def start_flow(
        self,
        conversation_id: str,
        skill_name: str,
        flow: FlowDefinition,
        initial_params: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
    ) -> str:
        """启动一个流程，返回欢迎消息"""
        session = self._session_manager.create_session(
            conversation_id=conversation_id,
            skill_name=skill_name,
            flow=flow,
            timeout_seconds=timeout_seconds,
        )

        # 注入初始参数到 state
        if initial_params:
            session.state.update(initial_params)

        # 发射会话开始事件
        await self._emit_session_event(
            conversation_id, EventType.SESSION_STARTED,
            MessageSource.SKILL,
            payload={
                "session_id": session.session_id,
                "skill_name": skill_name,
                "total_steps": len(flow.steps),
            },
        )

        # 执行第一步
        if flow.steps:
            return await self._execute_step(conversation_id, flow.steps[0])

        # 无步骤，直接完成
        self._session_manager.complete_session(conversation_id)
        return flow.on_complete or "流程已完成"

    # ─── 处理用户输入 ─────────────────────────────────────────

    async def handle_session_input(
        self,
        conversation_id: str,
        user_text: str,
    ) -> str:
        """处理会话中的用户输入"""
        session = self._session_manager.get_session(conversation_id)
        if not session:
            return "⚠️ 没有活跃的会话"

        # ── 登录流程中的用户输入：转发给 Skill 处理 ──
        if session.login_step:
            return await self._handle_login_input(conversation_id, session, user_text)

        current_step = self._session_manager.get_current_step(conversation_id)
        if not current_step:
            # 所有步骤完成
            self._session_manager.complete_session(conversation_id)
            return session.flow.on_complete or "流程已完成"

        # 根据当前步骤类型处理用户输入
        if current_step.type == FlowStepType.USER_CHOICE:
            # 解析用户选择
            choice = self._parse_choice(user_text, current_step.options)
            self._session_manager.update_state(conversation_id, f"step_{current_step.step_id}_choice", choice)
            self._session_manager.update_state(conversation_id, current_step.step_id, choice)

            # 推送用户选择确认
            await self._emit_session_event(
                conversation_id, EventType.SESSION_STEP,
                MessageSource.SKILL,
                payload={
                    "step_name": current_step.name,
                    "user_choice": choice,
                    "step_type": "user_choice",
                },
            )

        elif current_step.type == FlowStepType.USER_INPUT:
            # 保存用户自由输入
            self._session_manager.update_state(conversation_id, f"step_{current_step.step_id}_input", user_text)
            self._session_manager.update_state(conversation_id, current_step.step_id, user_text)

        elif current_step.type == FlowStepType.LLM:
            # LLM 步骤不需要用户输入，但用户可以在 LLM 输出后修改
            self._session_manager.update_state(conversation_id, f"step_{current_step.step_id}_feedback", user_text)

        elif current_step.type == FlowStepType.SKILL_EXEC:
            # Skill 执行步骤中用户输入可能是参数补充
            self._session_manager.update_state(conversation_id, f"step_{current_step.step_id}_input", user_text)

        # 推进到下一步
        next_step = self._session_manager.advance_step(conversation_id)
        self._session_manager.set_active(conversation_id)

        if next_step:
            return await self._execute_step(conversation_id, next_step)

        # 所有步骤完成
        self._session_manager.complete_session(conversation_id)
        await self._emit_session_event(
            conversation_id, EventType.SESSION_ENDED,
            MessageSource.SKILL,
            payload={"session_id": session.session_id, "status": "completed"},
        )
        return session.flow.on_complete or "流程已完成"

    async def _handle_login_input(
        self,
        conversation_id: str,
        session: SkillSession,
        user_text: str,
    ) -> str:
        """处理登录流程中的用户输入（手机号/验证码）

        将用户输入转发给 Skill，由 Skill 的 CDP 逻辑完成浏览器操作。
        """
        import re

        # 判断用户输入是手机号还是验证码
        phone = re.sub(r'\D', '', user_text.strip())
        is_phone = bool(re.match(r'^1\d{10}$', phone))
        is_code = bool(re.match(r'^\d{4,6}$', user_text.strip()))

        # 构建 Skill 执行参数
        if session.login_step == "input_phone" and is_phone:
            action = "fill_phone"
            skill_params = {
                "action": "fill_phone",
                "phone": phone,
                "state": session.state,
                "step_id": "login",
                "step_name": "登录",
            }
        elif session.login_step == "input_code" and is_code:
            action = "fill_code"
            skill_params = {
                "action": "fill_code",
                "code": user_text.strip(),
                "state": session.state,
                "step_id": "login",
                "step_name": "登录",
                "pending_prompt": session.state.get("pending_prompt", ""),
                "pending_params": session.state.get("pending_params", {}),
            }
        else:
            # 无法识别的输入
            if session.login_step == "input_phone":
                return '请输入有效的手机号（1开头的11位数字），或输入"取消"退出流程'
            else:
                return '请输入4-6位数字验证码，或输入"取消"退出流程'

        # 执行 Skill 处理登录
        self._session_manager.set_processing(conversation_id)

        try:
            skill_runner = self._vault_manager.get_skill_runner(session.skill_name)
            if skill_runner is None:
                raise RuntimeError(f"Skill not found: {session.skill_name}")

            task = Task(
                conversation_id=conversation_id,
                user_id="flow_engine",
                origin_message=f"login:{action}",
                skill_name=session.skill_name,
            )

            result = await asyncio.wait_for(
                skill_runner.run(task, skill_params),
                timeout=self._config.task_timeout_seconds,
            )

            # 推送媒体文件
            files_info = await self._media_pusher.push(task, conversation_id)

            if isinstance(result, dict):
                result_text = result.get("reply", "")
                need_login = result.get("need_login", False)
                new_login_step = result.get("login_step", "")

                if need_login:
                    # 登录未完成，更新 login_step 继续等待
                    session.login_step = new_login_step
                    self._session_manager.set_active(conversation_id)

                    await self._emit_session_event(
                        conversation_id, EventType.SESSION_STEP,
                        MessageSource.SKILL,
                        payload={
                            "step_name": "登录",
                            "step_type": "skill_exec",
                            "result": result_text,
                            "need_login": True,
                            "login_step": new_login_step,
                            "files": [f.model_dump() for f in files_info] if files_info else [],
                        },
                    )
                    return result_text

                else:
                    # 登录成功！清除 login_step，继续执行后续流程步骤
                    session.login_step = ""
                    self._session_manager.set_active(conversation_id)

                    # 如果登录成功后 Skill 直接返回了图片结果，直接完成
                    if result.get("image_url") or result.get("files"):
                        await self._emit_session_event(
                            conversation_id, EventType.SESSION_STEP,
                            MessageSource.SKILL,
                            payload={
                                "step_name": "生成图片",
                    "step_type": "skill_exec",
                    "result": result_text,
                    "files": [f.model_dump() for f in files_info] if files_info else [],
                    "image_url": result.get("image_url", "") if isinstance(result, dict) else "",
                    "video_url": result.get("video_url", "") if isinstance(result, dict) else "",
                },
            )
                        # 推进步骤
                        next_step = self._session_manager.advance_step(conversation_id)
                        if next_step:
                            return result_text + "\n\n---\n\n" + await self._execute_step(conversation_id, next_step)
                        # 所有步骤完成
                        self._session_manager.complete_session(conversation_id)
                        return result_text

                    # 登录成功但没有直接返回结果，继续当前 skill_exec 步骤
                    await self._emit_session_event(
                        conversation_id, EventType.SESSION_STEP,
                        MessageSource.SKILL,
                        payload={
                            "step_name": "登录",
                            "step_type": "skill_exec",
                            "result": result_text,
                            "files": [f.model_dump() for f in files_info] if files_info else [],
                        },
                    )

                    # 重新执行当前 skill_exec 步骤（此时已登录）
                    current_step = self._session_manager.get_current_step(conversation_id)
                    if current_step and current_step.type == FlowStepType.SKILL_EXEC:
                        return result_text + "\n\n---\n\n" + await self._execute_step(conversation_id, current_step)

                    # 推进到下一步
                    next_step = self._session_manager.advance_step(conversation_id)
                    if next_step:
                        return result_text + "\n\n---\n\n" + await self._execute_step(conversation_id, next_step)

                    self._session_manager.complete_session(conversation_id)
                    return result_text

            else:
                self._session_manager.set_active(conversation_id)
                return str(result)

        except asyncio.TimeoutError:
            self._session_manager.set_active(conversation_id)
            return '⚠️ 登录操作超时，请重试或输入"取消"退出流程'
        except Exception as e:
            logger.error("flow_login_step_failed", error=str(e))
            self._session_manager.set_active(conversation_id)
            return f'⚠️ 登录操作失败: {e}\n\n请重试或输入"取消"退出流程'

    # ─── 执行步骤 ─────────────────────────────────────────────

    async def _execute_step(self, conversation_id: str, step: FlowStep) -> str:
        """执行一个流程步骤"""
        session = self._session_manager.get_session(conversation_id)
        if not session:
            return "⚠️ 会话不存在"

        logger.info(
            "flow_step_executing",
            conversation_id=conversation_id,
            step_name=step.name,
            step_type=step.type.value,
        )

        if step.type == FlowStepType.USER_CHOICE:
            return await self._execute_user_choice(conversation_id, step)
        elif step.type == FlowStepType.USER_INPUT:
            return await self._execute_user_input(conversation_id, step)
        elif step.type == FlowStepType.LLM:
            return await self._execute_llm_step(conversation_id, step)
        elif step.type == FlowStepType.SKILL_EXEC:
            return await self._execute_skill_step(conversation_id, step)

        return f"未知步骤类型: {step.type}"

    async def _execute_user_choice(self, conversation_id: str, step: FlowStep) -> str:
        """执行用户选择步骤：展示选项，等待用户选择"""
        options_text = "\n".join(
            f"  {i+1}. {opt}" for i, opt in enumerate(step.options)
        )
        prompt = step.prompt or "请选择："
        msg = f"**{step.name}**\n\n{prompt}\n\n{options_text}\n\n_请输入序号或选项名称_"

        await self._emit_session_event(
            conversation_id, EventType.SESSION_STEP,
            MessageSource.SKILL,
            payload={
                "step_name": step.name,
                "step_type": "user_choice",
                "options": step.options,
                "prompt": step.prompt,
            },
        )
        return msg

    async def _execute_user_input(self, conversation_id: str, step: FlowStep) -> str:
        """执行用户输入步骤：等待用户自由输入"""
        prompt = step.prompt or "请输入："
        msg = f"**{step.name}**\n\n{prompt}"

        await self._emit_session_event(
            conversation_id, EventType.SESSION_STEP,
            MessageSource.SKILL,
            payload={
                "step_name": step.name,
                "step_type": "user_input",
                "prompt": step.prompt,
            },
        )
        return msg

    async def _execute_llm_step(self, conversation_id: str, step: FlowStep) -> str:
        """执行 LLM 步骤：LLM 作为受控步骤参与流程"""
        self._session_manager.set_processing(conversation_id)

        session = self._session_manager.get_session(conversation_id)
        if not session:
            return "⚠️ 会话不存在"

        # 构建 LLM 消息
        system_prompt = step.llm_system_prompt or (
            f"你是流程「{session.skill_name}」的步骤「{step.name}」的助手。"
            f"请根据以下上下文和用户要求，生成专业的内容。"
            f"直接输出结果，不要添加多余的解释。"
        )

        # 将 session.state 中的信息注入到用户消息中
        state_summary = self._format_state_for_llm(session.state)
        user_message = step.prompt or "请根据上下文生成内容"
        if state_summary:
            user_message = f"当前流程状态：\n{state_summary}\n\n任务：{user_message}"

        try:
            llm = self._get_llm()
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ]
            result = await llm.chat(
                messages,
                temperature=self._config.llm_temperature,
                max_tokens=self._config.llm_max_tokens,
            )

            # 将 LLM 结果写入 state
            self._session_manager.update_state(conversation_id, f"step_{step.step_id}_llm_result", result)
            self._session_manager.update_state(conversation_id, step.step_id, result)

            # 推送 LLM 结果
            await self._emit_session_event(
                conversation_id, EventType.SESSION_STEP,
                MessageSource.FLOW_LLM,
                payload={
                    "step_name": step.name,
                    "step_type": "llm",
                    "result": result,
                },
            )

            self._session_manager.set_active(conversation_id)

            # LLM 步骤完成后，自动推进到下一步
            next_step = self._session_manager.advance_step(conversation_id)
            if next_step:
                return f"**{step.name}** (LLM 生成)\n\n{result}\n\n---\n\n" + await self._execute_step(conversation_id, next_step)

            # 所有步骤完成
            self._session_manager.complete_session(conversation_id)
            await self._emit_session_event(
                conversation_id, EventType.SESSION_ENDED,
                MessageSource.SKILL,
                payload={"session_id": session.session_id, "status": "completed"},
            )
            return f"**{step.name}** (LLM 生成)\n\n{result}\n\n---\n\n{session.flow.on_complete or '流程已完成'}"

        except Exception as e:
            logger.error("flow_llm_step_failed", step=step.name, error=str(e))
            self._session_manager.set_active(conversation_id)
            cancel_hint = '请重试或输入"取消"退出流程'
            return f"⚠️ LLM 步骤「{step.name}」执行失败: {e}\n\n{cancel_hint}"

    async def _execute_skill_step(self, conversation_id: str, step: FlowStep) -> str:
        """执行 Skill 代码步骤"""
        session = self._session_manager.get_session(conversation_id)
        if not session:
            return "⚠️ 会话不存在"

        self._session_manager.set_processing(conversation_id)

        # 构建 Skill 执行参数
        skill_params = {
            "action": step.skill_action,
            "state": session.state,
            "step_id": step.step_id,
            "step_name": step.name,
        }

        # 自动从 state 中提取 LLM 步骤的输出作为 prompt，供 Skill 使用
        for key, value in session.state.items():
            if key.endswith("_llm_result") and isinstance(value, str) and value.strip():
                skill_params["prompt"] = value
                break

        # 自动从 state 中提取 user_choice / user_input 步骤的结果
        # key 格式: step_{step_id}_choice / step_{step_id}_input
        # 将结果注入 skill_params，key 为步骤名称（小写+下划线）
        for key, value in session.state.items():
            if key.endswith("_choice") and isinstance(value, str) and value.strip():
                # 找到对应的步骤名称
                for s in session.flow.steps:
                    if key.startswith(f"step_{s.step_id}_"):
                        param_key = s.name.lower().replace(" ", "_")
                        if param_key not in skill_params:
                            skill_params[param_key] = value
                        break
            elif key.endswith("_input") and isinstance(value, str) and value.strip():
                for s in session.flow.steps:
                    if key.startswith(f"step_{s.step_id}_"):
                        param_key = s.name.lower().replace(" ", "_")
                        if param_key not in skill_params:
                            skill_params[param_key] = value
                        break

        # 创建 Task
        task = Task(
            conversation_id=conversation_id,
            user_id="flow_engine",
            origin_message=f"flow_step:{step.name}",
            skill_name=session.skill_name,
        )
        self._task_queue.add(task)

        # 如果是后台步骤，释放会话
        if step.background:
            self._session_manager.release_to_background(conversation_id, task.task_id)
            # 异步执行后台任务
            asyncio_task = asyncio.create_task(
                self._execute_background_task(task, skill_params, conversation_id, step)
            )
            self._running_tasks[task.task_id] = asyncio_task

            await self._emit_session_event(
                conversation_id, EventType.SESSION_STEP,
                MessageSource.SKILL,
                payload={
                    "step_name": step.name,
                    "step_type": "skill_exec",
                    "background": True,
                    "task_id": task.task_id,
                },
            )
            return f"**{step.name}** 🔄 已转入后台执行\n\n任务 ID: `{task.task_id[:8]}`\n\n你可以继续对话，完成后会自动推送结果。"

        # 前台执行
        try:
            skill_runner = self._vault_manager.get_skill_runner(session.skill_name)
            if skill_runner is None:
                raise RuntimeError(f"Skill not found: {session.skill_name}")

            result = await asyncio.wait_for(
                skill_runner.run(task, skill_params),
                timeout=self._config.task_timeout_seconds,
            )

            # 将结果写入 state
            if isinstance(result, dict):
                session.state.update(result)
                self._session_manager.update_state(
                    conversation_id, f"step_{step.step_id}_result", result
                )

            # ── 检测 need_login：Skill 需要登录，暂停流程推进 ──
            if isinstance(result, dict) and result.get("need_login"):
                login_step = result.get("login_step", "input_phone")
                # 更新会话的 login_step 标记
                session.login_step = login_step
                # 保存 pending 信息到 state，供后续登录完成后继续
                if result.get("pending_prompt"):
                    self._session_manager.update_state(
                        conversation_id, "pending_prompt", result["pending_prompt"]
                    )
                if result.get("pending_params"):
                    self._session_manager.update_state(
                        conversation_id, "pending_params", result["pending_params"]
                    )

                # 推送媒体文件（登录页截图等）
                files_info = await self._media_pusher.push(task, conversation_id)

                # 推送步骤结果
                result_text = result.get("reply", "请完成登录后继续")

                await self._emit_session_event(
                    conversation_id, EventType.SESSION_STEP,
                    MessageSource.SKILL,
                    payload={
                        "step_name": step.name,
                        "step_type": "skill_exec",
                        "result": result_text,
                        "need_login": True,
                        "login_step": login_step,
                        "files": [f.model_dump() for f in files_info] if files_info else [],
                    },
                )

                # 保持会话 ACTIVE，不推进步骤，等待用户输入登录信息
                self._session_manager.set_active(conversation_id)
                logger.info(
                    "flow_skill_step_need_login",
                    step=step.name,
                    login_step=login_step,
                    conversation_id=conversation_id,
                )
                return result_text

            # 推送媒体文件
            files_info = await self._media_pusher.push(task, conversation_id)

            # 推送步骤结果
            result_text = ""
            if isinstance(result, dict):
                result_text = result.get("reply", result.get("output", str(result)))
            else:
                result_text = str(result)

            await self._emit_session_event(
                conversation_id, EventType.SESSION_STEP,
                MessageSource.SKILL,
                payload={
                    "step_name": step.name,
                    "step_type": "skill_exec",
                    "result": result_text if isinstance(result_text, str) else str(result_text),
                    "files": [f.model_dump() for f in files_info] if files_info else [],
                    "image_url": result.get("image_url", "") if isinstance(result, dict) else "",
                    "video_url": result.get("video_url", "") if isinstance(result, dict) else "",
                },
            )

            self._session_manager.set_active(conversation_id)

            # 前台步骤完成后，自动推进
            next_step = self._session_manager.advance_step(conversation_id)
            if next_step:
                prefix = f"**{step.name}** ✅ 完成\n\n"
                if files_info:
                    prefix += f"生成了 {len(files_info)} 个文件\n\n"
                prefix += "---\n\n"
                return prefix + await self._execute_step(conversation_id, next_step)

            # 所有步骤完成
            self._session_manager.complete_session(conversation_id)
            await self._emit_session_event(
                conversation_id, EventType.SESSION_ENDED,
                MessageSource.SKILL,
                payload={"session_id": session.session_id, "status": "completed"},
            )
            return f"**{step.name}** ✅ 完成\n\n{session.flow.on_complete or '流程已完成'}"

        except asyncio.TimeoutError:
            self._session_manager.set_active(conversation_id)
            cancel_hint = '请重试或输入"取消"退出流程'
            return f"⚠️ 步骤「{step.name}」执行超时，{cancel_hint}"
        except Exception as e:
            logger.error("flow_skill_step_failed", step=step.name, error=str(e))
            self._session_manager.set_active(conversation_id)
            cancel_hint = '请重试或输入"取消"退出流程'
            return f"⚠️ 步骤「{step.name}」执行失败: {e}\n\n{cancel_hint}"

    async def _execute_background_task(
        self,
        task: Task,
        skill_params: dict,
        conversation_id: str,
        step: FlowStep,
    ) -> None:
        """执行后台任务"""
        async with self._semaphore:
            try:
                self._state_machine.transition(task, TaskStatus.RUNNING)
                self._task_queue.update(task)

                skill_runner = self._vault_manager.get_skill_runner(task.skill_name)
                if skill_runner is None:
                    raise RuntimeError(f"Skill not found: {task.skill_name}")

                result = await asyncio.wait_for(
                    skill_runner.run(task, skill_params),
                    timeout=self._config.task_timeout_seconds,
                )

                self._state_machine.transition(task, TaskStatus.SUCCESS)
                task.result = result
                self._task_queue.update(task)

                # 推送媒体文件
                files_info = await self._media_pusher.push(task, conversation_id)

                # 发射 task_completed 事件（含 source=background）
                payload = (result or {}).copy() if isinstance(result, dict) else {"output": str(result)}
                payload["_source"] = MessageSource.BACKGROUND.value
                payload["_step_name"] = step.name
                if files_info:
                    payload["_files"] = files_info
                await self._event_bus.emit(
                    make_event(
                        EventType.TASK_COMPLETED,
                        conversation_id=conversation_id,
                        task_id=task.task_id,
                        skill_name=task.skill_name,
                        status=TaskStatus.SUCCESS,
                        payload=payload,
                    )
                )

                # 完成后台会话
                session = self._session_manager.get_background_session(conversation_id)
                if session:
                    self._session_manager.complete_session(conversation_id, result)

                # 推送会话结束事件
                await self._emit_session_event(
                    conversation_id, EventType.SESSION_ENDED,
                    MessageSource.BACKGROUND,
                    payload={
                        "session_id": session.session_id if session else "",
                        "status": "completed",
                        "step_name": step.name,
                        "task_id": task.task_id,
                    },
                )

            except Exception as e:
                self._state_machine.transition(task, TaskStatus.FAILED)
                task.error = str(e)
                self._task_queue.update(task)

                await self._event_bus.emit(
                    make_event(
                        EventType.TASK_FAILED,
                        conversation_id=conversation_id,
                        task_id=task.task_id,
                        skill_name=task.skill_name,
                        status=TaskStatus.FAILED,
                        payload={"error": str(e), "_source": MessageSource.BACKGROUND.value},
                    )
                )

                # 后台失败也结束会话
                session = self._session_manager.get_background_session(conversation_id)
                if session:
                    self._session_manager.complete_session(conversation_id)

            finally:
                self._running_tasks.pop(task.task_id, None)

    # ─── 取消流程 ─────────────────────────────────────────────

    async def cancel_flow(self, conversation_id: str) -> str:
        """取消当前流程"""
        session = self._session_manager.get_session(conversation_id)
        if not session:
            return "没有活跃的会话"

        cancel_msg = session.flow.on_cancel or "流程已取消"
        self._session_manager.cancel_session(conversation_id, reason="user_cancel")

        await self._emit_session_event(
            conversation_id, EventType.SESSION_ENDED,
            MessageSource.SKILL,
            payload={"session_id": session.session_id, "status": "cancelled"},
        )
        return cancel_msg

    # ─── 辅助方法 ─────────────────────────────────────────────

    def _parse_choice(self, user_text: str, options: list[str]) -> str:
        """解析用户选择"""
        text = user_text.strip()

        # 尝试数字序号
        try:
            idx = int(text) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass

        # 尝试直接匹配选项
        for opt in options:
            if text.lower() == opt.lower():
                return opt

        # 模糊匹配
        for opt in options:
            if text.lower() in opt.lower() or opt.lower() in text.lower():
                return opt

        # 无法识别，返回原始输入
        return text

    def _format_state_for_llm(self, state: dict[str, Any]) -> str:
        """将 session state 格式化为 LLM 可理解的上下文"""
        lines = []
        for key, value in state.items():
            if key.startswith("_"):
                continue
            lines.append(f"- {key}: {value}")
        return "\n".join(lines) if lines else ""

    async def _emit_session_event(
        self,
        conversation_id: str,
        event_type: EventType,
        source: MessageSource,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """发射会话事件"""
        event = make_event(
            event_type=event_type,
            conversation_id=conversation_id,
            payload={**(payload or {}), "source": source.value},
        )
        await self._event_bus.emit(event)
