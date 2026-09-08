"""digest 折叠状态机单测:golden 语义 / 确定性 / abrupt / dropped / unparsed。

事件构造器自带(不依赖 obs conftest —— contracts 测试独立于任何其他包)。
"""

from __future__ import annotations

import json

from tsecbench_contracts import digest as D

# ── 合成 pi 事件构造器(语义与 obs/tests/conftest 一致) ──

def session_ev(sid: str = "ab12cd", ts: str = "2026-09-04T11:52:03.942Z",
               cwd: str = "/work/a-05") -> dict:
    return {"type": "session", "version": 3, "id": sid, "timestamp": ts, "cwd": cwd}


def attempt_ev(n: int = 1) -> dict:
    return {"type": "_attempt", "attempt": n}


def user_msg_ev(text: str = "解题 a-05", ts: str = "2026-09-04T11:52:04.100Z") -> dict:
    return {"type": "message_start",
            "message": {"role": "user", "content": [{"type": "text", "text": text}],
                        "timestamp": ts}}


def turn_start_ev() -> dict:
    return {"type": "turn_start"}


def turn_end_ev(tokens: int | None = None, stop: str = "tool_use") -> dict:
    msg: dict = {"role": "assistant", "stopReason": stop}
    if tokens is not None:
        msg["usage"] = {"totalTokens": tokens}
    return {"type": "turn_end", "message": msg}


def tool_start_ev(call: str = "call_1", name: str = "bash", cmd: str = "ls -la /tmp") -> dict:
    return {"type": "tool_execution_start", "toolCallId": call, "toolName": name,
            "args": {"command": cmd}}


def tool_end_ev(call: str = "call_1", out: str = "", err: bool = False) -> dict:
    return {"type": "tool_execution_end", "toolCallId": call, "isError": err,
            "result": {"content": [{"type": "text", "text": out}]}}


def assistant_msg_ev(text: str, ts: str = "2026-09-04T11:52:05.200Z") -> dict:
    return {"type": "message_end",
            "message": {"role": "assistant", "timestamp": ts,
                        "content": [{"type": "text", "text": text}]}}


def agent_end_ev() -> dict:
    return {"type": "agent_end"}


def make_run_events(events: list[dict]) -> list[dict]:
    """事件 -> 摄取行形态(每行 {payload: 原文 JSON}),fold_rows 的输入。"""
    return [{"seq": i, "type": e.get("type", ""), "ts": None,
             "payload": json.dumps(e, ensure_ascii=False)}
            for i, e in enumerate(events)]


def _golden_events():
    """典型压缩后会话:session -> 提示词 -> turn1(bash) -> 回复 -> turn2 -> agent_end。"""
    return [session_ev(), {"type": "agent_start"}, user_msg_ev(),
            turn_start_ev(),
            tool_start_ev("call_1", "bash", "ls -la /tmp"),
            tool_end_ev("call_1", "total 8\nfile.txt"),
            assistant_msg_ev("看到文件了"),
            turn_end_ev(tokens=42, stop="tool_use"),
            turn_start_ev(),
            assistant_msg_ev("完成"),
            turn_end_ev(tokens=None, stop="end_turn"),
            agent_end_ev()]


def test_golden_fold():
    out = D.fold_rows(make_run_events(_golden_events()))
    entries = out["entries"]
    assert out["next_seq"] == 8
    kinds = [e["kind"] for e in entries]
    assert kinds == ["session", "note", "turn", "tool", "text", "turn", "text", "note"]
    meta = out["meta"]
    assert meta == {"sessions": 1, "agent_ends": 1, "truncated": False, "dropped": False,
                    "abrupt": False, "live": False, "unparsed": 0}
    e = entries[0]  # session
    assert e["sid"] == "ab12cd"
    assert e["note"] == "cwd: /work/a-05"
    assert e["t"] > 1e12
    assert e["turn"] == 0
    assert entries[1]["note"] == "任务输入（提示词）就绪"
    e2 = entries[2]  # turn 1,后被 turn_end 补 tokens/stop
    assert e2["n"] == 1
    assert e2["tokens"] == 42
    assert e2["stop"] == "tool_use"
    e3 = entries[3]  # tool
    assert e3["tool"] == "bash"
    assert e3["cmd"] == "ls -la /tmp"
    assert e3["id"] == "call_1"
    assert e3["out"] == "total 8\nfile.txt"
    assert e3["out_len"] == len("total 8\nfile.txt")
    assert e3["err"] is False
    assert entries[4]["text"] == "看到文件了"  # 短文本无截断符
    assert entries[4]["more"] is False
    e5 = entries[5]
    assert e5["n"] == 2 and e5["stop"] == "end_turn"
    assert "tokens" not in e5
    assert entries[6]["text"] == "完成"
    assert entries[7]["note"] == "agent 正常结束"


