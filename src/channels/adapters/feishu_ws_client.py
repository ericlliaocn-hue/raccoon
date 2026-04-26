"""Feishu WebSocket long-connection bridge.

说明：
- 使用官方 `lark-oapi` WS 客户端接收飞书事件；
- 在独立线程运行，避免阻塞 FastAPI 主事件循环；
- 事件统一转换为 `FeishuParseResult`，与 HTTP callback 共享同一条处理链路。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from src.channels.adapters.feishu import parse_feishu_ws_event
from src.config import RaccoonConfig

logger = structlog.get_logger(__name__)


class FeishuWebSocketBridge:
    """Feishu WS 通道桥接器（可启动/停止）。"""

    def __init__(
        self,
        *,
        config: RaccoonConfig,
        on_parsed_event: Callable[[Any], Awaitable[None]],
    ) -> None:
        self._config = config
        self._on_parsed_event = on_parsed_event

        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._thread_loop: asyncio.AbstractEventLoop | None = None
        self._ws_client: Any | None = None

        self._start_event = threading.Event()
        self._started = False
        self._stopping = False
        self._start_error: Exception | None = None

    async def start(self) -> None:
        """启动 WS 长连接。"""
        if self._started:
            return
        if not self._config.feishu_enabled or self._config.feishu_mode != "websocket":
            return
        if not self._config.feishu_app_id or not self._config.feishu_app_secret:
            raise RuntimeError("feishu websocket mode requires feishu_app_id and feishu_app_secret")

        self._main_loop = asyncio.get_running_loop()
        self._stopping = False
        self._start_error = None
        self._start_event.clear()

        self._thread = threading.Thread(
            target=self._run_ws_thread,
            name="raccoon-feishu-ws",
            daemon=True,
        )
        self._thread.start()

        await asyncio.to_thread(self._start_event.wait, 5.0)
        await asyncio.sleep(0.2)

        if self._start_error:
            raise RuntimeError(f"feishu websocket start failed: {self._start_error}") from self._start_error
        if not self._thread.is_alive():
            raise RuntimeError("feishu websocket worker exited unexpectedly during startup")

        self._started = True
        logger.info("feishu_ws_started", mode="websocket")

    async def stop(self) -> None:
        """停止 WS 长连接。"""
        if not self._thread:
            return

        self._stopping = True

        # 优先优雅断链，再停止线程事件循环。
        if self._thread_loop and self._ws_client is not None:
            disconnect = getattr(self._ws_client, "_disconnect", None)
            if callable(disconnect):
                try:
                    fut = asyncio.run_coroutine_threadsafe(disconnect(), self._thread_loop)
                    await asyncio.to_thread(fut.result, 3.0)
                except Exception as e:
                    logger.debug("feishu_ws_disconnect_failed", error=str(e))

        if self._thread_loop and not self._thread_loop.is_closed():
            try:
                self._thread_loop.call_soon_threadsafe(self._thread_loop.stop)
            except Exception as e:
                logger.debug("feishu_ws_loop_stop_failed", error=str(e))

        thread = self._thread
        await asyncio.to_thread(thread.join, 5.0)
        if thread.is_alive():
            logger.warning("feishu_ws_thread_join_timeout")

        self._thread = None
        self._thread_loop = None
        self._ws_client = None
        self._started = False

    # ─── internals ──────────────────────────────────────────

    def _run_ws_thread(self) -> None:
        try:
            from lark_oapi.core.enum import LogLevel
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import Client as FeishuWSClient
            import lark_oapi.ws.client as ws_client_module
        except Exception as e:  # pragma: no cover - 依赖缺失由启动校验兜底
            self._start_error = e
            self._start_event.set()
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._thread_loop = loop
        ws_client_module.loop = loop

        try:
            # WS 通道已基于 app_id/app_secret 建立鉴权，避免因 verification token
            # 配置不一致导致消息被 SDK 层误拦截（常见于本地联调和多套配置切换）。
            builder = EventDispatcherHandler.builder(
                self._config.feishu_encrypt_key or "",
                "",
                level=LogLevel.INFO,
            )
            builder = builder.register_p2_im_message_receive_v1(self._on_ws_message)
            dispatcher = builder.build()

            ws_client = FeishuWSClient(
                self._config.feishu_app_id,
                self._config.feishu_app_secret,
                log_level=LogLevel.INFO,
                event_handler=dispatcher,
                domain=_resolve_feishu_domain(self._config.feishu_domain),
                auto_reconnect=bool(self._config.feishu_ws_auto_reconnect),
            )
            self._ws_client = ws_client
            self._start_event.set()
            ws_client.start()
        except RuntimeError as e:
            # 正常停机时，loop.stop 会导致 start() 抛出该异常，可忽略。
            if not self._stopping or "Event loop stopped before Future completed" not in str(e):
                self._start_error = e
                logger.error("feishu_ws_runtime_error", error=str(e))
        except Exception as e:
            self._start_error = e
            logger.error("feishu_ws_worker_failed", error=str(e))
        finally:
            self._start_event.set()
            self._close_thread_loop(loop)

    def _on_ws_message(self, ws_event: Any) -> None:
        parsed = parse_feishu_ws_event(
            ws_event,
            verification_token="",
            mention_required_in_group=self._config.feishu_mention_required_in_group,
            allow_chat_ids=self._config.feishu_allow_chat_ids,
            allow_user_ids=self._config.feishu_allow_user_ids,
        )
        if not parsed.ok:
            logger.warning("feishu_ws_message_invalid", detail=parsed.detail)
            return
        if not parsed.channel_event:
            logger.debug("feishu_ws_message_ignored", detail=parsed.detail)
            return

        if not self._main_loop:
            logger.warning("feishu_ws_main_loop_missing")
            return

        self._main_loop.call_soon_threadsafe(self._dispatch_parsed_event, parsed)

    def _dispatch_parsed_event(self, parsed: Any) -> None:
        if not self._main_loop:
            return
        asyncio.create_task(self._on_parsed_event(parsed))

    @staticmethod
    def _close_thread_loop(loop: asyncio.AbstractEventLoop) -> None:
        if loop.is_closed():
            return
        try:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        finally:
            try:
                loop.close()
            except Exception:
                pass


def _resolve_feishu_domain(domain: str) -> str:
    value = str(domain or "").strip()
    if not value or value == "feishu":
        return "https://open.feishu.cn"
    if value == "lark":
        return "https://open.larksuite.com"
    if value.startswith("http://") or value.startswith("https://"):
        return value.rstrip("/")
    return f"https://{value.rstrip('/')}"
