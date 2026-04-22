"""clipboard Skill - 读写剪贴板内容

macOS 使用 pbcopy/pbpaste

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [] }
"""
import json
import subprocess
import sys


def _read_clipboard() -> str | None:
    """读取剪贴板文本内容"""
    try:
        result = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _write_clipboard(text: str) -> bool:
    """写入文本到剪贴板"""
    try:
        result = subprocess.run(
            ["pbcopy"],
            input=text,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _detect_action(text: str) -> str:
    """从消息中判断操作类型：read / write"""
    # 写入的触发词
    write_triggers = ["复制到剪贴板", "写入剪贴板", "设置剪贴板", "粘贴板写入", "复制内容"]
    for trigger in write_triggers:
        if trigger in text:
            return "write"
    # 默认读取
    return "read"


def _extract_write_content(text: str) -> str:
    """从消息中提取要写入的内容"""
    for trigger in ["复制到剪贴板", "写入剪贴板", "设置剪贴板", "粘贴板写入", "复制内容"]:
        text = text.replace(trigger, " ")
    return text.strip().strip("'\"")


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    origin = data.get("origin_message", "")
    params = data.get("params", {})

    raw = (params.get("rest") or origin).strip()
    action = _detect_action(raw)

    if action == "write":
        content = _extract_write_content(raw)
        # 也检查 params.rest 中是否有内容
        if not content:
            content = (params.get("rest") or "").strip()
            for trigger in ["复制到剪贴板", "写入剪贴板", "设置剪贴板", "粘贴板写入", "复制内容"]:
                content = content.replace(trigger, " ")
            content = content.strip().strip("'\"")

        if not content:
            result = {
                "task_id": task_id,
                "reply": "❌ 未提供要复制的内容，请说「复制到剪贴板 xxx」",
                "files": [],
            }
            print(json.dumps(result, ensure_ascii=False))
            return

        if _write_clipboard(content):
            preview = content[:100] + ("..." if len(content) > 100 else "")
            result = {
                "task_id": task_id,
                "reply": f"✅ 已复制到剪贴板：\n```\n{preview}\n```",
                "files": [],
            }
        else:
            result = {
                "task_id": task_id,
                "reply": "❌ 写入剪贴板失败",
                "files": [],
            }
        print(json.dumps(result, ensure_ascii=False))
        return

    # 读取剪贴板
    content = _read_clipboard()
    if content is None:
        result = {
            "task_id": task_id,
            "reply": "📋 剪贴板为空或无法读取",
            "files": [],
        }
    else:
        # 限制显示长度
        if len(content) > 2000:
            preview = content[:2000]
            result = {
                "task_id": task_id,
                "reply": f"📋 剪贴板内容（前 2000 字符 / 共 {len(content)} 字符）：\n```\n{preview}\n...\n```",
                "files": [],
            }
        else:
            result = {
                "task_id": task_id,
                "reply": f"📋 剪贴板内容：\n```\n{content}\n```",
                "files": [],
            }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
