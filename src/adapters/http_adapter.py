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
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel as APIModel

from src.config import load_config, RaccoonConfig, LLM_PRESETS
from src.conversation_store import ConversationStore
from src.eventbus.bus import EventBus
from src.eventbus.events import EventType, make_event
from src.executor.agent import Executor, LlmClassification
from src.executor.file_manager import FileManager
from src.llm import LLMFactory
from src.router.router import Router
from src.scheduler.scheduler import Scheduler
from src.scheduler.schedule_store import ScheduleStore
from src.notifier.notifier import Notifier
from src.skill_vault.vault_manager import VaultManager
from src.supervisor.approval_engine import ApprovalEngine, ApprovalStatus
from src.supervisor.audit_logger import AuditLogger
from src.types import Event, RouteResult, RouteType, ScheduleEntry, ScheduleStatus, RetryPolicy, WorkflowEntry, WorkflowStep
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
    enabled: bool = False
    risk_level: str = "low"
    interactive: bool = False
    intent_tags: list[str] = []
    starred: bool = False
    installed_from: str | None = None
    installed_at: str | None = None


class ScheduleCreateRequest(APIModel):
    name: str
    cron: str
    message: str
    conversation_id: str | None = None
    user_id: str = "http_user"
    max_retries: int = 3
    retry_interval_seconds: int = 60
    retry_on_failure: bool = True


class ScheduleInfo(APIModel):
    schedule_id: str
    name: str
    cron: str
    message: str
    conversation_id: str
    status: str = "enabled"       # enabled / paused / disabled
    enabled: bool = True          # 兼容旧前端
    retry_count: int = 0
    last_run: str | None = None
    next_run: str | None = None
    last_run_result: str | None = None
    last_run_error: str | None = None


# ─── App Factory ───────────────────────────────────────────────

