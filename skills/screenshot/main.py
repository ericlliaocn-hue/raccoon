"""screenshot Skill - 截取屏幕截图并推送给用户

仅支持 macOS（使用 screencapture 命令）

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [{"path": "...", "name": "..."}] }
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
SCREENSHOT_DIR = HOME / "Downloads"


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    if b < 1048576:
        return f"{b / 1024:.1f}KB"
    if b < 1073741824:
        return f"{b / 1048576:.1f}MB"
    return f"{b / 1073741824:.1f}GB"


def _take_screenshot() -> Path | None:
    """使用 macOS screencapture 截取全屏"""
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"screenshot_{timestamp}.png"
    filepath = SCREENSHOT_DIR / filename

    try:
        # -x: 无声音  -t png: 格式
        subprocess.run(
            ["screencapture", "-x", "-t", "png", str(filepath)],
            timeout=10,
            check=True,
        )
        if filepath.exists() and filepath.stat().st_size > 0:
            return filepath
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError):
        pass

    return None


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")

    # 确保目录存在
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    # 截图
    filepath = _take_screenshot()

    if not filepath:
        result = {
            "task_id": task_id,
            "reply": "❌ 截图失败，请确保屏幕未锁定且有权限访问屏幕录制",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    size = filepath.stat().st_size
    result = {
        "task_id": task_id,
        "reply": f"📸 截图成功！[{filepath.name}]（{_fmt_size(size)}），正在推送...",
        "files": [{"path": str(filepath), "name": filepath.name}],
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
