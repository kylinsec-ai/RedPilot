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

from ghost_worker.adapter.solver import create_solver
from ghost_worker.adapter.solver.pi_agent import cleanup_instance_processes
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


def _adapter_cfg(**changes):
    """按朋友引擎的 from_env 口径造配置（12 字段），只覆写指定项。"""
    import dataclasses
    from ghost_worker.adapter.config import SolverConfig as _SC
    return dataclasses.replace(_SC.from_env(), **changes)


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
    from ghost_worker.adapter.solver import create_solver

    workdir = tmp_path / "wd"
    workdir.mkdir()
    monkeypatch.setattr("shutil.which", lambda cmd: str(fake))
    # 引擎把 fake 当 pi 拉起（cmd 经构造函数透传）。
    # HOME 逐题隔离、令牌回收、provider 配置落地都在引擎内部，本用例不关心。
    solver = create_solver(model="deepseek/prov-model")
    solver.cmd = str(fake)
    cfg = _adapter_cfg(model="deepseek/prov-model", session_seconds=30)
    result = solver.solve("p", str(workdir), cfg, transcript_path=str(tmp_path / "t.jsonl"))
    assert result.turns == 0
    assert "MissingSessionID" in result.error


def _json_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


# ── 会话级重试：框架自己那层已随 orchestration.solve_one 退位 ──
#
# 原用例断言的是框架侧 `orchestration.solve_one` 的会话重试循环（0-turn+报错
# 重开至多 PROVIDER_FAILURE_RETRIES 次，耗尽上抛 ProviderFailure 给 driver 熔断）。
# 竞技场主循环（orchestrator._solve_one_unlocked）**自己**就是多会话模型，
# 按 difficulty/时间盒/stoploss 给每道题多个 session，并有它自己的 B59 账号级
# 故障熔断（`_is_api_fault` / `_mark_api_fault` / api_pause）。那一层由仓库根的
# tests/test_solver_regressions.py 与 test_scheduler_stoploss_backoff.py 覆盖。
#
# 这里保留的判据是**跨两层共用的那个**：`SolveResult.provider_failure`
# （上面三条用例）。引擎替掉、编排退位，这两件事都没有改变"0-turn + 报错
# = LLM 上游故障"这条判据 —— 而它正是 2026-09-08 静默烧题的入口。


# ── driver 层的连续熔断：框架版已退位 ──
#
# 原用例断言框架 driver 的 `provider_fail_streak` → exit 3 / 成功清零 / 任务结束
# exit 0 三条。竞技场主循环的对应物是**两层**，都不再是"连败 3 题 exit 3"：
#   - 题目级：stoploss 的多维止损（zero_flag_cutoff / dry_facts / 时间盒）；
#   - 账号级：B59 的 API 暂停 + 退避（`_is_api_fault` → `api_pause_count`），
#     刻意**不退出** —— 退出会被 restart:on-failure 拉起、进程内计数清零，
#     每轮重启再烧 3 题形成无限循环（这正是框架侧那段注释记的事故）。
# 两者的回归在 tests/test_solver_regressions.py、test_scheduler_stoploss_backoff.py。


def test_nonzero_exit_leaves_a_diagnosable_error(tmp_path, monkeypatch):
    """非零退出且只往 stderr 输出:必须留下 error,否则 0-turn 护栏失效。

    这是 2026-09-08 那类静默烧题的另一种形态:pi 因坏模型名/缺凭据/参数错误
    立刻非零退出,stdout 没有任何 JSON 事件 → turns==0 且 error=="" → 上层
    把它当成"正常跑完没解出来"（编排层判 provider 故障 / 账号级故障**都靠
    error 文本**，空 error 等于两道护栏同时失效）。

    断言刻意只锁"诊断信息进来了"：`provider_failure` 是框架侧 SolveResult 的
    属性，朋友引擎的结果模型里没有它 —— 判据由编排层自建（见 orchestrator 的
    `_is_api_fault` 与 stoploss），本用例不越界去断言别人家的属性。
    """
    from ghost_worker.adapter.solver import create_solver

    fake = tmp_path / "fakepi.sh"
    # 只往 stderr 写,stdout 空,退出码 2
    fake.write_text('#!/bin/sh\necho "model not found: prov/nope" >&2\nexit 2\n')
    fake.chmod(0o755)

    workdir = tmp_path / "wd"
    workdir.mkdir()
    monkeypatch.setattr("shutil.which", lambda cmd: str(fake))
    # 竞技场主循环直接构造朋友引擎（框架侧的 FriendSolver 桥接已退役）
    solver = create_solver(model="deepseek/prov-nope")
    solver.cmd = str(fake)
    result = solver.solve("p", str(workdir), _adapter_cfg(model="deepseek/prov-nope"))

    assert result.turns == 0, "非零退出不该被记成一场正常会话"
    assert result.error, "非零退出未留下 error —— 0-turn 护栏失效"
    assert "model not found" in result.error


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


# ── assignment 模式的 provider 熔断：整条链路已退位 ──
#
# assignment（向控制面 claim job → solve → 回报 attempt）是框架独有的一条链路，
# 竞技场主循环完全不知道它（它自己 list_challenges + 自派发）。随框架侧编排
# 退位，assignment.* 及其熔断用例一并删除；见 ghost_worker/driver.py 的头注释。
