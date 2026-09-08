"""provider 失败护栏测试:0-turn+报错的会话不再被当作"正常完成"。

2026-09-08 事故回归(280 run/0 flag/63 题静默烧库):pi 对 provider 400 只发
stopReason=error 的收尾消息,pi_agent 漏读 → err=none → 编排层当完成处理。
本文件覆盖:pi_agent 读出 stopReason=error(SolveResult.provider_failure)、
solve_one 会话级重试与 ProviderFailure 上抛、driver 连续熔断 exit 3。

pytest 由仓库根起(packages/worker 在 pythonpath,tsecbench_worker 可导入)。
"""

from __future__ import annotations

import json

import pytest

from tsecbench_worker.orchestration import (
    PROVIDER_FAILURE_RETRIES,
    ProviderFailure,
    LiveReporter,
    solve_one,
)
from tsecbench_worker.solver.base import SolveResult


# ── SolveResult.provider_failure 判定 ──

def test_provider_failure_zero_turns_with_error():
    r = SolveResult(turns=0, error="400 Error from provider (Console Go): MissingSessionID")
    assert r.provider_failure


def test_zero_turns_without_error_is_not_provider_failure():
    # 无报错的 0-turn 会话(如空响应)不触发熔断语义
    assert not SolveResult(turns=0).provider_failure


def test_turns_with_error_is_not_provider_failure():
    # 真实干过活的会话(哪怕带错误)不算 provider 失败
    assert not SolveResult(turns=3, error="timeout").provider_failure


# ── pi_agent: stopReason=error 读出(用假 pi 可执行脚本驱动真 solve 循环) ──

_AGENT_END_ERROR_EVENT = json.dumps({
    "type": "agent_end",
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "task"}]},
        {"role": "assistant", "content": [], "stopReason": "error",
         "errorMessage": "400 Error from provider (Console Go): MissingSessionID"},
    ],
})


def test_pi_agent_surfaces_stop_reason_error(tmp_path, monkeypatch):
    """假 pi 只发 agent_end(stopReason=error):solve 必须带出错误而非 err=none"""
    fake = tmp_path / "fakepi.sh"
    fake.write_text(f'#!/bin/sh\necho {_json_quote(_AGENT_END_ERROR_EVENT)}\n')
    fake.chmod(0o755)
    from tsecbench_worker.solver.pi_agent import PiAgentBackend
    from tsecbench_worker.config import SolverConfig

    workdir = tmp_path / "wd"
    workdir.mkdir()
    monkeypatch.setattr("shutil.which", lambda cmd: str(fake))
    backend = PiAgentBackend(cmd=str(fake), model="prov/model")
    cfg = SolverConfig(model="prov/model", session_seconds=30)
    result = backend.solve("p", str(workdir), cfg)
    assert result.turns == 0
    assert "MissingSessionID" in result.error
    assert result.provider_failure


def _json_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


# ── solve_one: 会话级重试耗尽后上抛 ProviderFailure ──