def create_app(config: RaccoonConfig | None = None) -> FastAPI:
    """创建 FastAPI 应用"""
    config = config or load_config()

    event_bus = EventBus(config)
    vault_manager = VaultManager(config)
    router = Router()
    executor = Executor(event_bus, vault_manager, config)
    audit_logger = AuditLogger(config)

    # 只注册已启用的 Skill
    for skill in vault_manager.list_enabled_skills():
        router.register_skill(skill)

    # 定时调度
    schedule_store = ScheduleStore(config)
    schedule_store.recover()
    scheduler = Scheduler(event_bus, schedule_store, config)

    # 通知网关
    notifier = Notifier(event_bus, config)
    notifier.initialize()

    # 审批引擎
    approval_engine = ApprovalEngine(
        auto_approve=config.auto_approve,
        approval_timeout_seconds=config.approval_timeout_seconds,
        notifier=notifier,
    )

    # L3 学习引擎
    llm_client = LLMFactory.create(config)
    from src.brain.learning_engine import LearningEngine
    learning_engine = LearningEngine(
        config=config,
        vault_manager=vault_manager,
        llm_client=llm_client,
        scheduler=scheduler,
        router=router,
    )
    executor.set_learning_engine(learning_engine)
    executor.set_scheduler(scheduler)

    # 工作流编排
    workflow_store = WorkflowStore(config)
    workflow_store.recover()
    workflow_engine = WorkflowEngine(event_bus, router, executor, workflow_store, config)

    # 入站网关
    gateway = GatewayInbound(event_bus, config)

    # 会话持久化
    conversation_store = ConversationStore(config)

    # SSE 事件队列（每个连接带关注的 conversation_id）
    # 格式: list of (queue, set_of_conversation_ids_or_None)
    # None 表示关注所有事件（兼容旧行为）
    sse_subscribers: list[tuple[asyncio.Queue, set[str] | None]] = []

    # 文件管理器（共享给 Executor 和 HTTP 端点）
    file_manager = FileManager()

    # EventBus handler: 将事件推送到 SSE 队列（按 conversation_id 过滤）
    async def on_any_event(event: Event) -> None:
        for q, conv_ids in sse_subscribers:
            # conv_ids 为 None 表示关注所有事件
            if conv_ids is None or event.conversation_id in conv_ids:
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
    app.state.sse_subscribers = sse_subscribers
    app.state.file_manager = file_manager
    app.state.scheduler = scheduler
    app.state.notifier = notifier
    app.state.approval_engine = approval_engine
    app.state.workflow_store = workflow_store
    app.state.workflow_engine = workflow_engine
    app.state.gateway = gateway
    app.state.conversation_store = conversation_store

    # ─── Web UI ────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """Web UI 首页"""
        html_path = Path(__file__).parent / "static" / "index.html"
        return HTMLResponse(content=html_path.read_text("utf-8"))

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ─── Routes ────────────────────────────────────────────────

    @app.on_event("startup")
    async def startup() -> None:
        await event_bus.start()
        await scheduler.start()
        await approval_engine.start()
        logger.info("http_server_started", port=config.http_port)

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await approval_engine.stop()
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
        # 检查待确认的学习请求
        elif executor.intercept_learn_request(conv_id, req.text):
            route = executor.intercept_learn_request(conv_id, req.text)
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
        # 检查待确认的学习请求
        elif executor.intercept_learn_request(conv_id, req.text):
            route = executor.intercept_learn_request(conv_id, req.text)
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

        # LEARN 路由：直接返回完整结果（非流式）
        if route.route_type == RouteType.LEARN:
            reply = await executor.handle_route_result(route, event)
            async def _single():
                yield f"data: {json.dumps({'type': 'text', 'content': reply or '(无响应)', 'conversation_id': conv_id, 'source': 'learn_confirm'}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
            return StreamingResponse(_single(), media_type="text/event-stream")

        # 非流式路由（SKILL、LEARN、SKILL_SESSION 等）：直接返回完整结果
        if route.route_type != RouteType.LLM:
            reply = await executor.handle_route_result(route, event)
            skill_name = route.skill_name or ""
            async def _single():
                yield f"data: {json.dumps({'type': 'text', 'content': reply or '(无响应)', 'conversation_id': conv_id, 'source': 'skill', 'skill_name': skill_name}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'conversation_id': conv_id, 'source': 'skill', 'skill_name': skill_name}, ensure_ascii=False)}\n\n"
            return StreamingResponse(_single(), media_type="text/event-stream")

        # LLM 路由：先分类再分流
        text = route.params.get("original_text", "")
        classify = await executor.classify_llm_message(text)

        if classify.classification == LlmClassification.CHITCHAT:
            # 闲聊 → 流式推送，保留打字机效果
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
                        yield f"data: {json.dumps({'type': 'text', 'content': chunk, 'conversation_id': conv_id, 'source': 'llm'}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
                except Exception as e:
                    logger.error("stream_chat_failed", error=str(e))
                    yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
            return StreamingResponse(_stream(), media_type="text/event-stream")

        elif classify.classification == LlmClassification.SKILL_MATCHED:
            # 匹配到 Skill → 构造 RouteResult 走 Skill 执行，一次性返回
            skill_route = RouteResult(
                route_type=RouteType.SKILL,
                skill_name=classify.skill_name,
                params={"rest": text},
                confidence=0.6,
            )
            reply = await executor.handle_route_result(skill_route, event)
            skill_name = classify.skill_name or ""
            async def _single():
                yield f"data: {json.dumps({'type': 'text', 'content': reply or '(无响应)', 'conversation_id': conv_id, 'source': 'skill_matched', 'skill_name': skill_name}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'conversation_id': conv_id, 'source': 'skill_matched', 'skill_name': skill_name}, ensure_ascii=False)}\n\n"
            return StreamingResponse(_single(), media_type="text/event-stream")

        else:
            # NEEDS_LEARN → 走 _handle_llm 第三段（学习确认），一次性返回
            reply = await executor.handle_route_result(route, event)
            async def _single():
                yield f"data: {json.dumps({'type': 'text', 'content': reply or '(无响应)', 'conversation_id': conv_id, 'source': 'learn_confirm'}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'conversation_id': conv_id}, ensure_ascii=False)}\n\n"
            return StreamingResponse(_single(), media_type="text/event-stream")

    @app.get("/events")
    async def event_stream(conversation_id: str | None = None) -> StreamingResponse:
        """SSE 事件流，可选按 conversation_id 过滤"""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=100)
        # 如果指定了 conversation_id，只关注该会话的事件
        conv_ids = {conversation_id} if conversation_id else None
        subscriber = (queue, conv_ids)
        sse_subscribers.append(subscriber)

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
                sse_subscribers.remove(subscriber)

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
        """列出所有已安装 Skill（含启用/禁用状态）"""
        skills = vault_manager.list_skills()
        return [
            SkillInfo(
                name=s.name,
                version=s.version,
                description=s.description,
                trigger_words=s.trigger_words,
                enabled=s.enabled,
                risk_level=s.risk_level,
                interactive=s.interactive,
                intent_tags=getattr(s, 'intent_tags', []) or [],
                starred=getattr(s, 'starred', False) or False,
                installed_from=getattr(s, 'installed_from', None),
                installed_at=getattr(s, 'installed_at', None),
            )
            for s in skills
        ]

    @app.post("/skills/{name}/toggle")
    async def toggle_skill(name: str) -> dict:
        """切换 Skill 启用/禁用"""
        try:
            meta = vault_manager.toggle_skill(name)
            # 重新注册路由
            if meta.enabled:
                router.register_skill(meta)
            else:
                router.unregister_skill(meta)
            return {"name": name, "enabled": meta.enabled}
        except KeyError:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=404, content={"error": f"Skill '{name}' not found"})

    @app.post("/skills/{name}/star")
    async def toggle_star(name: str) -> dict:
        """切换 Skill 收藏/取消收藏"""
        try:
            meta = vault_manager.toggle_star(name)
            return {"name": name, "starred": meta.starred}
        except KeyError:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=404, content={"error": f"Skill '{name}' not found"})

    # ─── Market API ──────────────────────────────────────────────

    class MarketSkillInfo(APIModel):
        name: str
        version: str = "0.1.0"
        description: str = ""
        trigger_words: list[str] = []
        intent_tags: list[str] = []
        category: str = ""
        risk_level: str = "low"
        interactive: bool = False
        source: str = "builtin"
        installed: bool = False
        installed_version: str | None = None

    @app.get("/market", response_model=list[MarketSkillInfo])
    async def list_market(category: str | None = None, q: str | None = None) -> list[MarketSkillInfo]:
        """列出市场 Skill（合并市场索引 + 已安装状态）"""
        import json as _json
        market_path = Path(__file__).parent.parent.parent / "market_index.json"
        if not market_path.exists():
            return []
        data = _json.loads(market_path.read_text(encoding="utf-8"))
        skills = data.get("skills", [])

        # 搜索过滤
        if q:
            ql = q.lower()
            skills = [s for s in skills if ql in s.get("name", "").lower() or ql in s.get("description", "").lower()]

        # 分类过滤
        if category:
            skills = [s for s in skills if s.get("category") == category]

        # 标记已安装状态
        installed_names = {s.name for s in vault_manager.list_skills()}
        result = []
        for s in skills:
            is_installed = s["name"] in installed_names
            installed_ver = None
            if is_installed:
                local = vault_manager.get_skill(s["name"])
                if local:
                    installed_ver = local.version
            result.append(MarketSkillInfo(
                name=s["name"],
                version=s.get("version", "0.1.0"),
                description=s.get("description", ""),
                trigger_words=s.get("trigger_words", []),
                intent_tags=s.get("intent_tags", []),
                category=s.get("category", ""),
                risk_level=s.get("risk_level", "low"),
                interactive=s.get("interactive", False),
                source=s.get("source", "builtin"),
                installed=is_installed,
                installed_version=installed_ver,
            ))
        return result

    @app.post("/market/{name}/install")
    async def install_skill_from_market(name: str) -> dict:
        """从市场安装 Skill"""
        import json as _json
        market_path = Path(__file__).parent.parent.parent / "market_index.json"
        if not market_path.exists():
            raise HTTPException(status_code=404, detail="市场索引不存在")

        data = _json.loads(market_path.read_text(encoding="utf-8"))
        skill_entry = None
        for s in data.get("skills", []):
            if s["name"] == name:
                skill_entry = s
                break
        if not skill_entry:
            raise HTTPException(status_code=404, detail=f"市场未找到 Skill: {name}")

        # 已安装则返回
        if vault_manager.get_skill(name):
            return {"status": "already_installed", "name": name}

        # 发送安装中事件
        await event_bus.emit(make_event(
            EventType.SKILL_INSTALLING,
            conversation_id="system",
            payload={"name": name, "version": skill_entry.get("version", "0.1.0")},
        ))

        try:
            source = skill_entry.get("source", "builtin")
            if source and source.startswith("http"):
                meta = await vault_manager.install(source)
            else:
                # builtin skill — 重新从本地 skills 目录加载
                vault_manager._load_existing_skills()
                meta = vault_manager.get_skill(name)
                if meta:
                    meta.installed_from = "builtin"
                    meta.installed_at = datetime.now(timezone.utc).isoformat()
                    vault_manager._persist_skill_meta(name)

            # 注册路由
            if meta and meta.enabled:
                router.register_skill(meta)

            await event_bus.emit(make_event(
                EventType.SKILL_INSTALLED,
                conversation_id="system",
                payload={"name": name, "version": meta.version if meta else "unknown"},
            ))

            return {"status": "installed", "name": name, "version": meta.version if meta else "unknown"}
        except Exception as e:
            await event_bus.emit(make_event(
                EventType.SKILL_INSTALL_FAILED,
                conversation_id="system",
                payload={"name": name, "error": str(e)},
            ))
            raise HTTPException(status_code=500, detail=f"安装失败: {e}")

    @app.delete("/skills/{name}")
    async def uninstall_skill(name: str) -> dict:
        """卸载已安装的 Skill"""
        if not vault_manager.get_skill(name):
            raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
        # 先取消注册路由
        meta = vault_manager.get_skill(name)
        if meta:
            router.unregister_skill(meta)
        vault_manager.uninstall(name)
        return {"status": "uninstalled", "name": name}

    @app.post("/skills/{name}/upgrade")
    async def upgrade_skill(name: str) -> dict:
        """升级已安装的 Skill"""
        try:
            meta = await vault_manager.upgrade(name)
            if meta.enabled:
                router.register_skill(meta)
            return {"status": "upgraded", "name": name, "version": meta.version}
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))

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

    # ─── LLM Settings API ──────────────────────────────────────

    class LLMSettingsRequest(APIModel):
        provider: str | None = None
        api_key: str | None = None
        model: str | None = None
        base_url: str | None = None
        temperature: float | None = None
        max_tokens: int | None = None

    class LLMTestRequest(APIModel):
        provider: str | None = None
        api_key: str | None = None
        model: str | None = None
        base_url: str | None = None

    @app.get("/settings/llm")
    async def get_llm_settings() -> dict:
        """获取当前 LLM 配置（API Key 脱敏）"""
        return {
            "provider": config.llm_provider,
            "api_key": config.mask_api_key(),
            "api_key_set": bool(config.llm_api_key),
            "model": config.llm_model,
            "base_url": config.llm_base_url,
            "temperature": config.llm_temperature,
            "max_tokens": config.llm_max_tokens,
        }

    @app.put("/settings/llm")
    async def update_llm_settings(req: LLMSettingsRequest) -> dict:
        """更新 LLM 配置并热重载"""
        # 如果 api_key 是脱敏格式（包含 ***），不更新
        api_key = req.api_key
        if api_key and "***" in api_key:
            api_key = None  # 跳过脱敏值

        config.update_llm(
            provider=req.provider,
            api_key=api_key,
            model=req.model,
            base_url=req.base_url,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
        )

        # 热重载：重建 LLM 客户端
        try:
            new_llm = LLMFactory.create(config)
            executor._llm_client = new_llm
            learning_engine._llm_client = new_llm
            logger.info("llm_hot_reloaded", provider=config.llm_provider, model=config.llm_model)
        except Exception as e:
            logger.warning("llm_hot_reload_failed", error=str(e))

        return {
            "status": "updated",
            "provider": config.llm_provider,
            "model": config.llm_model,
            "api_key": config.mask_api_key(),
        }

    @app.get("/settings/llm/presets")
    async def get_llm_presets() -> list[dict]:
        """获取 LLM 预设模板列表"""
        return LLM_PRESETS

    @app.post("/settings/llm/test")
    async def test_llm_connection(req: LLMTestRequest) -> dict:
        """测试 LLM 连接"""
        # 临时构建配置测试
        test_config = RaccoonConfig(
            llm_provider=req.provider or config.llm_provider,
            llm_api_key=req.api_key or config.llm_api_key,
            llm_model=req.model or config.llm_model,
            llm_base_url=req.base_url or config.llm_base_url,
        )

        try:
            client = LLMFactory.create(test_config)
            reply = await client.chat(
                [{"role": "user", "content": "你好，请回复'连接成功'"}],
                temperature=0.1,
                max_tokens=50,
            )
            return {
                "status": "ok",
                "reply": reply[:200],
                "provider": test_config.llm_provider,
                "model": test_config.llm_model,
            }
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "provider": test_config.llm_provider,
                "model": test_config.llm_model,
            }

    # ─── Multi-Model Management API ────────────────────────────────

    class ModelAddRequest(APIModel):
        id: str = ""
        name: str
        vendor: str = "Custom"
        url: str = ""
        apiKey: str = ""
        maxInputTokens: int = 0
        maxOutputTokens: int = 0
        supportsToolCall: bool = False
        supportsImages: bool = False
        supportsReasoning: bool = False

    class ModelUpdateRequest(APIModel):
        name: str | None = None
        vendor: str | None = None
        url: str | None = None
        apiKey: str | None = None
        maxInputTokens: int | None = None
        maxOutputTokens: int | None = None
        supportsToolCall: bool | None = None
        supportsImages: bool | None = None
        supportsReasoning: bool | None = None

    @app.get("/settings/models")
    async def list_models() -> list[dict]:
        """列出所有已配置的模型"""
        return config.list_models()

    @app.get("/settings/models/{model_id}")
    async def get_model(model_id: str) -> dict:
        """获取模型详情（apiKey 脱敏）"""
        models = config.list_models()
        for m in models:
            if m.get("id") == model_id:
                return m
        raise HTTPException(status_code=404, detail="模型不存在")

    @app.post("/settings/models")
    async def add_model(req: ModelAddRequest) -> dict:
        """新增模型"""
        model_data = req.model_dump()
        result = config.add_model(model_data)
        return result

    @app.put("/settings/models/{model_id}")
    async def update_model(model_id: str, req: ModelUpdateRequest) -> dict:
        """更新模型配置"""
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        result = config.update_model(model_id, updates)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="模型不存在")

        # 如果更新的是当前激活模型，热重载 LLM 客户端
        if model_id == config.llm_active_model_id:
            try:
                new_llm = LLMFactory.create(config)
                executor._llm_client = new_llm
                learning_engine._llm_client = new_llm
            except Exception as e:
                logger.warning("llm_hot_reload_failed", error=str(e))

        return result

    @app.delete("/settings/models/{model_id}")
    async def delete_model(model_id: str) -> dict:
        """删除模型"""
        result = config.delete_model(model_id)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="模型不存在")
        return result

    @app.post("/settings/models/{model_id}/activate")
    async def activate_model(model_id: str) -> dict:
        """切换当前激活的模型"""
        result = config.activate_model(model_id)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="模型不存在")

        # 热重载 LLM 客户端
        try:
            new_llm = LLMFactory.create(config)
            executor._llm_client = new_llm
            learning_engine._llm_client = new_llm
            logger.info("llm_model_switched", provider=config.llm_provider, model=config.llm_model)
        except Exception as e:
            logger.warning("llm_hot_reload_failed", error=str(e))

        return result

    # ─── Conversation Persistence ────────────────────────────────

    @app.post("/conversations/save")
    async def save_conversation(req: ConversationSave) -> dict:
        """保存会话消息"""
        conversation_store.save(req.conversation_id, [m.model_dump() for m in req.messages])
        return {"status": "saved", "conversation_id": req.conversation_id, "count": len(req.messages)}

    @app.get("/conversations")
    async def list_conversations() -> list[dict]:
        """列出所有保存的会话"""
        return conversation_store.list_all()

    @app.get("/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str) -> dict:
        """获取会话详情"""
        msgs = conversation_store.get(conversation_id)
        return {"conversation_id": conversation_id, "messages": msgs}

    @app.delete("/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str) -> dict:
        """删除会话"""
        conversation_store.delete(conversation_id)
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

    def _entry_to_info(e: ScheduleEntry) -> ScheduleInfo:
        """ScheduleEntry → ScheduleInfo 辅助函数"""
        return ScheduleInfo(
            schedule_id=e.schedule_id,
            name=e.name,
            cron=e.cron,
            message=e.message,
            conversation_id=e.conversation_id,
            status=e.status.value,
            enabled=e.enabled,
            retry_count=e.retry_count,
            last_run=e.last_run.isoformat() if e.last_run else None,
            next_run=e.next_run.isoformat() if e.next_run else None,
            last_run_result=e.last_run_result,
            last_run_error=e.last_run_error,
        )

    # SCHEDULE_TRIGGERED 事件处理：定时触发时走正常 Router → Executor 流程
    async def on_schedule_triggered(event: Event) -> None:
        """定时任务触发：当作 USER_MESSAGE 处理"""
        text = event.payload.get("text", "")
        if not text:
            return
        route = await router.route(text)
        await executor.handle_route_result(route, event)

        # 记录执行结果（由 Executor 回调 scheduler.record_run_result）
        audit_logger.log(
            action="schedule_triggered",
            actor="scheduler",
            target=event.conversation_id,
            detail={
                "schedule_id": event.payload.get("schedule_id"),
                "schedule_name": event.payload.get("schedule_name"),
                "message": text,
                "route_type": route.route_type.value,
                "is_retry": event.payload.get("is_retry", False),
            },
        )

    event_bus.on(EventType.SCHEDULE_TRIGGERED, on_schedule_triggered)

    @app.get("/schedules", response_model=list[ScheduleInfo])
    async def list_schedules() -> list[ScheduleInfo]:
        """列出所有定时任务"""
        entries = scheduler.list_schedules()
        return [_entry_to_info(e) for e in entries]

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
            retry_policy={
                "max_retries": req.max_retries,
                "retry_interval_seconds": req.retry_interval_seconds,
                "retry_on_failure": req.retry_on_failure,
            },
        )
        entry = await scheduler.add_schedule(entry)
        return _entry_to_info(entry)

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
        return _entry_to_info(entry)

    @app.post("/schedules/{schedule_id}/pause")
    async def pause_schedule(schedule_id: str) -> ScheduleInfo:
        """暂停定时任务"""
        entry = await scheduler.pause_schedule(schedule_id)
        if not entry:
            raise HTTPException(status_code=404, detail="定时任务不存在")
        return _entry_to_info(entry)

    @app.post("/schedules/{schedule_id}/resume")
    async def resume_schedule(schedule_id: str) -> ScheduleInfo:
        """恢复暂停的定时任务"""
        entry = await scheduler.resume_schedule(schedule_id)
        if not entry:
            raise HTTPException(status_code=404, detail="定时任务不存在或未暂停")
        return _entry_to_info(entry)

    @app.get("/schedules/{schedule_id}/runs")
    async def get_schedule_runs(schedule_id: str, limit: int = 10) -> list[dict]:
        """获取定时任务执行记录"""
        return scheduler.get_schedule_runs(schedule_id, limit)

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

    # ─── Approval Management ────────────────────────────────────

    class ApprovalActionRequest(APIModel):
        reason: str = ""

    @app.get("/approvals/pending")
    async def list_pending_approvals() -> list[dict]:
        """列出待审批条目"""
        entries = approval_engine.get_pending()
        return [
            {
                "approval_id": e.approval_id,
                "task_id": e.task.task_id,
                "skill_name": e.task.skill_name,
                "risk_level": e.risk_level,
                "status": e.status.value,
                "created_at": e.created_at.isoformat(),
                "remaining_seconds": e.remaining_seconds,
                "origin_message": e.task.origin_message[:200],
            }
            for e in entries
        ]

    @app.post("/approvals/{approval_id}/approve")
    async def approve_approval(approval_id: str, req: ApprovalActionRequest | None = None) -> dict:
        """批准审批"""
        result = await approval_engine.approve(approval_id, approver="http_user")
        if not result and result.reason == "not_found":
            raise HTTPException(status_code=404, detail="审批条目不存在")
        audit_logger.log(
            action="approval_approved",
            actor="http_user",
            target=approval_id,
            detail={"reason": req.reason if req else ""},
        )
        return {
            "status": result.status.value,
            "approved": result.approved,
            "reason": result.reason,
            "approval_id": approval_id,
        }

    @app.post("/approvals/{approval_id}/reject")
    async def reject_approval(approval_id: str, req: ApprovalActionRequest | None = None) -> dict:
        """拒绝审批"""
        reason = req.reason if req else ""
        result = await approval_engine.reject(approval_id, reason=reason, rejector="http_user")
        if not result and result.reason == "not_found":
            raise HTTPException(status_code=404, detail="审批条目不存在")
        audit_logger.log(
            action="approval_rejected",
            actor="http_user",
            target=approval_id,
            detail={"reason": reason},
        )
        return {
            "status": result.status.value,
            "approved": result.approved,
            "reason": result.reason,
            "approval_id": approval_id,
        }

    @app.get("/approvals/{approval_id}")
    async def get_approval(approval_id: str) -> dict:
        """获取审批条目详情"""
        entry = approval_engine.get_entry(approval_id)
        if not entry:
            raise HTTPException(status_code=404, detail="审批条目不存在")
        return {
            "approval_id": entry.approval_id,
            "task_id": entry.task.task_id,
            "skill_name": entry.task.skill_name,
            "risk_level": entry.risk_level,
            "status": entry.status.value,
            "created_at": entry.created_at.isoformat(),
            "resolved_at": entry.resolved_at.isoformat() if entry.resolved_at else None,
            "resolved_by": entry.resolved_by,
            "reason": entry.reason,
            "remaining_seconds": entry.remaining_seconds,
            "origin_message": entry.task.origin_message[:200],
        }

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
            # 检查待确认的学习请求
            elif executor.intercept_learn_request(event.conversation_id, req.text):
                route = executor.intercept_learn_request(event.conversation_id, req.text)
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
