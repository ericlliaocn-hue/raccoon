"""测试 SkillSession + FlowEngine 多轮交互流程"""

import asyncio
import pytest

from src.types import (
    FlowDefinition,
    FlowStep,
    FlowStepType,
    MessageSource,
    RouteResult,
    RouteType,
    SessionStatus,
    SkillSession,
    SkillMetadata,
    EventType,
)
from src.executor.skill_session_manager import SkillSessionManager


# ─── 测试用 Flow 定义 ──────────────────────────────────────────

SAMPLE_FLOW = FlowDefinition(
    steps=[
        FlowStep(
            name="选择风格",
            type=FlowStepType.USER_CHOICE,
            prompt="请选择画面风格：",
            options=["写实", "动漫", "水彩"],
        ),
        FlowStep(
            name="润色提示词",
            type=FlowStepType.LLM,
            prompt="根据用户描述和风格润色提示词",
            llm_system_prompt="你是提示词专家",
        ),
        FlowStep(
            name="输入描述",
            type=FlowStepType.USER_INPUT,
            prompt="请描述你想要的画面：",
        ),
        FlowStep(
            name="生成图片",
            type=FlowStepType.SKILL_EXEC,
            skill_action="generate",
            background=False,
        ),
    ],
    on_complete="🎨 完成！",
    on_cancel="已取消",
)

BACKGROUND_FLOW = FlowDefinition(
    steps=[
        FlowStep(
            name="选择风格",
            type=FlowStepType.USER_CHOICE,
            prompt="选择风格：",
            options=["写实", "动漫"],
        ),
        FlowStep(
            name="生成视频",
            type=FlowStepType.SKILL_EXEC,
            skill_action="generate_video",
            background=True,
        ),
    ],
    on_complete="🎬 视频完成！",
)


# ─── SkillSessionManager 测试 ──────────────────────────────────

class TestSkillSessionManager:

    def setup_method(self):
        self.manager = SkillSessionManager()

    def test_create_session(self):
        session = self.manager.create_session(
            conversation_id="conv-1",
            skill_name="image_gen",
            flow=SAMPLE_FLOW,
        )
        assert session.conversation_id == "conv-1"
        assert session.skill_name == "image_gen"
        assert session.status == SessionStatus.ACTIVE
        assert len(session.flow.steps) == 4

    def test_get_session(self):
        self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        session = self.manager.get_session("conv-1")
        assert session is not None
        assert session.skill_name == "image_gen"

    def test_get_session_not_found(self):
        assert self.manager.get_session("nonexistent") is None

    def test_should_intercept(self):
        self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        assert self.manager.should_intercept("conv-1") is True
        assert self.manager.should_intercept("conv-2") is False

    def test_intercept_route(self):
        self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        route = self.manager.intercept_route("conv-1", "写实风格")
        assert route.route_type == RouteType.SKILL_SESSION
        assert route.skill_name == "image_gen"
        assert route.params["original_text"] == "写实风格"

    def test_advance_step(self):
        self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        next_step = self.manager.advance_step("conv-1")
        assert next_step is not None
        assert next_step.name == "润色提示词"

    def test_advance_step_beyond_end(self):
        session = self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        session.current_step_index = 3  # 最后一步
        result = self.manager.advance_step("conv-1")
        assert result is None

    def test_get_current_step(self):
        self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        step = self.manager.get_current_step("conv-1")
        assert step.name == "选择风格"

    def test_update_state(self):
        self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        self.manager.update_state("conv-1", "style", "写实")
        session = self.manager.get_session("conv-1")
        assert session.state["style"] == "写实"

    def test_complete_session(self):
        self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        self.manager.complete_session("conv-1", {"result": "ok"})
        assert self.manager.get_session("conv-1") is None  # completed 不算活跃

    def test_cancel_session(self):
        self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        self.manager.cancel_session("conv-1", "user_cancel")
        assert self.manager.get_session("conv-1") is None  # cancelled 不算活跃

    def test_release_to_background(self):
        self.manager.create_session("conv-1", "image_gen", BACKGROUND_FLOW)
        self.manager.release_to_background("conv-1", "task-123")
        # background 不算活跃会话
        assert self.manager.get_session("conv-1") is None
        # 但可以获取 background 会话
        bg = self.manager.get_background_session("conv-1")
        assert bg is not None
        assert bg.status == SessionStatus.BACKGROUND
        assert bg.task_id == "task-123"

    def test_set_processing_and_active(self):
        self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        self.manager.set_processing("conv-1")
        session = self.manager.get_session("conv-1")
        assert session.status == SessionStatus.PROCESSING
        self.manager.set_active("conv-1")
        session = self.manager.get_session("conv-1")
        assert session.status == SessionStatus.ACTIVE

    def test_overwrite_existing_session(self):
        self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        self.manager.create_session("conv-1", "video_gen", BACKGROUND_FLOW)
        session = self.manager.get_session("conv-1")
        assert session.skill_name == "video_gen"

    def test_list_active_sessions(self):
        self.manager.create_session("conv-1", "image_gen", SAMPLE_FLOW)
        self.manager.create_session("conv-2", "video_gen", BACKGROUND_FLOW)
        active = self.manager.list_active_sessions()
        assert len(active) == 2


# ─── FlowStep 类型测试 ──────────────────────────────────────────

class TestFlowStepTypes:

    def test_flow_step_types(self):
        assert FlowStepType.LLM == "llm"
        assert FlowStepType.USER_CHOICE == "user_choice"
        assert FlowStepType.SKILL_EXEC == "skill_exec"
        assert FlowStepType.USER_INPUT == "user_input"

    def test_message_source_types(self):
        assert MessageSource.LLM == "llm"
        assert MessageSource.SKILL == "skill"
        assert MessageSource.FLOW_LLM == "flow_llm"
        assert MessageSource.BACKGROUND == "background"

    def test_route_type_skill_session(self):
        assert RouteType.SKILL_SESSION == "skill_session"

    def test_event_type_session(self):
        assert EventType.SESSION_STARTED == "session_started"
        assert EventType.SESSION_STEP == "session_step"
        assert EventType.SESSION_ENDED == "session_ended"


# ─── SkillMetadata interactive 测试 ─────────────────────────────

class TestSkillMetadataInteractive:

    def test_non_interactive_skill(self):
        meta = SkillMetadata(name="echo", version="0.1.0")
        assert meta.interactive is False
        assert meta.flow is None

    def test_interactive_skill_with_flow(self):
        meta = SkillMetadata(
            name="image_gen",
            version="1.0.0",
            interactive=True,
            flow=SAMPLE_FLOW,
        )
        assert meta.interactive is True
        assert meta.flow is not None
        assert len(meta.flow.steps) == 4

    def test_skill_session_model(self):
        session = SkillSession(
            conversation_id="conv-1",
            skill_name="image_gen",
            flow=SAMPLE_FLOW,
        )
        assert session.current_step_index == 0
        assert session.status == SessionStatus.ACTIVE
        assert isinstance(session.state, dict)
