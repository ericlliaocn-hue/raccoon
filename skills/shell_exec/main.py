"""shell_exec Skill - 执行 Shell 命令并返回输出

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [] }

安全策略：
  - 危险命令黑名单：rm -rf /, mkfs, dd, format, del /f, shutdown, reboot, halt, poweroff
  - 命令超时 30 秒
  - 输出截断 10KB
"""
import json
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

# 危险命令黑名单（子串匹配，忽略大小写）
DANGEROUS_PATTERNS = [
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", "format ",
    "del /f", "shutdown", "reboot", "halt", "poweroff",
    ":(){:|:&};:", "fork bomb",
]

MAX_OUTPUT = 10240  # 10KB
TIMEOUT = 30


def _is_dangerous(cmd: str) -> bool:
    lower = cmd.lower().strip()
    return any(p in lower for p in DANGEROUS_PATTERNS)


def _extract_command(text: str) -> str:
    """从原始消息中提取要执行的命令"""
    # 去掉常见触发词
    for trigger in ["执行", "运行", "shell", "命令", "run", "exec"]:
        text = text.replace(trigger, " ", 1)
    return text.strip()


def _build_trace(task_id: str) -> str:
    prefix = (task_id or "task")[:8]
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    origin = data.get("origin_message", "")
    params = data.get("params", {})
    trace_id = _build_trace(task_id)

    # 优先用 params.rest，否则从 origin_message 解析
    cmd = (params.get("rest") or "").strip()
    if not cmd:
        cmd = _extract_command(origin)

    if not cmd:
        result = {
            "task_id": task_id,
            "reply": "❌ 未提供命令，请说「执行 ls -la」或「shell pwd」",
            "files": [],
            "artifacts": {
                "command": "",
                "exit_code": None,
                "task_id": task_id,
                "trace": trace_id,
                "error": "missing_command",
            },
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    if _is_dangerous(cmd):
        result = {
            "task_id": task_id,
            "reply": f"⛔ 拒绝执行危险命令：{cmd}\n涉及系统破坏性操作，已被安全策略拦截。",
            "files": [],
            "artifacts": {
                "command": cmd,
                "exit_code": None,
                "task_id": task_id,
                "trace": trace_id,
                "blocked": True,
                "error": "dangerous_command",
            },
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    try:
        # 尝试分割参数，失败则用 shell=True
        try:
            args = shlex.split(cmd)
            use_shell = False
        except ValueError:
            args = cmd
            use_shell = True

        proc = subprocess.run(
            args,
            shell=use_shell,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=str(Path.home()),
        )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode

        # 截断过长输出
        output = stdout
        if stderr:
            output += f"\n[stderr]\n{stderr}"
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + f"\n\n... (输出截断，共 {len(output)} 字符)"

        if exit_code == 0:
            reply = (
                f"✅ 命令执行成功 (exit {exit_code})\n"
                f"trace={trace_id} task={task_id}\n\n```\n{output.strip()}\n```"
            )
        else:
            reply = (
                f"⚠️ 命令返回非零退出码 (exit {exit_code})\n"
                f"trace={trace_id} task={task_id}\n\n```\n{output.strip()}\n```"
            )

        result = {
            "task_id": task_id,
            "reply": reply,
            "files": [],
            "artifacts": {
                "command": cmd,
                "exit_code": exit_code,
                "task_id": task_id,
                "trace": trace_id,
            },
        }
        print(json.dumps(result, ensure_ascii=False))

    except subprocess.TimeoutExpired:
        result = {
            "task_id": task_id,
            "reply": f"⏱️ 命令超时（{TIMEOUT}秒）：{cmd}",
            "files": [],
            "artifacts": {
                "command": cmd,
                "exit_code": None,
                "task_id": task_id,
                "trace": trace_id,
                "error": "timeout",
            },
        }
        print(json.dumps(result, ensure_ascii=False))
    except FileNotFoundError:
        not_found = cmd.split()[0] if isinstance(cmd, str) else cmd[0]
        result = {
            "task_id": task_id,
            "reply": f"❌ 命令未找到：{not_found}",
            "files": [],
            "artifacts": {
                "command": cmd,
                "exit_code": None,
                "task_id": task_id,
                "trace": trace_id,
                "error": "command_not_found",
            },
        }
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        result = {
            "task_id": task_id,
            "reply": f"❌ 执行出错：{e}",
            "files": [],
            "artifacts": {
                "command": cmd,
                "exit_code": None,
                "task_id": task_id,
                "trace": trace_id,
                "error": str(e),
            },
        }
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
