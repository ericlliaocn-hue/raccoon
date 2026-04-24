"""log_watcher Skill - 日志监控

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [], "matches": [...] }

功能：
  - tail: 读取日志文件末尾 N 行
  - search: 搜索关键词
  - errors: 提取错误行
  - stats: 日志统计（行数、错误数、时间范围）
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

MAX_LINES = 500
ERROR_PATTERNS = [
    r"\bERROR\b", r"\bFATAL\b", r"\bCRITICAL\b", r"\bException\b",
    r"\bTraceback\b", r"\bpanic\b", r"\bFAIL\b",
]


def cmd_tail(params: dict) -> dict:
    """读取日志末尾"""
    path = params.get("path", "")
    lines = params.get("lines", 50)

    if not path:
        return {"reply": "请提供 path 参数", "matches": []}

    log_path = Path(path)
    if not log_path.exists():
        return {"reply": f"文件不存在: {path}", "matches": []}

    try:
        all_lines = log_path.read_text("utf-8", errors="replace").splitlines()
        tail = all_lines[-lines:]
        return {"reply": f"日志末尾 {len(tail)} 行:\n```\n" + "\n".join(tail) + "\n```", "matches": []}
    except Exception as e:
        return {"reply": f"读取失败: {e}", "matches": []}


def cmd_search(params: dict) -> dict:
    """搜索关键词"""
    path = params.get("path", "")
    keyword = params.get("keyword", "")
    regex = params.get("regex", "")
    context = params.get("context", 2)  # 上下文行数
    max_results = params.get("max_results", 50)

    if not path:
        return {"reply": "请提供 path 参数", "matches": []}
    if not keyword and not regex:
        return {"reply": "请提供 keyword 或 regex 参数", "matches": []}

    log_path = Path(path)
    if not log_path.exists():
        return {"reply": f"文件不存在: {path}", "matches": []}

    try:
        all_lines = log_path.read_text("utf-8", errors="replace").splitlines()
    except Exception as e:
        return {"reply": f"读取失败: {e}", "matches": []}

    # 编译正则
    try:
        if regex:
            pattern = re.compile(regex, re.IGNORECASE)
        else:
            pattern = re.compile(re.escape(keyword), re.IGNORECASE)
    except re.error as e:
        return {"reply": f"正则表达式错误: {e}", "matches": []}

    matches = []
    for i, line in enumerate(all_lines):
        if pattern.search(line):
            start = max(0, i - context)
            end = min(len(all_lines), i + context + 1)
            ctx_lines = all_lines[start:end]
            matches.append({
                "line_number": i + 1,
                "line": line[:200],
                "context": [
                    f"{line_no + 1}: {ctx_line}"
                    for line_no, ctx_line in enumerate(ctx_lines, start=start)
                ],
            })
            if len(matches) >= max_results:
                break

    if not matches:
        return {"reply": f"未找到匹配: {keyword or regex}", "matches": []}

    result_lines = []
    for m in matches:
        result_lines.append(f"L{m['line_number']}: {m['line'][:100]}")

    return {
        "reply": f"找到 {len(matches)} 处匹配 (关键词: {keyword or regex}):\n" + "\n".join(result_lines),
        "matches": matches,
    }


def cmd_errors(params: dict) -> dict:
    """提取错误行"""
    path = params.get("path", "")
    max_results = params.get("max_results", 50)

    if not path:
        return {"reply": "请提供 path 参数", "matches": []}

    log_path = Path(path)
    if not log_path.exists():
        return {"reply": f"文件不存在: {path}", "matches": []}

    try:
        all_lines = log_path.read_text("utf-8", errors="replace").splitlines()
    except Exception as e:
        return {"reply": f"读取失败: {e}", "matches": []}

    error_re = re.compile("|".join(ERROR_PATTERNS), re.IGNORECASE)
    errors = []
    for i, line in enumerate(all_lines):
        if error_re.search(line):
            errors.append({"line_number": i + 1, "line": line[:200]})
            if len(errors) >= max_results:
                break

    if not errors:
        return {"reply": "未检测到错误行", "matches": []}

    result_lines = [f"L{e['line_number']}: {e['line'][:100]}" for e in errors]
    return {
        "reply": f"检测到 {len(errors)} 处错误:\n" + "\n".join(result_lines),
        "matches": errors,
    }


def cmd_stats(params: dict) -> dict:
    """日志统计"""
    path = params.get("path", "")

    if not path:
        return {"reply": "请提供 path 参数", "matches": []}

    log_path = Path(path)
    if not log_path.exists():
        return {"reply": f"文件不存在: {path}", "matches": []}

    try:
        all_lines = log_path.read_text("utf-8", errors="replace").splitlines()
    except Exception as e:
        return {"reply": f"读取失败: {e}", "matches": []}

    total = len(all_lines)
    size = log_path.stat().st_size

    # 统计错误
    error_re = re.compile("|".join(ERROR_PATTERNS), re.IGNORECASE)
    error_count = sum(1 for line in all_lines if error_re.search(line))

    # 统计日志级别
    level_re = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|FATAL|CRITICAL)\b")
    levels = Counter()
    for line in all_lines:
        m = level_re.search(line)
        if m:
            levels[m.group(1)] += 1

    stats_lines = [
        f"文件: {path}",
        f"大小: {size / 1024:.1f} KB",
        f"总行数: {total}",
        f"错误行数: {error_count}",
    ]
    if levels:
        stats_lines.append("日志级别分布:")
        for level, count in levels.most_common():
            stats_lines.append(f"  {level}: {count}")

    return {"reply": "\n".join(stats_lines), "matches": []}


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    params = data.get("params", {})
    origin = data.get("origin_message", "")

    # 解析子命令
    subcmd = params.get("subcmd", "")
    if not subcmd:
        for kw in ["tail", "search", "errors", "stats"]:
            if kw in origin.lower():
                subcmd = kw
                break
        if not subcmd:
            subcmd = "tail"

    if subcmd == "tail":
        result = cmd_tail(params)
    elif subcmd == "search":
        result = cmd_search(params)
    elif subcmd == "errors":
        result = cmd_errors(params)
    elif subcmd == "stats":
        result = cmd_stats(params)
    else:
        result = {"reply": f"未知子命令: {subcmd}，可用: tail, search, errors, stats"}

    result["task_id"] = task_id
    result.setdefault("files", [])
    result.setdefault("matches", [])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
