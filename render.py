# -*- coding: utf-8 -*-
"""渲染层：Playwright 无头浏览器打开 HTML 模板 → 注入 init(data) → 截图（移植自 Amiya-Bot Html.create_html_image）。"""

import asyncio
import json
import logging
import os

logger = logging.getLogger("astrbot")

_browser = None
_playwright = None


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
    """
    url = "file:///" + os.path.abspath(template_path).replace("\\", "/")
    page = None
    browser = None
    pw = None
    try:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
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

        result = await page.screenshot(full_page=True)
        return result
    finally:
        if page is not None:
            try:
                await page.context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass
