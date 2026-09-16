#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B49：worker-1「他管」层 —— 舰队外部监督者（动作面被架构限死为一种）。

为什么 worker-1 只能做这么少（改之前先读完）：

  * worker-1 容器内**没有 docker socket，也没有 docker 二进制**（实测：
    /var/run/docker.sock 不存在、command -v docker 为空）。它物理上不可能
    重启任何容器 —— 这是刻意保留的性质。宿主上曾经有个 supervisor.py 正是
    死在 `docker restart worker-1`：worker-1 是 worker-2/3 的 netns 提供者
    （compose 里 network_mode: service:worker-1），重启它 = 同时打断两个
    解题 worker 的网络。危险能力必须从架构上根除，而不是靠纪律。
  * 所以「他管」的全部能力面 = **一个动作**：
        协作式热重载标记   touch <workdir>/.reload.wid{N}
    被监督者自己的 _reload_watch 在**会话边界收尾后**才 exit 86，
    restart:on-failure 拉起新码 —— 零孤儿靶场实例、零网络中断。
    换句话说：worker-1 只能「请求对方重启」，不能「替对方重启」。
  * 本模块**刻意不 import benchmark_driver**（避免模块级副作用、避免把
    driver 的依赖拖进来），只读共享卷里的 status/worker-*.json ——
    那几个文件由驱动用 os.replace 原子写，读方不会看到半写 JSON。

两条判据（都只看 status 文件，不查平台 API；查平台在 409/网络抖动时会误判）：

  A. 心跳停（硬）—— _heartbeat_loop 是**独立守护线程**每 30s 一跳，且
     _await_task 在轮询循环里也补跳，所以 last_beat 陈旧 = 整个进程
     wedge/死了，而不是「正忙」。阈值 ADAPTER_SUPERVISE_STALE（默认 180s
     = 连丢 6 跳）。
  B. 空转（软）—— code/sessions/solved 三个进度量长时间全无变化。
     形态 B1：solving_active=True → 会话卡死（此时心跳往往还是新鲜的，
     A 抓不到，这是 B 存在的唯一理由）；形态 B2：solving_active=False
     → 调度侧卡住不接题。阈值 ADAPTER_SUPERVISE_IDLE（默认 1500s）。

  ⚠️ B 有一个**同伴判据**（防误杀的关键）：只有当「目标 worker 停住，而
  同队另一个解题 worker 仍在推进」时才动手。理由是卡死是**离群**事件；
  全队一起停更可能是任务终态 / 平台无题可派 —— 那属于正常等待，不是故障。
  没有这条，每轮任务结束后都会反复热重载两个健康 worker，纯属自伤。

安全阀：
  * 绝不碰 wid0（自己）。
  * 启动宽限 ADAPTER_SUPERVISE_GRACE（默认 600s）—— 避免 w1 刚起来时
    读到上一轮遗留的陈旧 status 就动手。
  * 每 worker 冷却 ADAPTER_SUPERVISE_COOLDOWN（默认 1800s）。
  * 动作留痕到 work/_supervise.json（网页可读）。
  * 任何异常一律放弃本轮（fail-safe）：宁可不动作，也不误杀正在解题的 worker。

