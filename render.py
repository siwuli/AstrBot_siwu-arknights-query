# -*- coding: utf-8 -*-
"""渲染层：Playwright 无头浏览器打开 HTML 模板 → 注入 init(data) → 截图（移植自 Amiya-Bot Html.create_html_image）。

浏览器内核路径由 PLAYWRIGHT_BROWSERS_PATH 指定：容器内 HOME 为 /tmp，
Playwright 默认的 ~/.cache/ms-playwright 会随容器重建丢失，因此统一指向
镜像内预装的 /opt/ms-playwright（见 astrbot.Dockerfile）。
"""

import asyncio
import json
import logging
import os

logger = logging.getLogger("astrbot")

# 必须在导入 playwright 之前设置：容器内 HOME=/tmp，默认缓存路径不持久
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/ms-playwright")

_playwright = None
_browser = None
_browser_lock = None


class RenderUnavailable(RuntimeError):
    """渲染依赖不可用（Playwright 未安装 / Chromium 内核或系统依赖缺失）。"""


def _get_lock() -> asyncio.Lock:
    """惰性创建锁，避免在导入期（可能无运行中的事件循环）绑定事件循环。"""
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


def _browser_alive() -> bool:
    """浏览器进程是否仍然存活（崩溃后返回 False，触发重建）。"""
    if _browser is None:
        return False
    try:
        return bool(_browser.is_connected())
    except Exception:  # noqa: BLE001
        return False


async def _ensure_browser():
    """复用进程内的 Chromium 实例。

    原先每次渲染都 launch/close 一个浏览器（约 2~3 秒开销），改为复用后
    单次渲染只需新建页面，显著降低延迟与内存波动。
    """
    global _playwright, _browser
    if _browser_alive():
        return _browser
    async with _get_lock():
        if _browser_alive():
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RenderUnavailable(
                "未安装 Playwright，图片渲染不可用。请在 AstrBot 容器内执行："
                "pip install playwright && python -m playwright install --with-deps chromium"
            ) from e
        if _playwright is None:
            _playwright = await async_playwright().start()
        try:
            _browser = await _playwright.chromium.launch(
                headless=True,
                # --disable-dev-shm-usage：容器默认 /dev/shm 仅 64MB，大页面截图会崩
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
        except Exception as e:  # noqa: BLE001
            raise RenderUnavailable(
                f"Chromium 启动失败（浏览器内核缺失或系统依赖不完整）：{e}"
            ) from e
        return _browser


async def shutdown() -> None:
    """关闭复用的浏览器与 Playwright（插件卸载/重载时调用）。"""
    global _playwright, _browser
    browser, pw = _browser, _playwright
    _browser = None
    _playwright = None
    if browser is not None:
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass
    if pw is not None:
        try:
            await pw.stop()
        except Exception:  # noqa: BLE001
            pass


async def html_to_image(template_path: str, data: dict, width: int = 375, render_time: int = 500,
                        timeout: int = 30, device_scale_factor: int = 2) -> bytes:
    """渲染 HTML 模板并全页截图，返回 PNG bytes。

    Args:
        template_path: 模板 html 的本地绝对路径
        data: 注入 window.init(data) 的数据
        width: 视口宽度（CSS px）
        render_time: 注入后等待渲染的时间（毫秒）
        timeout: 页面加载超时（秒）
        device_scale_factor: 截图倍率（2 保证高清）

    Raises:
        RenderUnavailable: Playwright 未安装或 Chromium 无法启动。
    """
    url = "file:///" + os.path.abspath(template_path).replace("\\", "/")
    browser = await _ensure_browser()
    page = None
    try:
        page = await browser.new_page(
            viewport={"width": width, "height": 720},
            device_scale_factor=device_scale_factor,
        )

        await page.goto(url, timeout=timeout * 1000)
        await page.wait_for_load_state("load", timeout=timeout * 1000)

        if data:
            injected = (
                "if ('init' in window) { init(%s) } else { console.warn('window.init missing'); }"
                % json.dumps(data)
            )
            await page.evaluate(injected)

        # 等待 Vue 渲染
        await asyncio.sleep(render_time / 1000)

        # 等待页面内所有图片加载完成（公招/干员等模板立绘多，固定延时可能截到空白图）。
        # 条件用 complete：图片进入终态（成功或失败）即视为结束——路径缺失/加载失败的图
        # 不会让等待无限阻塞，只等待真正还在加载中的图片
        try:
            await page.wait_for_function(
                """() => {
                    const imgs = Array.from(document.querySelectorAll('img'));
                    if (imgs.length === 0) return true;
                    return imgs.every(i => i.complete);
                }""",
                timeout=10000,
            )
        except Exception:
            logger.warning("等待图片加载超时，使用当前渲染结果截图")

        return await page.screenshot(full_page=True)
    finally:
        # 只关页面/上下文，浏览器实例保留给下次渲染复用
        if page is not None:
            try:
                await page.context.close()
            except Exception:  # noqa: BLE001
                pass