class _FlakySolver:
    """连续返回 provider 失败结果的假后端"""
    name = "flaky"

    def __init__(self, failures: int = 99):
        self.failures = failures
        self.calls = 0

    def solve(self, prompt, workdir, cfg, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            return SolveResult(turns=0, error="400 MissingSessionID")
        return SolveResult(turns=2, duration_s=1.0)


class _StubClient:
    """solve_one 用到的最小平台 client(start/hint/close;不会走到 submit)"""

    class _R:
        closed = True
        container_addr: list = []
        hint = ""

    async def start_challenge(self, code):
        return self._R()

    async def get_hint(self, code):
        return self._R()

    async def submit_flag(self, code, flag):  # pragma: no cover - 不应到达
        raise AssertionError("submit should not be reached on provider failure")

    async def close_challenge(self, code):
        return self._R()


class _Ch:
    unique_code = "x-01"
    description = "test"
    container_addr: list = []
    flag_count = 1
    correct_flag_count = 0
    difficulty = "easy"
    total_score = 100
    is_completed = False


def test_solve_one_retries_then_raises_provider_failure(monkeypatch):
    """会话级重试:1 次首跑 + PROVIDER_FAILURE_RETRIES 次重开,耗尽后上抛"""
    import asyncio

    async def run():
        monkeypatch.setattr("tsecbench_worker.orchestration._async_sleep", _async_sleep_noop)
        solver = _FlakySolver(failures=PROVIDER_FAILURE_RETRIES + 1)
        cfg = type("C", (), {"model": "prov/model", "session_seconds": 5})()
        with pytest.raises(ProviderFailure):
            await solve_one(_StubClient(), _Ch(), cfg=cfg, solver_backend=solver,
                            reporter=LiveReporter(), relay=None,
                            workdir_root="/tmp/tsec-test-work")
        return solver.calls

    assert asyncio.run(run()) == PROVIDER_FAILURE_RETRIES + 1


def test_solve_one_recovers_when_provider_recovers(monkeypatch):
    """前两次失败,第三次恢复:正常返回,不上抛"""
    import asyncio

    async def run():
        monkeypatch.setattr("tsecbench_worker.orchestration._async_sleep", _async_sleep_noop)
        solver = _FlakySolver(failures=2)
        cfg = type("C", (), {"model": "prov/model", "session_seconds": 5})()
        solved, accepted = await solve_one(_StubClient(), _Ch(), cfg=cfg,
                                           solver_backend=solver,
                                           reporter=LiveReporter(), relay=None,
                                           workdir_root="/tmp/tsec-test-work")
        return solver.calls, solved, accepted

    calls, solved, accepted = asyncio.run(run())
    assert calls == 3
    assert solved is False and accepted == []


async def _async_sleep_noop(_s):
    return None


# ── driver: 连续 PROVIDER_FAILURE_EXIT_STREAK 题 → exit 3 熔断 ──

def test_driver_exits_3_on_consecutive_provider_failures(monkeypatch):
    """连败 3 题必须 exit 3,绝不静默烧完 roster(第 4/5 题不被触碰)"""
    import asyncio

    import tsecbench_worker.driver as driver

    calls = {"n": 0}

    async def fake_solve_one(client, ch, **kwargs):
        calls["n"] += 1
        raise ProviderFailure(f"{ch.unique_code}: 0 turns, error X")

    class _StubClient2:
        async def list_challenges(self):
            return [_Ch() for _ in range(5)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(driver, "TSecBenchmarkAsync", lambda **kw: _StubClient2())
    monkeypatch.setattr(driver, "solve_one", fake_solve_one)
    monkeypatch.setattr(driver, "_reporter_set", lambda **kw: None)

    settings = type("S", (), {"benchmark_base_url": "http://x", "benchmark_token": "t",
                              "workdir": "/tmp/tsec-test-work",
                              "flag_format": "flag{...}"})()
    cfg = type("C", (), {"model": "prov/model"})()

    # 直接驱动 amain(绕过 main 的装配单例):SystemExit.code 应为 3
    async def runner():
        try:
            await driver.amain(settings, cfg, object())
        except SystemExit as e:
            return e.code
        return None

    code = asyncio.run(runner())
    assert code == 3
    # 熔断发生在第 3 题:后续题目未被触碰(第 4/5 题没被烧)
    assert calls["n"] == driver.PROVIDER_FAILURE_EXIT_STREAK


def test_driver_streak_resets_on_success(monkeypatch):
    """败→成→败→成→败 相间出现:永不熔断,5 题全处理完正常收尾"""
    import asyncio

    import tsecbench_worker.driver as driver

    # 交替出现:streak 每次被成功清零,最多到 1
    outcomes: list = [ProviderFailure("x-01: 0 turns"), (False, []),
                      ProviderFailure("y-01: 0 turns"), (False, []),
                      ProviderFailure("z-01: 0 turns"), (False, []),
                      ProviderFailure("w-01: 0 turns"), (False, []),
                      ProviderFailure("v-01: 0 turns"), (False, [])]
    idx = {"n": 0}

    async def fake_solve_one(client, ch, **kwargs):
        o = outcomes[idx["n"]]
        idx["n"] += 1
        if isinstance(o, ProviderFailure):
            raise o
        return o

    class _StubClient2:
        def __init__(self):
            self.calls = 0

        async def list_challenges(self):
            # 第 1 次列 10 题;第 2 次(一轮刷完后)模拟任务结束,让 amain 走 exit 0
            self.calls += 1
            if self.calls > 1:
                import tsec_benchmark
                raise tsec_benchmark.InvalidState("done")
            return [_Ch() for _ in range(10)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    stub = _StubClient2()
    monkeypatch.setattr(driver, "TSecBenchmarkAsync", lambda **kw: stub)
    monkeypatch.setattr(driver, "solve_one", fake_solve_one)
    monkeypatch.setattr(driver, "_reporter_set", lambda **kw: None)

    settings = type("S", (), {"benchmark_base_url": "http://x", "benchmark_token": "t",
                              "workdir": "/tmp/tsec-test-work",
                              "flag_format": "flag{...}"})()
    cfg = type("C", (), {"model": "prov/model"})()

    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(driver.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr("tsecbench_worker.orchestration._async_sleep", _async_sleep_noop)

    async def runner():
        try:
            await driver.amain(settings, cfg, object())
        except SystemExit as e:
            return e.code
        return None

    code = asyncio.run(runner())
    assert code == 0  # 任务结束正常退出,不是熔断的 3
    assert idx["n"] == 10  # 10 题全处理,没有中途熔断
