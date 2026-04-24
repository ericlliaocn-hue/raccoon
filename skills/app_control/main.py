"""app_control Skill - 控制应用程序

支持操作：打开应用、列出运行中应用、关闭应用、用指定浏览器打开网页
macOS: open / osascript / ps

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "reply": "...", "files": [] }
"""
import json
import subprocess
import sys

# 不允许关闭的系统关键应用
PROTECTED_APPS = {
    "Finder", "Dock", "SystemUIServer", "loginwindow",
    "WindowServer", "kernel_task", "launchd",
    "System Events", "AppleSpell", "Keychain Access",
}

# 应用别名映射（用户输入 -> macOS 标准应用名）
APP_ALIASES = {
    # 浏览器
    "chrome": "Google Chrome",
    "谷歌浏览器": "Google Chrome",
    "google chrome": "Google Chrome",
    "googlechrome": "Google Chrome",
    "chorme": "Google Chrome",
    "chrom": "Google Chrome",
    "浏览器": "Google Chrome",
    "safari浏览器": "Safari",
    "firefox浏览器": "Firefox",
    "火狐": "Firefox",
    "火狐浏览器": "Firefox",
    "edge浏览器": "Microsoft Edge",
    "微软浏览器": "Microsoft Edge",
    # 通讯
    "微信": "WeChat",
    "wechat": "WeChat",
    "qq": "QQ",
    "钉钉": "DingTalk",
    "dingtalk": "DingTalk",
    "飞书": "Feishu",
    "feishu": "Feishu",
    "telegram": "Telegram",
    "电报": "Telegram",
    # 开发
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "vsc": "Visual Studio Code",
    "代码编辑器": "Visual Studio Code",
    "iterm": "iTerm",
    "iterm2": "iTerm",
    "终端": "Terminal",
    "terminal": "Terminal",
    # 办公
    "word": "Microsoft Word",
    "excel": "Microsoft Excel",
    "ppt": "Microsoft PowerPoint",
    "powerpoint": "Microsoft PowerPoint",
    "notes": "Notes",
    "备忘录": "Notes",
    "日历": "Calendar",
    "计算器": "Calculator",
    # 媒体
    "音乐": "Music",
    "apple music": "Music",
    "播客": "Podcasts",
    "tv": "TV",
    "照片": "Photos",
    # 其他
    "app store": "App Store",
    "商店": "App Store",
    "系统设置": "System Settings",
    "设置": "System Settings",
    "finder": "Finder",
    "文件管理器": "Finder",
}

# 浏览器隐私模式映射（macOS 应用名 -> CLI 路径 + 隐私参数）
BROWSER_PROFILES = {
    "chrome": {
        "app": "Google Chrome",
        "path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "incognito_arg": "--incognito",
    },
    "google chrome": {
        "app": "Google Chrome",
        "path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "incognito_arg": "--incognito",
    },
    "safari": {
        "app": "Safari",
        # Safari 没有命令行隐私模式，需要用 AppleScript 新建私有窗口
        "path": None,
        "incognito_applescript": '''
            tell application "Safari"
                activate
                if (count of windows) is 0 then
                    make new document
                end if
                tell application "System Events" to keystroke "n" using {{command down, shift down}}
            end tell
        ''',
    },
    "firefox": {
        "app": "Firefox",
        "path": "/Applications/Firefox.app/Contents/MacOS/firefox",
        "incognito_arg": "--private-window",
    },
    "edge": {
        "app": "Microsoft Edge",
        "path": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "incognito_arg": "--inprivate",
    },
    "arc": {
        "app": "Arc",
        "path": None,
        "incognito_applescript": '''
            tell application "Arc"
                activate
                tell application "System Events" to keystroke "n" using {{command down, shift down}}
            end tell
        ''',
    },
}


def _detect_action(text: str) -> str:
    """从消息中判断操作类型"""
    close_triggers = ["关闭应用", "退出应用", "kill应用", "结束进程", "关闭app", "退出app", "关掉", "quit"]
    list_triggers = ["正在运行的应用", "应用列表", "列出应用", "运行中的应用", "哪些应用", "running apps"]

    for trigger in close_triggers:
        if trigger in text:
            return "close"
    for trigger in list_triggers:
        if trigger in text:
            return "list"
    # 默认打开
    return "open"


