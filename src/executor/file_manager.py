"""文件管理：按 task_id 隔离存储，安全流式下载

文件布局：
  output/
    {task_id}/
      {filename}  # 原始文件名，UTF-8
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import AsyncGenerator

import structlog

logger = structlog.get_logger(__name__)

SAFE_CHARS_RE = re.compile(r"[^\w\-_.\u4e00-\u9fff]")
MAX_FILENAME_LEN = 200


def _safe_filename(name: str) -> str:
    """规范化文件名，防止路径遍历和非法字符"""
    name = SAFE_CHARS_RE.sub("_", name)
    name = name.strip(" ./")
    if not name or name.startswith("_"):
        name = "unnamed_file"
    return name[:MAX_FILENAME_LEN]


class FileManager:
    """文件管理器：存储、检索、流式发送文件"""

    def __init__(self, output_dir: Path | None = None) -> None:
        import uuid
        self._output_dir = output_dir or (Path(__file__).parent.parent.parent / "output")
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def prepare_task_dir(self, task_id: str) -> Path:
        """为任务创建隔离目录"""
        td = self._output_dir / task_id[:16]
        td.mkdir(parents=True, exist_ok=True)
        return td

    def store_file(
        self,
        task_id: str,
        original_name: str,
        source_path: Path | None = None,
        content: bytes | None = None,
    ) -> tuple[str, int]:
        """存储文件，返回 (存储路径相对output_dir, 文件大小)

        优先从 source_path 复制，否则用 content 直接写入。
        """
        task_dir = self.prepare_task_dir(task_id)
        safe_name = _safe_filename(original_name)
        dest = task_dir / safe_name

        # 避免文件名冲突
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            counter = 1
            while dest.exists():
                dest = task_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            safe_name = dest.name

        if source_path:
            import shutil
            shutil.copy2(source_path, dest)
        elif content is not None:
            dest.write_bytes(content)
        else:
            raise ValueError("必须提供 source_path 或 content")

        size = dest.stat().st_size
        rel = dest.relative_to(self._output_dir)
        logger.debug("file_stored", path=str(rel), size=size)
        return str(rel), size

    async def stream_file(self, task_id: str, filename: str) -> tuple[AsyncGenerator[bytes, None], str, int]:
        """流式发送文件，返回 (字节生成器, MIME类型, 文件大小)

        path traversal 防护：filename 只允许 task_id[:16] 前缀目录下的文件。
        """
        # 安全校验
        safe_task = task_id[:16]
        filename = _safe_filename(filename)

        task_dir = self._output_dir / safe_task
        file_path = (task_dir / filename).resolve()

        # 防止 path traversal
        if not str(file_path).startswith(str(task_dir.resolve())):
            raise PermissionError(f"禁止访问: {filename}")

        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {filename}")

        size = file_path.stat().st_size
        mime = _mime_type(file_path.suffix)

        async def _gen():
            with open(file_path, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk

        return _gen(), mime, size

    def list_task_files(self, task_id: str) -> list[dict]:
        """列出任务目录下所有文件"""
        safe_task = task_id[:16]
        task_dir = self._output_dir / safe_task
        if not task_dir.exists():
            return []

        files = []
        for f in sorted(task_dir.iterdir()):
            if f.is_file():
                stat = f.stat()
                files.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "mime": _mime_type(f.suffix),
                })
        return files

    def file_exists(self, task_id: str, filename: str) -> bool:
        safe_task = task_id[:16]
        filename = _safe_filename(filename)
        return (self._output_dir / safe_task / filename).exists()


def _mime_type(ext: str) -> str:
    MAP = {
        ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
        ".zip": "application/zip", ".tar": "application/x-tar", ".gz": "application/gzip",
        ".json": "application/json", ".yaml": "application/yaml", ".yml": "application/yaml",
        ".txt": "text/plain", ".md": "text/markdown", ".html": "text/html",
        ".py": "text/x-python", ".js": "application/javascript",
        ".sh": "application/x-sh", ".css": "text/css",
    }
    ext = ext.lower()
    return MAP.get(ext, "application/octet-stream")
