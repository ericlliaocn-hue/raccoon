"""媒体推送：任务结果（文字+文件）推送到对话

接入 Web UI 文件流：
- 任务完成时扫描 result.files，复制到 output/{task_id}/
- SSE 推送 file_ready 事件含文件元信息
- Web UI 通过 GET /files/{task_id}/{filename} 流式读取并内联渲染
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from src.executor.file_manager import FileManager, _mime_type
from src.types import Task

logger = structlog.get_logger(__name__)


class MediaPusher:
    """媒体推送器：将任务结果（文字+文件）推送到对话"""

    def __init__(self, file_manager: FileManager | None = None) -> None:
        self._fm: FileManager | None = file_manager

    def set_file_manager(self, fm: FileManager) -> None:
        self._fm = fm

    async def push(self, task: Task, conversation_id: str) -> list[dict]:
        """推送任务结果到对话，返回文件元信息列表供 SSE 注入"""
        files_info: list[dict] = []

        if task.status.value == "success":
            logger.info(
                "media_push_result",
                task_id=task.task_id,
                conversation_id=conversation_id,
                skill=task.skill_name,
                status="success",
            )
        else:
            logger.info(
                "media_push_failure",
                task_id=task.task_id,
                conversation_id=conversation_id,
                skill=task.skill_name,
                error=task.error,
            )

        # 处理 result.files（Skill 返回的文件路径列表）
        result = task.result or {}
        raw_files: list = []
        if isinstance(result, dict):
            raw_files = result.get("files", [])
        elif isinstance(result, list):
            raw_files = result

        logger.debug("media_push_check", task_id=task.task_id, has_fm=bool(self._fm), raw_files_count=len(raw_files), result_keys=list(result.keys()) if isinstance(result, dict) else type(result).__name__)

        if raw_files and self._fm:
            for entry in raw_files:
                if isinstance(entry, dict):
                    src_path = Path(entry.get("path", ""))
                    display_name = entry.get("name", src_path.name)
                else:
                    src_path = Path(str(entry))
                    display_name = src_path.name

                if not src_path.exists():
                    logger.warning("file_not_found", path=str(src_path))
                    continue

                try:
                    # 在线程池执行同步 IO（不阻塞 event loop）
                    rel_path, size = await asyncio.to_thread(
                        self._fm.store_file,
                        task.task_id, display_name, source_path=src_path
                    )
                    files_info.append({
                        "name": display_name,
                        "path": rel_path,
                        "size": size,
                        "mime": _mime_type(Path(display_name).suffix),
                    })
                    logger.debug("file_pushed", name=display_name, path=rel_path, size=size)
                except Exception as e:
                    logger.error("file_push_failed", name=display_name, error=str(e))

        return files_info

    async def push_progress(self, task: Task, message: str) -> None:
        """推送任务进度"""
        logger.info(
            "media_push_progress",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            message=message,
        )
