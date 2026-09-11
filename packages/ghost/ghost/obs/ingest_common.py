"""ingest 共享:token 鉴权 + store 取用(telemetry/canonical 共用,不再各自复制)。

另含读端鉴权 `check_read_token` —— 读端与写端共用同一头名(都是"观测凭据"),
但**取值不同**:读端用 `read_token`(未单独配置时回落 ingest token),写端只认
`obs_token`。这样"能写遥测"不等于"能读明文 flag 与完整实录"。
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from ghost.obs.store import ObsStore

HEADER = "X-Observability-Token"


def _compare(got: str | None, expected: str) -> bool:
    """常量时间比较(两侧都可能来自 env,长度不固定 —— 仍用 compare_digest)。"""
    return got is not None and hmac.compare_digest(got.encode(), expected.encode())


def check_token(request: Request) -> None:
    """写端(ingest)鉴权:未配置 → 503;不匹配 → 401。"""

    token = getattr(request.app.state, "obs_token", None)
    if not token:
        raise HTTPException(503, "observability token not configured")
    if not _compare(request.headers.get(HEADER), token):
        raise HTTPException(401, "bad or missing token")


def check_read_token(request: Request) -> None:
    """读端鉴权。

    读端返回**明文 flag 与完整 agent 实录**(评测答案材料),而 worker 容器持 ingest
    token 且与平台同网 —— 故读端必须自带凭据,不能靠"网络位置"兜底。

    未配置任何读凭据时 fail-closed(503),与 ingest 同款语义:宁可响亮点坏,
    也不要静默地把答案开放出去。
    """

    token = getattr(request.app.state, "read_token", None)
    if not token:
        raise HTTPException(503, "observability read token not configured")
    if not _compare(request.headers.get(HEADER), token):
        raise HTTPException(401, "bad or missing read token")


def require_store(request: Request) -> ObsStore:
    return request.app.state.store
