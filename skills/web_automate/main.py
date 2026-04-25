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
import time
from pathlib import Path
from typing import Any

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

    # 6. 没有 URL 也没有操作 → 对当前页截图，不主动把已复用页面导航到空白页
    if not actions:
        actions.append({"action": "screenshot"})

    return actions


_RESUME_SIGNALS = ("继续", "重试", "再试", "接着", "恢复", "从断点")


def _is_resume_intent(text: str) -> bool:
    lower = str(text or "").lower()
    return any(signal in lower for signal in _RESUME_SIGNALS)


def _resolve_resume_plan(
    *,
    origin: str,
    params: dict[str, Any],
    action_list: list[dict[str, Any]],
    session: Any,
    owner: str,
) -> tuple[list[dict[str, Any]], int, bool]:
    explicit = params.get("resume_from_step")
    if isinstance(explicit, int):
        start = max(0, explicit)
        if start >= len(action_list):
            return action_list, 0, False
        return action_list[start:], start, start > 0

    if params.get("auto_resume", True) is False:
        return action_list, 0, False

    last_run = getattr(session, "last_run", {}) or {}
    if not isinstance(last_run, dict):
        return action_list, 0, False
    if str(last_run.get("owner") or "") != str(owner):
        return action_list, 0, False

    failed_step = last_run.get("failed_step")
    prev_actions = last_run.get("action_list")
    if not isinstance(failed_step, int) or failed_step < 0:
        return action_list, 0, False

    should_resume = _is_resume_intent(origin)
    # 用户只说“继续/重试”这类短消息时，优先续跑上一次链路
    if not should_resume and not (not params.get("actions") and len(action_list) <= 1):
        return action_list, 0, False

    base_actions = prev_actions if isinstance(prev_actions, list) and prev_actions else action_list
    if failed_step >= len(base_actions):
        return action_list, 0, False
    return base_actions[failed_step:], failed_step, True


def _build_result(
    mode: str,
    details: list[dict[str, Any]],
    runtime_artifacts: dict[str, Any] | None = None,
    *,
    resumed: bool = False,
    resume_from: int = 0,
) -> dict[str, Any]:
    """把动作结果整理成 Skill 协议输出。"""
    files = []
    for detail in details:
        data = detail.get("data") or {}
        if detail.get("success") and detail.get("action") == "screenshot" and data.get("path"):
            files.append({"path": data["path"], "name": data.get("name", "screenshot.png")})

    success_count = sum(1 for d in details if d.get("success"))
    total = len(details)
    summary_lines = [f"🌐 浏览器自动化完成（{success_count}/{total} 成功，模式：{mode}）"]
    if resumed:
        summary_lines.append(f"🔁 已从步骤 #{resume_from} 自动恢复执行")
    for detail in details:
        marker = "✅" if detail.get("success") else "❌"
        summary_lines.append(f"  {marker} {detail.get('message', '')}")

    return {
        "reply": "\n".join(summary_lines),
        "files": files,
        "details": details,
        "artifacts": runtime_artifacts or {},
        "resumed": resumed,
        "resume_from_step": resume_from if resumed else 0,
    }


def _persist_run_artifacts(
    *,
    task_id: str,
    owner: str,
    mode: str,
    action_list: list[dict[str, Any]],
    details: list[dict[str, Any]],
    runtime_artifacts: dict[str, Any],
    output_dir: Path | None = None,
) -> str:
    out_dir = output_dir or (Path(__file__).parent.parent.parent / "output")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    base = re.sub(r"[^a-zA-Z0-9_.-]+", "_", task_id or owner or "run")
    filename = f"web_automate_artifacts_{base}_{stamp}.json"
    path = out_dir / filename
    payload = {
        "task_id": task_id,
        "owner": owner,
        "mode": mode,
        "action_count": len(action_list),
        "details": details,
        "runtime_artifacts": runtime_artifacts,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


async def run_browser_skill(data: dict[str, Any]) -> dict[str, Any]:
    """默认运行路径：通过 BrowserSessionManager 获取/释放浏览器会话。"""
    task_id = data.get("task_id", "")
    origin = data.get("origin_message", "")
    conversation_id = data.get("conversation_id", "")
    params = data.get("params", {}) or {}

    mode = _detect_mode(origin)
    raw = str(params.get("prompt") or origin).strip()
    url = _extract_url(raw)
    action_list = params.get("actions") if isinstance(params.get("actions"), list) else _parse_actions(raw, url)
    continue_on_error = bool(params.get("continue_on_error", False))
    owner = str(conversation_id or task_id or "web_automate")

    try:
        from .session_manager import get_session_manager
    except ImportError:  # 兼容 subprocess 直接运行 main.py
        from session_manager import get_session_manager

    session = None
    manager = get_session_manager()
    try:
        session = await manager.acquire(mode, owner=owner, reuse_url=url or "")
        run_actions, resume_from, resumed = _resolve_resume_plan(
            origin=origin,
            params=params,
            action_list=action_list,
            session=session,
            owner=owner,
        )
        details = await session.engine.execute(
            run_actions,
            start_index=resume_from,
            continue_on_error=continue_on_error,
        )
        runtime = session.engine.get_runtime_artifacts()
        artifact_manifest = _persist_run_artifacts(
            task_id=task_id,
            owner=owner,
            mode=mode,
            action_list=run_actions,
            details=details,
            runtime_artifacts=runtime,
        )
        runtime["artifact_manifest"] = artifact_manifest
        result = _build_result(
            mode,
            details,
            runtime,
            resumed=resumed,
            resume_from=resume_from,
        )
        failed_step: int | None = None
        for detail in details:
            if not detail.get("success"):
                checkpoint = detail.get("step_checkpoint") or {}
                step_index = checkpoint.get("step_index")
                if isinstance(step_index, int):
                    failed_step = step_index
                break
        session.last_run = {
            "owner": owner,
            "action_list": action_list,
            "failed_step": failed_step,
            "details": details,
            "runtime": runtime,
        }
    finally:
        if session is not None:
            await manager.release(session.id)

    result["task_id"] = task_id
    return result


# ── 主入口 ────────────────────────────────────────────────

def main() -> None:
    data = json.loads(sys.stdin.read())
    try:
        import asyncio
        result = asyncio.run(run_browser_skill(data))
    except Exception as e:
        result = {
            "reply": f"❌ 浏览器自动化执行失败：{e}",
            "files": [],
            "details": [],
            "task_id": data.get("task_id", ""),
        }

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
