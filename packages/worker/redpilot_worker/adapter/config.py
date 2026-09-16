"""
配置管理模块
- SolverConfig:  Pi Agent 求解引擎的模型配置
- LLMConfig:     验证器 LLM 配置
- ControllerConfig: 控制器参数（调度、并发、止损等）

环境变量驱动，支持 deepseek / glm 预设及托管网关模式。
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


# ── 模型预设 ──────────────────────────────────────────────────

_SOLVER_PRESETS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        # 默认求解模型（2026-09 起）：MiMo V2.5。
        # 容器内 provider 名是网关的 "deepseek"（见 _write_pi_models），
        # 故完整形式为 deepseek/mimo-v2.5；model id 由 strip_provider 后透传。
        "model": "mimo-v2.5",
        "small_fast_model": "deepseek-v4-flash",
    },
    "deepseek-1m": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-pro[1m]",
        "small_fast_model": "deepseek-v4-flash",
        "subagent_model": "deepseek-v4-flash",
        "effort_level": "max",
        "auto_compact_window": "786432",
        "api_timeout_ms": "3000000",
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.3",
        "small_fast_model": "glm-5.3",
    },
    "glm-1m": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-5.3",
        "small_fast_model": "glm-5.3",
        "auto_compact_window": "1000000",
        "api_timeout_ms": "3000000",
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
    small_fast_model: str
    max_turns: int
    session_seconds: int
    reasoning: bool
    subagent_model: str = ""
    effort_level: str = ""
    auto_compact_window: str = ""
    api_timeout_ms: str = ""

    @classmethod
    def from_env(cls) -> "SolverConfig":
        provider = (_env("SOLVER_PROVIDER") or _env("ADAPTER_PROVIDER", "deepseek") or "deepseek").lower()
        preset = _SOLVER_PRESETS.get(provider, _SOLVER_PRESETS["deepseek"])
        # 网关优先：网页 Agent 舰队页配置的 ANTHROPIC_* 覆盖预设
        base = (_env("ANTHROPIC_BASE_URL")
                or _env("SOLVER_BASE_URL", preset["base_url"]) or preset["base_url"])
        if _env("SOLVER_GATEWAY", "0") == "1":
            base = _to_gateway(base)
        # 网关 key 优先：配置了 ANTHROPIC_AUTH_TOKEN 时用网关 key，
        # SOLVER_API_KEY 仅在未配置 ANTHROPIC 时作为兜底
        key = (_env("ANTHROPIC_AUTH_TOKEN")
               or _env("ANTHROPIC_API_KEY")
               or _env("SOLVER_API_KEY") or "")
        model = (_env("ANTHROPIC_MODEL")
                 or _env("SOLVER_MODEL", preset["model"]) or preset["model"])
        check_model_expiry(model)
        return cls(
            provider=provider,
            base_url=base.rstrip("/"),
            api_key=key,
            model=model,
            small_fast_model=_env("SOLVER_SMALL_FAST_MODEL", preset["small_fast_model"]) or preset["small_fast_model"],
            max_turns=int(_env("SOLVER_MAX_TURNS", "60") or "60"),
            session_seconds=int(_env("SOLVER_SESSION_SECONDS", "1500") or "1500"),
            reasoning=(_env("SOLVER_REASONING", "0") == "1"),
            subagent_model=_env("SOLVER_SUBAGENT_MODEL", preset.get("subagent_model", "")) or "",
            effort_level=_env("SOLVER_EFFORT", preset.get("effort_level", "")) or "",
            auto_compact_window=_env("SOLVER_AUTO_COMPACT_WINDOW", preset.get("auto_compact_window", "")) or "",
            api_timeout_ms=_env("SOLVER_API_TIMEOUT_MS", preset.get("api_timeout_ms", "")) or "",
        )



# ── 模型到期检查 ──────────────────────────────────────────────
# 模型名里带 expires-on-MMDD（如 deepseek-v4.1-flash-expires-on-0910）。
# 平台会按日轮换模型，到期后调用全部失败，而 fleet 处于 await-task 轮询时
# 没有会话在跑、不会报错 —— 必须在启动时把它喊出来。
_MODEL_EXPIRY_RX = re.compile(r"expires?-on-(\d{2})(\d{2})", re.I)


def check_model_expiry(model: str) -> None:
    """模型名含 expires-on-MMDD 时按剩余天数告警；无该后缀则静默。"""
    m = _MODEL_EXPIRY_RX.search(model or "")
    if not m:
        return
    today = datetime.date.today()
    try:
        exp = datetime.date(today.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return
    # B31：模型名只带 MMDD，不带年份。12 月看到 01 月、1 月看到 12 月会解析成
    # 同年 → 得到 ±300 多天的假差值（12 月底把"次年 1 月 5 日"报成"今年已过期"，
    # 触发一次没必要的换模型）。按最近的一个该月日来定年份。
    if (exp - today).days < -180:
        exp = datetime.date(today.year + 1, exp.month, exp.day)
    elif (exp - today).days > 180:
        exp = datetime.date(today.year - 1, exp.month, exp.day)
    days = (exp - today).days
    if days > 0:
        if days <= 3:
            log.warning("模型 %s 还有 %d 天到期（%s）—— 请提前在 :8003 控制台更换",
                        model, days, exp.isoformat())
        return
    # B39：日期过了**不等于**模型会失效 —— 名字里的 expires-on-MMDD 只是平台命名
    # 约定，不构成硬约束（实测 ...expires-on-0910 在"到期日"当天仍正常服务，用户
    # 已确认不会到期）。原实现在这里打 ERROR「请立刻换模型；否则所有会话都会失败」，
    # 那是一句**事实错误**的断言，而且每启动一次喊一次、永久刷屏 —— 真出问题时
    # 会被它淹没。日期已过却没有会话失败，恰恰说明该后缀不生效，降为 INFO 陈述
    # 事实即可；模型真失效的话，会话本身会大声报错，不需要这里替它喊。
    log.info("模型 %s 名字里的到期日 %s 已过 %d 天；该后缀非硬约束，"
             "若会话正常则无需处理", model, exp.isoformat(), -days)


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
    fast_model: str = ""
    # [B47c] 判断 Agent 专用（唯一消费者是 benchmark_driver 的 verifier）。
    # 2→4：实测模型会连续返回空响应，三次全空时判断 Agent 无意见、闸门静默消失。
    empty_retries: int = 4
    max_tokens_fast: int = 3072

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
        # [B53] 1024→4096。判断 Agent 目前显式传 4096，这一行是**兜底**：
        # `_max = max_tokens or self.cfg.max_tokens`，哪天显式参数被去掉，
        # 1024 会把「思维链吃光预算→正文永远为空」原样复现。
        max_tokens=int(_env("LLM_MAX_TOKENS", "4096") or "4096"),
        timeout=int(_env("LLM_TIMEOUT", "120") or "120"),
        min_interval=float(_env("LLM_MIN_INTERVAL", "0") or "0"),
        thinking=(_env("LLM_THINKING", "0") == "1"),
        reasoning_effort=_env("LLM_REASONING_EFFORT", "low") or "low",
        max_tokens_fast=int(_env("LLM_MAX_TOKENS_FAST", "1024") or "1024"),
        # [B47c] 2→4（=最多 5 次尝试）。超时是套在一次 chat() 外层的 30s join，
        # 实测每次约 3.2s，5 次最坏约 16s 仍在预算内；再大就有被 join 熔断
        # 吃掉的风险，反而连已拿到的裁决都丢。
        empty_retries=int(_env("LLM_EMPTY_RETRIES", "4") or "4"),
        fast_model=_env("LLM_FAST_MODEL", preset["model"]) or preset["model"],
    )


# ── 控制器配置 ─────────────────────────────────────────────

# 各难度单次会话时间盒（秒）— 简单/中等/困难解题耗时不同，分开配置
_DEFAULT_TIMEBOX = {"easy": 3600, "medium": 3600, "hard": 3600}
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
    use_hints: bool
    skeptic_votes: int
    min_request_interval: float
    round_timeboxes: list
    total_seconds: int
    secs_per_turn: float
    keepalive_max: int
    platform_mode: str = "tsecbench-http"   # 平台接入模式: tsecbench-http / tsecbench-sdk / generic
    timebox_easy: int = 3600  # 1小时
    timebox_medium: int = 3600  # 1小时
    timebox_hard: int = 3600  # 1小时
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
        rounds_raw = (_env("ADAPTER_ROUND_TIMEBOXES", "480,820,1500,2000")
                      or "480,820,1500,2000")
        timeboxes = [int(x) for x in rounds_raw.split(",")
                     if x.strip().isdigit()] or [480, 820, 1500, 2000]
        return cls(
            workdir=_env("ADAPTER_WORKDIR", "/work") or "/work",
            max_concurrency=max(1, int(_env("ADAPTER_MAX_CONCURRENCY", "3") or "3")),
            best_of=max(1, int(_env("ADAPTER_BEST_OF", "1") or "1")),
            per_challenge_seconds=int(_env("ADAPTER_PER_CHALLENGE_SECONDS", "4000") or "4000"),
            max_sessions_per_challenge=int(_env("ADAPTER_MAX_SESSIONS", "8") or "8"),
            dry_facts_cutoff=int(_env("ADAPTER_DRY_FACTS_CUTOFF", "3") or "3"),
            use_hints=(_env("ADAPTER_USE_HINTS", "0") == "1"),
            skeptic_votes=max(1, int(_env("SKEPTIC_VOTES", "1") or "1")),
            min_request_interval=float(_env("ADAPTER_MIN_REQUEST_INTERVAL", "0.4") or "0.4"),
            round_timeboxes=timeboxes,
            total_seconds=int(_env("ADAPTER_TOTAL_SECONDS", "21300") or "21300"),
            secs_per_turn=float(_env("ADAPTER_SECS_PER_TURN", "5") or "5"),
            keepalive_max=max(0, int(_env("ADAPTER_KEEPALIVE_MAX", "2") or "2")),
            platform_mode=_env("ADAPTER_PLATFORM", "tsecbench-http") or "tsecbench-http",
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
