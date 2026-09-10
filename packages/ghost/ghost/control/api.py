"""FastAPI application implementing the Ghost Challenges API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
import hmac
from pathlib import Path
from typing import Any, Literal

import httpx  # noqa: F401 (保留:历史导入路径兼容,实际投递见 outbox 模块)
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ghost.control.challenges import ChallengeFacade
from ghost.control.config import Settings
from ghost.control.control import ControlPlaneService
from ghost.control.errors import (APIError, admin_not_configured, admin_required,
                     assignment_not_found, evaluation_not_found,
                     lease_conflict, worker_required,
                     worker_token_not_configured)
from ghost.control.models import parse_task_config
from ghost.control.outbox import outbox_lifespan
from ghost.control.provisioner import ContainerProvisioner, provisioner_for
from ghost.control.scheduling import SchedulingFacade
from ghost.control.service import ChallengeService
from ghost.control.store import Store
from ghost.control.vpn import VPNManager


log = logging.getLogger("ghost.api")


# NOTE: canonical outbox 投递循环已抽到 ghost.outbox.dispatch_outbox_loop,
# 此处仅做 lifespan 装配(语义不变,见 outbox 模块文档)。


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


class EvaluationCreateRequest(BaseModel):
    task_token: str = Field(min_length=1, max_length=4096)
    project_id: str = Field(default="default", min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


class WorkerRegisterRequest(BaseModel):
    capabilities: dict[str, Any] = Field(default_factory=dict)


class WorkerHeartbeatRequest(BaseModel):
    status: Literal["offline", "idle", "busy", "draining"] = Field(default="idle")


class ClaimRequest(BaseModel):
    lease_seconds: int = Field(default=300, ge=30, le=3600)


class AttemptHeartbeatRequest(BaseModel):
    lease_seconds: int = Field(default=300, ge=30, le=3600)


class AttemptEventRequest(BaseModel):
    event_id: str | None = Field(default=None, min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=128)
    seq: int = Field(default=0, ge=0)
    occurred_at: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AttemptEventsRequest(BaseModel):
    lease_id: str = Field(min_length=1, max_length=128)
    events: list[AttemptEventRequest] = Field(default_factory=list, max_length=500)


class AttemptCompleteRequest(BaseModel):
    lease_id: str = Field(min_length=1, max_length=128)
    status: Literal["solved", "done", "failed", "interrupted"] = Field()
    solved: bool = False
    flags_found: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=2000)


class AttemptHeartbeatRouteRequest(AttemptHeartbeatRequest):
    lease_id: str = Field(min_length=1, max_length=128)


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

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 启停语义单源在 outbox_lifespan(统一 app 与本工厂共用,防装配路径漂移)。
        async with outbox_lifespan(
            store, settings.observability_url, settings.observability_token
        ):
            yield
    app = FastAPI(title="Ghost Platform", version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.service = service
    control = ControlPlaneService(store, public_base_url=settings.public_base_url)
    app.state.control = control
    # 内部边界:challenges(业务) vs scheduling(调度)外观;路由组只经各自外观调用,
    # 不再直调 Store/Service(见 challenges.py/scheduling.py/store_facets.py)。
    challenges = ChallengeFacade(service)
    scheduling = SchedulingFacade(store, control)
    app.state.challenges = challenges
    app.state.scheduling = scheduling
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

    def authenticated_admin(admin_token: str | None = Header(default=None, alias="GHOST_ADMIN_TOKEN")) -> None:
        """管理端点凭据(openvpn 生命周期等平台全局特权操作)。

        与参与方任务 token 严格分离:未配置 GHOST_ADMIN_TOKEN → 503 fail closed,
        缺失/不符 → 401(常量时间比较)。任一任务 token 都不可触达这些端点。
        """
        expected = settings.admin_token
        if not expected:
            raise admin_not_configured()
        if admin_token is None or not hmac.compare_digest(admin_token.encode(), expected.encode()):
            raise admin_required()

    def authenticated_worker(worker_token: str | None = Header(default=None, alias="X-Worker-Token")) -> None:
        expected = settings.worker_token
        if not expected:
            raise worker_token_not_configured()
        if worker_token is None or not hmac.compare_digest(worker_token.encode(), expected.encode()):
            raise worker_required()

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

    # ---- 控制面 API: evaluation/job/attempt/worker -----------------
    @app.post("/api/v1/evaluations", tags=["control-plane"])
    def create_evaluation(
        payload: EvaluationCreateRequest,
        _admin: None = Depends(authenticated_admin),
    ) -> dict:
        try:
            return scheduling.create_evaluation(
                payload.task_token,
                project_id=payload.project_id,
                idempotency_key=payload.idempotency_key,
            )
        except KeyError as exc:
            if exc.args and exc.args[0] == "task_not_found":
                raise APIError(404, "task_not_found", "Task not found") from exc
            raise
        except ValueError as exc:
            if str(exc) == "task_not_active":
                raise APIError(409, "invalid_state", "Task is no longer active") from exc
            if str(exc) == "idempotency_key_reuse":
                raise APIError(409, "idempotency_key_reuse", "Idempotency key already used for another task") from exc
            raise

    @app.get("/api/v1/evaluations", tags=["control-plane"])
    def list_evaluations(
        project_id: str | None = Query(default=None),
        _admin: None = Depends(authenticated_admin),
    ) -> dict:
        return {"evaluations": scheduling.list_evaluations(project_id)}

    @app.get("/api/v1/evaluations/{evaluation_id}", tags=["control-plane"])
    def get_evaluation(
        evaluation_id: str,
        _admin: None = Depends(authenticated_admin),
    ) -> dict:
        row = scheduling.get_evaluation(evaluation_id)
        if row is None:
            raise evaluation_not_found()
        return row

    @app.post("/api/v1/evaluations/{evaluation_id}/cancel", tags=["control-plane"])
    def cancel_evaluation(
        evaluation_id: str,
        _admin: None = Depends(authenticated_admin),
    ) -> dict:
        row = scheduling.cancel_evaluation(evaluation_id)
        if row is None:
            raise evaluation_not_found()
        return row

    @app.get("/api/v1/workers", tags=["control-plane"])
    def list_workers(_admin: None = Depends(authenticated_admin)) -> dict:
        return {"workers": scheduling.list_workers()}

    @app.post("/api/v1/workers/{worker_id}/register", tags=["worker"])
    def register_worker(
        worker_id: str,
        payload: WorkerRegisterRequest,
        _auth: None = Depends(authenticated_worker),
    ) -> dict:
        return scheduling.register_worker(worker_id, payload.capabilities)

    @app.post("/api/v1/workers/{worker_id}/heartbeat", tags=["worker"])
    def worker_heartbeat(
        worker_id: str,
        payload: WorkerHeartbeatRequest,
        _auth: None = Depends(authenticated_worker),
    ) -> dict:
        if not scheduling.worker_heartbeat(worker_id, payload.status):
            raise APIError(404, "worker_not_found", "Worker not registered")
        return {"ok": True, "worker_id": worker_id, "status": payload.status}

    @app.post("/api/v1/workers/{worker_id}/claim", response_model=None, tags=["worker"])
    def claim_job(
        worker_id: str,
        payload: ClaimRequest,
        _auth: None = Depends(authenticated_worker),
    ) -> dict | Response:
        try:
            assignment = scheduling.claim(worker_id, payload.lease_seconds)
        except KeyError as exc:
            if exc.args and exc.args[0] == "worker_not_registered":
                raise APIError(404, "worker_not_registered", "Worker not registered") from exc
            raise
        if assignment is None:
            raise assignment_not_found()
        return {"assignment": assignment}

    @app.post("/api/v1/attempts/{attempt_id}/heartbeat", tags=["worker"])
    def attempt_heartbeat(
        attempt_id: str,
        payload: AttemptHeartbeatRouteRequest,
        request: Request,
        _auth: None = Depends(authenticated_worker),
    ) -> dict:
        ok = scheduling.heartbeat_assignment(
            attempt_id,
            request.headers.get("X-Worker-Id", ""),
            payload.lease_id,
            payload.lease_seconds,
        )
        if not ok:
            raise lease_conflict()
        return {"ok": True, "attempt_id": attempt_id}

    @app.post("/api/v1/attempts/{attempt_id}/events", tags=["worker"])
    def append_attempt_events(
        attempt_id: str,
        payload: AttemptEventsRequest,
        request: Request,
        _auth: None = Depends(authenticated_worker),
    ) -> dict:
        # lease 与 heartbeat/complete 一致走 JSON body:Query 会进访问日志,
        # bearer 不应出现在 URL 里。
        worker_id = request.headers.get("X-Worker-Id", "")
        try:
            inserted = scheduling.append_events(
                attempt_id,
                worker_id,
                payload.lease_id,
                [event.model_dump() if hasattr(event, "model_dump") else event.dict() for event in payload.events],
            )
        except KeyError as exc:
            raise APIError(404, "attempt_not_found", "Attempt not found") from exc
        except PermissionError as exc:
            log.warning("attempt %s lease mismatch (worker=%s)", attempt_id, worker_id)
            raise lease_conflict() from exc
        except (TypeError, ValueError) as exc:
            raise APIError(422, "invalid_event", str(exc)) from exc
        return {"ok": True, "inserted": inserted}

    @app.post("/api/v1/attempts/{attempt_id}/complete", tags=["worker"])
    def complete_attempt(
        attempt_id: str,
        payload: AttemptCompleteRequest,
        request: Request,
        _auth: None = Depends(authenticated_worker),
    ) -> dict:
        worker_id = request.headers.get("X-Worker-Id", "")
        try:
            return scheduling.complete_attempt(
                attempt_id,
                worker_id,
                payload.lease_id,
                status=payload.status,
                solved=payload.solved,
                flags_found=payload.flags_found,
                error=payload.error,
            )
        except KeyError as exc:
            raise APIError(404, "attempt_not_found", "Attempt not found") from exc
        except PermissionError as exc:
            raise lease_conflict() from exc
        except ValueError as exc:
            raise APIError(422, "invalid_attempt", str(exc)) from exc

    @app.get("/api/v1/attempts/{attempt_id}/events", tags=["control-plane"])
    def attempt_events(
        attempt_id: str,
        _admin: None = Depends(authenticated_admin),
    ) -> dict:
        return {"events": scheduling.attempt_events(attempt_id)}

    # ---- OpenVPN lifecycle(平台全局特权操作;仅 GHOST_ADMIN_TOKEN,参与方 token 不可达)----
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
