"""FastAPI 会话中间件：cookie 会话，JSON 文件持久化。

与 Django 版控制台的行为兼容：控制台配置（平台连接/LLM/行为策略）
保存在会话里，浏览器关闭后仍可恢复。
"""

from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from typing import Any

SESSION_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "fastapi_sessions"
_lock = threading.Lock()


def _load(session_id: str) -> dict[str, Any]:
    path = SESSION_DIR / f"{session_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(session_id: str, data: dict[str, Any]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSION_DIR / f"{session_id}.json"
    with _lock:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class SessionStore:
    """基于文件 JSON 的会话存储（一个进程内共享）。"""

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}

    def load(self, session_id: str) -> dict[str, Any]:
        if session_id not in self._cache:
            self._cache[session_id] = _load(session_id)
        return self._cache[session_id]

    def save(self, session_id: str, data: dict[str, Any]) -> None:
        self._cache[session_id] = dict(data)
        _save(session_id, data)

    def new(self) -> str:
        return secrets.token_hex(16)


store = SessionStore()

SESSION_COOKIE = "fastapi_sessionid"


class SessionMiddleware:
    """Starlette 中间件：读取/创建会话，保存到 request.state.session。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        import starlette.requests as sr

        request = sr.Request(scope, receive)
        session_id = request.cookies.get(SESSION_COOKIE) or store.new()
        data = store.load(session_id)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                import http.cookies

                cookie = http.cookies.SimpleCookie()
                cookie[SESSION_COOKIE] = session_id
                cookie[SESSION_COOKIE]["path"] = "/"
                cookie[SESSION_COOKIE]["max-age"] = 30 * 24 * 3600
                headers.append(
                    (b"set-cookie", cookie[SESSION_COOKIE].OutputString().encode("utf-8"))
                )
                message = {**message, "headers": headers}
            await send(message)

        # 会话对象：读写通过 request.state.session
        scope.setdefault("state", {})
        scope["state"]["session"] = data
        scope["state"]["session_id"] = session_id

        # 标记会话是否被修改（由业务代码设置 request.state.session_dirty）
        scope["state"]["session_dirty"] = False

        # 保存回调：响应发送完后写入文件
        original_send = send_wrapper

        class _SendWrapper:
            def __init__(self):
                self._response_started = False

            async def __call__(self, message):
                if message["type"] == "http.response.start":
                    self._response_started = True
                if message["type"] == "http.response.body" and scope["state"].get("session_dirty"):
                    store.save(scope["state"]["session_id"], scope["state"]["session"])
                await original_send(message)

        await self.app(scope, receive, _SendWrapper())


def mark_dirty(request) -> None:
    """业务代码调用：标记会话已修改，响应结束时持久化。"""
    request.scope["state"]["session_dirty"] = True