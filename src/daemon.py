"""Raccoon 进程管理器

支持 start / stop / restart / status 命令，
通过 PID 文件管理后台守护进程。

用法：
  raccoon start [--http] [--port PORT]   # 后台启动
  raccoon stop                            # 停止
  raccoon restart [--http] [--port PORT]  # 重启
  raccoon status                          # 查看状态
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# PID 文件路径
_PID_DIR = Path.home() / ".raccoon"
_PID_FILE = _PID_DIR / "raccoon.pid"
_LOG_FILE = _PID_DIR / "raccoon.log"


def _ensure_pid_dir() -> None:
    _PID_DIR.mkdir(parents=True, exist_ok=True)


def _read_pid() -> int | None:
    """读取 PID 文件"""
    if not _PID_FILE.exists():
        return None
    try:
        return int(_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _write_pid(pid: int) -> None:
    """写入 PID 文件"""
    _ensure_pid_dir()
    _PID_FILE.write_text(str(pid))


def _remove_pid() -> None:
    """删除 PID 文件"""
    _PID_FILE.unlink(missing_ok=True)


def _is_running(pid: int) -> bool:
    """检查进程是否存活"""
    try:
        os.kill(pid, 0)  # signal 0 = 不发信号，只检查
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _find_raccoon_process(pid: int) -> bool:
    """检查 PID 对应的进程是否是 raccoon"""
    try:
        import psutil
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline())
        return "raccoon" in cmdline
    except (psutil.NoSuchProcess, psutil.AccessDenied, ImportError):
        # 没有 psutil 就只检查进程存活
        return _is_running(pid)


def status() -> dict:
    """查询 raccoon 运行状态

    Returns:
        {"running": bool, "pid": int|None, "uptime": str, "mode": str}
    """
    pid = _read_pid()
    if pid is None:
        return {"running": False, "pid": None, "uptime": "", "mode": ""}

    if not _is_running(pid):
        _remove_pid()  # 清理过期 PID
        return {"running": False, "pid": None, "uptime": "", "mode": ""}

    # 计算运行时长
    try:
        import psutil
        proc = psutil.Process(pid)
        create_time = proc.create_time()
        elapsed = int(time.time() - create_time)
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime = f"{hours}h {minutes}m {seconds}s"
        cmdline = " ".join(proc.cmdline())
        mode = "http" if "--http" in cmdline else "cli"
    except (ImportError, Exception):
        uptime = "unknown"
        mode = "unknown"

    return {"running": True, "pid": pid, "uptime": uptime, "mode": mode}


def start(http: bool = True, host: str = "0.0.0.0", port: int = 8900) -> bool:
    """后台启动 raccoon

    Args:
        http: 是否以 HTTP 模式启动
        host: 监听地址
        port: 监听端口

    Returns:
        是否启动成功
    """
    # 检查是否已在运行
    st = status()
    if st["running"]:
        logger.warning("raccoon_already_running", pid=st["pid"])
        print(f"Raccoon 已在运行中 (PID: {st['pid']}, 模式: {st['mode']}, 运行时长: {st['uptime']})")
        return False

    _ensure_pid_dir()

    # 构建启动命令
    python = sys.executable
    script = str(Path(__file__).resolve().parent.parent / "raccoon.py")
    cmd = [python, script]
    if http:
        cmd += ["--http", "--host", host, "--port", str(port)]

    # 后台启动
    log_f = open(_LOG_FILE, "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_f,
        stderr=log_f,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # 脱离终端
    )

    # 等一下确认启动成功
    time.sleep(1)
    if not _is_running(proc.pid):
        logger.error("raccoon_start_failed")
        print("❌ Raccoon 启动失败，查看日志: " + str(_LOG_FILE))
        return False

    _write_pid(proc.pid)

    mode_str = f"HTTP 模式 (http://{host}:{port})" if http else "CLI 模式"
    print(f"🦝 Raccoon 已启动 ({mode_str})")
    print(f"   PID: {proc.pid}")
    print(f"   日志: {_LOG_FILE}")

    return True


def stop() -> bool:
    """停止 raccoon

    Returns:
        是否停止成功
    """
    st = status()
    if not st["running"]:
        print("Raccoon 未在运行")
        return False

    pid = st["pid"]
    logger.info("stopping_raccoon", pid=pid)

    # 先发 SIGTERM，优雅退出
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pid()
        print("Raccoon 已停止（进程已不存在）")
        return True

    # 等待最多 5 秒
    for _ in range(10):
        time.sleep(0.5)
        if not _is_running(pid):
            _remove_pid()
            print("🦝 Raccoon 已停止")
            return True

    # 还没退出，SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
        print("🦝 Raccoon 已强制停止")
    except ProcessLookupError:
        pass

    _remove_pid()
    return True


def restart(http: bool = True, host: str = "0.0.0.0", port: int = 8900) -> bool:
    """重启 raccoon"""
    st = status()
    if st["running"]:
        print("正在停止 Raccoon...")
        stop()
        time.sleep(1)

    print("正在启动 Raccoon...")
    return start(http=http, host=host, port=port)
