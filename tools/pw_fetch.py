#!/usr/bin/env python3
"""浏览器抓取脚本(playwright + 系统 chromium)——给求解 agent 的现成工具。

用途:需要真实浏览器渲染/执行 JS 的场景(登录后页面、XSS/CSRF 验证、SPA 内容、
需要 cookie 会话的访问),curl 拿不到的页面内容用它。

用法:
  python3 /opt/tools/pw_fetch.py <url> [选项]
选项:
  --html            输出完整 HTML(默认输出渲染后的可见文本)
  --wait-ms N       加载后额外等待毫秒(JS 渲染/弹窗)
  --shot PATH       截图存 PATH(png)
  --cookie 'k=v; k2=v2'   附加 cookie
  --ignore-https    忽略证书错误(自签靶场常见)
  --timeout-ms N    页面加载超时(默认 30000)
  --max-chars N     输出截断(默认 30000)

容器内以 root 运行 → chromium 必须 --no-sandbox(见 launch 参数,勿移除)。
"""
import argparse
import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--html", action="store_true")
    ap.add_argument("--wait-ms", type=int, default=0)
    ap.add_argument("--shot")
    ap.add_argument("--cookie", default="")
    ap.add_argument("--ignore-https", action="store_true")
    ap.add_argument("--timeout-ms", type=int, default=30000)
    ap.add_argument("--max-chars", type=int, default=30000)
    a = ap.parse_args()

    try:
        with sync_playwright() as p:
            # 优先 playwright 捆绑浏览器(版本精确匹配);构建期 CDN 失败时退系统 chromium
            try:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            except Exception:
                browser = p.chromium.launch(
                    executable_path="/usr/bin/chromium",
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            ctx = browser.new_context(ignore_https_errors=a.ignore_https,
                                      user_agent="Mozilla/5.0 (X11; Linux x86_64) "
                                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                                 "Chrome/140.0 Safari/537.36")
            if a.cookie:
                for pair in a.cookie.split(";"):
                    pair = pair.strip()
                    if not pair or "=" not in pair:
                        continue
                    k, _, v = pair.partition("=")
                    ctx.add_cookies([{"name": k.strip(), "value": v.strip(),
                                      "url": a.url}])
            page = ctx.new_page()
            try:
                page.goto(a.url, timeout=a.timeout_ms, wait_until="domcontentloaded")
            except Exception as e:
                print(f"[pw_fetch] goto warning: {e}", file=sys.stderr)
            if a.wait_ms:
                page.wait_for_timeout(a.wait_ms)
            final_url = page.url
            if a.shot:
                page.screenshot(path=a.shot, full_page=False)
                print(f"[pw_fetch] screenshot saved: {a.shot}")
            if a.html:
                content = page.content()
                kind = "html"
            else:
                content = page.inner_text("body") if page.locator("body").count() else ""
                kind = "text"
            title = page.title()
            print(f"== title: {title!r} | final_url: {final_url} | {kind} "
                  f"{len(content)} chars (truncated {a.max_chars}) ==")
            print(content[: a.max_chars])
            browser.close()
        return 0
    except Exception as e:
        print(f"[pw_fetch] FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
