"""git_helper Skill - Git 操作助手

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [] }

功能：
  - log: 查看 git log（最近 N 条）
  - diff: 查看 diff（指定文件或全部）
  - status: 查看 git status
  - branch: 列出分支
  - stash: stash 操作
"""

import json
import subprocess
import sys
from pathlib import Path

MAX_OUTPUT = 10240  # 10KB


def _run_git(args: list[str], cwd: str | None = None) -> tuple[int, str]:
    """运行 git 命令"""
    try:
        proc = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd or str(Path.cwd()),
        )
        output = proc.stdout or ""
        if proc.stderr:
            output += f"\n[stderr]\n{proc.stderr}"
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + f"\n\n... (截断，共 {len(output)} 字符)"
        return proc.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return -1, "命令超时"
    except FileNotFoundError:
        return -1, "git 未安装"
    except Exception as e:
        return -1, str(e)


def cmd_log(params: dict) -> dict:
    """查看 git log"""
    count = params.get("count", 10)
    oneline = params.get("oneline", True)
    repo = params.get("repo", None)

    args = ["log", f"-{count}"]
    if oneline:
        args.append("--oneline")
    args.extend(["--decorate", "--all"])

    code, output = _run_git(args, cwd=repo)
    if code != 0:
        return {"reply": f"git log 失败: {output}"}
    return {"reply": f"最近 {count} 条提交:\n```\n{output}\n```"}


def cmd_diff(params: dict) -> dict:
    """查看 diff"""
    target = params.get("target", "")  # 文件或 commit
    staged = params.get("staged", False)
    repo = params.get("repo", None)

    args = ["diff"]
    if staged:
        args.append("--staged")
    if target:
        args.append(target)

    code, output = _run_git(args, cwd=repo)
    if code != 0:
        return {"reply": f"git diff 失败: {output}"}
    if not output:
        return {"reply": "没有差异"}
    return {"reply": f"```\n{output}\n```"}


def cmd_status(params: dict) -> dict:
    """查看 git status"""
    repo = params.get("repo", None)
    short = params.get("short", True)

    args = ["status"]
    if short:
        args.append("--short")

    code, output = _run_git(args, cwd=repo)
    if code != 0:
        return {"reply": f"git status 失败: {output}"}
    return {"reply": f"```\n{output}\n```"}


def cmd_branch(params: dict) -> dict:
    """列出分支"""
    repo = params.get("repo", None)
    all_branches = params.get("all", False)

    args = ["branch"]
    if all_branches:
        args.append("--all")

    code, output = _run_git(args, cwd=repo)
    if code != 0:
        return {"reply": f"git branch 失败: {output}"}
    return {"reply": f"```\n{output}\n```"}


def cmd_stash(params: dict) -> dict:
    """stash 操作"""
    action = params.get("action", "list")  # list, push, pop
    repo = params.get("repo", None)

    if action == "list":
        code, output = _run_git(["stash", "list"], cwd=repo)
    elif action == "push":
        msg = params.get("message", "")
        args = ["stash", "push"]
        if msg:
            args.extend(["-m", msg])
        code, output = _run_git(args, cwd=repo)
    elif action == "pop":
        code, output = _run_git(["stash", "pop"], cwd=repo)
    else:
        return {"reply": f"未知 stash 操作: {action}，可用: list, push, pop"}

    if code != 0:
        return {"reply": f"git stash {action} 失败: {output}"}
    return {"reply": f"```\n{output}\n```" if output else "stash 为空"}


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    params = data.get("params", {})
    origin = data.get("origin_message", "")

    # 解析子命令
    subcmd = params.get("subcmd", "")
    if not subcmd:
        for kw in ["log", "diff", "status", "branch", "stash"]:
            if kw in origin.lower():
                subcmd = kw
                break
        if not subcmd:
            subcmd = "status"

    if subcmd == "log":
        result = cmd_log(params)
    elif subcmd == "diff":
        result = cmd_diff(params)
    elif subcmd == "status":
        result = cmd_status(params)
    elif subcmd == "branch":
        result = cmd_branch(params)
    elif subcmd == "stash":
        result = cmd_stash(params)
    else:
        result = {"reply": f"未知子命令: {subcmd}，可用: log, diff, status, branch, stash"}

    result["task_id"] = task_id
    result.setdefault("files", [])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
