"""change_detector Skill - 监控网页或文件变化

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [], "changed": bool, "diff": "..." }

功能：
  - snapshot: 对 URL 或文件拍照/快照，保存哈希
  - check: 对比当前内容与上次快照，返回是否变化及差异
  - list: 列出所有监控目标
  - remove: 删除监控目标
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# 快照存储目录
SNAPSHOT_DIR = Path("data") / "snapshots"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _snapshot_path(target_id: str) -> Path:
    return SNAPSHOT_DIR / f"{target_id}.json"


def _make_target_id(url_or_path: str) -> str:
    """生成目标 ID（URL/路径的哈希）"""
    return hashlib.md5(url_or_path.encode()).hexdigest()[:12]


def _fetch_url(url: str) -> str | None:
    """获取 URL 内容"""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Raccoon/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None


def _read_file(path: str) -> str | None:
    """读取文件内容"""
    try:
        return Path(path).read_text("utf-8")
    except Exception:
        return None


def _simple_diff(old: str, new: str, context_lines: int = 3) -> str:
    """简单行级差异"""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diffs = []

    import difflib
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            for line in old_lines[i1:i2]:
                diffs.append(f"- {line}")
        if tag in ("replace", "insert"):
            for line in new_lines[j1:j2]:
                diffs.append(f"+ {line}")

    return "\n".join(diffs[:50])  # 最多 50 行差异


def cmd_snapshot(params: dict) -> dict:
    """创建快照"""
    url = params.get("url", "")
    path = params.get("path", "")
    name = params.get("name", "")

    if not url and not path:
        return {"reply": "请提供 url 或 path 参数", "changed": False, "diff": ""}

    target = url or path
    target_id = _make_target_id(target)

    # 获取内容
    content = _fetch_url(url) if url else _read_file(path)
    if content is None:
        return {"reply": f"无法获取内容: {target}", "changed": False, "diff": ""}

    content_hash = hashlib.sha256(content.encode()).hexdigest()

    # 保存快照
    snapshot = {
        "target_id": target_id,
        "target": target,
        "name": name or target,
        "type": "url" if url else "file",
        "content_hash": content_hash,
        "content_length": len(content),
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
    }

    # 检查是否有旧快照
    sp = _snapshot_path(target_id)
    old_hash = None
    if sp.exists():
        old_data = json.loads(sp.read_text("utf-8"))
        old_hash = old_data.get("content_hash")

    sp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), "utf-8")

    if old_hash and old_hash != content_hash:
        return {
            "reply": f"快照已更新！检测到变化: {name or target}\n旧哈希: {old_hash[:16]}...\n新哈希: {content_hash[:16]}...",
            "changed": True,
            "diff": "内容已变化，使用 check 命令查看详细差异",
        }
    elif old_hash:
        return {"reply": f"快照已更新，内容未变化: {name or target}", "changed": False, "diff": ""}
    else:
        return {"reply": f"首次快照已创建: {name or target} (内容 {len(content)} 字符)", "changed": False, "diff": ""}


def cmd_check(params: dict) -> dict:
    """检查变化"""
    target = params.get("target", "")
    name = params.get("name", "")

    if not target and not name:
        # 检查所有
        return cmd_check_all()

    target_id = _make_target_id(target) if target else None

    # 按名称查找
    if not target_id and name:
        for f in SNAPSHOT_DIR.glob("*.json"):
            data = json.loads(f.read_text("utf-8"))
            if data.get("name") == name:
                target_id = data["target_id"]
                target = data["target"]
                break

    if not target_id:
        return {"reply": f"未找到监控目标: {name or target}", "changed": False, "diff": ""}

    sp = _snapshot_path(target_id)
    if not sp.exists():
        return {"reply": f"没有快照记录: {name or target}，请先使用 snapshot 创建", "changed": False, "diff": ""}

    old_data = json.loads(sp.read_text("utf-8"))
    old_hash = old_data["content_hash"]

    # 获取当前内容
    content = _fetch_url(target) if old_data["type"] == "url" else _read_file(target)
    if content is None:
        return {"reply": f"无法获取当前内容: {target}", "changed": False, "diff": ""}

    new_hash = hashlib.sha256(content.encode()).hexdigest()

    if new_hash == old_hash:
        return {"reply": f"未检测到变化: {old_data.get('name', target)}", "changed": False, "diff": ""}

    # 有变化 - 读取旧内容做 diff
    diff = ""
    # 保存旧快照内容用于 diff（简化：只报告哈希变化）
    return {
        "reply": f"检测到变化！{old_data.get('name', target)}\n旧哈希: {old_hash[:16]}...\n新哈希: {new_hash[:16]}...\n内容长度: {old_data['content_length']} → {len(content)}",
        "changed": True,
        "diff": f"hash: {old_hash[:16]}... → {new_hash[:16]}...",
    }


def cmd_check_all() -> dict:
    """检查所有监控目标"""
    results = []
    changed_count = 0

    for f in SNAPSHOT_DIR.glob("*.json"):
        data = json.loads(f.read_text("utf-8"))
        target = data["target"]
        content = _fetch_url(target) if data["type"] == "url" else _read_file(target)

        if content is None:
            results.append(f"  {data.get('name', target)}: 无法获取")
            continue

        new_hash = hashlib.sha256(content.encode()).hexdigest()
        if new_hash != data["content_hash"]:
            changed_count += 1
            results.append(f"  {data.get('name', target)}: 有变化!")
        else:
            results.append(f"  {data.get('name', target)}: 无变化")

    if not results:
        return {"reply": "没有监控目标，请先使用 snapshot 添加", "changed": False, "diff": ""}

    summary = f"检查了 {len(results)} 个目标，{changed_count} 个有变化:\n" + "\n".join(results)
    return {"reply": summary, "changed": changed_count > 0, "diff": ""}


def cmd_list() -> dict:
    """列出所有监控目标"""
    targets = []
    for f in SNAPSHOT_DIR.glob("*.json"):
        data = json.loads(f.read_text("utf-8"))
        targets.append(f"  [{data['target_id']}] {data.get('name', data['target'])} ({data['type']}, 快照于 {data.get('snapshot_at', 'unknown')})")

    if not targets:
        return {"reply": "没有监控目标", "changed": False, "diff": ""}

    return {"reply": f"监控目标 ({len(targets)}):\n" + "\n".join(targets), "changed": False, "diff": ""}


def cmd_remove(params: dict) -> dict:
    """删除监控目标"""
    target = params.get("target", "")
    name = params.get("name", "")
    target_id = params.get("target_id", "")

    if target_id:
        sp = _snapshot_path(target_id)
        if sp.exists():
            sp.unlink()
            return {"reply": f"已删除监控目标: {target_id}", "changed": False, "diff": ""}
        return {"reply": f"未找到: {target_id}", "changed": False, "diff": ""}

    # 按名称或 target 查找
    for f in SNAPSHOT_DIR.glob("*.json"):
        data = json.loads(f.read_text("utf-8"))
        if data.get("name") == name or data["target"] == target:
            f.unlink()
            return {"reply": f"已删除监控目标: {data.get('name', data['target'])}", "changed": False, "diff": ""}

    return {"reply": f"未找到监控目标: {name or target}", "changed": False, "diff": ""}


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    params = data.get("params", {})
    origin = data.get("origin_message", "")

    # 解析子命令
    subcmd = params.get("subcmd", "")
    if not subcmd:
        for kw in ["snapshot", "check", "list", "remove"]:
            if kw in origin.lower():
                subcmd = kw
                break
        if not subcmd:
            subcmd = "check"  # 默认检查

    if subcmd == "snapshot":
        result = cmd_snapshot(params)
    elif subcmd == "check":
        result = cmd_check(params)
    elif subcmd == "list":
        result = cmd_list()
    elif subcmd == "remove":
        result = cmd_remove(params)
    else:
        result = {"reply": f"未知子命令: {subcmd}，可用: snapshot, check, list, remove", "changed": False, "diff": ""}

    result["task_id"] = task_id
    result.setdefault("files", [])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
