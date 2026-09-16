"""
通用 OpenAPI 平台适配器

不绑定任何具体平台。通过 JSON spec（文件路径或内联 dict）描述平台差异：
- 认证方式（header / bearer / query）
- 各接口的路径模板与参数位置（query / body）
- 业务错误码 → HTTP 状态码映射
- 响应字段（支持嵌套取值的点路径）
- VPN 预检地址（可选）

spec 默认结构对齐 TSecBench（CHALLENGES_API.md），其他平台只需覆盖差异项。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import requests

from .base import (
    APIError, Challenge, ChallengeNotFound, CloseResult, DuplicateSubmit,
    HintResult, InvalidState, PlatformBackend, ResourceUnavailable,
    StartResult, SubmitResult, TaskNotFound, VpnCheckError, VpnCheckResult,
    _parse_files, _parse_tags,
)

log = logging.getLogger("adapter.platform.generic")


# ── 默认 spec：对齐 TSecBench 平台 ───────────────────────────

DEFAULT_SPEC = {
    "name": "generic-openapi",
    "auth": {
        "mode": "header",            # header | bearer | query
        "header_name": "BENCHMARK_TOKEN",
        "query_param": "token",
    },
    "paths": {
        "list":   {"method": "GET",  "path": "/openapi/v1/challenges"},
        "start":  {"method": "POST", "path": "/openapi/v1/challenges/start",  "param": "query"},
        "hint":   {"method": "GET",  "path": "/openapi/v1/challenges/hint",   "param": "query"},
        "submit": {"method": "POST", "path": "/openapi/v1/challenges/submit", "param": "body"},
        "close":  {"method": "POST", "path": "/openapi/v1/challenges/close",  "param": "query"},
    },
    "list": {
        "wrap": "",                    # 响应包裹字段（空 = 直接数组；如 "data" 则取 data）
        "fields": {                    # 平台字段 → 统一字段的点路径
            "unique_code": "unique_code",
            "description": "description",
            "difficulty": "difficulty",
            "level": "level",
            "total_score": "total_score",
            "flag_count": "flag_count",
            "correct_flag_count": "correct_flag_count",
            "is_completed": "is_completed",
            "container_status": "container_status",
            "container_addr": "container_addr",
            # Optional metadata only. Platforms using another field name can
            # map it explicitly through their JSON spec.
            "files": "files",
        },
    },
    "submit_fields": {                 # submit 请求体字段名
        "unique_code": "unique_code",
        "flag": "flag",
    },
    "errors": {                        # 业务错误码 → 统一异常类
        "task_not_found": "task_not_found",
        "challenge_not_found": "challenge_not_found",
        "invalid_state": "invalid_state",
        "duplicate": "duplicate",
        "resource_unavailable": "resource_unavailable",
    },
    "vpn_check_url": "",               # 留空 = 不强制 VPN 预检
    "id_param": "unique_code",         # start/hint/close 的标识参数名
}


def _resolve(data: dict, path: str, default: Any = None) -> Any:
    """按点路径取值，如 "container.addr[0]"，支持简单下标"""
    cur: Any = data
    for part in path.split("."):
        if cur is None:
            return default
        if "[" in part:
            key, _, idx = part.partition("[")
            idx = idx.rstrip("]")
            if isinstance(cur, dict) and key:
                cur = cur.get(key, {})
            if isinstance(cur, list) and idx.isdigit() and int(idx) < len(cur):
                cur = cur[int(idx)]
            else:
                return default
        elif isinstance(cur, dict):
            cur = cur.get(part, default)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
    return cur


class GenericOpenAPIBackend(PlatformBackend):
    """
    通用 OpenAPI 平台适配器。
    spec 来源优先级: 构造参数 > PLATFORM_SPEC_FILE 环境变量 > 默认 tsecbench 结构。
    """

    name = "generic-openapi"

    _EXC_MAP = {
        "task_not_found": TaskNotFound,
        "challenge_not_found": ChallengeNotFound,
        "invalid_state": InvalidState,
        "duplicate": DuplicateSubmit,
        "resource_unavailable": ResourceUnavailable,
    }

    def __init__(self, base_url: str, token: str, *,
                 timeout: int = 30, spec: Optional[dict] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

        # spec 加载: 参数 > 环境变量文件 > 默认
        if spec is None:
            spec_file = os.environ.get("PLATFORM_SPEC_FILE", "")
            if spec_file and os.path.isfile(spec_file):
                with open(spec_file, encoding="utf-8") as f:
                    spec = json.load(f)
        self.spec = self._merge(DEFAULT_SPEC, spec or {})

        self.auth = self.spec.get("auth", DEFAULT_SPEC["auth"])
        self._session = requests.Session()
        if self.auth.get("mode") == "bearer":
            self._session.headers.update({"Authorization": f"Bearer {token}"})
        elif self.auth.get("mode", "header") == "header":
            name = self.auth.get("header_name", "BENCHMARK_TOKEN")
            self._session.headers.update({name: token})

    @staticmethod
    def _merge(base: dict, override: dict) -> dict:
        """深合并：override 覆盖 base"""
        out = dict(base)
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = GenericOpenAPIBackend._merge(out[k], v)
            else:
                out[k] = v
        return out

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _auth_kwargs(self) -> dict:
        """非 header 模式时附加认证参数"""
        if self.auth.get("mode") == "query":
            return {"params": {self.auth.get("query_param", "token"): self.token}}
        return {}

    def _handle_error(self, resp: requests.Response):
        if resp.status_code < 400:
            return
        try:
            body = resp.json()
        except Exception:
            resp.raise_for_status()
            return
        code = body.get("code", "unknown")
        message = body.get("message", resp.text[:200])
        detail = body.get("detail") or {}
        status = resp.status_code

        mapped = self.spec.get("errors", {}).get(code)
        exc_cls = self._EXC_MAP.get(mapped, APIError)
        raise exc_cls(code=code, message=message, status=status, detail=detail)

    def _call(self, op: str, id_value: Optional[str] = None,
              body: Optional[dict] = None) -> requests.Response:
        p = self.spec["paths"][op]
        method = p.get("method", "GET").upper()
        url = self._url(p["path"])
        params = dict(self._auth_kwargs().get("params", {}))
        if id_value is not None and p.get("param") == "query":
            params[self.spec.get("id_param", "unique_code")] = id_value
        if body and p.get("param") == "body":
            return self._session.request(method, url, json=body,
                                         params=params or None, timeout=self.timeout)
        return self._session.request(method, url, params=params or None,
                                     timeout=self.timeout)

    # ── 接口实现 ──

    def list_challenges(self) -> list[Challenge]:
        resp = self._call("list")
        self._handle_error(resp)
        data = resp.json()
        wrap = self.spec.get("list", {}).get("wrap", "")
        items = _resolve(data, wrap) if wrap else data
        if not isinstance(items, list):
            items = [items]
        fields = self.spec.get("list", {}).get("fields", {})
        out = []
        for c in items:
            if not isinstance(c, dict):
                continue
            out.append(Challenge(
                unique_code=str(_resolve(c, fields.get("unique_code", "unique_code"), "")),
                description=_resolve(c, fields.get("description", "description"), "") or "",
                difficulty=_resolve(c, fields.get("difficulty", "difficulty"), "") or "",
                level=int(_resolve(c, fields.get("level", "level"), 1) or 1),
                total_score=int(_resolve(c, fields.get("total_score", "total_score"), 0) or 0),
                flag_count=int(_resolve(c, fields.get("flag_count", "flag_count"), 1) or 1),
                correct_flag_count=int(_resolve(c, fields.get("correct_flag_count", "correct_flag_count"), 0) or 0),
                is_completed=bool(_resolve(c, fields.get("is_completed", "is_completed"), False)),
                container_status=_resolve(c, fields.get("container_status", "container_status"), "stopped") or "stopped",
                container_addr=list(_resolve(c, fields.get("container_addr", "container_addr"), []) or []),
                category=_resolve(c, fields.get("category", "category"), "")
                         or _resolve(c, "type", "") or _resolve(c, "challenge_type", ""),
                tags=_parse_tags(_resolve(c, fields.get("tags", "tags"), [])),
                files=_parse_files(_resolve(c, fields.get("files", "files"), [])),
            ))
        return out

    def start_challenge(self, unique_code: str) -> StartResult:
        resp = self._call("start", id_value=unique_code)
        self._handle_error(resp)
        d = resp.json()
        return StartResult(
            unique_code=d.get(self.spec.get("id_param", "unique_code"), unique_code),
            container_addr=d.get("container_addr") or [],
        )

    def get_hint(self, unique_code: str) -> HintResult:
        resp = self._call("hint", id_value=unique_code)
        self._handle_error(resp)
        d = resp.json()
        return HintResult(
            unique_code=d.get(self.spec.get("id_param", "unique_code"), unique_code),
            hint=d.get("hint"),
        )

    def submit_flag(self, unique_code: str, flag: str) -> SubmitResult:
        sf = self.spec.get("submit_fields", {})
        body = {
            sf.get("unique_code", "unique_code"): unique_code,
            sf.get("flag", "flag"): flag,
        }
        resp = self._call("submit", body=body)
        if resp.status_code == 409:
            try:
                if resp.json().get("code") == "duplicate":
                    return SubmitResult(correct=False, duplicate=True, error="duplicate")
            except Exception:
                pass
        self._handle_error(resp)
        d = resp.json()
        return SubmitResult(
            correct=d.get("correct", False),
            awarded=int(d.get("awarded", 0) or 0),
            cumulative_score=int(d.get("cumulative_score", 0) or 0),
            correct_flag_count=int(d.get("correct_flag_count", 0) or 0),
            total_flag_count=int(d.get("total_flag_count", 0) or 0),
            matched_flag_index=d.get("matched_flag_index"),
        )

    def close_challenge(self, unique_code: str) -> CloseResult:
        resp = self._call("close", id_value=unique_code)
        if resp.status_code in (409, 404):
            return CloseResult(unique_code=unique_code, closed=False)
        self._handle_error(resp)
        d = resp.json()
        return CloseResult(
            unique_code=d.get(self.spec.get("id_param", "unique_code"), unique_code),
            closed=d.get("closed", True),
        )

    def check_vpn(self, *, timeout: float = 10.0) -> VpnCheckResult:
        url = self.spec.get("vpn_check_url", "")
        if not url:
            return VpnCheckResult(status="ok", ok=True)
        try:
            resp = requests.get(url, timeout=timeout)
            body = resp.json()
        except requests.RequestException:
            raise VpnCheckError(reason="network_error") from None
        except ValueError:
            raise VpnCheckError(reason="bad_body") from None
        if resp.status_code != 200 or body.get("status") != "ok":
            raise VpnCheckError(reason="status_not_ok")
        return VpnCheckResult(
            status=body.get("status", "ok"),
            client_ip=body.get("client_ip", ""),
            time=body.get("time", ""),
            ok=True,
        )
