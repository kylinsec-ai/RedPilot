"""control proxy(可选插件):obs 同源转发控制面 API。

定位:纯网络转发代理,无代码 import core(依赖方向约束)。默认不挂载:
app 仅当 Settings.control_proxy_enabled 为真(OBS_CONTROL_URL 已配且未被
OBS_ENABLE_CONTROL_PROXY=0 显式关闭)时才 include 本 router。
未挂载时 /api/v1/* 不存在(404);挂载但 control_url 缺失时同样 404。
读端观测 API(/api/status|events|runs|...)不受本开关影响。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from urllib import error as url_error
from urllib import request as url_request

log = logging.getLogger("obs.control_proxy")

router = APIRouter(include_in_schema=False)


def _control_url(request: Request) -> str | None:
    return getattr(request.app.state, "control_url", None)


def _proxy_control_sync(
    url: str, method: str, body: bytes, admin_token: str,
    content_type: str | None,
) -> tuple[int, bytes, str]:
    headers = {"GHOST_ADMIN_TOKEN": admin_token}
    if content_type:
        headers["Content-Type"] = content_type
    req = url_request.Request(
        url,
        data=body if method != "GET" else None,
        headers=headers,
        method=method,
    )
    try:
        with url_request.urlopen(req, timeout=10) as response:
            return response.status, response.read(), response.headers.get_content_type()
    except url_error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get_content_type()
    except (url_error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(502, f"control plane unreachable: {exc}") from exc


@router.api_route("/api/v1/{path:path}", methods=["GET", "POST"])
async def control_proxy(path: str, request: Request) -> Response:
    """同源转发控制面 API；管理员 token 只在浏览器会话与平台内网间传递。"""

    control_url = _control_url(request)
    if not control_url:
        raise HTTPException(404, "control plane proxy not configured")
    token = request.headers.get("X-Platform-Admin-Token", "")
    target = control_url + "/api/v1/" + path
    if request.url.query:
        target += "?" + request.url.query
    body = await request.body()
    status_code, response_body, media_type = await run_in_threadpool(
        _proxy_control_sync,
        target,
        request.method,
        body,
        token,
        request.headers.get("content-type"),
    )
    return Response(content=response_body, status_code=status_code, media_type=media_type)
