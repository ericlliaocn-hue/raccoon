"""Echo Skill - 回显用户消息

输入 (stdin JSON):
  { "task_id": "...", "origin_message": "...", "params": {} }

输出 (stdout JSON):
  { "echo": "...", "task_id": "..." }
"""

import json
import sys


def main():
    data = json.loads(sys.stdin.read())
    message = data.get("origin_message", "")
    task_id = data.get("task_id", "")

    result = {
        "echo": message,
        "task_id": task_id,
        "status": "completed",
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