def _extract_app_name(text: str) -> str:
    """从消息中提取应用名"""
    for trigger in [
        "打开应用", "打开app", "启动应用", "运行应用", "打开", "启动",
        "关闭应用", "退出应用", "kill应用", "结束进程", "关闭app", "退出app",
        "关掉", "quit",
    ]:
        text = text.replace(trigger, " ")
    return text.strip().strip("'\"")


def _resolve_app_name(name: str) -> str:
    """解析应用别名，返回 macOS 标准应用名"""
    # 精确匹配别名
    key = name.lower().strip()
    if key in APP_ALIASES:
        return APP_ALIASES[key]

    # 模糊匹配：去掉空格再试
    no_space = key.replace(" ", "")
    for alias, canonical in APP_ALIASES.items():
        if alias.replace(" ", "") == no_space:
            return canonical

    # 包含匹配：用户输入是别名的子串或超串
    for alias, canonical in APP_ALIASES.items():
        if key in alias or alias in key:
            return canonical

    return name  # 原样返回，让 macOS open -a 尝试


def _open_app(app_name: str) -> str:
    """打开应用"""
    if not app_name:
        return "❌ 未指定应用名，请说「打开应用 Safari」或「打开 微信」"

    resolved = _resolve_app_name(app_name)

    try:
        # 尝试用 open -a 打开
        result = subprocess.run(
            ["open", "-a", resolved],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            display = resolved if resolved != app_name else app_name
            return f"✅ 已打开应用：{display}"
        # 如果失败，尝试直接作为路径打开
        if "Unable to find application" in (result.stderr or ""):
            return f"❌ 找不到应用：{app_name}\n请检查应用名是否正确（如 Safari、微信、VS Code 等）"
        return f"❌ 打开应用失败：{result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return f"⚠️ 打开应用超时：{app_name}"
    except FileNotFoundError:
        return "❌ 系统不支持 open 命令"


def _list_apps() -> str:
    """列出正在运行的应用"""
    try:
        # 使用 osascript 获取前台应用列表
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get name of every process whose background only is false'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            apps = [a.strip() for a in result.stdout.strip().split(",") if a.strip()]
            lines = [f"📱 正在运行的应用（{len(apps)} 个）："]
            for i, app in enumerate(apps, 1):
                lines.append(f"  {i}. {app}")
            return "\n".join(lines)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # fallback: 用 ps
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # 提取 .app 进程
            apps = set()
            for line in result.stdout.splitlines():
                if ".app/Contents/MacOS/" in line:
                    parts = line.split("/")
                    for i, p in enumerate(parts):
                        if p.endswith(".app"):
                            apps.add(p[:-4])
                            break
            if apps:
                lines = [f"📱 正在运行的应用（{len(apps)} 个）："]
                for i, app in enumerate(sorted(apps), 1):
                    lines.append(f"  {i}. {app}")
                return "\n".join(lines)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "❌ 无法获取应用列表"


def _close_app(app_name: str) -> str:
    """关闭应用"""
    if not app_name:
        return "❌ 未指定应用名，请说「关闭应用 Safari」或「退出 微信」"

    resolved = _resolve_app_name(app_name)

    # 安全检查
    if resolved in PROTECTED_APPS:
        return f"⚠️ 不允许关闭系统关键应用：{resolved}"

    try:
        # 使用 osascript 优雅关闭
        result = subprocess.run(
            ["osascript", "-e", f'tell application "{resolved}" to quit'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return f"✅ 已关闭应用：{resolved}"
        # 如果应用不存在
        if "not running" in (result.stderr or "").lower() or "Application doesn't exist" in (result.stderr or ""):
            return f"⚠️ 应用未运行或不存在：{resolved}"
        return f"❌ 关闭应用失败：{result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return f"⚠️ 关闭应用超时：{app_name}"
    except FileNotFoundError:
        return "❌ 系统不支持 osascript 命令"


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    origin = data.get("origin_message", "")
    params = data.get("params", {})

    raw = (params.get("rest") or origin).strip()
    action = _detect_action(raw)
    app_name = _extract_app_name(raw)

    if action == "list":
        reply = _list_apps()
    elif action == "close":
        reply = _close_app(app_name)
    else:  # open
        reply = _open_app(app_name)

    result = {
        "task_id": task_id,
        "reply": reply,
        "files": [],
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