总开关 ADAPTER_SUPERVISE=1。
"""

from __future__ import annotations

import json
import os
import threading
import time

__all__ = ["supervise_tick", "run_forever", "mark_driver_side",
           "reset_monitor_baseline", "DEFAULTS"]

_TAG = "[SUPERVISE]"

# ``_supervise.json`` deliberately survives worker-1 replacement so the web
# console can retain a small action audit.  The process-local marker below is
# therefore the only reliable indication that a *new* monitor process has
# taken over the same shared volume.
_DRIVER_BASELINE_LOCK = threading.Lock()
_DRIVER_BASELINE_WORKDIR: str | None = None

# 解题 worker 的 wid（monitor=wid0 永不在此列，物理上也不许碰）
_SOLVER_WIDS = (1, 2)


def _env_f(key: str, default: float) -> float:
    try:
        v = os.environ.get(key, "")
        return float(v) if str(v).strip() else default
    except (TypeError, ValueError):
        return default


DEFAULTS = {
    # 总开关：0 时 supervise_tick 直接返回，一行日志都不写
    "on": os.environ.get("ADAPTER_SUPERVISE", "1") == "1",
    # w1 侧启动宽限：这段时间内只观察不动手
    "grace": _env_f("ADAPTER_SUPERVISE_GRACE", 600.0),
    # A：last_beat 陈旧阈值
    "stale": _env_f("ADAPTER_SUPERVISE_STALE", 180.0),
    # B：进度量不变阈值
    "idle": _env_f("ADAPTER_SUPERVISE_IDLE", 1500.0),
    # B 的同伴活跃窗口：同伴在这个窗口内进度有变化才算「仍在推进」
    "peer_window": _env_f("ADAPTER_SUPERVISE_PEER_WINDOW", 900.0),
    # 每 worker 两次动作之间的最小间隔
    "cooldown": _env_f("ADAPTER_SUPERVISE_COOLDOWN", 1800.0),
    # driver 侧线程与 standalone 进程共用的 tick 间隔
    "interval": _env_f("ADAPTER_SUPERVISE_INTERVAL", 30.0),
}


# ── 小工具：读必须容错，写必须原子 ────────────────────────

def _safe_load(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _atomic_dump(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _state_path(workdir: str) -> str:
    return os.path.join(workdir, "_supervise.json")


def _status_path(workdir: str, wid) -> str:
    return os.path.join(workdir, "status", f"worker-{wid}.json")


def _load_state(workdir: str) -> dict:
    st = _safe_load(_state_path(workdir))
    if not isinstance(st, dict):
        st = {}
    if not isinstance(st.get("workers"), dict):
        st["workers"] = {}
    if not isinstance(st.get("actions"), list):
        st["actions"] = []
    if not isinstance(st.get("driver_side_ts"), (int, float)):
        st["driver_side_ts"] = 0
    st.setdefault("first_tick_ts", 0)
    return st


def reset_monitor_baseline(workdir: str, *, now=None, driver_side: bool = False) -> bool:
    """Start a new supervisor observation epoch without touching workers.

    ``work/_supervise.json`` is shared and survives a worker-1 rebuild.  Its
    old ``first_tick_ts`` and per-worker signatures must not be inherited by a
    newly started monitor: doing so skips the configured grace period and can
    interpret old status snapshots as an immediately stale worker.  Resetting
    this local observation baseline only affects the supervisor's own
    bookkeeping; it neither restarts containers nor changes any solver state.

    The historical action list remains available for operators.  Clearing the
    per-worker records also drops stale action cooldowns and peer-progress
    timestamps, which are meaningful only within one monitor process.
    """
    now = time.time() if now is None else now
    try:
        os.makedirs(workdir, exist_ok=True)
        state = _load_state(workdir)
        try:
            generation = max(0, int(state.get("baseline_generation", 0) or 0))
        except (TypeError, ValueError):
            generation = 0
        state["baseline_generation"] = generation + 1
        state["baseline_reset_ts"] = now
        state["first_tick_ts"] = now
        state["workers"] = {}
        # A standalone observer must not inherit a departed driver's fresh
        # heartbeat and retire immediately.  The driver-side caller writes its
        # own fresh timestamp as part of the same atomic state update.
        state["driver_side_ts"] = now if driver_side else 0
        _atomic_dump(_state_path(workdir), state)
        return True
    except Exception:
        return False


def _sig_of(st: dict) -> str:
    """进度指纹：题号 + 会话数 + 已解数。三者任一变化都算「还在动」。"""
    return "{}|{}|{}".format(
        str(st.get("current_code") or ""),
        st.get("sessions", 0),
        st.get("challenges_solved", 0),
    )


def _activity_age(st: dict, rec: dict, now: float) -> float:
    """返回最近一次真实求解活动的年龄，兼容旧版 status 文件。"""
    if bool(st.get("solving_active")):
        ts = st.get("last_activity")
        if isinstance(ts, (int, float)) and ts > 0:
            return max(0.0, now - float(ts))
    ts = rec.get("sig_ts", now)
    return max(0.0, now - float(ts))


# ── 核心：一轮监督 ────────────────────────────────────────

def supervise_tick(workdir: str, log, *, now=None) -> list:
    """跑一轮监督，返回本轮采取的动作列表（便于单测断言）。

    只做两件事：读 status、必要时 touch 热重载标记。任何异常都吞掉并放弃
    本轮 —— 监督者自身出问题绝不能反过来把健康的 worker 拖下水。
    """
    if not DEFAULTS["on"]:
        return []
    now = time.time() if now is None else now
    actions = []

    try:
        state = _load_state(workdir)
        if not state["first_tick_ts"]:
            state["first_tick_ts"] = now
        workers = state["workers"]

        # 启动宽限：w1 刚起来时 work/ 里可能还躺着上一轮的陈旧状态
        if now - state["first_tick_ts"] < DEFAULTS["grace"]:
            _atomic_dump(_state_path(workdir), state)
            return []

        # ① 先无条件刷新所有解题 worker 的进度指纹（同伴判据要用最新值）
        #    两个时间戳含义不同，别合并：
        #      sig_ts        —— 当前这个指纹**首次被观测到**的时刻（判「停住多久」）
        #      sig_change_ts —— 指纹**真的变过**的最近时刻（判「同伴还在推进」）
        #    合成一个会让「刚建立基线」被误当成「刚推进过」，进而让两个同时
        #    停住的 worker 互相把对方当作活跃同伴 —— 同伴判据就整个失效了。
        snaps = {}
        for wid in _SOLVER_WIDS:
            st = _safe_load(_status_path(workdir, wid))
            if not isinstance(st, dict):
                continue          # 读不到就跳过，绝不当成「卡死」来处理
            sig = _sig_of(st)
            rec = workers.setdefault(str(wid), {})
            if rec.get("sig") != sig:
                first_sight = "sig" not in rec
                rec["sig"] = sig
                rec["sig_ts"] = now
                if not first_sight:
                    rec["sig_change_ts"] = now
            rec.setdefault("sig_ts", now)
            rec.setdefault("sig_change_ts", 0)
            rec["last_seen_ts"] = now
            snaps[wid] = st

        # ② 逐个判据
        for wid, st in sorted(snaps.items()):
            rec = workers[str(wid)]
            last_action = rec.get("last_action_ts", 0)
            if now - last_action < DEFAULTS["cooldown"]:
                continue

            reason = detail = None

            # ── A：心跳停。进程 wedge/死，与「忙不忙」无关 ──
            lb = st.get("last_beat")
            if isinstance(lb, (int, float)) and (now - lb) > DEFAULTS["stale"]:
                reason = "beat-stale"
                detail = "last_beat %.0fs 前（阈值 %.0fs）" % (
                    now - lb, DEFAULTS["stale"])

            # ── B：空转。必须同时满足「本机停住」+「同伴仍在推进」──
            else:
                sig_age = _activity_age(st, rec, now)
                if sig_age > DEFAULTS["idle"]:
                    peer = _first_progressing_peer(workers, wid, now)
                    if peer is None:
                        # 全队一起停 = 更像任务终态/无题可派，不是卡死。
                        # 只留痕不动手，避免任务结束后反复重载健康 worker。
                        _log_line(log, "debug",
                                  "%s w%d 活动 %.0fs 无变化，但无同伴在推进 "
                                  "— 判为全队等待，不动作", _TAG, wid, sig_age)
                    else:
                        active = bool(st.get("solving_active"))
                        reason = "stuck-session" if active else "stuck-idle"
                        detail = ("活动 %.0fs 无变化（code=%s sessions=%s "
                                  "solved=%s, active=%s），同伴 w%d 仍在推进"
                                  % (sig_age, st.get("current_code") or "-",
                                     st.get("sessions", 0),
                                     st.get("challenges_solved", 0),
                                     active, peer))

            if reason is None:
                continue

            # ── 唯一允许的动作：写协作式热重载标记 ──
            try:
                marker = os.path.join(workdir, f".reload.wid{wid}")
                with open(marker, "a"):
                    os.utime(marker, None)
            except Exception as e:
                _log_line(log, "warning",
                          "%s w%d 请求热重载失败（标记写不进去）: %s",
                          _TAG, wid, e)
                continue

            rec["last_action_ts"] = now
            rec["last_action_reason"] = reason
            rec["last_action_detail"] = detail
            act = {"ts": now, "wid": wid, "reason": reason, "detail": detail}
            actions.append(act)
            state["actions"] = (state["actions"] + [act])[-40:]
            _log_line(log, "warning",
                      "%s w%d 判定【%s】→ 已请求协作式热重载 "
                      "(.reload.wid%d)：%s", _TAG, wid, reason, wid, detail)

        _atomic_dump(_state_path(workdir), state)
    except Exception as e:
        _log_line(log, "debug", "%s tick 异常已忽略: %s", _TAG, e)
    return actions


def _first_progressing_peer(workers: dict, wid, now) -> int:
    """同队另一个解题 worker 里，有没有人在 peer_window 内**真的变过进度**。

    只看 sig_change_ts（真变化），不看 sig_ts（首见时刻）—— 后者会把
    「刚建立基线」误判成「刚推进过」，让两个同时停住的 worker 互相当同伴。
    """
    for other in _SOLVER_WIDS:
        if other == wid:
            continue
        rec = workers.get(str(other)) or {}
        ts = rec.get("sig_change_ts")
        if isinstance(ts, (int, float)) and ts > 0 and (now - ts) <= DEFAULTS["peer_window"]:
            return other
    return None


def _log_line(log, level: str, fmt: str, *args) -> None:
    fn = getattr(log, level, None) or getattr(log, "info", None)
    if fn is None:
        return
    try:
        fn(fmt, *args)
    except Exception:
        pass


# ── 与 driver 侧 thread 的互认 ────────────────────────────
# driver 的 _monitor_loop 每轮调 supervise_tick 后会打这个戳。standalone
# 进程看到戳新鲜就自行退出 —— 保证「同一时刻只有一个监督者在动手」。

def mark_driver_side(workdir: str, *, now=None) -> None:
    """Publish the in-driver supervisor heartbeat.

    The first call made by each monitor *process* also establishes a fresh
    observation baseline.  ``_monitor_loop`` calls this before every
    ``supervise_tick``; a rebuilt worker-1 therefore receives its full grace
    period even though the shared state file was created by the prior
    container.  Subsequent calls only refresh the heartbeat and do not extend
    that grace indefinitely.
    """
    global _DRIVER_BASELINE_WORKDIR
    now = time.time() if now is None else now
    normalized = os.path.abspath(workdir)
    with _DRIVER_BASELINE_LOCK:
        if _DRIVER_BASELINE_WORKDIR != normalized:
            if reset_monitor_baseline(workdir, now=now, driver_side=True):
                _DRIVER_BASELINE_WORKDIR = normalized
            return
        try:
            state = _load_state(workdir)
            state["driver_side_ts"] = now
            _atomic_dump(_state_path(workdir), state)
        except Exception:
            pass


def _driver_side_alive(workdir: str) -> bool:
    st = _safe_load(_state_path(workdir))
    if not isinstance(st, dict):
        return False
    ts = st.get("driver_side_ts")
    return isinstance(ts, (int, float)) and (time.time() - ts) < 180


# ── standalone 入口（driver 尚未重载时先顶班）─────────────

class _FileLog:
    """docker exec -d 起的进程没有 stdout 去处，落到 work/_supervise.log。"""

    def __init__(self, path: str):
        self.path = path

    def _emit(self, level: str, fmt: str, *args) -> None:
        try:
            msg = fmt % args if args else fmt
        except Exception:
            msg = str(fmt)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("%s %-7s %s\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), level, msg))
        except Exception:
            pass

    def info(self, fmt, *a):
        self._emit("INFO", fmt, *a)

    def warning(self, fmt, *a):
        self._emit("WARN", fmt, *a)

    def debug(self, fmt, *a):
        self._emit("DEBUG", fmt, *a)


def run_forever(workdir: str) -> None:
    """standalone 常驻循环。driver 侧监督上线后自行退休。"""
    log = _FileLog(os.path.join(workdir, "_supervise.log"))
    log.info("%s standalone 监督进程启动 (workdir=%s, 间隔 %.0fs)",
             _TAG, workdir, DEFAULTS["interval"])
    # A standalone observer can also be started after a monitor replacement.
    # If the driver is already alive, leave its baseline alone and retire; if
    # it is not, discard stale observation timestamps before making any
    # decision.  This remains bookkeeping-only and cannot restart a container.
    if _driver_side_alive(workdir):
        log.info("%s driver 侧监督已上线 — standalone 不接管", _TAG)
        return
    if not reset_monitor_baseline(workdir):
        log.debug("%s standalone 初始基线重置失败 — 保持 fail-safe", _TAG)
    while True:
        try:
            if _driver_side_alive(workdir):
                log.info("%s driver 侧监督已上线 — standalone 自行退休退出",
                         _TAG)
                return
            supervise_tick(workdir, log)
        except Exception as e:
            log.debug("%s loop 异常已忽略: %s", _TAG, e)
        time.sleep(max(5.0, DEFAULTS["interval"]))


def main() -> int:
    import argparse
    import logging

    ap = argparse.ArgumentParser(description="worker-1 他管层（B49）")
    ap.add_argument("--workdir", default=os.getenv("ADAPTER_WORKDIR", "/work"))
    ap.add_argument("--once", action="store_true",
                    help="只跑一轮就退出（自检/排查用）")
    a = ap.parse_args()

    if a.once:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s")
        acts = supervise_tick(a.workdir, logging.getLogger("supervise"))
        print(json.dumps(acts, ensure_ascii=False, indent=2))
        return 0

    run_forever(a.workdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
