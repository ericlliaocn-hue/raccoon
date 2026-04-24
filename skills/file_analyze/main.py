"""file_analyze Skill - 读取文本类文件内容并分析

支持文件类型：.txt/.md/.py/.js/.ts/.json/.yaml/.yml/.csv/.log/.html/.css/.sh/.sql/.xml/.toml/.ini/.cfg/.env/.go/.java/.rs/.rb/.php/.c/.cpp/.h/.hpp/.swift/.kt

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [] }
"""
import json
import sys
from pathlib import Path

HOME = Path.home()
MAX_FILE_SIZE = 1 * 1024 * 1024  # 1MB

TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".csv", ".log", ".html", ".htm",
    ".css", ".scss", ".less", ".sh", ".bash", ".zsh",
    ".sql", ".xml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".go", ".java", ".rs", ".rb", ".php", ".c", ".cpp", ".h", ".hpp",
    ".swift", ".kt", ".lua", ".r", ".R", ".m", ".mm",
    ".dockerfile", ".makefile", ".cmake",
    ".gitignore", ".editorconfig", ".properties",
    ".vue", ".svelte",
}


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    if b < 1048576:
        return f"{b / 1024:.1f}KB"
    if b < 1073741824:
        return f"{b / 1048576:.1f}MB"
    return f"{b / 1073741824:.1f}GB"


def _resolve_filepath(raw: str) -> Path | None:
    """解析文件路径，限制在 HOME 目录下"""
    raw = raw.strip().strip("'\"")
    if not raw:
        return None

    # 尝试绝对路径
    p = Path(raw)
    if p.is_absolute() and p.exists() and p.is_file():
        try:
            resolved = p.resolve()
            if str(resolved).startswith(str(HOME.resolve())):
                return resolved
        except Exception:
            pass

    # 尝试相对于 HOME
    p = HOME / raw
    if p.exists() and p.is_file():
        try:
            resolved = p.resolve()
            if str(resolved).startswith(str(HOME.resolve())):
                return resolved
        except Exception:
            pass

    # 尝试常见目录
    for subdir in ["Downloads", "Desktop", "Documents"]:
        p = HOME / subdir / raw
        if p.exists() and p.is_file():
            try:
                resolved = p.resolve()
                if str(resolved).startswith(str(HOME.resolve())):
                    return resolved
            except Exception:
                pass

    return None


def _extract_filepath(text: str) -> str:
    """从原始消息中提取文件路径"""
    for trigger in ["分析文件", "看看内容", "读一下", "查看文件", "打开文件", "分析一下", "文件内容", "看看文件", "读文件", "查看内容"]:
        text = text.replace(trigger, " ")
    text = text.strip()
    # 按空格/逗号分割，取含路径特征的片段
    segments = text.replace("，", ",").replace("。", ".").replace("、", ",").split(",")
    for seg in segments:
        seg = seg.strip()
        if "/" in seg or "\\" in seg or "." in seg:
            for prefix in ["文件夹里的", "文件夹上的", "里的", "上的", "的"]:
                if seg.startswith(prefix):
                    seg = seg[len(prefix):].strip()
            return seg
    candidates = [s.strip() for s in segments if s.strip()]
    return candidates[0] if candidates else text.strip()


def _analyze_content(content: str, path: Path) -> str:
    """对文件内容做简单分析摘要"""
    lines = content.splitlines()
    total_lines = len(lines)
    non_empty = sum(1 for line in lines if line.strip())
    ext = path.suffix.lower()

    parts = [f"📄 文件：{path.name}"]
    parts.append(f"   路径：{path}")
    parts.append(f"   大小：{_fmt_size(path.stat().st_size)}")
    parts.append(f"   行数：{total_lines}（非空 {non_empty}）")
    parts.append(f"   类型：{ext}")
    parts.append("")

    # 显示内容（限制 200 行）
    max_display = 200
    if total_lines <= max_display:
        parts.append("── 文件内容 ──")
        parts.append(content)
    else:
        parts.append(f"── 文件内容（前 {max_display} 行 / 共 {total_lines} 行）──")
        parts.append("\n".join(lines[:max_display]))
        parts.append(f"\n... 省略 {total_lines - max_display} 行")

    return "\n".join(parts)


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    origin = data.get("origin_message", "")
    params = data.get("params", {})

    # 提取文件路径
    raw = (params.get("rest") or "").strip()
    if raw:
        raw = _extract_filepath(raw)
    if not raw:
        raw = _extract_filepath(origin)

    if not raw:
        result = {
            "task_id": task_id,
            "reply": "❌ 未提供文件路径，请说「分析文件 /path/to/file」或「看看内容 config.yaml」",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    path = _resolve_filepath(raw)
    if not path:
        result = {
            "task_id": task_id,
            "reply": f"❌ 文件不存在或无法访问：{raw}\n请确保文件路径正确且在用户目录下",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    # 检查扩展名
    ext = path.suffix.lower()
    if ext and ext not in TEXT_EXTENSIONS:
        # 无扩展名也允许（如 Makefile, Dockerfile）
        if path.name.lower() not in {"makefile", "dockerfile", "vagrantfile", "gemfile", "rakefile", "procfile"}:
            result = {
                "task_id": task_id,
                "reply": f"⚠️ 暂不支持分析此类型文件：{ext}\n支持：代码/配置/文档/日志等文本文件",
                "files": [],
            }
            print(json.dumps(result, ensure_ascii=False))
            return

    # 检查文件大小
    size = path.stat().st_size
    if size > MAX_FILE_SIZE:
        result = {
            "task_id": task_id,
            "reply": f"⚠️ 文件过大（{_fmt_size(size)}），最大支持 {_fmt_size(MAX_FILE_SIZE)}\n建议用「发给我」直接下载查看",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    # 读取文件内容
    try:
        # 尝试 UTF-8，失败则尝试其他编码
        for encoding in ["utf-8", "gbk", "gb2312", "latin-1"]:
            try:
                content = path.read_text(encoding=encoding)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        else:
            result = {
                "task_id": task_id,
                "reply": f"❌ 无法解码文件：{path.name}，可能不是文本文件",
                "files": [],
            }
            print(json.dumps(result, ensure_ascii=False))
            return
    except Exception as e:
        result = {
            "task_id": task_id,
            "reply": f"❌ 读取文件失败：{e}",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    # 分析并返回
    analysis = _analyze_content(content, path)
    result = {
        "task_id": task_id,
        "reply": analysis,
        "files": [],
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