def test_deterministic():
    rows = make_run_events(_golden_events())
    a = D.fold_rows(rows)
    b = D.fold_rows(rows)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_after_slice():
    out = D.fold_rows(make_run_events(_golden_events()), after=4)
    assert out["next_seq"] == 8
    assert [e["seq"] for e in out["entries"]] == [4, 5, 6, 7]


def test_abrupt_and_live():
    events = _golden_events()[:-1]  # 无 agent_end → abrupt
    assert D.fold_rows(make_run_events(events))["meta"]["abrupt"] is True
    assert D.fold_rows(make_run_events(events), live=True)["meta"]["abrupt"] is False
    assert D.fold_rows(make_run_events(events), live=True)["meta"]["live"] is True


def test_attempt_and_error_entries():
    rows = make_run_events([attempt_ev(2),
                            {"type": "error", "message": "connect timeout after 480s"}])
    out = D.fold_rows(rows)
    assert out["meta"] == {"sessions": 0, "agent_ends": 0, "truncated": False,
                           "dropped": False, "abrupt": False, "live": False, "unparsed": 0}
    assert out["entries"][0]["kind"] == "attempt" and out["entries"][0]["n"] == 2
    assert out["entries"][1]["kind"] == "error"
    assert out["entries"][1]["note"] == "connect timeout after 480s"


def test_unparsed_rows_skipped():
    rows = make_run_events([session_ev()]) + [{"seq": 9, "type": "", "ts": None,
                                               "payload": "not json at all"}]
    out = D.fold_rows(rows)
    assert out["meta"]["unparsed"] == 1
    assert len(out["entries"]) == 1


def test_streaming_updates_folded_when_present():
    """message_update 增量折叠(旧 digest 能力;DB 流里 worker 已丢,保留分支以防回填)。"""
    ts = "2026-09-04T11:52:06.000Z"

    def upd(subtype: str, delta: str | None = None) -> dict:
        ev: dict = {"type": "message_update",
                    "message": {"role": "assistant", "timestamp": ts}}
        a: dict = {"type": subtype, "contentIndex": 0, "partial": {}}
        if delta is not None:
            a["delta"] = delta
        ev["assistantMessageEvent"] = a
        return ev

    events = [upd("text_start"), upd("text_delta", "看到"),
              upd("text_delta", "文件了"), upd("text_end")]
    out = D.fold_rows(make_run_events(events))
    assert len(out["entries"]) == 1
    assert out["entries"][0]["kind"] == "text"
    assert out["entries"][0]["text"] == "看到文件了"


def test_text_truncation_more(monkeypatch):
    monkeypatch.setattr(D, "TEXT_FLUSH", 10)
    events = [{"type": "message_update", "message": {"role": "assistant", "timestamp": None},
               "assistantMessageEvent": {"type": "text_start", "contentIndex": 0}},
              {"type": "message_update", "message": {"role": "assistant", "timestamp": None},
               "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0,
                                         "partial": {}, "delta": "字" * 1400}}]
    out = D.fold_rows(make_run_events(events))
    entry = out["entries"][0]
    assert entry["kind"] == "text"
    assert entry["more"] is True
    assert entry["text"] == "字" * D.TEXT_ENTRY_MAX + "…"


def test_entry_cap_drops_oldest(monkeypatch):
    monkeypatch.setattr(D, "ENTRY_CAP", 8)
    events = [attempt_ev(i) for i in range(30)]
    out = D.fold_rows(make_run_events(events))
    assert out["meta"]["dropped"] is True
    assert len(out["entries"]) <= 8
    # 丢的是最旧(seq 最小的 attempt 没了)
    assert all(e["seq"] > 0 for e in out["entries"])


def test_foldstate_incremental_parity():
    """增量喂 FoldState(逐条)与 fold_rows(全量)结果一致 —— worker I/O 壳的语义钉子。"""
    events = _golden_events()
    st = D.FoldState()
    for ev in events:
        st.feed(ev)
    st.flush_text()
    full = D.fold_rows(make_run_events(events))
    assert st.entries == full["entries"]
    assert st.next_seq == full["next_seq"]
    assert st.sessions == full["meta"]["sessions"]
    assert st.agent_ends == full["meta"]["agent_ends"]
