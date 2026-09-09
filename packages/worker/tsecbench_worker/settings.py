"""worker 进程配置 — 散落 getenv 的单一收编点。

env 变量名全部不变(compose 兼容是硬约束);整合的是"读取点"而非命名。
此前 driver/relay/roster/status_server 各自 os.getenv 共 10+ 处;
现统一 WorkerSettings.from_env() 一处读取,消费方经构造参数接收。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger("tsecbench_worker.settings")


def _parse_status_port(raw: str | None = None) -> int:
    """STATUS_PORT 安全解析:import 期绝不抛;空串=禁用(0),垃圾值回退 8080 并告警"""
    raw = raw if raw is not None else os.getenv("STATUS_PORT", "8080")
    text = (raw or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        log.warning("bad STATUS_PORT=%r, falling back to 8080", raw)
        return 8080


def _status_bind() -> str:
    """STATUS_BIND:默认 127.0.0.1(状态服务数据无鉴权,默认不对局域网开放)"""
    bind = (os.getenv("STATUS_BIND") or "").strip() or "127.0.0.1"
    if bind.startswith("-"):
        log.warning("bad STATUS_BIND=%r, falling back to 127.0.0.1", bind)
        return "127.0.0.1"
    return bind


@dataclass
class WorkerSettings:
    """worker 进程装配配置(一次性 from_env,消费方不再各自 getenv)"""

    benchmark_base_url: str = ""
    benchmark_token: str = ""
    workdir: str = "/work"
    worker_id: str = "worker-1"
    status_port: int = 8080
    # 状态服务监听地址:默认仅回环(数据无鉴权:实时 FLAG/完整实录,不应对局域网开放);
    # compose 内由编排显式置 0.0.0.0 —— docker-proxy 转发需容器全网卡监听,宿主侧再收成回环
    status_bind: str = "127.0.0.1"
    observability_url: str = ""
    observability_token: str = ""
    flag_format: str = "flag{...}"   # 只渲染进任务 prompt(提取正则硬编码 flag{...})

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        return cls(
            benchmark_base_url=os.getenv("BENCHMARK_BASE_URL", "").strip(),
            benchmark_token=os.getenv("BENCHMARK_TOKEN", "").strip(),
            workdir=os.getenv("ADAPTER_WORKDIR", "/work").strip() or "/work",
            worker_id=(os.getenv("WORKER_ID", "worker-1").strip() or "worker-1"),
            status_port=_parse_status_port(),
            status_bind=_status_bind(),
            observability_url=os.getenv("OBSERVABILITY_URL", "").strip().rstrip("/"),
            observability_token=os.getenv("OBSERVABILITY_TOKEN", "").strip(),
            flag_format=os.getenv("ADAPTER_FLAG_FORMAT", "flag{...}") or "flag{...}",
        )
