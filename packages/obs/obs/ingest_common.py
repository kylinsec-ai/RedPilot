"""ingest 共享:token 鉴权 + store 取用(telemetry/canonical 共用,不再各自复制)。"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from .store import ObsStore

HEADER = "X-Observability-Token"


def check_token(request: Request) -> None:
    token = getattr(request.app.state, "obs_token", None)
    if not token:
        raise HTTPException(503, "observability token not configured")
    got = request.headers.get(HEADER)
    if got is None or not hmac.compare_digest(got.encode(), token.encode()):
        raise HTTPException(401, "bad or missing token")


def require_store(request: Request) -> ObsStore:
    return request.app.state.store
