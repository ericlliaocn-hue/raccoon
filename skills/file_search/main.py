"""file_search Skill - 搜索本地文件并推送给用户

支持搜索范围：
- ~/Downloads（默认）
- ~/Desktop
- ~/Documents
- 自定义 HOME 子目录

搜索条件：
- 关键词（文件名包含）
- 扩展名（如 .mp4 .pdf）
- 修改时间（今天/最近N天）

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [{"path": "...", "name": "..."}] }
"""
import json
import sys
import time
from pathlib import Path

HOME = Path.home()
SEARCH_DIRS = [
    HOME / "Downloads",
    HOME / "Desktop",
    HOME / "Documents",
]
DIR_ALIASES = {
    "下载": "Downloads", "下载文件夹": "Downloads",
    "桌面": "Desktop", "桌面文件夹": "Desktop",
    "文档": "Documents", "文档文件夹": "Documents",
    "downloads": "Downloads", "desktop": "Desktop", "documents": "Documents",
}
ALLOWED_EXTENSIONS = {
    ".mp4", ".webm", ".mov",
    ".mp3", ".wav", ".ogg", ".m4a",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".pdf", ".zip", ".tar", ".gz",
    ".txt", ".md", ".json", ".yaml", ".yml",
    ".py", ".js", ".sh", ".css", ".html",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".csv", ".log",
}
MAX_RESULTS = 10
MAX_DEPTH = 3


def _fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    if b < 1048576:
        return f"{b / 1024:.1f}KB"
    if b < 1073741824:
        return f"{b / 1048576:.1f}MB"
    return f"{b / 1073741824:.1f}GB"


def _fmt_time(ts: float) -> str:
    """格式化修改时间"""
    diff = time.time() - ts
    if diff < 3600:
        return f"{int(diff / 60)}分钟前"
    if diff < 86400:
        return f"{int(diff / 3600)}小时前"
    if diff < 604800:
        return f"{int(diff / 86400)}天前"
    return time.strftime("%m-%d", time.localtime(ts))


