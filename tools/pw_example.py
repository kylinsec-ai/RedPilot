#!/usr/bin/env python3
"""playwright 自定义脚本模板/示例——agent 写复杂浏览器流程时复制此骨架。

必改点:目标 URL、步骤;可复用:root 无沙箱 launch、上下文/新页、输出约定。
更简单的抓取用 /opt/tools/pw_fetch.py 即可,无需写脚本。
"""
from playwright.sync_api import sync_playwright

URL = "http://target/"          # TODO: 替换为实际目标

with sync_playwright() as p:
    try:
        browser = p.chromium.launch(
            headless=True,
            # 容器内 root 运行:--no-sandbox 必需;--disable-dev-shm-usage 防 /dev/shm 不足
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
    except Exception:
        browser = p.chromium.launch(
            executable_path="/usr/bin/chromium",
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
    ctx = browser.new_context(ignore_https_errors=True)
    page = ctx.new_page()

    page.goto(URL, timeout=30000, wait_until="domcontentloaded")

    # ── 在这里写你的浏览器步骤 ──────────────────────────────
    # 示例:填表提交
    # page.fill('input[name="user"]', "admin")
    # page.fill('input[name="pass"]', "pass123")
    # page.click('button[type="submit"]')
    # page.wait_for_timeout(1000)
    #
    # 示例:读 cookie / localStorage
    # print(page.context.cookies())
    # print(page.evaluate("localStorage"))
    # ────────────────────────────────────────────────────────

    # 输出约定:打印关键结果(文本/响应/内容),勿打印整页无界内容
    print("final_url:", page.url)
    print("title:", page.title())
    body = page.locator("body").inner_text() if page.locator("body").count() else ""
    print(body[:20000])

    browser.close()
