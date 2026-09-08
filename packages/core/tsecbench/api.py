"""FastAPI application implementing the TSecBench Challenges API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
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

    app = FastAPI(title="TSecBench Platform", version="1.0.0")
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

    return app
