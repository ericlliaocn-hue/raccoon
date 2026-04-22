"""web_automate 浏览器引擎

三种模式统一封装：
  1. headless  — 后台运行，不可见（默认）
  2. headed    — 前台可见，用 Playwright 自带 Chromium
  3. cdp       — 连接真实 Chrome（带用户登录态/插件）

所有模式共享同一个 Actions 接口。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from actions import Actions, ActionResult

# CDP 模式用的 Chrome 路径和 profile
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9222
CDP_PROFILE_DIR = Path.home() / ".raccoon" / "chrome_profile"
CDP_INCOGNITO_PORT = 9223
CDP_INCOGNITO_PROFILE_DIR = Path.home() / ".raccoon" / "chrome_incognito_profile"

# 截图输出目录
OUTPUT_DIR = Path(__file__).parent.parent.parent / "output"


class BrowserEngine:
    """浏览器引擎——三种模式统一封装"""

    def __init__(self, mode: str = "headless"):
        self._mode = mode
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._actions: Actions | None = None
        self._chrome_process: subprocess.Popen | None = None

    @property
    def actions(self) -> Actions:
        if self._actions is None:
            raise RuntimeError("浏览器未启动，请先调用 start()")
        return self._actions

    async def start(self, reuse_url: str = "") -> str:
        """启动浏览器，返回状态信息

        Args:
            reuse_url: CDP 模式下，优先复用 URL 包含此字符串的已有 tab。
                       用于多步操作（如登录流程）保持同一页面。
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return "❌ 未安装 playwright，请运行：pip install playwright && playwright install chromium"

        self._playwright = await async_playwright().start()

        if self._mode == "headless":
            self._browser = await self._playwright.chromium.launch(headless=True)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            self._page = await self._context.new_page()

        elif self._mode == "headed":
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                slow_mo=100,  # 慢放，让人看得清
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            self._page = await self._context.new_page()

        elif self._mode == "cdp":
            cdp_port = CDP_PORT
            cdp_profile = CDP_PROFILE_DIR
            incognito = False
            try:
                await self._start_cdp(cdp_port, cdp_profile, incognito, reuse_url=reuse_url)
            except RuntimeError as e:
                return str(e)

        elif self._mode == "cdp_incognito":
            cdp_port = CDP_INCOGNITO_PORT
            cdp_profile = CDP_INCOGNITO_PROFILE_DIR
            incognito = True
            try:
                await self._start_cdp(cdp_port, cdp_profile, incognito)
            except RuntimeError as e:
                return str(e)

        else:
            return f"❌ 未知模式：{self._mode}，支持 headless / headed / cdp / cdp_incognito"

        self._actions = Actions(self._page, OUTPUT_DIR)
        return f"✅ 浏览器已启动（模式：{self._mode}）"

    async def stop(self) -> str:
        """关闭浏览器"""
        messages = []
        try:
            if self._browser:
                if self._mode in ("cdp", "cdp_incognito"):
                    # CDP 模式不断开浏览器，只断开连接
                    await self._browser.close()
                    messages.append("✅ 已断开与 Chrome 的连接")
                else:
                    await self._browser.close()
                    messages.append("✅ 浏览器已关闭")
        except Exception as e:
            messages.append(f"⚠️ 关闭浏览器时出错：{e}")

        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

        return "\n".join(messages) if messages else "✅ 已关闭"

    # ── CDP 辅助方法 ─────────────────────────────────

    def find_page_by_url(self, pattern: str):
        """在已有 context 中查找 URL 包含 pattern 的 page

        用于多步操作时复用同一个 tab（如登录流程）。
        返回匹配的 Page 对象，未找到返回 None。
        """
        if not self._browser:
            return None
        try:
            for ctx in self._browser.contexts:
                for page in ctx.pages:
                    if pattern in page.url:
                        return page
        except Exception:
            pass
        return None

    def _is_chrome_cdp_running(self, port: int = CDP_PORT) -> bool:
        """检查 Chrome CDP 是否已在运行"""
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _start_chrome_cdp(self, port: int = CDP_PORT, profile_dir: Path = CDP_PROFILE_DIR, incognito: bool = False) -> None:
        """启动 Chrome 并开启 CDP 调试端口"""
        profile_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            CHROME_PATH,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            # 禁用 DownloadBubble，避免 Playwright connect_over_cdp 时
            # Browser.setDownloadBehavior 协议错误（Chrome 147+ 兼容）
            "--disable-features=DownloadBubble,DownloadBubbleV2",
        ]
        if incognito:
            cmd.append("--incognito")

        self._chrome_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def _start_cdp(self, cdp_port: int, cdp_profile: Path, incognito: bool, reuse_url: str = "") -> None:
        """CDP 连接公共逻辑

        如果 Chrome 已在运行 → 连接并在已有 context 新建 tab（复用浏览器）
        如果 Chrome 未运行 → 启动 Chrome → 连接 → 新建 tab

        Args:
            reuse_url: 如果指定，优先在已有 tab 中查找 URL 包含此字符串的页面复用，
                       找不到才新建 tab。用于多步操作（如登录流程）保持同一页面。
        """
        chrome_already_running = self._is_chrome_cdp_running(cdp_port)

        if not chrome_already_running:
            await self._start_chrome_cdp(cdp_port, cdp_profile, incognito)
            # 等待 Chrome 启动
            for _ in range(30):
                if self._is_chrome_cdp_running(cdp_port):
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("❌ Chrome 启动超时，请确认 Chrome 已安装")

        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                f"http://localhost:{cdp_port}"
            )
        except Exception as e:
            error_msg = str(e)
            # Playwright connect_over_cdp 可能因 Browser.setDownloadBehavior 协议错误失败
            # 这是 Playwright 与较新 Chrome 版本（147+）的兼容性问题
            if "setDownloadBehavior" in error_msg or "Browser context management" in error_msg:
                logger.warning("cdp_setDownloadBehavior_error_restarting_chrome", error=error_msg)
                # 如果 Chrome 是 Raccoon 启动的，关闭并用 --disable-features=DownloadBubble 重启
                if self._chrome_process:
                    self._chrome_process.terminate()
                    self._chrome_process = None
                    await asyncio.sleep(2)
                    await self._start_chrome_cdp(cdp_port, cdp_profile, incognito)
                    for _ in range(30):
                        if self._is_chrome_cdp_running(cdp_port):
                            break
                        await asyncio.sleep(1)
                    else:
                        raise RuntimeError("❌ Chrome 重启超时")
                    try:
                        self._browser = await self._playwright.chromium.connect_over_cdp(
                            f"http://localhost:{cdp_port}"
                        )
                    except Exception as e2:
                        raise RuntimeError(f"❌ 连接 Chrome 失败（重试后）：{e2}\n请确认 Chrome 已启动且调试端口 {cdp_port} 可用")
                else:
                    # Chrome 是用户手动启动的，无法重启，提示用户
                    raise RuntimeError(
                        f"❌ 连接 Chrome 失败：Chrome 版本过新导致协议不兼容。\n"
                        f"请关闭当前 Chrome，然后运行以下命令重新启动：\n"
                        f'  open -a "Google Chrome" --args --remote-debugging-port={cdp_port} --disable-features=DownloadBubble\n'
                        f"或让 Raccoon 自动启动 Chrome（先关闭现有 Chrome 进程）。"
                    )
            else:
                raise RuntimeError(f"❌ 连接 Chrome 失败：{e}\n请确认 Chrome 已启动且调试端口 {cdp_port} 可用")

        # 连接成功后，尝试复用已有 tab 或新建 tab
        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]

            # 如果指定了 reuse_url，优先查找已有页面复用
            if reuse_url:
                existing_page = self.find_page_by_url(reuse_url)
                if existing_page:
                    self._page = existing_page
                    # 更新 Actions 绑定的 page
                    if self._actions:
                        self._actions = Actions(self._page, OUTPUT_DIR)
                    return

            # 复用已有 context 的第一个 page，避免 new_page() 触发协议错误
            existing_pages = self._context.pages
            if existing_pages:
                self._page = existing_pages[0]
            else:
                try:
                    self._page = await self._context.new_page()
                except Exception:
                    # new_page 也可能触发 setDownloadBehavior 错误，降级使用 CDP session
                    logger.warning("new_page_failed_using_existing")
                    if self._context.pages:
                        self._page = self._context.pages[-1]
                    else:
                        raise RuntimeError("❌ 无法创建或找到可用的浏览器标签页")
        else:
            self._context = await self._browser.new_context()
            self._page = await self._context.new_page()

    # ── 执行动作序列 ─────────────────────────────────

    async def execute(self, action_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """执行动作序列，返回每个动作的结果"""
        results = []
        for act in action_list:
            action_type = act.get("action", "")
            result = await self._do_action(act, action_type)
            results.append({
                "action": action_type,
                "success": result.success,
                "message": result.message,
                "data": result.data,
            })
            # 如果动作失败，继续执行下一个（不中断）
        return results

    async def _do_action(self, act: dict[str, Any], action_type: str) -> ActionResult:
        """分发到具体的动作方法"""
        a = self.actions

        if action_type == "open":
            return await a.open(
                url=act.get("url", ""),
                wait_until=act.get("wait_until", "domcontentloaded"),
            )
        elif action_type == "click":
            return await a.click(selector=act.get("selector", ""))
        elif action_type == "type":
            return await a.type_text(
                selector=act.get("selector", ""),
                text=act.get("text", ""),
                clear=act.get("clear", True),
            )
        elif action_type == "press_key":
            return await a.press_key(
                selector=act.get("selector", ""),
                key=act.get("key", "Enter"),
            )
        elif action_type == "read":
            return await a.read(selector=act.get("selector", ""))
        elif action_type == "read_page":
            return await a.read_page()
        elif action_type == "screenshot":
            return await a.screenshot(
                name=act.get("name"),
                full_page=act.get("full_page", False),
            )
        elif action_type == "wait":
            return await a.wait(ms=act.get("ms", 2000))
        elif action_type == "wait_for_selector":
            return await a.wait_for_selector(
                selector=act.get("selector", ""),
                timeout=act.get("timeout", 10000),
            )
        elif action_type == "scroll":
            return await a.scroll(
                direction=act.get("direction", "down"),
                pixels=act.get("pixels", 500),
            )
        elif action_type == "get_page_info":
            return await a.get_page_info()
        elif action_type == "get_cookies":
            return await a.get_cookies(urls=act.get("urls"))
        elif action_type == "get_storage":
            return await a.get_storage(key=act.get("key"))
        else:
            return ActionResult(success=False, message=f"❌ 未知动作：{action_type}")


# ── 同步封装（给 main.py 调用）──────────────────────────

def run_engine(mode: str, action_list: list[dict[str, Any]]) -> dict[str, Any]:
    """同步运行引擎，返回结果（内部跑 async 事件循环）"""
    engine = BrowserEngine(mode)

    async def _run():
        # 1. 启动浏览器
        start_msg = await engine.start()
        if start_msg.startswith("❌"):
            return {"reply": start_msg, "files": [], "details": []}

        # 2. 执行动作
        details = []
        files = []

        for act in action_list:
            action_type = act.get("action", "")
            result = await engine._do_action(act, action_type)
            details.append({
                "action": action_type,
                "success": result.success,
                "message": result.message,
                "data": result.data,
            })
            # 收集截图文件
            if result.success and action_type == "screenshot" and result.data.get("path"):
                files.append({"path": result.data["path"], "name": result.data.get("name", "screenshot.png")})

        # 3. 关闭浏览器（headless/headed 自动关闭，cdp/cdp_incognito 不断开）
        if mode not in ("cdp", "cdp_incognito"):
            await engine.stop()

        # 4. 生成汇总回复
        success_count = sum(1 for d in details if d["success"])
        total = len(details)
        summary_lines = [f"🌐 浏览器自动化完成（{success_count}/{total} 成功，模式：{mode}）"]
        for d in details:
            summary_lines.append(f"  {'✅' if d['success'] else '❌'} {d['message']}")

        return {
            "reply": "\n".join(summary_lines),
            "files": files,
            "details": details,
        }

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(_run())
    except Exception as e:
        return {"reply": f"❌ 引擎执行失败：{e}", "files": [], "details": []}
    finally:
        try:
            loop.close()
        except Exception:
            pass
