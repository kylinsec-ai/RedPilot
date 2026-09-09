"""FastAPI 控制台：平台转发 / VPN / AI 解题 / Agent 舰队 / 派单。

复用 Django 版业务逻辑：
- tsecbench 平台层（store / service / vpn）
- tsecweb.agent（舰队控制、派单队列、worker 战绩解析）
- tsecweb.solver（LLM 客户端 + flag 提取）
"""

from __future__ import annotations

import json
import sys
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
from .solver import SYSTEM_PROMPT, ask_llm, extract_flags  # noqa: E402

from .cfg import get_cfg, remote_config  # noqa: E402
from .session import mark_dirty  # noqa: E402


def _remote_request(base: str, token: str, path: str, method: str = "GET", body: dict | None = None):
    """转发到远端平台。"""
    url = base + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"BENCHMARK_TOKEN": token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("message", "")
        except Exception:
            pass
        raise APIError(exc.code, "remote_error", f"远端平台错误: {detail or exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise APIError(502, "remote_unreachable", f"远端平台不可达: {exc}") from exc


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
        result = _remote_request(
            remote[0], remote[1], f"/openapi/v1/challenges/hint?unique_code={unique_code}"
        )
        remote_state = session.setdefault("console_remote_state", {}).setdefault(unique_code, {})
        remote_state["hint"] = result.get("hint")
        remote_state["hint_viewed"] = True
        mark_dirty_holder = None  # 由调用方标记
        return result
    return service.hint(_resolve_token(session), unique_code)


def submit_flag(session: dict, unique_code: str, flag: str):
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
    """整题自动流水线：启动 → 提示 → 多轮 AI 解题 →（可选）关闭。"""
    from datetime import datetime

    cfg = get_cfg(session)
    logs = []

    def log(log_type, text):
        logs.append({"time": datetime.now().strftime("%H:%M:%S"), "type": log_type, "text": text})

    brief = challenge_brief(session, code)
    if brief["container_status"] != "available":
        try:
            started = start_challenge(session, code)
            log("api", f"容器已就绪: {', '.join(started.get('container_addr', []))}")
        except APIError as exc:
            log("error", f"启动容器失败 [{exc.code}]: {exc.message}")
            return {"logs": logs, "completed": False}

    completed = brief["completed"]
    if cfg["useHint"] and not brief["hint_viewed"] and not completed:
        try:
            hint = get_hint(session, code)
            log("info", f"已获取提示（后续提交将按比例扣分）: {hint.get('hint') or '(无提示)'}")
        except APIError as exc:
            log("error", f"获取提示失败 [{exc.code}]: {exc.message}")

    max_rounds = max(1, cfg["maxRounds"])
    for round_index in range(1, max_rounds + 1):
        if completed:
            break
        log("info", f"--- 第 {round_index}/{max_rounds} 轮 AI 解题 ---")
        try:
            result = run_ai_round(session, code)
        except APIError as exc:
            log("error", f"LLM 调用失败: {exc.message}")
            break
        logs.extend(result["logs"])
        completed = result["completed"]
        if not result["made_progress"]:
            log("warn", "本轮无新进展，提前停止")
            break

    if cfg["autoClose"] and brief["container_status"] != "stopped":
        try:
            close_challenge(session, code)
            log("success", "容器已关闭，资源已释放")
        except APIError as exc:
            log("error", f"关闭容器失败 [{exc.code}]: {exc.message}")
    log("info", "AI 解题完成" if completed else f"已停止（{max_rounds} 轮内未通关）")
    return {"logs": logs, "completed": completed}


# ── 题目概要（供 AI 上下文）──────────────────────────

def challenge_brief(session: dict, code: str) -> dict:
    remote = remote_config(session)
    if remote:
        base, token = remote
        rows = _remote_request(base, token, "/openapi/v1/challenges")
        item = next((x for x in rows if x.get("unique_code") == code), None)
        if item is None:
            raise APIError(404, "challenge_not_found", "Challenge not found")
        remote_state = session.setdefault("console_remote_state", {}).setdefault(code, {})
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
            "hint": remote_state.get("hint"),
            "hint_viewed": bool(remote_state.get("hint_viewed")),
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


def build_context(brief: dict, history: dict) -> list[dict]:
    accepted = history.get("accepted", [])
    rejected = history.get("rejected", [])
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
        "【历史提交】",
        "\n".join(f"- {f} (正确)" for f in accepted) if accepted else "- (暂无正确提交)",
        "\n".join(f"- {f} (错误，不要重复)" for f in rejected) if rejected else "",
        "",
        "请分析题目并只输出候选 flag 的 JSON 数组。",
    ]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def run_ai_round(session: dict, code: str) -> dict:
    """一轮 AI 解题（LLM 盲猜，推理型题辅助）。"""
    from datetime import datetime

    cfg = get_cfg(session)
    history = session.setdefault("console_ai_history", {}).setdefault(code, {"accepted": [], "rejected": []})
    logs = []

    def log(log_type, text):
        logs.append({"time": datetime.now().strftime("%H:%M:%S"), "type": log_type, "text": text})

    brief = challenge_brief(session, code)
    content = ask_llm(cfg, build_context(brief, history))
    candidates = extract_flags(content)
    if not candidates:
        log("error", "LLM 未返回候选 flag，本轮跳过")
        return {"candidates": [], "results": [], "completed": brief["completed"], "logs": logs, "made_progress": False}

    log("info", f"LLM 给出 {len(candidates)} 个候选: {', '.join(candidates)}")
    results = []
    made = 0
    completed = brief["completed"]
    for flag in candidates:
        try:
            res = submit_flag(session, code, flag)
            results.append({
                "flag": flag,
                "correct": res.get("correct"),
                "awarded": res.get("awarded", 0),
                "cumulative_score": res.get("cumulative_score", 0),
                "correct_flag_count": res.get("correct_flag_count", 0),
                "total_flag_count": res.get("total_flag_count", 0),
            })
            if res.get("correct"):
                made += 1
                if flag not in history["accepted"]:
                    history["accepted"].append(flag)
                log("success", f"√ 正确: {flag} (+{res.get('awarded', 0)}) 累计 {res.get('cumulative_score', 0)}")
            else:
                if flag not in history["rejected"]:
                    history["rejected"].append(flag)
                log("error", f"× 错误: {flag}")
            if res.get("correct_flag_count", 0) >= res.get("total_flag_count", 0):
                completed = True
                log("success", "题目已通关！")
                break
        except APIError as exc:
            results.append({"flag": flag, "error": exc.code})
            log("error", f"提交失败 [{exc.code}]: {exc.message}")
    return {
        "candidates": candidates,
        "results": results,
        "completed": completed,
        "flag_count": brief["flag_count"],
        "logs": logs,
        "made_progress": made > 0,
    }