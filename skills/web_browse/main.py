"""web_browse Skill - 打开网页/获取网页标题和摘要

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [] }
"""
import json
import re
import sys
import webbrowser
from urllib.parse import urlparse


def _extract_url(text: str) -> str | None:
    """从消息中提取 URL"""
    for trigger in ["打开网页", "访问网页", "打开网站", "打开链接", "浏览网页", "open url", "open website", "访问链接", "打开url"]:
        text = text.replace(trigger, " ")
    text = text.strip().strip("'\"<>")

    if not text:
        return None

    # 已经是完整 URL
    if text.startswith(("http://", "https://")):
        return text

    # 常见域名模式
    url_pattern = r'[\w.-]+\.(com|cn|org|net|io|dev|app|co|me|info|xyz|top|cc|vip)[\w./-]*'
    m = re.search(url_pattern, text)
    if m:
        return f"https://{m.group()}"

    # 当作域名尝试
    if "." in text and " " not in text:
        return f"https://{text}"

    return None


def _fetch_title(url: str) -> str | None:
    """获取网页标题"""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read(65536).decode("utf-8", errors="ignore")
            m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return None


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    origin = data.get("origin_message", "")
    params = data.get("params", {})

    raw = (params.get("rest") or origin).strip()
    url = _extract_url(raw)

    if not url:
        result = {
            "task_id": task_id,
            "reply": "❌ 未提供网址，请说「打开网页 https://example.com」或「访问网站 google.com」",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    # 打开网页
    try:
        webbrowser.open(url)
    except Exception as e:
        result = {
            "task_id": task_id,
            "reply": f"❌ 打开网页失败：{e}",
            "files": [],
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    # 尝试获取标题
    title = _fetch_title(url)
    if title:
        reply = f"🌐 已打开网页：{url}\n📄 标题：{title}"
    else:
        reply = f"🌐 已打开网页：{url}"

    result = {
        "task_id": task_id,
        "reply": reply,
        "files": [],
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
