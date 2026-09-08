"""静态构建产物 mime 表 + 穿越守卫 — 两个 HTTP 服务(obs FastAPI / worker stdlib)共用。

注意:只共享"哪些扩展名可服务、什么 Content-Type、名字守卫"这一层;
Cache-Control 策略是各服务自己的事(obs=immutable 哈希资产 / worker=no-store 热更),不在此统一。
"""

from __future__ import annotations

import re

# 名字只允许 URL 安全平铺名(vite 只发 <hash>.js/.css),杜绝路径穿越
ASSET_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}
ASSET_RX = re.compile(r"[A-Za-z0-9._-]{1,120}")


def asset_content_type(ext: str) -> str | None:
    """扩展名(小写) -> Content-Type;不在白名单返回 None(调用方 404)。"""
    return ASSET_TYPES.get(ext.lower())