def _parse_query(text: str) -> dict:
    """从自然语言中解析搜索条件

    支持的输入格式：
    - "找一下 mp4" → 关键词: mp4
    - "搜索文件 视频" → 关键词: 视频
    - "找文件 下载文件夹里的 mp4" → 目录: Downloads, 关键词: mp4
    - "找一下最近的 pdf" → 关键词: pdf, 时间: 最近7天
    - "搜一下桌面的截图" → 目录: Desktop, 关键词: 截图
    - "找一下今天修改的文档" → 时间: 今天, 关键词: 文档
    """
    query = {
        "keyword": "",
        "extension": "",
        "dir": None,  # None = 搜索所有默认目录
        "days": 0,    # 0 = 不限时间
    }

    # 去掉触发词
    for trigger in ["找文件", "搜索文件", "查找文件", "找一下", "搜一下", "find file", "search file"]:
        text = text.replace(trigger, " ")
    text = text.strip()

    if not text:
        return query

    # 解析目录（长的 alias 优先匹配）
    sorted_aliases = sorted(DIR_ALIASES.items(), key=lambda x: len(x[0]), reverse=True)
    for alias, dir_name in sorted_aliases:
        # 匹配 "下载里的" "下载文件夹上的" 等变体
        for suffix in ["文件夹里的", "文件夹上的", "里的", "上的", "中的", "的", "文件夹"]:
            pattern = alias + suffix
            if pattern in text:
                query["dir"] = dir_name
                text = text.replace(pattern, " ")
                break
        if query["dir"]:
            break

    text = text.strip()

    # 解析时间范围（长的 pattern 优先匹配）
    time_patterns = [
        ("最近一个月", 30), ("最近30天", 30),
        ("最近三天", 3), ("最近3天", 3),
        ("最近一周", 7), ("最近7天", 7),
        ("最近的", 7), ("最近", 7),
        ("今天的", 1), ("今天", 1), ("今日", 1),
        ("这周", 7), ("本周", 7),
    ]
    for pattern, days in time_patterns:
        if pattern in text:
            query["days"] = days
            text = text.replace(pattern, " ")
            break

    text = text.strip()

    # 解析扩展名
    ext_map = {
        "视频": [".mp4", ".webm", ".mov", ".avi", ".mkv"],
        "音频": [".mp3", ".wav", ".ogg", ".m4a", ".flac"],
        "音乐": [".mp3", ".wav", ".ogg", ".m4a", ".flac"],
        "图片": [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"],
        "照片": [".jpg", ".jpeg", ".png", ".heic"],
        "截图": [".png", ".jpg", ".jpeg"],
        "文档": [".pdf", ".doc", ".docx", ".txt", ".md"],
        "代码": [".py", ".js", ".sh", ".css", ".html", ".json"],
        "压缩包": [".zip", ".tar", ".gz", ".rar"],
        "表格": [".xls", ".xlsx", ".csv"],
    }

    remaining = text
    for category, exts in ext_map.items():
        if category in remaining:
            query["extension"] = ",".join(exts)
            # 去掉类型词及其后的"的"
            idx = remaining.find(category)
            end = idx + len(category)
            # 检查类型词后面是否有"的"或"文件"
            while end < len(remaining) and remaining[end:end+1] in ("的", "文件"):
                if remaining[end:end+2] == "文件":
                    end += 2
                else:
                    end += 1
            remaining = remaining[:idx] + " " + remaining[end:]

    # 检查是否有显式扩展名（如 .mp4）
    for part in remaining.split():
        if part.startswith(".") and len(part) > 1:
            query["extension"] = part.lower()
            remaining = remaining.replace(part, " ")

    # 剩余文本作为关键词
    keyword = remaining.strip().strip("，。、").strip()
    # 清理无意义的残留词
    for noise in ["修改的", "的", "文件"]:
        keyword = keyword.replace(noise, " ")
    keyword = keyword.strip()
    if keyword:
        query["keyword"] = keyword

    return query


def _search_files(query: dict) -> list[dict]:
    """执行文件搜索，返回匹配的文件列表"""
    results = []
    keyword = query["keyword"].lower()
    extensions = query["extension"]
    days = query["days"]
    dir_name = query["dir"]

    # 确定搜索目录
    if dir_name:
        search_dir = HOME / dir_name
        if search_dir.exists():
            dirs = [search_dir]
        else:
            return []
    else:
        dirs = [d for d in SEARCH_DIRS if d.exists()]

    # 解析扩展名过滤
    ext_filter = set()
    if extensions:
        for ext in extensions.split(","):
            ext_filter.add(ext.lower().strip())

    # 时间阈值
    time_threshold = time.time() - (days * 86400) if days > 0 else 0

    for search_dir in dirs:
        try:
            results.extend(_walk_dir(search_dir, keyword, ext_filter, time_threshold, depth=0))
        except PermissionError:
            continue

    # 按修改时间排序（新的在前）
    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results[:MAX_RESULTS]


def _walk_dir(
    directory: Path,
    keyword: str,
    ext_filter: set[str],
    time_threshold: float,
    depth: int,
) -> list[dict]:
    """递归遍历目录搜索文件"""
    if depth > MAX_DEPTH:
        return []

    results = []
    try:
        entries = list(directory.iterdir())
    except PermissionError:
        return []

    for entry in entries:
        if entry.name.startswith("."):
            continue

        if entry.is_file():
            # 安全检查：只能在 HOME 下
            try:
                resolved = entry.resolve()
                if not str(resolved).startswith(str(HOME.resolve())):
                    continue
            except Exception:
                continue

            # 扩展名检查
            ext = entry.suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                continue
            if ext_filter and ext not in ext_filter:
                continue

            # 关键词检查
            name_lower = entry.name.lower()
            if keyword and keyword not in name_lower:
                continue

            # 时间检查
            try:
                stat = entry.stat()
                if time_threshold and stat.st_mtime < time_threshold:
                    continue
            except OSError:
                continue

            results.append({
                "path": str(entry),
                "name": entry.name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "ext": ext,
            })

        elif entry.is_dir() and depth < MAX_DEPTH:
            results.extend(_walk_dir(entry, keyword, ext_filter, time_threshold, depth + 1))

    return results


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    origin = data.get("origin_message", "")
    params = data.get("params", {})

    # 解析搜索条件
    raw = (params.get("rest") or "").strip()
    if raw:
        query = _parse_query(raw)
    else:
        query = _parse_query(origin)

    # 如果没有任何条件，返回提示
    if not query["keyword"] and not query["extension"] and not query["dir"] and not query["days"]:
        result = {
            "task_id": task_id,
            "reply": "🔍 请告诉我你要找什么文件？\n例如：\n• 找一下 mp4\n• 搜索文件 视频\n• 找一下最近的 pdf\n• 搜一下桌面的截图",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    # 执行搜索
    matches = _search_files(query)

    if not matches:
        desc_parts = []
        if query["keyword"]:
            desc_parts.append(f"关键词「{query['keyword']}」")
        if query["extension"]:
            desc_parts.append(f"类型「{query['extension']}」")
        if query["dir"]:
            desc_parts.append(f"目录「{query['dir']}」")
        if query["days"]:
            desc_parts.append(f"时间「最近{query['days']}天」")
        desc = "、".join(desc_parts) or "所有文件"

        result = {
            "task_id": task_id,
            "reply": f"🔍 没有找到匹配的文件（{desc}）\n试试换个关键词或扩大搜索范围？",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    # 构建结果
    # 如果只有一个结果，直接发送文件
    if len(matches) == 1:
        m = matches[0]
        result = {
            "task_id": task_id,
            "reply": f"📎 找到 1 个文件：[{m['name']}]（{_fmt_size(m['size'])}，{_fmt_time(m['mtime'])}），正在推送...",
            "files": [{"path": m["path"], "name": m["name"]}],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    # 多个结果：列出列表，发送所有文件
    lines = [f"🔍 找到 {len(matches)} 个文件："]
    files = []
    for i, m in enumerate(matches, 1):
        lines.append(f"  {i}. [{m['name']}]（{_fmt_size(m['size'])}，{_fmt_time(m['mtime'])}）")
        files.append({"path": m["path"], "name": m["name"]})

    lines.append("\n正在推送所有文件...")

    result = {
        "task_id": task_id,
        "reply": "\n".join(lines),
        "files": files,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
