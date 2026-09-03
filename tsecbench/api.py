"""FastAPI application implementing the TSecBench Challenges API."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .errors import APIError
from .models import parse_task_config
from .provisioner import ContainerProvisioner, provisioner_for
from .service import ChallengeService
from .store import Store
from .vpn import VPNManager


class SubmitRequest(BaseModel):
    unique_code: str
    flag: str = Field(min_length=1, max_length=4096)


class ChallengeResponse(BaseModel):
    unique_code: str
    description: str | None
    difficulty: str
    level: int
    total_score: int
    flag_count: int
    correct_flag_count: int
    is_completed: bool
    container_status: str
    container_addr: list[str]


class StartResponse(BaseModel):
    unique_code: str
    container_addr: list[str]


class HintResponse(BaseModel):
    unique_code: str
    hint: str | None


class SubmitResponse(BaseModel):
    correct: bool
    awarded: int
    cumulative_score: int
    correct_flag_count: int
    total_flag_count: int
    matched_flag_index: int | None


class CloseResponse(BaseModel):
    unique_code: str
    closed: bool


class VPNConfigRequest(BaseModel):
    content: str = Field(min_length=1, description="OpenVPN 配置文件内容 (.ovpn)")


def _error_response(error: APIError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.as_response())


def create_app(
    settings: Settings | None = None,
    *,
    database_path: str | None = None,
    tasks: Any = None,
    provisioner: ContainerProvisioner | None = None,
    max_active_challenges: int | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(database_path or settings.database_path)
    service = ChallengeService(
        store,
        provisioner or provisioner_for(settings.provisioner),
        settings.max_active_challenges if max_active_challenges is None else max_active_challenges,
    )
    if tasks is None:
        normalized_tasks = settings.load_tasks()
    else:
        normalized_tasks = parse_task_config(tasks)
    service.seed(normalized_tasks)

    # 复用同一个客户端：代理模式下控制台每 2.5s 轮询一次，连接池复用免去每次握手。
    # 惰性创建：本地模式（未配置 BENCHMARK_BASE_URL）与单元测试（裸 TestClient 不跑
    # lifespan）不会遗留未关闭的客户端；重复进入 lifespan 时已关闭的客户端也会重建。
    proxy_client: httpx.AsyncClient | None = None

    def _get_proxy_client() -> httpx.AsyncClient:
        nonlocal proxy_client
        if proxy_client is None or proxy_client.is_closed:
            proxy_client = httpx.AsyncClient(timeout=30, follow_redirects=False)
        return proxy_client

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if proxy_client is not None:
            await proxy_client.aclose()

    app = FastAPI(title="TSecBench Platform", version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.service = service
    app.state.settings = settings
    app.state.vpn = VPNManager(Path(database_path or settings.database_path).parent / "vpn")

    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, error: APIError) -> JSONResponse:
        return _error_response(error)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
        return _error_response(APIError(500, "internal_error", "Internal server error"))

    def authenticated_token(benchmark_token: str | None = Header(default=None, alias="BENCHMARK_TOKEN")) -> str:
        return service.authenticate(benchmark_token)

    @app.get(
        "/openapi/v1/challenges",
        response_model=list[ChallengeResponse],
        tags=["challenges"],
    )
    def list_challenges(token: str = Depends(authenticated_token)) -> list[dict]:
        return service.list_challenges(token)

    @app.post(
        "/openapi/v1/challenges/start",
        response_model=StartResponse,
        tags=["challenges"],
    )
    def start_challenge(
        unique_code: str = Query(...),
        token: str = Depends(authenticated_token),
    ) -> dict:
        return service.start(token, unique_code)

    @app.get(
        "/openapi/v1/challenges/hint",
        response_model=HintResponse,
        tags=["challenges"],
    )
    def get_hint(
        unique_code: str = Query(...),
        token: str = Depends(authenticated_token),
    ) -> dict:
        return service.hint(token, unique_code)

    @app.post(
        "/openapi/v1/challenges/submit",
        response_model=SubmitResponse,
        tags=["challenges"],
    )
    def submit_flag(
        submission: SubmitRequest,
        token: str = Depends(authenticated_token),
    ) -> dict:
        return service.submit(token, submission.unique_code, submission.flag)

    @app.post(
        "/openapi/v1/challenges/close",
        response_model=CloseResponse,
        tags=["challenges"],
    )
    def close_challenge(
        unique_code: str = Query(...),
        token: str = Depends(authenticated_token),
    ) -> dict:
        return service.close(token, unique_code)

    # ---- OpenVPN lifecycle ----
    @app.get("/openapi/v1/vpn/status", tags=["vpn"])
    def vpn_status(token: str = Depends(authenticated_token)) -> dict:
        return app.state.vpn.as_dict()

    @app.post("/openapi/v1/vpn/config", response_model=None, tags=["vpn"])
    def vpn_upload(payload: VPNConfigRequest, token: str = Depends(authenticated_token)) -> dict:
        return app.state.vpn.as_dict(app.state.vpn.save_config(payload.content))

    @app.post("/openapi/v1/vpn/start", tags=["vpn"])
    def vpn_start(token: str = Depends(authenticated_token)) -> dict:
        return app.state.vpn.as_dict(app.state.vpn.start())

    @app.post("/openapi/v1/vpn/stop", tags=["vpn"])
    def vpn_stop(token: str = Depends(authenticated_token)) -> dict:
        return app.state.vpn.as_dict(app.state.vpn.stop())

    # ---- Frontend (Range Console) ----
    static_dir = Path(__file__).resolve().parent / "static"

    def _render_console_page() -> str:
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        console_config = (
            json.dumps(
                {"baseUrl": settings.benchmark_base_url, "token": settings.benchmark_token},
                ensure_ascii=False,
            )
            # 防止配置中嵌入 `</script>` 时逃逸出内联脚本
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )
        return html.replace("__TSECBENCH_CONFIG__", console_config)

    # 配置与模板在启动时即固定，组装一次避免每个请求重复替换；
    # 文件缺失时退回按请求渲染（旧行为），避免整进程在导入期崩溃。
    try:
        console_page = _render_console_page()
    except FileNotFoundError:
        console_page = None

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        if console_page is None:
            return HTMLResponse(_render_console_page())
        return HTMLResponse(console_page)

    # 单一路由同时覆盖裸路径（题目列表）与子路径：`/benchmark{path:path}` 能匹配
    # 空路径，免去第二个路由与 Starlette 追加斜杠的 307 重定向。
    @app.api_route("/benchmark{path:path}", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
    async def benchmark_proxy(request: Request, path: str = "") -> Response:
        """Forward the console to the configured remote benchmark API.

        The browser calls this same-origin path; the proxy forwards to
        BENCHMARK_BASE_URL injecting BENCHMARK_TOKEN, dodging the remote's
        lack of CORS support.
        """
        base_url = (settings.benchmark_base_url or "").strip()
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return _error_response(APIError(400, "config_error", "BENCHMARK_BASE_URL 未配置或不是合法的 http(s) URL"))
        body = await request.body()
        base = base_url.rstrip("/") + "/openapi/v1/challenges"
        target = f"{base}{path}" if path else base
        if request.url.query:
            target = f"{target}?{request.url.query}"
        headers = {"Content-Type": request.headers.get("content-type", "application/json")}
        token = settings.benchmark_token or request.headers.get("BENCHMARK_TOKEN")
        if token:
            headers["BENCHMARK_TOKEN"] = token
        try:
            upstream = await _get_proxy_client().request(request.method, target, headers=headers, content=body or None)
        except httpx.HTTPError:
            return _error_response(APIError(502, "upstream_error", "上游服务不可达"))
        media = upstream.headers.get("content-type", "application/json")
        return Response(content=upstream.content, status_code=upstream.status_code, media_type=media)

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    return app
