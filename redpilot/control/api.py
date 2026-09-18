"""FastAPI application implementing the RedPilot Challenges API."""

from __future__ import annotations

import logging
import hmac
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from redpilot.control.challenges import ChallengeFacade
from redpilot.control.config import Settings
from redpilot.control.errors import APIError, admin_not_configured, admin_required
from redpilot.control.models import parse_task_config
from redpilot.control.provisioner import ContainerProvisioner, provisioner_for
from redpilot.control.service import ChallengeService
from redpilot.control.store import Store
from redpilot.control.vpn import VPNManager


log = logging.getLogger("redpilot.api")



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
    content: str = Field(min_length=1, max_length=262144, description="OpenVPN 配置文件内容 (.ovpn)")



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
    # NOTE:启动期种子经 service 直写;运行时路由只经 ChallengeFacade(见下)。

    app = FastAPI(title="RedPilot Platform", version="1.0.0")
    app.state.store = store
    app.state.service = service
    # 内部边界:路由组只经 challenges 外观调用,不直调 Store/Service(见 challenges.py)。
    challenges = ChallengeFacade(service)
    app.state.challenges = challenges
    app.state.settings = settings
    app.state.vpn = VPNManager(Path(database_path or settings.database_path).parent / "vpn")

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
        log.exception("unhandled control-plane error on %s %s", request.method, request.url.path)
        return _error_response(APIError(500, "internal_error", "Internal server error"))

    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, error: APIError) -> JSONResponse:
        return _error_response(error)


    def authenticated_token(benchmark_token: str | None = Header(default=None, alias="BENCHMARK_TOKEN")) -> str:
        return challenges.authenticate(benchmark_token)

    def authenticated_admin(admin_token: str | None = Header(default=None, alias="REDPILOT_ADMIN_TOKEN")) -> None:
        """管理端点凭据(openvpn 生命周期等平台全局特权操作)。

        与参与方任务 token 严格分离:未配置 REDPILOT_ADMIN_TOKEN → 503 fail closed,
        缺失/不符 → 401(常量时间比较)。任一任务 token 都不可触达这些端点。
        """
        expected = settings.admin_token
        if not expected:
            raise admin_not_configured()
        if admin_token is None or not hmac.compare_digest(admin_token.encode(), expected.encode()):
            raise admin_required()

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True, "service": "control"}

    @app.get(
        "/openapi/v1/challenges",
        response_model=list[ChallengeResponse],
        tags=["challenges"],
    )
    def list_challenges(token: str = Depends(authenticated_token)) -> list[dict]:
        return challenges.list_challenges(token)

    @app.post(
        "/openapi/v1/challenges/start",
        response_model=StartResponse,
        tags=["challenges"],
    )
    def start_challenge(
        unique_code: str = Query(...),
        token: str = Depends(authenticated_token),
    ) -> dict:
        return challenges.start(token, unique_code)

    @app.get(
        "/openapi/v1/challenges/hint",
        response_model=HintResponse,
        tags=["challenges"],
    )
    def get_hint(
        unique_code: str = Query(...),
        token: str = Depends(authenticated_token),
    ) -> dict:
        return challenges.hint(token, unique_code)

    @app.post(
        "/openapi/v1/challenges/submit",
        response_model=SubmitResponse,
        tags=["challenges"],
    )
    def submit_flag(
        submission: SubmitRequest,
        token: str = Depends(authenticated_token),
    ) -> dict:
        return challenges.submit(token, submission.unique_code, submission.flag)

    @app.post(
        "/openapi/v1/challenges/close",
        response_model=CloseResponse,
        tags=["challenges"],
    )
    def close_challenge(
        unique_code: str = Query(...),
        token: str = Depends(authenticated_token),
    ) -> dict:
        return challenges.close(token, unique_code)

    # ---- OpenVPN lifecycle(平台全局特权操作;仅 REDPILOT_ADMIN_TOKEN,参与方 token 不可达)----
    @app.get("/openapi/v1/vpn/status", tags=["vpn"])
    def vpn_status(_admin: str = Depends(authenticated_admin)) -> dict:
        return app.state.vpn.as_dict()

    @app.post("/openapi/v1/vpn/config", response_model=None, tags=["vpn"])
    def vpn_upload(payload: VPNConfigRequest, _admin: str = Depends(authenticated_admin)) -> dict:
        return app.state.vpn.as_dict(app.state.vpn.save_config(payload.content))

    @app.post("/openapi/v1/vpn/start", tags=["vpn"])
    def vpn_start(_admin: str = Depends(authenticated_admin)) -> dict:
        return app.state.vpn.as_dict(app.state.vpn.start())

    @app.post("/openapi/v1/vpn/stop", tags=["vpn"])
    def vpn_stop(_admin: str = Depends(authenticated_admin)) -> dict:
        return app.state.vpn.as_dict(app.state.vpn.stop())

    return app
