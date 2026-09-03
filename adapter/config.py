"""
配置管理模块
- SolverConfig:  Pi Agent 求解引擎的模型配置
- LLMConfig:     验证器 LLM 配置
- ControllerConfig: 控制器参数（调度、并发、止损等）

环境变量驱动，支持 deepseek / glm 预设及托管网关模式。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


# ── 模型预设 ──────────────────────────────────────────────────

_SOLVER_PRESETS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-v4-flash",
    },
    "deepseek-1m": {
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-v4-pro[1m]",
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "model": "glm-5.3",
    },
    "glm-1m": {
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "model": "glm-5.3",
    },
}


def _to_gateway(url: str) -> str:
    """将 API 域名转换为平台网关地址 (host -> host.tsecbench.gw, https -> http)"""
    if not url:
        return url
    if ".tsecbench.gw" in url:
        u = url
    else:
        m = url.split("://", 1)
        scheme, rest = (m[0], m[1]) if len(m) == 2 else ("https", url)
        host, _, path = rest.partition("/")
        u = f"{scheme}://{host}.tsecbench.gw" + (("/" + path) if path else "")
    return u.replace("https://", "http://", 1)


# ── Solver 配置 ─────────────────────────────────────────────

@dataclass
class SolverConfig:
    """Pi Agent 求解引擎配置"""
    provider: str
    base_url: str
    api_key: str
    model: str
    session_seconds: int

    @classmethod
    def from_env(cls) -> "SolverConfig":
        provider = (_env("SOLVER_PROVIDER") or _env("ADAPTER_PROVIDER", "deepseek") or "deepseek").lower()
        preset = _SOLVER_PRESETS.get(provider, _SOLVER_PRESETS["deepseek"])
        base = _env("SOLVER_BASE_URL", preset["base_url"]) or preset["base_url"]
        if _env("SOLVER_GATEWAY", "0") == "1":
            base = _to_gateway(base)
        key = (_env("SOLVER_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN")
               or _env("ANTHROPIC_API_KEY") or "")
        return cls(
            provider=provider,
            base_url=base.rstrip("/"),
            api_key=key,
            model=_env("SOLVER_MODEL", preset["model"]) or preset["model"],
            session_seconds=int(_env("SOLVER_SESSION_SECONDS", "1500") or "1500"),
        )


# ── LLM (验证器) 配置 ────────────────────────────────────────

_VERIFIER_PRESETS = {
    "deepseek": {"provider": "openai", "base_url": "https://api.deepseek.com",
                 "model": "deepseek-v4-flash"},
    "glm": {"provider": "zai", "base_url": "https://open.bigmodel.cn/api/paas/v4",
             "model": "glm-5.3"},
}


@dataclass
class LLMConfig:
    """验证器 / 通用 LLM 配置"""
    provider: str
    base_url: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int
    timeout: int
    min_interval: float = 0.0
    thinking: bool = True
    reasoning_effort: str = "high"
    empty_retries: int = 2

    def is_usable(self) -> bool:
        if self.provider in ("zai", "zhipu", "glm"):
            return bool(self.api_key)
        return bool(self.api_key and self.base_url)


def build_verifier_config(solver: SolverConfig) -> LLMConfig:
    """根据 solver 配置自动推导验证器配置"""
    family = "glm" if solver.provider.startswith("glm") else "deepseek"
    preset = _VERIFIER_PRESETS.get(family, _VERIFIER_PRESETS["deepseek"])
    provider = (_env("LLM_PROVIDER") or preset["provider"]).lower()
    base = _env("LLM_BASE_URL") or preset["base_url"]
    if _env("SOLVER_GATEWAY", "0") == "1":
        base = _to_gateway(base)
    return LLMConfig(
        provider=provider,
        base_url=(base or "").rstrip("/"),
        api_key=(_env("LLM_API_KEY") or solver.api_key or ""),
        model=_env("LLM_MODEL") or preset["model"],
        temperature=float(_env("LLM_TEMPERATURE", "0.3") or "0.3"),
        max_tokens=int(_env("LLM_MAX_TOKENS", "1024") or "1024"),
        timeout=int(_env("LLM_TIMEOUT", "120") or "120"),
        min_interval=float(_env("LLM_MIN_INTERVAL", "0") or "0"),
        thinking=(_env("LLM_THINKING", "0") == "1"),
        reasoning_effort=_env("LLM_REASONING_EFFORT", "low") or "low",
        empty_retries=int(_env("LLM_EMPTY_RETRIES", "2") or "2"),
    )


# ── 控制器配置 ─────────────────────────────────────────────

# 各难度单次会话时间盒（秒）— 简单/中等/困难解题耗时不同，分开配置
_DEFAULT_TIMEBOX = {"easy": 240, "medium": 480, "hard": 900}
# 轮次时间盒乘数（越靠后的轮次给越多时间）
_DEFAULT_ROUND_FACTORS = [1.0, 1.7, 3.0, 4.0]


@dataclass
class ControllerConfig:
    """调度与运行参数"""
    workdir: str
    max_concurrency: int
    best_of: int
    per_challenge_seconds: int
    max_sessions_per_challenge: int
    dry_facts_cutoff: int
    skeptic_votes: int
    min_request_interval: float
    total_seconds: int
    timebox_easy: int = 240
    timebox_medium: int = 480
    timebox_hard: int = 900
    round_factors: list = field(default_factory=lambda: list(_DEFAULT_ROUND_FACTORS))

    def timebox_for_difficulty(self, difficulty: str | None) -> int:
        """按难度返回基础时间盒（未知难度按 medium 处理）"""
        d = (difficulty or "").lower()
        if d == "easy":
            return self.timebox_easy
        if d == "hard":
            return self.timebox_hard
        return self.timebox_medium

    @classmethod
    def from_env(cls) -> "ControllerConfig":
        return cls(
            workdir=_env("ADAPTER_WORKDIR", "/work") or "/work",
            max_concurrency=max(1, int(_env("ADAPTER_MAX_CONCURRENCY", "3") or "3")),
            best_of=max(1, int(_env("ADAPTER_BEST_OF", "1") or "1")),
            per_challenge_seconds=int(_env("ADAPTER_PER_CHALLENGE_SECONDS", "4000") or "4000"),
            max_sessions_per_challenge=int(_env("ADAPTER_MAX_SESSIONS", "8") or "8"),
            dry_facts_cutoff=int(_env("ADAPTER_DRY_FACTS_CUTOFF", "3") or "3"),
            skeptic_votes=max(1, int(_env("SKEPTIC_VOTES", "1") or "1")),
            min_request_interval=float(_env("ADAPTER_MIN_REQUEST_INTERVAL", "0.4") or "0.4"),
            total_seconds=int(_env("ADAPTER_TOTAL_SECONDS", "21300") or "21300"),
            timebox_easy=int(_env("ADAPTER_TIMEBOX_EASY", str(_DEFAULT_TIMEBOX["easy"])) or _DEFAULT_TIMEBOX["easy"]),
            timebox_medium=int(_env("ADAPTER_TIMEBOX_MEDIUM", str(_DEFAULT_TIMEBOX["medium"])) or _DEFAULT_TIMEBOX["medium"]),
            timebox_hard=int(_env("ADAPTER_TIMEBOX_HARD", str(_DEFAULT_TIMEBOX["hard"])) or _DEFAULT_TIMEBOX["hard"]),
            round_factors=_parse_round_factors(_env("ADAPTER_ROUND_FACTORS", "")),
        )


def _parse_round_factors(raw: str) -> list:
    """解析轮次乘数列表，如 '1.0,1.7,3.0,4.0'；非法时用默认"""
    if not raw:
        return list(_DEFAULT_ROUND_FACTORS)
    try:
        vals = [float(x) for x in raw.split(",") if x.strip()]
        return vals or list(_DEFAULT_ROUND_FACTORS)
    except ValueError:
        return list(_DEFAULT_ROUND_FACTORS)
