"""web_automate 动作库

所有操作统一封装，三种模式共享同一套 Action 接口。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ActionResult:
    """单个动作的执行结果"""
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


class Actions:
    """浏览器动作库——三种模式共享"""

    def __init__(self, page, output_dir: Path):
        self._page = page
        self._output_dir = output_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def _try_selectors(self, selector: str, timeout: int = 10000) -> str:
        """逗号分隔的 selector 列表，依次尝试，返回第一个匹配的

        优先等待 visible，如果超时则回退到 attached（处理百度首页等
        视觉可见但 DOM 标记为 hidden 的特殊情况）
        """
        # 如果只有一个选择器，直接返回
        if "," not in selector:
            try:
                await self._page.wait_for_selector(selector, timeout=timeout, state="visible")
            except Exception:
                await self._page.wait_for_selector(selector, timeout=5000, state="attached")
            return selector

        # 多个选择器，依次尝试
        parts = [s.strip() for s in selector.split(",") if s.strip()]
        for sel in parts:
            try:
                await self._page.wait_for_selector(sel, timeout=3000, state="visible")
                return sel
            except Exception:
                # 回退到 attached
                try:
                    await self._page.wait_for_selector(sel, timeout=2000, state="attached")
                    return sel
                except Exception:
                    continue

        # 全部失败，用第一个选择器抛出异常
        try:
            await self._page.wait_for_selector(parts[0], timeout=timeout, state="visible")
        except Exception:
            await self._page.wait_for_selector(parts[0], timeout=5000, state="attached")
        return parts[0]

    # ── 页面导航 ──────────────────────────────────────

    async def open(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        """打开 URL"""
        # 特殊协议不补前缀
        if not url.startswith(("http://", "https://", "about:", "chrome:", "file:")):
            url = f"https://{url}"
        try:
            resp = await self._page.goto(url, wait_until=wait_until, timeout=30000)
            status = resp.status if resp else "unknown"
            title = await self._page.title()
            return ActionResult(
                success=True,
                message=f"✅ 已打开：{url}\n📄 标题：{title}\n🔢 状态码：{status}",
                data={"url": url, "title": title, "status": status},
            )
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 打开页面失败：{e}")

    # ── 元素交互 ──────────────────────────────────────

    async def click(self, selector: str) -> ActionResult:
        """点击元素"""
        try:
            sel = await self._try_selectors(selector)
            await self._page.click(sel)
            return ActionResult(success=True, message=f"✅ 已点击：{sel}")
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 点击失败 [{selector}]：{e}")

    async def type_text(self, selector: str, text: str, clear: bool = True) -> ActionResult:
        """输入文字

        优先用 fill（直接赋值，兼容 hidden 元素），失败则回退到 type（模拟键盘）
        """
        try:
            sel = await self._try_selectors(selector)
            if clear:
                try:
                    await self._page.fill(sel, "", force=True)
                except Exception:
                    # fill 不支持则用 triple-click + delete
                    await self._page.click(sel, click_count=3)
                    await self._page.keyboard.press("Backspace")
            try:
                await self._page.fill(sel, text, force=True)
            except Exception:
                # fill 失败则用 type 模拟键盘
                await self._page.type(sel, text, delay=50)
            return ActionResult(
                success=True,
                message=f"✅ 已输入 [{sel}]：{text[:50]}{'...' if len(text) > 50 else ''}",
            )
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 输入失败 [{selector}]：{e}")

    async def press_key(self, selector: str, key: str) -> ActionResult:
        """按键（Enter/Tab/Escape 等）"""
        try:
            if selector:
                await self._page.wait_for_selector(selector, timeout=10000)
                await self._page.press(selector, key)
            else:
                await self._page.keyboard.press(key)
            return ActionResult(success=True, message=f"✅ 已按键：{key}")
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 按键失败 [{key}]：{e}")

    # ── 信息获取 ──────────────────────────────────────

    async def read(self, selector: str) -> ActionResult:
        """读取元素文本"""
        try:
            sel = await self._try_selectors(selector)
            text = await self._page.text_content(sel)
            if text is None:
                text = await self._page.inner_text(sel)
            return ActionResult(
                success=True,
                message=f"✅ 读取内容 [{sel}]：{text[:500]}{'...' if len(text or '') > 500 else ''}",
                data={"text": text},
            )
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 读取失败 [{selector}]：{e}")

    async def read_page(self) -> ActionResult:
        """读取整个页面文本"""
        try:
            text = await self._page.inner_text("body")
            title = await self._page.title()
            return ActionResult(
                success=True,
                message=f"✅ 页面内容（{len(text)} 字）：{text[:300]}...",
                data={"title": title, "text": text},
            )
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 读取页面失败：{e}")

    # ── 截图 ──────────────────────────────────────────

    async def screenshot(self, name: str | None = None, full_page: bool = False) -> ActionResult:
        """截取页面截图"""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{name or 'screenshot'}_{timestamp}.png"
        filepath = self._output_dir / filename
        try:
            # 截图：设置较短的超时，跳过动画等待
            await self._page.screenshot(
                path=str(filepath),
                full_page=full_page,
                timeout=5000,  # 5秒超时
                animations="disabled",
                caret="hide",
            )
            
            size = filepath.stat().st_size
            size_str = f"{size / 1024:.1f}KB" if size < 1048576 else f"{size / 1048576:.1f}MB"
            return ActionResult(
                success=True,
                message=f"📸 截图成功：{filename}（{size_str}）",
                data={"path": str(filepath), "name": filename},
            )
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 截图失败：{e}")

    # ── 等待 ──────────────────────────────────────────

    async def wait(self, ms: int = 2000) -> ActionResult:
        """等待毫秒"""
        await asyncio.sleep(ms / 1000)
        return ActionResult(success=True, message=f"⏳ 等待 {ms}ms")

    async def wait_for_selector(self, selector: str, timeout: int = 10000) -> ActionResult:
        """等待元素出现"""
        try:
            await self._page.wait_for_selector(selector, timeout=timeout)
            return ActionResult(success=True, message=f"✅ 元素已出现：{selector}")
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 等待元素超时 [{selector}]：{e}")

    # ── 滚动 ──────────────────────────────────────────

    async def scroll(self, direction: str = "down", pixels: int = 500) -> ActionResult:
        """滚动页面"""
        dy = pixels if direction == "down" else -pixels
        try:
            await self._page.mouse.wheel(0, dy)
            return ActionResult(success=True, message=f"✅ 已向{direction}滚动 {pixels}px")
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 滚动失败：{e}")

    # ── 获取页面信息 ──────────────────────────────────

    async def get_page_info(self) -> ActionResult:
        """获取当前页面基本信息"""
        try:
            title = await self._page.title()
            url = self._page.url
            return ActionResult(
                success=True,
                message=f"📄 当前页面：{title}\n🔗 URL：{url}",
                data={"title": title, "url": url},
            )
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 获取页面信息失败：{e}")

    async def get_cookies(self, urls: str | None = None) -> ActionResult:
        """获取当前上下文的 cookies

        urls: 可选，逗号分隔的 URL 列表，只返回匹配这些 URL 的 cookies
        """
        try:
            context = self._page.context
            if urls:
                url_list = [u.strip() for u in urls.split(",") if u.strip()]
                cookies = await context.cookies(url_list)
            else:
                cookies = await context.cookies()
            return ActionResult(
                success=True,
                message=f"✅ 获取到 {len(cookies)} 个 cookie",
                data={"cookies": cookies, "count": len(cookies)},
            )
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 获取 cookies 失败：{e}")

    async def get_storage(self, key: str | None = None) -> ActionResult:
        """获取 localStorage 数据

        key: 可选，指定 key 只获取该项；不指定则获取全部
        """
        try:
            if key:
                value = await self._page.evaluate(f"() => localStorage.getItem('{key}')")
                return ActionResult(
                    success=True,
                    message=f"✅ localStorage['{key}'] = {str(value)[:200]}",
                    data={"key": key, "value": value},
                )
            else:
                storage = await self._page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
                count = len(storage) if storage else 0
                return ActionResult(
                    success=True,
                    message=f"✅ localStorage 共 {count} 项",
                    data={"storage": storage, "count": count},
                )
        except Exception as e:
            return ActionResult(success=False, message=f"❌ 获取 localStorage 失败：{e}")
