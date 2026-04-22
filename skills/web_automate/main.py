"""web_automate Skill - 浏览器自动化（三模式）

三种模式：
  1. headless — 后台运行，不可见（默认）
  2. headed   — 前台可见，用 Playwright Chromium
  3. cdp      — 连接真实 Chrome（带用户登录态/插件）

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [...] }
"""

from __future__ import annotations

import json
import re
import sys

# ── 模式检测 ──────────────────────────────────────────────

HEADED_TRIGGERS = ["可见", "我要看", "显示", "让我看到", "headed", "看着我操作", "前台"]
CDP_TRIGGERS = ["真机", "我的Chrome", "真实浏览器", "用Chrome", "用我的浏览器", "登录态", "cdp", "真实Chrome", "日常浏览器"]
INCOGNITO_TRIGGERS = ["无痕", "隐身", "incognito", "隐私模式", "隐私浏览"]
HEADLESS_TRIGGERS = ["后台", "headless", "静默", "无头"]


def _detect_mode(text: str) -> str:
    """从用户消息判断浏览器模式
    
    优先级：无痕 > 真机CDP > 后台 > 前台 > 默认CDP
    """
    lower = text.lower()
    for trigger in INCOGNITO_TRIGGERS:
        if trigger.lower() in lower:
            return "cdp_incognito"
    for trigger in CDP_TRIGGERS:
        if trigger.lower() in lower:
            return "cdp"
    for trigger in HEADLESS_TRIGGERS:
        if trigger.lower() in lower:
            return "headless"
    for trigger in HEADED_TRIGGERS:
        if trigger.lower() in lower:
            return "headed"
    # 默认 CDP 模式——连接真实 Chrome，保持浏览器打开
    return "cdp"


# ── 中文网站别名 ────────────────────────────────────────────

SITE_ALIASES = {
    "百度": "https://www.baidu.com",
    "baidu": "https://www.baidu.com",
    "谷歌": "https://www.google.com",
    "google": "https://www.google.com",
    "必应": "https://www.bing.com",
    "bing": "https://www.bing.com",
    "github": "https://github.com",
    "知乎": "https://www.zhihu.com",
    "zhihu": "https://www.zhihu.com",
    "微博": "https://weibo.com",
    "weibo": "https://weibo.com",
    "淘宝": "https://www.taobao.com",
    "taobao": "https://www.taobao.com",
    "京东": "https://www.jd.com",
    "jd": "https://www.jd.com",
    "b站": "https://www.bilibili.com",
    "bilibili": "https://www.bilibili.com",
    "哔哩哔哩": "https://www.bilibili.com",
    "YouTube": "https://www.youtube.com",
    "youtube": "https://www.youtube.com",
    "油管": "https://www.youtube.com",
    "推特": "https://twitter.com",
    "twitter": "https://twitter.com",
    "x.com": "https://x.com",
}


# ── URL 提取 ──────────────────────────────────────────────

def _extract_url(text: str) -> str | None:
    """从消息中提取 URL"""
    # 清理触发词
    for trigger in [
        "浏览器操作", "网页操作", "网页点击", "网页输入", "自动浏览",
        "帮我操作浏览器", "用我的Chrome", "网页截图", "网页自动化",
        "打开浏览器", "网页搜索",
        "真机", "可见",
        "打开", "访问", "浏览", "搜索",
        "前台", "后台", "我要看", "让我看到",
    ]:
        text = text.replace(trigger, " ")
    text = text.strip().strip("'\"<>")

    if not text:
        return None

    # 中文网站别名（按长度降序匹配，避免短别名误匹配，如 "x" 匹配到 "xiaoshongshu"）
    lower = text.lower()
    for alias, url in sorted(SITE_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        if alias.lower() in lower:
            return url

    if text.startswith(("http://", "https://")):
        return text

    # 常见域名
    url_pattern = r'[\w.-]+\.(com|cn|org|net|io|dev|app|co|me|info|xyz|top|cc|vip)[\w./-]*'
    m = re.search(url_pattern, text)
    if m:
        return f"https://{m.group()}"

    if "." in text and " " not in text:
        return f"https://{text}"

    return None


# ── 动作解析 ──────────────────────────────────────────────

def _parse_actions(text: str, url: str | None) -> list[dict]:
    """从用户消息解析动作序列"""
    actions = []

    # 1. 打开页面（如果有 URL）
    if url:
        actions.append({"action": "open", "url": url})

    lower = text.lower()

    # 2. 搜索关键词
    search_match = re.search(r"搜索[：:]?\s*(.+?)(?:$|然后|之后|再|接着|，|,|截图|截屏)", text)
    if not search_match:
        search_match = re.search(r"搜[一下下]?\s*(.+?)(?:$|然后|之后|再|接着|，|,|截图|截屏)", text)
    if search_match and url:
        # 在搜索框输入
        kw = search_match.group(1).strip()
        actions.append({"action": "type", "selector": "#kw, input[name='wd'], input[name='q'], input[type='search'], textarea[name='q']", "text": kw})
        actions.append({"action": "press_key", "selector": "", "key": "Enter"})
        actions.append({"action": "wait", "ms": 2000})
        # 搜索后默认截图
        actions.append({"action": "screenshot"})

    # 3. 截图
    if any(kw in lower for kw in ["截图", "截屏", "截个图", "截一下", "screenshot", "看看"]):
        actions.append({"action": "screenshot"})

    # 4. 获取 cookies/token
    if any(kw in lower for kw in ["cookie", "cookies", "token", "登录态", "获取cookie", "获取token"]):
        actions.append({"action": "get_cookies"})

    # 5. 获取 localStorage
    if any(kw in lower for kw in ["localstorage", "storage", "缓存数据"]):
        actions.append({"action": "get_storage"})

    # 6. 读取内容
    if any(kw in lower for kw in ["读取", "内容", "页面文字", "read", "获取内容", "看看内容"]):
        if not any(kw in lower for kw in ["截图", "截屏"]):
            actions.append({"action": "read_page"})

    # 7. 如果只有一个 URL 没有其他操作，默认打开+截图
    if url and len(actions) == 1:
        actions.append({"action": "wait", "ms": 2000})
        actions.append({"action": "screenshot"})

    # 6. 没有 URL 也没有操作 → 打开空白页截图
    if not actions:
        actions.append({"action": "open", "url": "about:blank"})
        actions.append({"action": "screenshot"})

    return actions


# ── 主入口 ────────────────────────────────────────────────

def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    origin = data.get("origin_message", "")
    params = data.get("params", {})

    # 模式检测必须用完整原始消息（因为路由器会把触发词从 rest 中去掉）
    mode = _detect_mode(origin)

    # URL 和动作解析也用原始消息（路由器的 rest 会截断触发词之后的内容，丢失关键信息）
    raw = origin.strip()

    # 提取 URL
    url = _extract_url(raw)

    # 解析动作
    action_list = _parse_actions(raw, url)

    # 执行
    from browser_engine import run_engine

    result = run_engine(mode, action_list)
    result["task_id"] = task_id

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
