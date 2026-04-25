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
import logging
import platform
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from .actions import Actions, ActionResult
except ImportError:  # 兼容 main.py 作为脚本直接运行
    from actions import Actions, ActionResult

logger = logging.getLogger("raccoon.browser_engine")

# CDP 模式用的 Chrome profile 目录
CDP_PROFILE_DIR = Path.home() / ".raccoon" / "chrome_profile"
CDP_INCOGNITO_PROFILE_DIR = Path.home() / ".raccoon" / "chrome_incognito_profile"

# 截图输出目录
OUTPUT_DIR = Path(__file__).parent.parent.parent / "output"


def _detect_chrome_path() -> str:
    """自动检测 Chrome 可执行文件路径

    按优先级检测：
    1. RaccoonConfig.chrome_path（用户配置）
    2. 系统默认路径（macOS / Linux / Windows）
    3. PATH 中的 google-chrome / chromium-browser
    """
    # 1. 尝试从配置读取
    try:
        sys_path = Path(__file__).resolve().parent.parent.parent
        config_path = sys_path / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                cfg = json.load(f)
            if cfg.get("chrome_path"):
                return cfg["chrome_path"]
    except Exception:
        pass

    # 2. 系统默认路径
    system = platform.system()
    candidates = []
    if system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif system == "Linux":
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        ]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Chromium\Application\chrome.exe",
        ]

    for path in candidates:
        if Path(path).exists():
            return path

    # 3. 尝试 PATH 查找
    import shutil
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    # 回退到 macOS 默认路径（向后兼容）
    return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _detect_cdp_port() -> int:
    """从配置读取 CDP 端口，默认 9222"""
    try:
        sys_path = Path(__file__).resolve().parent.parent.parent
        config_path = sys_path / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                cfg = json.load(f)
            if cfg.get("cdp_port"):
                return int(cfg["cdp_port"])
    except Exception:
        pass
    return 9222


def _detect_cdp_incognito_port() -> int:
    """从配置读取无痕模式 CDP 端口，默认 9223"""
    try:
        sys_path = Path(__file__).resolve().parent.parent.parent
        config_path = sys_path / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                cfg = json.load(f)
            if cfg.get("cdp_incognito_port"):
                return int(cfg["cdp_incognito_port"])
    except Exception:
        pass
    return 9223


# 延迟检测缓存（模块加载时不执行，首次使用时计算）
_detected_chrome_path: str | None = None
_detected_cdp_port: int | None = None
_detected_cdp_incognito_port: int | None = None


def get_chrome_path() -> str:
    global _detected_chrome_path
    if _detected_chrome_path is None:
        _detected_chrome_path = _detect_chrome_path()
    return _detected_chrome_path


def get_cdp_port() -> int:
    global _detected_cdp_port
    if _detected_cdp_port is None:
        _detected_cdp_port = _detect_cdp_port()
    return _detected_cdp_port


