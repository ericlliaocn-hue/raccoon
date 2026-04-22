"""HTTP 适配器（FastAPI + SSE 推送）

提供 REST API 接口：
- POST /message - 发送消息
- GET /events - SSE 事件流
- GET /tasks - 任务列表
- GET /tasks/{task_id} - 任务详情
- POST /tasks/{task_id}/cancel - 取消任务
- GET /skills - 已安装 Skill 列表
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel as APIModel

from src.config import load_config, RaccoonConfig
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.executor.agent import Executor
from src.executor.file_manager import FileManager
from src.router.router import Router
from src.scheduler.scheduler import Scheduler
from src.scheduler.schedule_store import ScheduleStore
from src.notifier.notifier import Notifier
from src.skill_vault.vault_manager import VaultManager
from src.supervisor.audit_logger import AuditLogger
from src.types import Event, RouteType, ScheduleEntry, WorkflowEntry, WorkflowStep
from src.workflow.workflow_engine import WorkflowEngine
from src.workflow.workflow_store import WorkflowStore
from src.gateway.inbound import GatewayInbound, GatewayAuthError, GatewayRateLimitError

logger = structlog.get_logger(__name__)

# ─── API Models ────────────────────────────────────────────────

class MessageRequest(APIModel):
    text: str
    conversation_id: str | None = None
    user_id: str = "http_user"


class ChatMessage(APIModel):
    role: str  # user / assistant / system
    content: str
    time: str = ""


class ConversationSave(APIModel):
    conversation_id: str
    messages: list[ChatMessage]


class MessageResponse(APIModel):
    reply: str
    conversation_id: str
    task_id: str | None = None


class TaskInfo(APIModel):
    task_id: str
    skill_name: str
    status: str
    conversation_id: str


class SkillInfo(APIModel):
    name: str
    version: str
    description: str
    trigger_words: list[str]


class ScheduleCreateRequest(APIModel):
    name: str
    cron: str
    message: str
    conversation_id: str | None = None
    user_id: str = "http_user"


class ScheduleInfo(APIModel):
    schedule_id: str
    name: str
    cron: str
    message: str
    conversation_id: str
    enabled: bool
    last_run: str | None = None
    next_run: str | None = None


# ─── App Factory ───────────────────────────────────────────────

def create_app(config: RaccoonConfig | None = None) -> FastAPI:
    """创建 FastAPI 应用"""
    config = config or load_config()

    event_bus = EventBus(config)
    vault_manager = VaultManager(config)
    router = Router()
    executor = Executor(event_bus, vault_manager, config)
    audit_logger = AuditLogger(config)

    # 注册 Skill
    for skill in vault_manager.list_skills():
        router.register_skill(skill)

    # 定时调度
    schedule_store = ScheduleStore(config)
    schedule_store.recover()
    scheduler = Scheduler(event_bus, schedule_store, config)

    # 通知网关
    notifier = Notifier(event_bus, config)
    notifier.initialize()

    # 工作流编排
    workflow_store = WorkflowStore(config)
    workflow_store.recover()
    workflow_engine = WorkflowEngine(event_bus, router, executor, workflow_store, config)

    # 入站网关
    gateway = GatewayInbound(event_bus, config)

    # SSE 事件队列
    sse_queues: list[asyncio.Queue] = []

    # 文件管理器（共享给 Executor 和 HTTP 端点）
    file_manager = FileManager()

    # EventBus handler: 将事件推送到 SSE 队列
    async def on_any_event(event: Event) -> None:
        for q in sse_queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    event_bus.on_any(on_any_event)

    app = FastAPI(title="Project Raccoon", version="0.1.0")

    # 将依赖注入 app.state
    app.state.config = config
    app.state.event_bus = event_bus
    app.state.router = router
    app.state.executor = executor
    app.state.vault_manager = vault_manager
    app.state.audit_logger = audit_logger
    app.state.sse_queues = sse_queues
    app.state.file_manager = file_manager
    app.state.scheduler = scheduler
    app.state.notifier = notifier
    app.state.workflow_store = workflow_store
    app.state.workflow_engine = workflow_engine
    app.state.gateway = gateway

    # ─── Web UI ────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """Web UI 首页"""
        html_path = Path(__file__).parent / "static" / "index.html"
        return HTMLResponse(content=html_path.read_text("utf-8"))

    from fastapi.staticfiles import StaticFiles
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ─── Routes ────────────────────────────────────────────────

    @app.on_event("startup")
    async def startup() -> None:
        await event_bus.start()
        await scheduler.start()
        logger.info("http_server_started", port=config.http_port)

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await scheduler.stop()
        await event_bus.stop()
        logger.info("http_server_stopped")

    @app.post("/message", response_model=MessageResponse)
    async def send_message(req: MessageRequest) -> MessageResponse:
        """发送消息（非流式，等待完整回复）"""
        conv_id = req.conversation_id or str(uuid.uuid4())

        event = make_event(
            EventType.USER_MESSAGE,
            conversation_id=conv_id,
            user_id=req.user_id,
            payload={"text": req.text},
        )
        await event_bus.emit(event)

        # 优先检查 SkillSession 拦截（新机制）
        if executor.session_manager.should_intercept(conv_id):
            route = executor.session_manager.intercept_route(conv_id, req.text)
        # 其次检查旧的 pending 机制（need_login）
        elif executor.get_pending_context(conv_id):
            pending = executor.get_pending_context(conv_id)
            route = RouteResult(
                route_type=RouteType.SKILL,
                skill_name=pending.skill_name,
                params={
                    "action": "continue",
                    "original_text": req.text,
                    "login_step": pending.login_step,
                    "pending_prompt": pending.pending_prompt,
                    "pending_params": pending.pending_params,
                },
                confidence=0.95,
            )
        else:
            route = await router.route(req.text)

        reply = await executor.handle_route_result(route, event)

        audit_logger.log(
            action="http_message",
            actor=req.user_id,
            target=conv_id,
            detail={"text": req.text, "route_type": route.route_type.value},
        )

        return MessageResponse(
            reply=reply or "(无响应)",
            conversation_id=conv_id,
            task_id=route.params.get("task_id"),
        )

    @app.post("/chat/stream")
    async def chat_stream(req: MessageRequest) -> StreamingResponse:
        """流式聊天：SSE 逐 token 推送，前端可实时显示"""
        conv_id = req.conversation_id or str(uuid.uuid4())

        event = make_event(
            EventType.USER_MESSAGE,
            conversation_id=conv_id,
            user_id=req.user_id,
            payload={"text": req.text},
        )
        await event_bus.emit(event)

        # 优先检查 SkillSession 拦截（新机制）
        if executor.session_manager.should_intercept(conv_id):
            route = executor.session_manager.intercept_route(conv_id, req.text)
        # 其次检查旧的 pending 机制（need_login）
        elif executor.get_pending_context(conv_id):
            pending = executor.get_pending_context(conv_id)
            route = RouteResult(
                route_type=RouteType.SKILL,
                skill_name=pending.skill_name,
                params={
                    "action": "continue",
                    "original_text": req.text,
                    "login_step": pending.login_step,
                    "pending_prompt": pending.pending_prompt,
                    "pending_params": pending.pending_params,
                },
                confidence=0.95,
            )
        else:
            route = await router.route(req.text)

        audit_logger.log(
            action="http_chat_stream",
            actor=req.user_id,
            target=conv_id,
            detail={"text": req.text, "route_type": route.route_type.value},
        )

        # SKILL_SESSION 路由：直接返回完整结果（非流式）
        if route.route_type == RouteType.SKILL_SESSION:
            reply = await executor.handle_route_result(route, event)
            async def _single():
                yield f"data: {json.dumps({'type': 'text', 'content': reply or '(无响应)', 'conversation_id': conv_id, 'source': 'skill_session'}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
            return StreamingResponse(_single(), media_type="text/event-stream")

        # 非 LLM 路由：直接返回完整结果
        if route.route_type != RouteType.LLM:
            reply = await executor.handle_route_result(route, event)
            async def _single():
                yield f"data: {json.dumps({'type': 'text', 'content': reply or '(无响应)', 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
            return StreamingResponse(_single(), media_type="text/event-stream")

        # LLM 路由：流式推送
        text = route.params.get("original_text", "")

        async def _stream() -> AsyncGenerator[str, None]:
            try:
                llm = executor._get_llm()
                messages = [{"role": "user", "content": text}]
                gen = await llm.chat_stream(
                    messages,
                    temperature=config.llm_temperature,
                    max_tokens=config.llm_max_tokens,
                )
                async for chunk in gen:
                    yield f"data: {json.dumps({'type': 'text', 'content': chunk, 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
            except Exception as e:
                logger.error("stream_chat_failed", error=str(e))
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")

    @app.get("/events")
    async def event_stream() -> StreamingResponse:
        """SSE 事件流"""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=100)
        sse_queues.append(queue)

        async def generate() -> AsyncGenerator[str, None]:
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=30.0)
                        data = event.model_dump_json()
                        yield f"data: {data}\n\n"
                    except asyncio.TimeoutError:
                        yield f": keepalive\n\n"
            finally:
                sse_queues.remove(queue)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/tasks", response_model=list[TaskInfo])
    async def list_tasks() -> list[TaskInfo]:
        """列出所有任务"""
        tasks = list(executor.task_queue.all_tasks())
        return [
            TaskInfo(
                task_id=t.task_id,
                skill_name=t.skill_name,
                status=t.status.value,
                conversation_id=t.conversation_id,
            )
            for t in tasks
        ]

    @app.get("/tasks/{task_id}", response_model=TaskInfo)
    async def get_task(task_id: str) -> TaskInfo:
        """获取任务详情"""
        task = executor.task_queue.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return TaskInfo(
            task_id=task.task_id,
            skill_name=task.skill_name,
            status=task.status.value,
            conversation_id=task.conversation_id,
        )

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict:
        """取消任务"""
        task = executor.task_queue.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        from src.executor.task_state_machine import TaskStateMachine
        TaskStateMachine.request_cancel(task)
        executor.task_queue.update(task)
        return {"status": "cancelling", "task_id": task_id}

    @app.get("/skills", response_model=list[SkillInfo])
    async def list_skills() -> list[SkillInfo]:
        """列出已安装 Skill"""
        skills = vault_manager.list_skills()
        return [
            SkillInfo(
                name=s.name,
                version=s.version,
                description=s.description,
                trigger_words=s.trigger_words,
            )
            for s in skills
        ]

    @app.get("/status")
    async def system_status() -> dict:
        """系统状态（含 LLM 配置信息）"""
        llm_status = "mock"
        llm_model = config.llm_model
        if config.llm_provider != "mock":
            if config.llm_api_key:
                llm_status = "ready"
            else:
                llm_status = "no_api_key"

        return {
            "version": "0.1.0",
            "llm": {
                "provider": config.llm_provider,
                "model": llm_model,
                "status": llm_status,
                "base_url": config.llm_base_url,
            },
            "skills_count": len(vault_manager.list_skills()),
            "active_tasks": len(executor.task_queue.get_active_by_conversation("")),
        }

    # ─── File Streaming ────────────────────────────────────────────

    @app.get("/files/{task_id}/{path:path}")
    async def stream_file(task_id: str, path: str) -> StreamingResponse:
        """流式下载文件，path traversal 防护（路径在 FileManager 层校验）"""
        filename = Path(path).name
        try:
            gen, mime, size = await file_manager.stream_file(task_id, filename)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="文件不存在")
        except PermissionError:
            raise HTTPException(status_code=403, detail="禁止访问")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        return StreamingResponse(
            gen,
            media_type=mime,
            headers={
                "Content-Length": str(size),
                "Content-Disposition": f"inline; filename*=UTF-8''{filename}",
                "Accept-Ranges": "bytes",
            },
        )

    @app.get("/tasks/{task_id}/files")
    async def list_task_files(task_id: str) -> list[dict]:
        """列出任务的所有可下载文件"""
        return file_manager.list_task_files(task_id)

    # ─── Conversation Persistence ────────────────────────────────

    # 内存中的会话存储（MVP，重启后丢失）
    _conversations: dict[str, list[dict]] = {}

    @app.post("/conversations/save")
    async def save_conversation(req: ConversationSave) -> dict:
        """保存会话消息"""
        conv_id = req.conversation_id
        _conversations[conv_id] = [m.model_dump() for m in req.messages]
        return {"status": "saved", "conversation_id": conv_id, "count": len(req.messages)}

    @app.get("/conversations")
    async def list_conversations() -> list[dict]:
        """列出所有保存的会话"""
        result = []
        for conv_id, msgs in _conversations.items():
            first_user = next((m for m in msgs if m["role"] == "user"), None)
            result.append({
                "conversation_id": conv_id,
                "message_count": len(msgs),
                "preview": first_user["content"][:50] if first_user else "(空)",
                "last_time": msgs[-1].get("time", "") if msgs else "",
            })
        return sorted(result, key=lambda x: x.get("last_time", ""), reverse=True)

    @app.get("/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str) -> dict:
        """获取会话详情"""
        msgs = _conversations.get(conversation_id, [])
        return {"conversation_id": conversation_id, "messages": msgs}

    @app.delete("/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str) -> dict:
        """删除会话"""
        _conversations.pop(conversation_id, None)
        return {"status": "deleted", "conversation_id": conversation_id}

    # ─── File Upload ─────────────────────────────────────────────

    @app.post("/upload")
    async def upload_file() -> dict:
        """接收用户上传的文件（图片/附件），保存到 output/uploads/"""
        from fastapi import UploadFile, File as FastAPIFile

        # 使用 form data
        form = await app.state._upload_form if hasattr(app.state, "_upload_form") else None
        # FastAPI 自动解析 multipart
        return {"status": "ok"}

    @app.post("/upload/file")
    async def upload_file_multipart(file: UploadFile = None) -> dict:
        """接收用户上传的文件"""
        if not file:
            raise HTTPException(status_code=400, detail="No file provided")

        import shutil
        upload_dir = Path("output") / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)

        dest = upload_dir / file.filename
        with open(dest, "wb") as f:
            content = await file.read()
            f.write(content)

        return {
            "status": "uploaded",
            "name": file.filename,
            "size": len(content),
            "path": str(dest),
            "url": f"/files/uploads/{file.filename}",
        }

    @app.get("/files/uploads/{path:path}")
    async def serve_upload(path: str) -> StreamingResponse:
        """提供上传文件的访问"""
        from src.executor.file_manager import _mime_type
        file_path = Path("output") / "uploads" / path
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")

        size = file_path.stat().st_size
        mime = _mime_type(file_path.suffix)

        async def _gen():
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk

        return StreamingResponse(
            _gen(),
            media_type=mime,
            headers={"Content-Length": str(size), "Content-Disposition": f"inline; filename={file_path.name}"},
        )

    # ─── Schedule Management ────────────────────────────────────

    # SCHEDULE_TRIGGERED 事件处理：定时触发时走正常 Router → Executor 流程
    async def on_schedule_triggered(event: Event) -> None:
        """定时任务触发：当作 USER_MESSAGE 处理"""
        text = event.payload.get("text", "")
        if not text:
            return
        route = await router.route(text)
        await executor.handle_route_result(route, event)

        audit_logger.log(
            action="schedule_triggered",
            actor="scheduler",
            target=event.conversation_id,
            detail={
                "schedule_id": event.payload.get("schedule_id"),
                "schedule_name": event.payload.get("schedule_name"),
                "message": text,
                "route_type": route.route_type.value,
            },
        )

    event_bus.on(EventType.SCHEDULE_TRIGGERED, on_schedule_triggered)

    @app.get("/schedules", response_model=list[ScheduleInfo])
    async def list_schedules() -> list[ScheduleInfo]:
        """列出所有定时任务"""
        entries = scheduler.list_schedules()
        return [
            ScheduleInfo(
                schedule_id=e.schedule_id,
                name=e.name,
                cron=e.cron,
                message=e.message,
                conversation_id=e.conversation_id,
                enabled=e.enabled,
                last_run=e.last_run.isoformat() if e.last_run else None,
                next_run=e.next_run.isoformat() if e.next_run else None,
            )
            for e in entries
        ]

    @app.post("/schedules", response_model=ScheduleInfo)
    async def create_schedule(req: ScheduleCreateRequest) -> ScheduleInfo:
        """创建定时任务"""
        # 验证 cron 表达式
        from src.scheduler.cron_parser import CronParser, CronParseError
        try:
            CronParser(req.cron)
        except CronParseError as e:
            raise HTTPException(status_code=400, detail=f"无效的 cron 表达式: {e}")

        conv_id = req.conversation_id or str(uuid.uuid4())
        entry = ScheduleEntry(
            name=req.name,
            cron=req.cron,
            message=req.message,
            conversation_id=conv_id,
            user_id=req.user_id,
        )
        entry = await scheduler.add_schedule(entry)

        return ScheduleInfo(
            schedule_id=entry.schedule_id,
            name=entry.name,
            cron=entry.cron,
            message=entry.message,
            conversation_id=entry.conversation_id,
            enabled=entry.enabled,
            last_run=entry.last_run.isoformat() if entry.last_run else None,
            next_run=entry.next_run.isoformat() if entry.next_run else None,
        )

    @app.delete("/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str) -> dict:
        """删除定时任务"""
        entry = await scheduler.remove_schedule(schedule_id)
        if not entry:
            raise HTTPException(status_code=404, detail="定时任务不存在")
        return {"status": "deleted", "schedule_id": schedule_id, "name": entry.name}

    @app.post("/schedules/{schedule_id}/toggle")
    async def toggle_schedule(schedule_id: str) -> ScheduleInfo:
        """启用/禁用定时任务"""
        entry = await scheduler.toggle_schedule(schedule_id)
        if not entry:
            raise HTTPException(status_code=404, detail="定时任务不存在")
        return ScheduleInfo(
            schedule_id=entry.schedule_id,
            name=entry.name,
            cron=entry.cron,
            message=entry.message,
            conversation_id=entry.conversation_id,
            enabled=entry.enabled,
            last_run=entry.last_run.isoformat() if entry.last_run else None,
            next_run=entry.next_run.isoformat() if entry.next_run else None,
        )

    # ─── Notification Management ────────────────────────────────

    @app.post("/notify")
    async def send_notification(title: str = "", body: str = "", priority: str = "normal") -> dict:
        """手动发送通知"""
        from src.notifier.channels.base import Priority
        try:
            pri = Priority(priority)
        except ValueError:
            pri = Priority.NORMAL
        results = await notifier.notify(title=title, body=body, priority=pri)
        return {"status": "sent", "results": results}

    @app.get("/notify/channels")
    async def list_notify_channels() -> list[dict]:
        """列出已配置的通知通道"""
        return [
            {"name": ch.name, "type": ch.__class__.__name__.replace("Channel", "").lower()}
            for ch in notifier.channels
        ]

    # ─── Workflow Management ────────────────────────────────────

    class WorkflowCreateRequest(APIModel):
        name: str
        description: str = ""
        steps: list[dict]  # [{"type": "skill", "skill_name": "...", "params": {}, ...}]

    class WorkflowExecuteRequest(APIModel):
        conversation_id: str | None = None
        params: dict = {}

    @app.get("/workflows")
    async def list_workflows() -> list[dict]:
        """列出所有工作流模板"""
        entries = workflow_store.list_all()
        return [
            {
                "workflow_id": e.workflow_id,
                "name": e.name,
                "description": e.description,
                "steps_count": len(e.steps),
                "created_at": e.created_at.isoformat(),
            }
            for e in entries
        ]

    @app.post("/workflows")
    async def create_workflow(req: WorkflowCreateRequest) -> dict:
        """创建工作流模板"""
        steps = []
        for i, s in enumerate(req.steps):
            steps.append(WorkflowStep(
                step_id=i,
                type=s.get("type", "skill"),
                skill_name=s.get("skill_name"),
                params=s.get("params", {}),
                input_from=s.get("input_from"),
                condition=s.get("condition"),
            ))
        entry = WorkflowEntry(name=req.name, description=req.description, steps=steps)
        entry = workflow_store.add(entry)
        return {"status": "created", "workflow_id": entry.workflow_id, "name": entry.name}

    @app.get("/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str) -> dict:
        """获取工作流模板详情"""
        entry = workflow_store.get(workflow_id)
        if not entry:
            raise HTTPException(status_code=404, detail="工作流不存在")
        return entry.model_dump()

    @app.delete("/workflows/{workflow_id}")
    async def delete_workflow(workflow_id: str) -> dict:
        """删除工作流模板"""
        entry = workflow_store.remove(workflow_id)
        if not entry:
            raise HTTPException(status_code=404, detail="工作流不存在")
        return {"status": "deleted", "workflow_id": workflow_id, "name": entry.name}

    @app.post("/workflows/{workflow_id}/execute")
    async def execute_workflow(workflow_id: str, req: WorkflowExecuteRequest) -> dict:
        """执行工作流"""
        ctx = await workflow_engine.execute_by_id(
            workflow_id,
            conversation_id=req.conversation_id,
            initial_params=req.params,
        )
        if not ctx:
            raise HTTPException(status_code=404, detail="工作流不存在")
        return {
            "status": ctx.status,
            "execution_id": ctx.execution_id,
            "workflow_name": ctx.workflow.name,
            "step_results": {str(k): str(v)[:200] for k, v in ctx.step_results.items()},
            "error": ctx.error,
        }

    @app.post("/workflows/execute/{name}")
    async def execute_workflow_by_name(name: str, req: WorkflowExecuteRequest) -> dict:
        """按名称执行工作流"""
        ctx = await workflow_engine.execute_by_name(
            name,
            conversation_id=req.conversation_id,
            initial_params=req.params,
        )
        if not ctx:
            raise HTTPException(status_code=404, detail=f"工作流 '{name}' 不存在")
        return {
            "status": ctx.status,
            "execution_id": ctx.execution_id,
            "workflow_name": ctx.workflow.name,
            "step_results": {str(k): str(v)[:200] for k, v in ctx.step_results.items()},
            "error": ctx.error,
        }

    # ─── Gateway Inbound ────────────────────────────────────────

    class GatewayInboundRequest(APIModel):
        text: str
        source: str = "webhook"
        token: str = ""
        conversation_id: str | None = None
        extra: dict = {}

    @app.post("/gateway/inbound")
    async def gateway_inbound(req: GatewayInboundRequest) -> dict:
        """入站网关：接收外部指令"""
        try:
            event = await gateway.handle_inbound(
                text=req.text,
                source=req.source,
                token=req.token,
                conversation_id=req.conversation_id,
                extra=req.extra,
            )
            # 触发 Router → Executor 流程
            # 优先检查 SkillSession 拦截
            if executor.session_manager.should_intercept(event.conversation_id):
                route = executor.session_manager.intercept_route(event.conversation_id, req.text)
            elif executor.get_pending_context(event.conversation_id):
                pending = executor.get_pending_context(event.conversation_id)
                route = RouteResult(
                    route_type=RouteType.SKILL,
                    skill_name=pending.skill_name,
                    params={
                        "action": "continue",
                        "original_text": req.text,
                        "login_step": pending.login_step,
                        "pending_prompt": pending.pending_prompt,
                        "pending_params": pending.pending_params,
                    },
                    confidence=0.95,
                )
            else:
                route = await router.route(req.text)
            reply = await executor.handle_route_result(route, event)
            return {
                "status": "ok",
                "event_id": event.event_id,
                "conversation_id": event.conversation_id,
                "reply": reply or "(无响应)",
            }
        except GatewayAuthError as e:
            raise HTTPException(status_code=401, detail=str(e))
        except GatewayRateLimitError as e:
            raise HTTPException(status_code=429, detail=str(e))

    @app.get("/gateway/status")
    async def gateway_status() -> dict:
        """网关状态"""
        return gateway.get_stats()

    return app
