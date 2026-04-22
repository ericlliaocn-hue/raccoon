"""file_read Skill - 读取本地文件，通过 Web UI 发送给用户

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [{"path": "...", "name": "..."}] }
"""
import json
import sys
from pathlib import Path

HOME = Path.home()
DOWNLOADS = HOME / "Downloads"


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    if b < 1048576:
        return f"{b / 1024:.1f}KB"
    if b < 1073741824:
        return f"{b / 1048576:.1f}MB"
    return f"{b / 1073741824:.1f}GB"


def _resolve_filename(raw: str) -> Path | None:
    """解析文件名到真实路径，优先 Downloads 文件夹"""
    raw = raw.strip()
    if not raw:
        return None
    # 直接Downloads下
    p = DOWNLOADS / raw
    if p.exists() and p.is_file():
        return p
    # 绝对路径
    p2 = Path(raw)
    if p2.is_absolute() and p2.exists() and p2.is_file():
        # 安全检查：只能在 HOME 下
        try:
            resolved = p2.resolve()
            if str(resolved).startswith(str(HOME.resolve())):
                return resolved
        except Exception:
            pass
    return None


def _extract_filename(text: str) -> str:
    """从原始消息中提取文件名（去掉触发词和无关内容）"""
    # 去掉常见触发词
    for trigger in ["发给我", "发过来", "下载"]:
        text = text.replace(trigger, " ")
    # 按逗号/句号分割，取含文件扩展名的片段
    segments = text.replace("，", ",").replace("。", ".").replace("、", ",").split(",")
    for seg in segments:
        seg = seg.strip()
        # 如果包含扩展名（如 .mp4），优先选它
        if "." in seg:
            # 去掉前缀修饰词
            for prefix in ["文件夹上有", "文件夹里的", "里的", "上的", "的"]:
                if seg.startswith(prefix):
                    seg = seg[len(prefix):].strip()
            return seg
    # 没有扩展名的，返回最长的非空段
    candidates = [s.strip() for s in segments if s.strip()]
    return candidates[0] if candidates else text.strip()


ALLOWED_EXTENSIONS = {
    ".mp4", ".webm", ".mov",
    ".mp3", ".wav", ".ogg", ".m4a",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".pdf", ".zip", ".tar", ".gz",
    ".txt", ".md", ".json", ".yaml", ".yml",
    ".py", ".js", ".sh", ".css", ".html",
}


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    origin = data.get("origin_message", "")
    params = data.get("params", {})

    # 优先用 params.rest，否则从 origin_message 解析
    # params.rest 可能是自然语言片段（如 "下载文件夹上有a.mp4,"），需要提取文件名
    raw = (params.get("rest") or "").strip()
    if raw:
        raw = _extract_filename(raw)
    if not raw:
        raw = _extract_filename(origin)

    if not raw:
        result = {
            "task_id": task_id,
            "reply": "❌ 未提供文件名，请说「a.mp4 发给我」或「下载 文件路径」",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    path = _resolve_filename(raw)
    if not path:
        result = {
            "task_id": task_id,
            "reply": f"❌ 文件不存在或无法访问：{raw}\n请确保文件在 ~/Downloads 文件夹中",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    ext = path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        result = {
            "task_id": task_id,
            "reply": f"⚠️ 暂不支持此类型文件：{ext}\n支持：视频/音频/图片/PDF/代码等",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    size = path.stat().st_size
    result = {
        "task_id": task_id,
        "reply": f"📎 找到文件 [{path.name}]（{_fmt_size(size)}），正在推送给你...",
        "files": [{"path": str(path), "name": path.name}],
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
