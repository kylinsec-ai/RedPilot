"""Playwright end-to-end tests for the TSecBench Range Console.

Run with:  python -m pytest e2e
Each test drives the real browser against a live server (see conftest.py).
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

try:
    # pytest 默认 prepend 模式：e2e/ 在 sys.path 上，conftest 以顶层模块导入
    from conftest import CHALLENGES, TOKEN
except ModuleNotFoundError:  # pragma: no cover — --import-mode=importlib 等运行方式
    from e2e.conftest import CHALLENGES, TOKEN

WEB = CHALLENGES[0]["unique_code"]


def connect(page: Page, server_url: str) -> None:
    page.goto(server_url + "/")
    page.fill("#tokenInput", TOKEN)
    page.click("#gateForm button[type=submit]")
    page.wait_for_selector(".card")


def web_card(page: Page):
    return page.locator(".card", has_text=WEB)


def test_connect_lists_all_challenges(page: Page, server_url: str) -> None:
    connect(page, server_url)

    expect(page.locator(".card")).to_have_count(3)
    expect(page.locator(".card-code", has_text=WEB)).to_be_visible()
    # 未作答：3 题满分 100+200+150，当前得分占位
    expect(page.locator("#statScore")).to_have_text("0 / 450")
    expect(page.locator("#statLive")).to_have_text("0")
    expect(page.locator("#toolbarCount")).to_contain_text("3")


def test_start_reveals_live_address_strip(page: Page, server_url: str) -> None:
    connect(page, server_url)

    web_card(page).locator("button[data-act=start]").click()

    expect(web_card(page).locator(".addr-item")).to_contain_text("10.0.1.5:8080")
    expect(web_card(page).locator(".status")).to_have_text("已就绪")
    expect(web_card(page)).to_have_class(re.compile(r"\bis-live\b"))
    expect(page.locator("#statLive")).to_have_text("1")


def test_submit_correct_flag_awards_score(page: Page, server_url: str) -> None:
    connect(page, server_url)

    web_card(page).locator("button[data-act=start]").click()
    page.wait_for_selector(".addr-strip")
    web_card(page).locator("button[data-act=submit]").click()

    page.fill("#flagInput", "flag{admin_sql}")
    page.click("#submitForm button[type=submit]")

    expect(page.locator("#submitResult")).to_have_class(re.compile(r"\bok\b"))
    expect(page.locator("#submitResult")).to_contain_text("+40")
    expect(page.locator("#statScore")).to_have_text("40")
    expect(web_card(page).locator(".progress-label")).to_have_text("1 / 2 flag")


def test_submit_wrong_flag_shows_error(page: Page, server_url: str) -> None:
    connect(page, server_url)

    web_card(page).locator("button[data-act=submit]").click()
    page.fill("#flagInput", "flag{not_real}")
    page.click("#submitForm button[type=submit]")

    expect(page.locator("#submitResult")).to_have_class(re.compile(r"\berr\b"))
    expect(page.locator("#statScore")).to_have_text("0 / 450")


def test_hint_modal_opens(page: Page, server_url: str) -> None:
    connect(page, server_url)

    web_card(page).locator("button[data-act=hint]").click()

    expect(page.locator("#hintModal")).to_be_visible()
    expect(page.locator("#hintBody")).to_have_text("尝试在登录表单的用户名字段使用单引号测试注入点")
    page.keyboard.press("Escape")
    expect(page.locator("#hintModal")).to_be_hidden()


def test_close_releases_instance(page: Page, server_url: str) -> None:
    connect(page, server_url)

    web_card(page).locator("button[data-act=start]").click()
    page.wait_for_selector(".addr-strip")
    web_card(page).locator("button[data-act=close]").click()

    expect(web_card(page).locator(".status")).to_have_text("已停止")
    expect(web_card(page).locator(".addr-strip")).to_have_count(0)
    expect(web_card(page).locator("button[data-act=start]")).to_be_visible()
    expect(page.locator("#statLive")).to_have_text("0")


def test_filters_and_score_view(page: Page, server_url: str) -> None:
    connect(page, server_url)

    page.click("[data-filter=live]")
    expect(page.locator(".card")).to_have_count(0)
    expect(page.locator(".empty")).to_be_visible()

    page.click("[data-filter=all]")
    expect(page.locator(".card")).to_have_count(3)

    page.click("[data-nav=score]")
    expect(page.locator("#scoreView")).to_be_visible()
    expect(page.locator("#scoreView")).to_contain_text("CURRENT SCORE")
    expect(page.locator("#scoreView .score-total")).to_have_text("0/ 450")
    expect(page.locator("#scoreView")).to_contain_text("简单")
    # 切回任务视图，卡片恢复
    page.click("[data-nav=tasks]")
    expect(page.locator("#cards")).to_be_visible()


def test_mobile_layout_collapses_sidebar(page: Page, server_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    connect(page, server_url)

    expect(page.locator(".wordmark-sub")).to_be_hidden()
    expect(page.locator(".stats")).to_be_hidden()
    expect(page.locator("#rail")).to_be_visible()
    # 题目卡片仍然完整可操作
    expect(web_card(page)).to_be_visible()
