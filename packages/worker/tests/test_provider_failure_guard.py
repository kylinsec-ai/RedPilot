"""provider 失败护栏测试:0-turn+报错的会话不再被当作"正常完成"。

2026-09-08 事故回归(280 run/0 flag/63 题静默烧库):pi 对 provider 400 只发
stopReason=error 的收尾消息,pi_agent 漏读 → err=none → 编排层当完成处理。
本文件覆盖:pi_agent 读出 stopReason=error(SolveResult.provider_failure)、
solve_one 会话级重试与 ProviderFailure 上抛、driver 连续熔断 exit 3。

pytest 由仓库根起(packages/worker 在 pythonpath,ghost_worker 可导入)。
"""

from __future__ import annotations

import json

import pytest

from ghost_worker.orchestration import (
    PROVIDER_FAILURE_RETRIES,
    ProviderFailure,
    LiveReporter,
    solve_one,
)
from ghost_worker.solver.base import SolveResult


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
    from ghost_worker.solver.friend import FriendSolver
    from ghost_worker.config import SolverConfig

    workdir = tmp_path / "wd"
    workdir.mkdir()
    monkeypatch.setattr("shutil.which", lambda cmd: str(fake))
    # 朋友的引擎把 fake 当 pi 拉起（cmd 经构造函数透传）。
    # HOME 逐题隔离、令牌回收、provider 配置落地都在引擎内部，本用例不关心。
    solver = FriendSolver()
    solver._backend.cmd = str(fake)
    solver._backend.model = "deepseek/prov-model"
    cfg = SolverConfig(model="deepseek/prov-model", session_seconds=30)
    result = solver.solve("p", str(workdir), cfg, transcript_path=str(tmp_path / "t.jsonl"))
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
        monkeypatch.setattr("ghost_worker.orchestration._async_sleep", _async_sleep_noop)
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
        monkeypatch.setattr("ghost_worker.orchestration._async_sleep", _async_sleep_noop)
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

    import ghost_worker.driver as driver

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

    monkeypatch.setattr(driver, "GhostmarkAsync", lambda **kw: _StubClient2())
    monkeypatch.setattr(driver, "solve_one", fake_solve_one)

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

    import ghost_worker.driver as driver

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
    monkeypatch.setattr(driver, "GhostmarkAsync", lambda **kw: stub)
    monkeypatch.setattr(driver, "solve_one", fake_solve_one)

    settings = type("S", (), {"benchmark_base_url": "http://x", "benchmark_token": "t",
                              "workdir": "/tmp/tsec-test-work",
                              "flag_format": "flag{...}"})()
    cfg = type("C", (), {"model": "prov/model"})()

    async def fake_sleep(_s):
        return None

    monkeypatch.setattr(driver.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr("ghost_worker.orchestration._async_sleep", _async_sleep_noop)

    async def runner():
        try:
            await driver.amain(settings, cfg, object())
        except SystemExit as e:
            return e.code
        return None

    code = asyncio.run(runner())
    assert code == 0  # 任务结束正常退出,不是熔断的 3
    assert idx["n"] == 10  # 10 题全处理,没有中途熔断


def test_nonzero_exit_with_only_stderr_is_provider_failure(tmp_path, monkeypatch):
    """非零退出且只往 stderr 输出:必须留下 error,否则 0-turn 护栏失效。

    这是 2026-09-08 那类静默烧题的另一种形态:pi 因坏模型名/缺凭据/参数错误
    立刻非零退出,stdout 没有任何 JSON 事件 → turns==0 且 error=="" →
    provider_failure 判否 → 该题被当成"正常未解"记入结果。
    """
    fake = tmp_path / "fakepi.sh"
    # 只往 stderr 写,stdout 空,退出码 2
    fake.write_text('#!/bin/sh\necho "model not found: prov/nope" >&2\nexit 2\n')
    fake.chmod(0o755)
    from ghost_worker.solver.friend import FriendSolver
    from ghost_worker.config import SolverConfig

    workdir = tmp_path / "wd"
    workdir.mkdir()
    monkeypatch.setattr("shutil.which", lambda cmd: str(fake))
    solver = FriendSolver()
    solver._backend.cmd = str(fake)
    solver._backend.model = "deepseek/prov-nope"
    result = solver.solve("p", str(workdir), SolverConfig(model="deepseek/prov-nope",
                                                         session_seconds=30))

    assert result.turns == 0
    assert result.error, "非零退出未留下 error —— 0-turn 护栏失效"
    assert "model not found" in result.error
    assert result.provider_failure


# ── lease 丢失时必须真杀 pi 进程 ──
#
# ⚠ 这一组用例断言的原实现（框架侧 `solver/pi_agent.py` 的 `_LIVE_SOLVERS`
#   登记表 + `kill_solver_processes`）已随引擎替换删除。它只能在**本进程内**
#   杀掉自己登记过的进程组 —— 驱动崩溃后脱组的 `nohup`/`setsid` 子孙它看不到。
#
#   朋友引擎改用**逐次访问的随机令牌**：driver 把 token 写进 workdir 的
#   `_instance.json`，pi 及其全部子孙继承该环境变量，收尾时扫 `/proc/*/environ`
#   按令牌回收。它不依赖本进程的登记表，因此崩溃后仍有效，且按构造无法误伤
#   driver / VPN provider / 其他 worker。
#
#   **当前缺口**：框架的编排链路还没有写 `_instance.json`，所以令牌拿不到，
#   按令牌回收在容器里是空操作。下面两条用例因此只断言"按令牌正确采集 PID"
#   这一半（另一半由 driver 侧接线补齐后才有意义）。

def test_cleanup_token_only_matches_tagged_processes(monkeypatch):
    """令牌采集必须只认带标记的进程，且令牌格式不合法时一律空手而归。"""
    import os as _os
    from ghost_worker.adapter.solver import pi_agent as eng

    assert eng._tagged_processes("") == []
    assert eng._tagged_processes("not-a-32-hex-token") == []
    assert eng._tagged_processes("A" * 32) == []   # 大写不算（正则要求小写十六进制）

    # 伪造一个带标记的进程条目：_tagged_processes 只读 /proc/<pid>/environ
    token = "a" * 32
    marker = (eng._INSTANCE_TOKEN_ENV + "=" + token).encode("ascii")
    fake_pid = str(_os.getpid() + 999999)

    real_listdir, real_open = _os.listdir, open

    def fake_listdir(path):
        if path == "/proc":
            return [fake_pid]
        return real_listdir(path)

    class _Ctx:
        def __enter__(self):
            import io
            return io.BytesIO(b"PATH=/bin\0" + marker + b"\0")

        def __exit__(self, *a):
            return False

    def fake_open(path, *a, **kw):
        if path == f"/proc/{fake_pid}/environ":
            return _Ctx()
        return real_open(path, *a, **kw)

    monkeypatch.setattr(eng.os, "listdir", fake_listdir)
    monkeypatch.setattr("builtins.open", fake_open)
    assert eng._tagged_processes(token) == [int(fake_pid)]


def test_cleanup_instance_processes_is_noop_without_token(tmp_path):
    """workdir 里没有（或令牌非法）`_instance.json` 时，回收必须安全空转。

    这是当前框架运行期的真实状态（编排链路尚未写该文件）——所以它同时是一条
    回归护栏：接线之前，`cleanup_instance_processes` 不得误杀任何东西。
    """
    from ghost_worker.adapter.solver.pi_agent import (
        _instance_cleanup_token, cleanup_instance_processes)

    workdir = tmp_path / "wd"
    workdir.mkdir()
    assert _instance_cleanup_token(str(workdir)) == ""
    assert cleanup_instance_processes(str(workdir), grace_seconds=0.1) == 0

    (workdir / "_instance.json").write_text('{"trace_scope": "NOPE"}', encoding="utf-8")
    assert _instance_cleanup_token(str(workdir)) == ""
    assert cleanup_instance_processes(str(workdir), grace_seconds=0.1) == 0


# ── assignment 模式的 provider 熔断 ──

class _RecordingAssignmentClient:
    """记录 complete 载荷的最小控制面 client。"""

    def __init__(self):
        self.completes: list[dict] = []
        self.heartbeats = 0

    async def complete(self, attempt_id, lease_id, **kwargs):
        self.completes.append({"attempt_id": attempt_id, **kwargs})
        return {"ok": True}

    async def attempt_heartbeat(self, attempt_id, lease_id, seconds):
        self.heartbeats += 1
        return {"ok": True}


def test_assignment_provider_failure_reports_interrupted(monkeypatch):
    """provider 失败必须上报 interrupted 而非 failed。

    failed 会让 core 把 job 置终态且**不可再 claim**(store.py 的 job 状态机)——
    LLM 上游一挂就逐题烧穿整个队列。interrupted 则让 job 回 pending 待重做,
    配合 driver 的进程内冷却,上游恢复后可继续。
    """
    import asyncio
    import ghost_worker.driver as driver

    async def failing_solve(*args, **kwargs):
        raise driver.ProviderFailure("provider 500")

    monkeypatch.setattr(driver, "solve_one", failing_solve)

    class _Bench:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def list_challenges(self): return [_Ch()]

    monkeypatch.setattr(driver, "GhostmarkAsync", lambda **kw: _Bench())

    client = _RecordingAssignmentClient()
    settings = type("S", (), {"workdir": "/tmp/wd", "flag_format": "flag{...}",
                             "assignment_lease_seconds": 300,
                             "benchmark_base_url": None, "platform_url": "http://p"})()
    cfg = type("C", (), {"model": "m", "session_seconds": 60})()
    assignment = {"attempt_id": "a1", "lease_id": "l1", "unique_code": "x-01",
                  "benchmark_base_url": "http://b", "benchmark_token": "t"}

    outcome = asyncio.run(driver._solve_assignment(
        settings, cfg, None, client, assignment,
        reporter=type("R", (), {"set": lambda *a, **k: None})(), relay=None))

    assert outcome == "provider_failure", "熔断信号未回传"
    assert client.completes, "未上报终态"
    assert client.completes[-1]["status"] == "interrupted", \
        f"provider 失败应报 interrupted(job 回 pending),实报 {client.completes[-1]['status']}"
