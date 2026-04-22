"""file_batch Skill - 批量文件操作

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [] }

功能：
  - rename: 批量重命名（支持前缀、后缀、序号、正则替换）
  - zip: 压缩文件/目录
  - unzip: 解压
  - organize: 按规则归类文件（按扩展名、按日期）
  - list: 列出目录下文件
"""

import json
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path


def cmd_rename(params: dict) -> dict:
    """批量重命名"""
    directory = Path(params.get("directory", "."))
    pattern = params.get("pattern", "")       # 匹配模式（glob）
    prefix = params.get("prefix", "")          # 添加前缀
    suffix = params.get("suffix", "")          # 添加后缀（扩展名前）
    replace_from = params.get("replace_from", "")  # 替换源
    replace_to = params.get("replace_to", "")      # 替换目标
    sequential = params.get("sequential", False)    # 序号重命名
    dry_run = params.get("dry_run", True)           # 默认试运行

    if not directory.exists():
        return {"reply": f"目录不存在: {directory}"}

    files = list(directory.glob(pattern or "*")) if pattern else [f for f in directory.iterdir() if f.is_file()]
    files = [f for f in files if f.is_file()]

    if not files:
        return {"reply": f"没有匹配的文件: {directory}/{pattern or '*'}"}

    renamed = []
    for i, f in enumerate(files, 1):
        stem = f.stem
        ext = f.suffix

        if replace_from and replace_to:
            new_stem = stem.replace(replace_from, replace_to)
        elif sequential:
            new_stem = f"{prefix}{i:03d}" if prefix else f"{i:03d}"
        elif prefix or suffix:
            new_stem = f"{prefix}{stem}{suffix}"
        else:
            continue

        new_name = f"{new_stem}{ext}"
        new_path = f.parent / new_name

        if dry_run:
            renamed.append(f"  {f.name} → {new_name}")
        else:
            try:
                f.rename(new_path)
                renamed.append(f"  {f.name} → {new_name}")
            except Exception as e:
                renamed.append(f"  {f.name} → 失败: {e}")

    mode = "试运行" if dry_run else "已执行"
    return {"reply": f"批量重命名 ({mode}, {len(renamed)} 个文件):\n" + "\n".join(renamed)}


def cmd_zip(params: dict) -> dict:
    """压缩文件/目录"""
    sources = params.get("sources", [])
    output = params.get("output", "archive.zip")

    if not sources:
        return {"reply": "请提供 sources 参数（文件/目录列表）"}

    try:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
            for src in sources:
                src_path = Path(src)
                if src_path.is_file():
                    zf.write(src_path, src_path.name)
                elif src_path.is_dir():
                    for file_path in src_path.rglob("*"):
                        if file_path.is_file():
                            arcname = file_path.relative_to(src_path.parent)
                            zf.write(file_path, arcname)

        size = Path(output).stat().st_size
        return {"reply": f"压缩完成: {output} ({size / 1024:.1f} KB)", "files": [output]}
    except Exception as e:
        return {"reply": f"压缩失败: {e}"}


def cmd_unzip(params: dict) -> dict:
    """解压"""
    archive = params.get("archive", "")
    output_dir = params.get("output_dir", ".")

    if not archive:
        return {"reply": "请提供 archive 参数"}

    if not Path(archive).exists():
        return {"reply": f"文件不存在: {archive}"}

    try:
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(output_dir)
            count = len(zf.namelist())

        return {"reply": f"解压完成: {archive} → {output_dir} ({count} 个文件)"}
    except Exception as e:
        return {"reply": f"解压失败: {e}"}


def cmd_organize(params: dict) -> dict:
    """按规则归类文件"""
    directory = Path(params.get("directory", "."))
    by = params.get("by", "extension")  # extension | date
    dry_run = params.get("dry_run", True)

    if not directory.exists():
        return {"reply": f"目录不存在: {directory}"}

    files = [f for f in directory.iterdir() if f.is_file()]
    if not files:
        return {"reply": f"目录为空: {directory}"}

    moves = []
    for f in files:
        if by == "extension":
            ext = f.suffix.lstrip(".") or "no_extension"
            dest_dir = directory / ext
        elif by == "date":
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            dest_dir = directory / mtime.strftime("%Y-%m")
        else:
            continue

        dest = dest_dir / f.name
        if dry_run:
            moves.append(f"  {f.name} → {dest_dir.name}/")
        else:
            dest_dir.mkdir(exist_ok=True)
            try:
                shutil.move(str(f), str(dest))
                moves.append(f"  {f.name} → {dest_dir.name}/")
            except Exception as e:
                moves.append(f"  {f.name} → 失败: {e}")

    mode = "试运行" if dry_run else "已执行"
    return {"reply": f"归类整理 ({mode}, {len(moves)} 个文件, 按{by}):\n" + "\n".join(moves)}


def cmd_list(params: dict) -> dict:
    """列出目录文件"""
    directory = Path(params.get("directory", "."))
    pattern = params.get("pattern", "*")
    recursive = params.get("recursive", False)

    if not directory.exists():
        return {"reply": f"目录不存在: {directory}"}

    if recursive:
        files = list(directory.rglob(pattern))
    else:
        files = list(directory.glob(pattern))

    files = [f for f in files if f.is_file()]
    if not files:
        return {"reply": f"没有文件: {directory}/{pattern}"}

    lines = []
    for f in sorted(files)[:100]:  # 最多显示 100 个
        size = f.stat().st_size
        if size > 1024 * 1024:
            size_str = f"{size / 1024 / 1024:.1f} MB"
        elif size > 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size} B"
        rel = f.relative_to(directory) if recursive else f.name
        lines.append(f"  {rel} ({size_str})")

    return {"reply": f"文件列表 ({len(files)} 个):\n" + "\n".join(lines)}


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    params = data.get("params", {})
    origin = data.get("origin_message", "")

    # 解析子命令
    subcmd = params.get("subcmd", "")
    if not subcmd:
        for kw in ["rename", "zip", "unzip", "organize", "list"]:
            if kw in origin.lower():
                subcmd = kw
                break
        if not subcmd:
            subcmd = "list"

    if subcmd == "rename":
        result = cmd_rename(params)
    elif subcmd == "zip":
        result = cmd_zip(params)
    elif subcmd == "unzip":
        result = cmd_unzip(params)
    elif subcmd == "organize":
        result = cmd_organize(params)
    elif subcmd == "list":
        result = cmd_list(params)
    else:
        result = {"reply": f"未知子命令: {subcmd}，可用: rename, zip, unzip, organize, list"}

    result["task_id"] = task_id
    result.setdefault("files", [])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
