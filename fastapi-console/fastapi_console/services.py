"""FastAPI 控制台：平台转发 / VPN / AI 解题 / Agent 舰队 / 派单。

复用 Django 版业务逻辑：
- tsecbench 平台层（store / service / vpn）
- tsecweb.agent（舰队控制、派单队列、worker 战绩解析）
- tsecweb.solver（LLM 客户端 + flag 提取）
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))                      # tsecbench


from tsecbench.errors import APIError  # noqa: E402
from tsecbench.vpn import VPNManager  # noqa: E402

from .agent import (  # noqa: E402
    fleet_start,
    fleet_status,
    fleet_stop,
    worker_logs,
    solve_one,
    single_status,
    list_transcripts,
    read_transcript,
    usage_summary,
)
from .solver import SYSTEM_PROMPT, ask_llm, redact_flag_like_text  # noqa: E402

from .cfg import get_cfg, remote_config  # noqa: E402
from .session import mark_dirty  # noqa: E402


# 传输层韧性：解析抖动/连接闪断不该直接变成 502。
# 背景（2026-09-10 实测）：宿主 systemd-resolved 会把域名送给 VPN 链路（tun0
# 声明 `~.` 路由域）下发的那个 DNS，而它一旦不通，平台域名解析就随机失败——
# 20 次里 1 次 gaierror（20s 后超时），另有多达 3~11s 的慢解析。gaierror 是
# OSError 子类，正好落进下面那个 502 分支；且 urlopen(timeout=60) 的 60 秒
# **不覆盖 getaddrinfo**，解析卡多久就干等多久。
# 因此：① 整次请求（含 DNS）丢线程池限时，超时即重试；② 传输层失败重试 3 次
# 带退避；③ 平台明确应答的 HTTP 错误（404/409/422…）是有效结论，直接上抛。
_REMOTE_ATTEMPTS = 3
_REMOTE_TIMEOUT = 20.0           # 单次请求预算，覆盖 DNS + 连接 + 读取
_REMOTE_BACKOFF = (0.3, 1.0)     # 每次重试前的等待
_REMOTE_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="remote")


def _remote_scope(remote: tuple[str, str]) -> str:
    """Return an irreversible identity for a configured remote connection."""
    base, token = remote
    return hashlib.sha256(f"{base}\0{token}".encode("utf-8")).hexdigest()


def _remote_hint_state(session: dict, remote: tuple[str, str]) -> tuple[dict, str, int]:
    """Get task-local hint state for exactly one remote connection.

    Older sessions keyed this cache only by challenge code.  A code is not a
    globally unique security boundary: a different platform, account, or
    benchmark run can reuse it.  Replace that legacy shape rather than trying
    to migrate its values, because their provenance cannot be established.
    """
    scope = _remote_scope(remote)
    state = session.get("console_remote_state")
    if isinstance(state, dict) and state.get("scope") == scope \
            and isinstance(state.get("hints"), dict):
        try:
            generation = max(0, int(state.get("generation", 0)))
        except (TypeError, ValueError):
            generation = 0
            state["generation"] = generation
        return state, scope, generation

    try:
        generation = max(0, int(state.get("generation", 0))) + 1
    except (AttributeError, TypeError, ValueError):
        generation = 1
    state = {
        "schema": 1,
        "scope": scope,
        "generation": generation,
        "hints": {},
    }
    session["console_remote_state"] = state
    return state, scope, generation


def _remote_state_is_current(session: dict, remote: tuple[str, str], scope: str,
                             generation: int) -> bool:
    """Reject results from a connection switched while its request was in flight."""
    current = remote_config(session)
    state = session.get("console_remote_state")
    return (
        current == remote
        and isinstance(state, dict)
        and state.get("scope") == scope
        and state.get("generation") == generation
        and isinstance(state.get("hints"), dict)
    )


def _remote_context_changed() -> APIError:
    return APIError(
        409,
        "remote_context_changed",
        "平台连接在请求期间已变更；已丢弃旧连接返回的题目上下文。",
    )


def _remote_once(url: str, data: bytes | None, headers: dict, method: str, timeout: float):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def _remote_request(base: str, token: str, path: str, method: str = "GET", body: dict | None = None):
    """转发到远端平台（传输层失败重试；HTTP 错误直接上抛）。"""
    url = base + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"BENCHMARK_TOKEN": token}
    if body is not None:
        headers["Content-Type"] = "application/json"

    last: Exception | None = None
    for attempt in range(_REMOTE_ATTEMPTS):
        try:
            fut = _REMOTE_POOL.submit(_remote_once, url, data, headers, method, _REMOTE_TIMEOUT)
            return fut.result(timeout=_REMOTE_TIMEOUT + 5)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message", "")
            except Exception:
                pass
            safe_detail = redact_flag_like_text(detail or exc.reason) or "远端平台返回了错误"
            raise APIError(exc.code, "remote_error", f"远端平台错误: {safe_detail}") from exc
        except concurrent.futures.TimeoutError as exc:
            last = exc
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        if attempt + 1 < _REMOTE_ATTEMPTS:
            time.sleep(_REMOTE_BACKOFF[min(attempt, len(_REMOTE_BACKOFF) - 1)])
    safe_last = redact_flag_like_text(last) or "传输层错误"
    raise APIError(502, "remote_unreachable",
                   f"远端平台不可达（重试 {_REMOTE_ATTEMPTS} 次仍失败）: {safe_last}") from last


# ── 平台单例（本地模式）──────────────────────────────

from dataclasses import replace  # noqa: E402

from tsecbench.config import Settings  # noqa: E402
from tsecbench.provisioner import provisioner_for  # noqa: E402
from tsecbench.service import ChallengeService  # noqa: E402
from tsecbench.store import Store  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

settings = Settings.from_env()
if not __import__("os").environ.get("TSECBENCH_DB_PATH"):
    settings = replace(settings, database_path=str(PROJECT_ROOT / "data" / "tsecbench.sqlite3"))

store = Store(settings.database_path)
service = ChallengeService(store, provisioner_for(settings.provisioner), settings.max_active_challenges)
service.seed(settings.load_tasks())
vpn = VPNManager(Path(settings.database_path).parent / "vpn")

DEFAULT_TOKEN = settings.benchmark_token or "demo-token-001"


def _resolve_token(session: dict) -> str:
    return DEFAULT_TOKEN


# ── 挑战接口 ─────────────────────────────────────────

def list_challenges(session: dict):
    remote = remote_config(session)
    if remote:
        return _remote_request(remote[0], remote[1], "/openapi/v1/challenges")
    return service.list_challenges(_resolve_token(session))


def start_challenge(session: dict, unique_code: str):
    remote = remote_config(session)
    if remote:
        return _remote_request(
            remote[0], remote[1], f"/openapi/v1/challenges/start?unique_code={unique_code}", "POST"
        )
    return service.start(_resolve_token(session), unique_code)


def get_hint(session: dict, unique_code: str):
    remote = remote_config(session)
    if remote:
        remote_state, scope, generation = _remote_hint_state(session, remote)
        result = _remote_request(
            remote[0], remote[1], f"/openapi/v1/challenges/hint?unique_code={unique_code}"
        )
        if not _remote_state_is_current(session, remote, scope, generation):
            raise _remote_context_changed()
        if not isinstance(result, dict):
            raise APIError(502, "remote_invalid_response", "远端平台返回了无效的提示响应")
        hint_state = remote_state["hints"].setdefault(unique_code, {})
        hint_state["hint"] = result.get("hint")
        hint_state["hint_viewed"] = True
        return result
    return service.hint(_resolve_token(session), unique_code)


def submit_flag(session: dict, unique_code: str, flag: str):
    """Forward an explicit manual-console submission.

    This is intentionally a thin platform proxy for the workspace's manual
    form.  It is not an automation primitive: LLM plan flows must never call
    it.  Automated solver submissions belong to the benchmark driver's
    evidence verifier, which binds a candidate to current-target tool output
    before contacting the platform.
    """
    remote = remote_config(session)
    if remote:
        return _remote_request(
            remote[0], remote[1], "/openapi/v1/challenges/submit", "POST",
            {"unique_code": unique_code, "flag": flag},
        )
    return service.submit(_resolve_token(session), unique_code, flag)


def close_challenge(session: dict, unique_code: str):
    remote = remote_config(session)
    if remote:
        return _remote_request(
            remote[0], remote[1], f"/openapi/v1/challenges/close?unique_code={unique_code}", "POST"
        )
    return service.close(_resolve_token(session), unique_code)


def run_ai_auto(session: dict, code: str) -> dict:
    """Backward-compatible plan-only endpoint.

    It returns exactly one investigation plan.  It has no target lifecycle or
    submission side effects; automated submissions remain exclusively in the
    worker pipeline's evidence verifier.
    """
    result = run_ai_round(session, code)
    # Machine-readable policy for compatibility clients.
    result["submission_authorization"] = "none"
    result["logs"].insert(0, {
        "time": result["logs"][0]["time"] if result["logs"] else "",
        "type": "info",
        "text": "自动通关已禁用；已返回仅供靶场取证使用的解题计划，未提交任何 flag。",
    })
    return result


# ── 题目概要（供 AI 上下文）──────────────────────────

def challenge_brief(session: dict, code: str) -> dict:
    remote = remote_config(session)
    if remote:
        base, token = remote
        remote_state, scope, generation = _remote_hint_state(session, remote)
        rows = _remote_request(base, token, "/openapi/v1/challenges")
        if not _remote_state_is_current(session, remote, scope, generation):
            raise _remote_context_changed()
        if not isinstance(rows, list):
            raise APIError(502, "remote_invalid_response", "远端平台返回了无效的题目列表")
        item = next((x for x in rows if x.get("unique_code") == code), None)
        if item is None:
            raise APIError(404, "challenge_not_found", "Challenge not found")
        hint_state = remote_state["hints"].get(code, {})
        return {
            "code": code,
            "description": item.get("description"),
            "difficulty": item.get("difficulty"),
            "level": item.get("level"),
            "flag_count": item.get("flag_count", 0),
            "correct_count": item.get("correct_flag_count", 0),
            "completed": bool(item.get("is_completed")),
            "container_status": item.get("container_status", "stopped"),
            "addresses": list(item.get("container_addr") or []),
            "hint": hint_state.get("hint") if isinstance(hint_state, dict) else None,
            "hint_viewed": bool(hint_state.get("hint_viewed")) if isinstance(hint_state, dict) else False,
        }
    token = _resolve_token(session)
    row = store.get_challenge(token, code)
    if row is None:
        raise APIError(404, "challenge_not_found", "Challenge not found")
    definition = row.definition
    correct = len(store.submissions(token, code))
    return {
        "code": code,
        "description": definition.description,
        "difficulty": definition.difficulty,
        "level": definition.level,
        "flag_count": len(definition.flags),
        "correct_count": correct,
        "completed": correct >= len(definition.flags),
        "container_status": row.container_status,
        "addresses": list(row.container_addresses) if row.container_status == "available" else [],
        "hint": definition.hint if row.hint_viewed else None,
        "hint_viewed": row.hint_viewed,
    }


def build_context(brief: dict) -> list[dict]:
    """Build a bounded, task-local context for an investigation plan.

    Deliberately omit prior candidate/submission history.  Even hashed or
    counted attempts are irrelevant to a plan and keeping them invites the UI
    path to become cross-session answer memory.
    """
    lines = [
        "【题目】",
        f"标识: {brief['code']}",
        f"难度: {brief.get('difficulty') or 'unknown'}",
        f"关卡: {brief.get('level') or 0}",
        f"描述: {brief.get('description') or '(无描述)'}",
        "",
        "【目标地址】(需在靶场网络内访问)",
        "\n".join(brief["addresses"]) if brief["addresses"] else "(容器未启动)",
        "",
        "【提示】",
        brief["hint"] if brief.get("hint_viewed") and brief.get("hint") else "(未查看，查看会扣分)",
        "",
        "【进度】",
        f"flag 总数: {brief['flag_count']}",
        f"已正确提交: {brief['correct_count']}",
        f"是否已完成: {'是' if brief['completed'] else '否'}",
        "",
        "【边界】",
        "- 仅输出调查计划，不要输出任何 flag、候选答案、密钥或可提交字符串。",
        "- 计划中的每个结论都要指出需要从当前目标取得的证据。",
    ]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _require_current_challenge_context(requested_code: str, brief: dict) -> None:
    """Reject a plan when the fetched brief does not belong to its request.

    A plan must remain task-local.  In particular, a stale cache or an
    integration bug must not let one challenge's description, addresses, or
    progress become context for another challenge's LLM request.
    """
    actual_code = str(brief.get("code") or "") if isinstance(brief, dict) else ""
    if not requested_code or actual_code != requested_code:
        raise APIError(
            409,
            "challenge_context_mismatch",
            "题目上下文不匹配；已拒绝将其发送给 AI。",
        )


def run_ai_round(session: dict, code: str) -> dict:
    """Return one evidence-oriented investigation plan; never submit a flag."""
    from datetime import datetime

    cfg = get_cfg(session)
    logs = []

    def log(log_type, text):
        logs.append({"time": datetime.now().strftime("%H:%M:%S"), "type": log_type, "text": text})

    brief = challenge_brief(session, code)
    _require_current_challenge_context(code, brief)
    content = ask_llm(cfg, build_context(brief))
    plan = redact_flag_like_text(content)
    if not plan:
        log("warn", "LLM 未生成可用计划；请先补充题目现场证据后再试")
    else:
        log("info", "AI 已生成调查计划；控制台不会从该输出提取或提交 flag。")
    return {
        "plan": plan,
        # Retain empty legacy fields so clients cannot mistake plan text for a
        # submit-ready candidate list.
        "candidates": [],
        "results": [],
        "completed": brief["completed"],
        "flag_count": brief["flag_count"],
        "logs": logs,
        "made_progress": False,
    }


# ── 观察者之镜（B61）—— 只读展示 ─────────────────────────────
#
# 观察者 Agent 读主 Agent 的思考，对照成一张 <heimdall-map>，注入**下一场**的
# prompt；状态落在 work/<code>/.heimdall.json。本接口把它摊给操作员看，回答两个
# 问题：「观察者看出了什么」与「主 Agent 当时究竟收到了什么」。
#
# ★ 走 adapter.heimdall.render() 渲染，**不另写一套展示逻辑** —— 否则面板上
#   看到的与 agent 实际收到的就可能不是同一张图，而"两者一致"正是这个面板
#   存在的意义（B62 的设计前提）。
# ★ .heimdall.json 的 why/evidence 是**工具输出摘录**，可能含答案明文，
#   故按 driver 清理路径的同一口径打码（adapter.compliance，B52 建立）。
#   打码命中时面板必须**显式标注** —— 否则操作员会把打码过的图误当成
#   agent 原样收到的东西，那这个面板就在骗人。
# ★ **只读**：不碰 session、不写盘、无副作用。任何异常 → available=False，
#   照常返回 200（同观察者红线 4：它缺席不该影响别的功能）。

_WORK_ROOT = PROJECT_ROOT / "work"

#: 题目标识白名单：字母数字与 `_ - .`，且不得是 `.`/`..`。
#: unique_code 直接来自 query string，会拼进文件路径 —— 必须挡穿越。
_CODE_RX = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def render_heimdall_payload(code: str) -> dict:
    """题目观察：work/<code>/.heimdall.json → 面板结构（**只读**）。

    命名函数而非内联在路由里：便于单测直接调（B60 的教训）。
    """
    code = (code or "").strip()
    if not _CODE_RX.match(code) or code in (".", ".."):
        return {"available": False, "code": code, "reason": "题目标识不合法"}

    workdir = str(_WORK_ROOT / code)
    if not os.path.isdir(workdir):
        return {"available": False, "code": code,
                "reason": "该题还没有工作目录（舰队尚未解过这道题）"}
    if not os.path.exists(os.path.join(workdir, ".heimdall.json")):
        return {"available": False, "code": code,
                "reason": "观察者还没有为这道题出过图"}

    try:
        from ghost_worker.adapter.heimdall import load_state, render, _KEEP_PER_KIND
        from ghost_worker.adapter.compliance import flag_plaintext_rx, scrub_text, count_redactions
    except Exception as exc:                                    # noqa: BLE001
        return {"available": False, "code": code, "reason": f"观察者模块不可用：{exc}"}

    # ★ 先自己验一遍**原始文件**：load_state 把「文件损坏」与「图本来就是空的」
    #   都折叠成同一个 empty_state()，面板上分不出来 —— 而这两件事对操作员含义
    #   完全不同：前者说明观察者**写盘在失败**（可操作信号），后者是正常状态。
    #   （load_state 那个折叠对 driver 侧是对的：永不抛、给个能用的 dict 就够；
    #     需要更细区分的是展示层，所以补在这里而不是改它。）
    try:
        with open(os.path.join(workdir, ".heimdall.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        raw_ok = isinstance(raw, dict) and isinstance(raw.get("nodes"), list)
    except (OSError, ValueError):
        raw_ok = False
    if not raw_ok:
        return {"available": False, "code": code,
                "reason": "状态文件损坏或格式不对（观察者这一场多半写盘失败了）"}

    try:
        st = load_state(workdir)
        session = st.get("session", -1)
        nodes = st.get("nodes") or []
        active = [n for n in nodes if n.get("status") == "active"]
        tens = [t for t in (st.get("tensions") or []) if t.get("status") == "active"]

        # 注入正文 = agent 实际收到的那一段。默认预算（_MAP_MAX）。
        shown = render(st)
        # 同一次渲染但放开**字数**预算，用来判断默认那份是不是被字数截过。
        # 注意：每类条数上限（_KEEP_PER_KIND）在这一版里**仍然生效** ——
        # 所以它叫"未受字数预算裁剪"，不叫"完整图"。别在 UI 上撒这个谎。
        roomy = render(st, max_chars=10 ** 9)
        map_text, _ = scrub_text(shown, flag_plaintext_rx(workdir))
    except Exception as exc:                                    # noqa: BLE001
        return {"available": False, "code": code, "reason": f"状态文件解析失败：{exc}"}

    if not shown.strip():
        # 合法但空：观察者出过图，只是当前无 active 条目、也没有本场刚退场的。
        # 与「文件损坏」是两回事，reason 必须分开写。
        return {"available": False, "code": code, "session": session,
                "updated_text": _fmt_ts(st.get("updated_at")),
                "reason": "观察者出过图，但当前没有可展示的条目"
                          "（无 active 条目，也没有本场刚退场的）"}

    counts = {k: sum(1 for n in active if n.get("kind") == k)
              for k in ("lock", "dead", "angles", "postpone")}
    # 两类退场措辞必须分开：撤销 = "这条被证伪了"；淡出 = "这条没再被观察到"。
    # 混在一起会把后者读成前者 —— 而误判死路正是观察者红线 2 要防的事。
    gone = [n for n in nodes if n.get("gone_at") == session]
    retracted = [{"id": n.get("id"), "text": n.get("text"), "why": n.get("retract_why") or ""}
                 for n in gone if n.get("status") == "retracted"]
    faded = [{"id": n.get("id"), "text": n.get("text")}
             for n in gone if n.get("status") != "retracted"]

    return {
        "available": True,
        "code": code,
        "session": session,
        "updated_at": float(st.get("updated_at") or 0),
        "updated_text": _fmt_ts(st.get("updated_at")),
        "counts": counts,
        "active_total": len(active),
        "tension_total": len(tens),
        "retracted": retracted,
        "faded": faded,
        "map_text": map_text,
        # 面板上这份文本里现有的打码痕迹总数（含 driver 清理路径早先写入的）。
        # > 0 时面板必须显式标注"这已不是 agent 原样看到的字面"。
        "redacted": count_redactions(map_text),
        # 每类超 _KEEP_PER_KIND 被丢弃的条数（render 的硬上限）
        "dropped_by_cap": {k: v - _KEEP_PER_KIND for k, v in counts.items() if v > _KEEP_PER_KIND},
        # 默认渲染是否被**字数**预算截过
        "char_trimmed": shown != roomy,
    }