def get_cdp_incognito_port() -> int:
    global _detected_cdp_incognito_port
    if _detected_cdp_incognito_port is None:
        _detected_cdp_incognito_port = _detect_cdp_incognito_port()
    return _detected_cdp_incognito_port


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
        self._checkpoints: list[dict[str, Any]] = []
        self._artifacts: list[dict[str, Any]] = []
        self._domain_health: dict[str, str] = {}

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
            cdp_port = get_cdp_port()
            cdp_profile = CDP_PROFILE_DIR
            incognito = False
            try:
                await self._start_cdp(cdp_port, cdp_profile, incognito, reuse_url=reuse_url)
            except RuntimeError as e:
                return str(e)

        elif self._mode == "cdp_incognito":
            cdp_port = get_cdp_incognito_port()
            cdp_profile = CDP_INCOGNITO_PROFILE_DIR
            incognito = True
            try:
                await self._start_cdp(cdp_port, cdp_profile, incognito, reuse_url=reuse_url)
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
        patterns = self._reuse_patterns(pattern)
        try:
            for ctx in self._browser.contexts:
                for page in ctx.pages:
                    if any(p and p in page.url for p in patterns):
                        return page
        except Exception:
            pass
        return None

    def select_reuse_page(self, pattern: str) -> bool:
        """把当前操作页切到已有匹配 tab，供 SessionManager 复用空闲会话。"""
        page = self.find_page_by_url(pattern)
        if not page:
            return False
        self._page = page
        self._actions = Actions(self._page, OUTPUT_DIR)
        return True

    def _reuse_patterns(self, pattern: str) -> list[str]:
        """生成复用匹配串：完整 URL + hostname，避免路径差异导致复用失败。"""
        if not pattern:
            return []
        patterns = [pattern]
        try:
            from urllib.parse import urlparse
            parsed = urlparse(pattern)
            if parsed.netloc:
                patterns.append(parsed.netloc)
        except Exception:
            pass
        return list(dict.fromkeys(patterns))

    def _is_chrome_cdp_running(self, port: int | None = None) -> bool:
        """检查 Chrome CDP 是否已在运行"""
        if port is None:
            port = get_cdp_port()
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _start_chrome_cdp(self, port: int | None = None, profile_dir: Path = CDP_PROFILE_DIR, incognito: bool = False) -> None:
        """启动 Chrome 并开启 CDP 调试端口"""
        if port is None:
            port = get_cdp_port()
        profile_dir.mkdir(parents=True, exist_ok=True)

        chrome_path = get_chrome_path()
        cmd = [
            chrome_path,
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
                logger.warning("cdp_setDownloadBehavior_error_restarting_chrome: %s", error_msg)
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

    def _current_domain(self) -> str:
        if not self._page:
            return ""
        try:
            return urlparse(self._page.url).netloc.lower()
        except Exception:
            return ""

    def _record_checkpoint(self, index: int, action_type: str, stage: str, result: ActionResult | None = None) -> dict[str, Any]:
        checkpoint = {
            "step_index": index,
            "action": action_type,
            "stage": stage,
            "timestamp": time.time(),
            "url": self._page.url if self._page else "",
            "domain": self._current_domain(),
        }
        if result is not None:
            checkpoint["success"] = result.success
            checkpoint["message"] = result.message
        self._checkpoints.append(checkpoint)
        return checkpoint

    def _update_domain_health(self, action_type: str, result: ActionResult) -> None:
        domain = self._current_domain()
        if not domain:
            return

        state = self._domain_health.get(domain, "valid")
        message = str(result.message or "").lower()
        if any(tag in message for tag in ("登录", "signin", "expired", "unauthorized", "forbidden")):
            self._domain_health[domain] = "invalid"
            return
        if action_type in {"open", "type", "click", "press_key"} and result.success:
            if state in {"invalid", "suspected_expired"}:
                self._domain_health[domain] = "suspected_expired"
            else:
                self._domain_health[domain] = "valid"

    async def _wait_visible_and_stable(self, action_type: str, act: dict[str, Any], timeout_ms: int) -> None:
        if not self._page:
            return
        selector = str(act.get("selector", "") or "").strip()
        if selector and action_type in {
            "click",
            "type",
            "check",
            "double_click",
            "hover",
            "read",
            "upload_file",
            "select_option",
            "wait_for_selector",
            "drag_and_drop",
        }:
            await self._page.wait_for_selector(selector, timeout=timeout_ms, state="visible")
        if action_type in {"open", "click", "press_key"}:
            await self._page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        # wait-stable
        await self._page.wait_for_timeout(min(500, max(120, timeout_ms // 40)))

    async def _should_block_for_login(self, action_type: str) -> tuple[bool, str]:
        if not self._page:
            return False, ""
        domain = self._current_domain()
        state = self._domain_health.get(domain, "valid")
        if state == "invalid" and action_type not in {"open", "type", "click", "press_key"}:
            return True, "登录态失效，需先执行补登流程"

        url_lower = str(self._page.url or "").lower()
        if any(token in url_lower for token in ("/login", "signin", "passport", "auth")):
            if action_type not in {"open", "type", "click", "press_key"}:
                self._domain_health[domain] = "suspected_expired"
                return True, "当前页面在登录入口，建议先完成登录后再继续"
        return False, ""

    async def _collect_failure_artifacts(self, index: int, action_type: str, reason: str) -> dict[str, Any]:
        artifact: dict[str, Any] = {
            "step_index": index,
            "action": action_type,
            "reason": reason,
            "url": self._page.url if self._page else "",
            "domain": self._current_domain(),
        }
        if not self._page:
            return artifact

        ts = int(time.time() * 1000)
        screenshot_path = OUTPUT_DIR / f"web_automate_fail_{index}_{ts}.png"
        dom_path = OUTPUT_DIR / f"web_automate_fail_{index}_{ts}.html"
        try:
            await self._page.screenshot(path=str(screenshot_path), full_page=True, timeout=5000)
            artifact["screenshot"] = str(screenshot_path)
        except Exception:
            pass
        try:
            html = await self._page.content()
            dom_path.write_text(html, encoding="utf-8")
            artifact["dom_snapshot"] = str(dom_path)
        except Exception:
            pass
        try:
            req_stats = await self._page.evaluate(
                """() => {
                    const list = performance.getEntriesByType('resource') || [];
                    return list.slice(-15).map(it => ({
                        name: it.name,
                        initiatorType: it.initiatorType,
                        duration: Math.round(it.duration || 0),
                    }));
                }"""
            )
            artifact["network_summary"] = req_stats
        except Exception:
            artifact["network_summary"] = []
        return artifact

    async def _execute_action_reliable(self, index: int, act: dict[str, Any], action_type: str) -> ActionResult:
        retries = max(0, int(act.get("retries", 2)))
        timeout_ms = max(500, int(act.get("timeout_ms", act.get("timeout", 12000))))
        last_reason = "action_failed"

        blocked, block_reason = await self._should_block_for_login(action_type)
        if blocked:
            artifact = await self._collect_failure_artifacts(index, action_type, block_reason)
            self._artifacts.append(artifact)
            return ActionResult(
                success=False,
                message=f"❌ {block_reason}",
                data={"failure_code": "login_state_invalid", "artifacts": artifact},
            )

        for attempt in range(retries + 1):
            try:
                await self._wait_visible_and_stable(action_type, act, timeout_ms)
            except Exception as e:
                last_reason = f"wait_visible_or_stable_failed: {e}"
                if attempt >= retries:
                    break
                await asyncio.sleep(0.25 * (attempt + 1))
                continue

            try:
                result = await asyncio.wait_for(self._do_action(act, action_type), timeout=timeout_ms / 1000)
            except Exception as e:
                last_reason = f"action_timeout_or_error: {e}"
                if attempt >= retries:
                    break
                await asyncio.sleep(0.25 * (attempt + 1))
                continue

            self._update_domain_health(action_type, result)
            assert_ok, assert_reason = await self._assert_action_result(action_type, act, result)
            if result.success and assert_ok:
                return result

            last_reason = assert_reason if result.success else result.message
            if attempt < retries:
                await asyncio.sleep(0.25 * (attempt + 1))

        artifact = await self._collect_failure_artifacts(index, action_type, last_reason)
        self._artifacts.append(artifact)
        return ActionResult(
            success=False,
            message=f"❌ {action_type} 失败（重试{retries}次后）: {last_reason}",
            data={"failure_code": "browser_action_failed", "artifacts": artifact},
        )

    async def _assert_action_result(
        self,
        action_type: str,
        act: dict[str, Any],
        result: ActionResult,
    ) -> tuple[bool, str]:
        if not result.success:
            return False, result.message
        data = result.data or {}
        if action_type == "screenshot":
            screenshot = data.get("path")
            if not screenshot or not Path(str(screenshot)).exists():
                return False, "screenshot_file_missing"
        if self._page is not None:
            current_url = str(self._page.url or "")
            if action_type in {"open", "click", "press_key"} and current_url.startswith("about:blank"):
                return False, "page_not_navigated"

            expected_url = act.get("expected_url_contains")
            if expected_url:
                expected = [str(expected_url)] if isinstance(expected_url, str) else [str(v) for v in expected_url if v]
                if expected and not any(token in current_url for token in expected):
                    return False, f"url_assert_failed(expected_contains={expected}, actual={current_url})"

            blocked_url = act.get("expected_url_not_contains")
            if blocked_url:
                blocked = [str(blocked_url)] if isinstance(blocked_url, str) else [str(v) for v in blocked_url if v]
                if blocked and any(token in current_url for token in blocked):
                    return False, f"url_assert_failed(unexpected_contains={blocked}, actual={current_url})"

            assert_selector = str(act.get("assert_selector") or "").strip()
            if assert_selector:
                timeout = max(200, int(act.get("assert_timeout_ms", 1500)))
                try:
                    await self._page.wait_for_selector(assert_selector, timeout=timeout, state="visible")
                except Exception:
                    return False, f"selector_assert_failed({assert_selector})"

            assert_text = act.get("assert_text_contains")
            if assert_text:
                candidates = [str(assert_text)] if isinstance(assert_text, str) else [str(v) for v in assert_text if v]
                if candidates:
                    source_text = str(data.get("text") or "")
                    if not source_text:
                        try:
                            source_text = str(await self._page.inner_text("body"))
                        except Exception:
                            source_text = ""
                    if not any(token in source_text for token in candidates):
                        return False, f"text_assert_failed(expected_contains={candidates})"
        return True, ""

    def get_runtime_artifacts(self) -> dict[str, Any]:
        return {
            "checkpoints": list(self._checkpoints),
            "artifacts": list(self._artifacts),
            "domain_health": dict(self._domain_health),
        }

    # ── 执行动作序列 ─────────────────────────────────

    async def execute(
        self,
        action_list: list[dict[str, Any]],
        *,
        start_index: int = 0,
        continue_on_error: bool = False,
    ) -> list[dict[str, Any]]:
        """执行动作序列，返回每个动作的结果"""
        results = []
        self._checkpoints = []
        self._artifacts = []
        for offset, act in enumerate(action_list):
            index = start_index + offset
            action_type = act.get("action", "")
            self._record_checkpoint(index, action_type, "before")
            result = await self._execute_action_reliable(index, act, action_type)
            checkpoint = self._record_checkpoint(index, action_type, "after", result)
            results.append({
                "action": action_type,
                "success": result.success,
                "message": result.message,
                "data": result.data,
                "step_checkpoint": checkpoint,
            })
            if not result.success and not continue_on_error:
                # 默认 fail-fast：失败即停，避免级联错误污染后续步骤
                break
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
        elif action_type == "select_option":
            return await a.select_option(
                selector=act.get("selector", ""),
                value=act.get("value"),
                label=act.get("label"),
                index=act.get("index"),
            )
        elif action_type == "upload_file":
            return await a.upload_file(
                selector=act.get("selector", ""),
                file_paths=act.get("file_paths", []),
            )
        elif action_type == "enter_iframe":
            return await a.enter_iframe(selector=act.get("selector", ""))
        elif action_type == "exit_iframe":
            return await a.exit_iframe()
        elif action_type == "hover":
            return await a.hover(selector=act.get("selector", ""))
        elif action_type == "drag_and_drop":
            return await a.drag_and_drop(
                source_selector=act.get("source_selector", ""),
                target_selector=act.get("target_selector", ""),
            )
        elif action_type == "check":
            return await a.check(
                selector=act.get("selector", ""),
                checked=act.get("checked", True),
            )
        elif action_type == "double_click":
            return await a.double_click(selector=act.get("selector", ""))
        else:
            return ActionResult(success=False, message=f"❌ 未知动作：{action_type}")


# ── 同步封装（给 main.py 调用）──────────────────────────

def run_engine(
    mode: str,
    action_list: list[dict[str, Any]],
    reuse_url: str = "",
) -> dict[str, Any]:
    """同步运行引擎，返回结果（内部跑 async 事件循环）"""
    engine = BrowserEngine(mode)

    async def _run():
        # 1. 启动浏览器
        start_msg = await engine.start(reuse_url=reuse_url if mode in ("cdp", "cdp_incognito") else "")
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
