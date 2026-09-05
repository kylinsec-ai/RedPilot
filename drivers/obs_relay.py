"""obs_relay — worker 最小观测中继:订阅 LiveBus,把 run 生命周期 / transcript 事件行 /
live 快照 / roster / ping 推送到本地观测平台(obs)的 POST /api/internal/*。

零司机语义改动:只用 LiveBus 帧 + transcript 文件字节续读驱动;OBSERVABILITY_URL 未设
则整体禁用(零行为变化)。事件与 run_close 走同一 FIFO,保证同 worker 全序:
run_close 恒在事件之后落库;平台幂等(UNIQUE run_id+seq)兜底重发。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid

import httpx

log = logging.getLogger("adapter.obs_relay")

_LIVE_CADENCE = 0.6     # live 快照推送节拍(≥0.5s,与 _live_set 1s 节流同量级)
_TAIL_CADENCE = 1.0     # transcript 字节续读节拍
_PING_CADENCE = 30.0    # 心跳(平台 stale_after=150s,余量 5x)
_ROSTER_CADENCE = 60.0  # roster 快照全量(与 worker 轮询同节奏)
_BATCH_MAX = 64         # 单批事件行数
_FLAG_FILES = ("FLAG", "flag.txt", "FLAG.txt")
_FLAG_MAX_LINES = 50
_ERR_MAX = 2000


class _Fifo:
    """无界 FIFO + 控制消息;单发送线程顺序 POST(与事件同队,保单 worker 全序)。"""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._pending_live: dict | None = None  # 最新 live 槽:同刻多帧只留最新
        self._lock = threading.Lock()

    def put(self, msg: dict) -> None:
        self._q.put(msg)

    def put_live(self, frame: dict) -> None:
        """live 槽合并:队列里若已有未发 live,替换为最新(绝不堆积)。"""
        with self._lock:
            self._pending_live = {"t": "live", "worker_id": frame.get("worker_id"),
                                  "kind": frame.get("kind", "lifecycle"),
                                  "frame": frame, "_stamp": time.monotonic()}

    def ship_live(self) -> bool:
        """引擎节拍把最新槽移交发送队列;未发帧静默丢旧,绝不堆积。"""
        with self._lock:
            if self._pending_live is None:
                return False
            self._q.put(self._pending_live)
            self._pending_live = None
        return True

    def depth(self) -> int:
        return self._q.qsize()


class ObsRelay:
    """单引擎线程:LiveBus 订阅帧驱动状态机;顺带跑 live/tail/ping/roster 节拍。
    sender 线程负责 HTTP;平台 down 时事件留在 FIFO(无界),恢复后续传零丢失。"""

    def __init__(self, live, bus, workdir: str, url: str, token: str | None,
                 worker_id: str = "worker-1"):
        self._live = live
        self._bus = bus
        self._workdir = workdir
        self._url = url.rstrip("/")
        self._token = token
        self._worker_id = (worker_id or "worker-1").strip() or "worker-1"
        self._fifo = _Fifo()
        self._run: dict | None = None          # {run_id, code, model, path, base, seq, emitted}
        self._file_lock = threading.Lock()     # 序列化文件续读(引擎 tick 与 driver flush 共用)
        self._poller = None                    # drivers.roster.RosterPoller(惰性起)
        self._stop = threading.Event()
        self._sent = threading.Event()         # sender 有进展(测试/健康用)

    # ── 生命周期 ──

    def start(self) -> None:
        t = threading.Thread(target=self._engine, daemon=True, name="obs-relay")
        threading.Thread(target=self._sender, daemon=True, name="obs-sender").start()
        t.start()
        log.info("obs relay armed: url=%s worker=%s", self._url, self._worker_id)

    def flush_run(self) -> None:
        """driver 在 compress_transcript 前调用:同步把当前 run 未读字节排干入队(防压缩吞行)。"""
        run = self._run
        if not run or not run.get("path"):
            return
        try:
            with self._file_lock:
                self._tail_once(run, force=True)
        except Exception:
            log.debug("obs flush failed", exc_info=True)

    # ── 事件行解析(与压缩同语义:message_update 丢弃;原文行入库) ──

    def _tail_once(self, run: dict, force: bool = False) -> bool:
        """读 path 自 run['base'] 的新行;过滤非 JSON / message_update;返回是否前进。"""
        path = run["path"]
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        base = run["base"]
        if size < base:
            if run.get("emitted"):
                # 压缩只删 message_update(已过滤不送)且 flush 先行 → 直接对齐新 EOF 零丢失
                run["base"] = size
            else:
                # 首次送行前收缩 = 本 run 起点的 >5MB 截断:文件已换新,从头读(内容只属本 run)
                run["base"] = 0
            base = run["base"]
        if size == base and not force:
            return False
        rows: list[dict] = []
        try:
            # 二进制迭代:文本模式迭代期间 f.tell() 被禁用(next() 调用会抛 OSError)
            with open(path, "rb") as f:
                f.seek(base)
                for raw in f:
                    run["base"] = f.tell()
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        typ = ev.get("type", "")
                    except Exception:
                        continue
                    if typ == "message_update":
                        continue
                    rows.append({"seq": run["seq"], "type": typ,
                                 "ts": None, "payload": line})
                    run["seq"] += 1
                    run["emitted"] = True
                    if len(rows) >= _BATCH_MAX:
                        self._fifo.put({"t": "events", "run": run, "rows": rows})
                        rows = []
        except OSError:
            return False
        if rows:
            self._fifo.put({"t": "events", "run": run, "rows": rows})
        return bool(run["emitted"])

    # ── run 状态机(纯帧驱动) ──

    def _on_frame(self, frame: dict) -> None:
        phase = str(frame.get("phase") or "")
        code = str(frame.get("challenge_code") or "")
        worker = str(frame.get("worker_id") or self._worker_id)
        run = self._run

        if run and phase in ("starting", "solving", "submitting") and code:
            if run["code"] != code:
                # 换题帧没走 closing?防御:先关旧 run(平台侧另有 idle/switch 守卫)
                self._close_run(run, frame, note="switch without close")

        if phase == "starting" and code and (run is None or run["code"] != code
                                             or run.get("closed")):
            self._run = {"run_id": uuid.uuid4().hex, "code": code, "model": "",
                         "path": None, "base": 0, "seq": 0, "emitted": False,
                         "worker_id": worker}
            log.info("obs run open: %s code=%s", self._run["run_id"], code)
            return

        if phase == "solving" and run and code and run["code"] == code and not run["path"]:
            # 求解帧到达即锚定文件起点(pi spawn 紧随其后;延迟损失仅毫秒级)
            path = str(frame.get("transcript_path") or "")
            if path:
                run["path"] = path
                run["base"] = os.path.getsize(path) if os.path.isfile(path) else 0
                run["model"] = str(frame.get("model") or run.get("model") or "")
            return

        if (phase == "closing" or (phase == "idle" and run and not run.get("closed"))):
            if run and not run.get("closed"):
                self._close_run(run, frame)

    def _close_run(self, run: dict, frame: dict, note: str = "") -> None:
        """drain 全部事件(同步续读到 EOF 并入队)后 POST run_close —— FIFO 保证全序。"""
        run["closed"] = True
        try:
            with self._file_lock:
                self._tail_once(run, force=True)
        except Exception:
            log.debug("obs close drain failed", exc_info=True)
        error = str(frame.get("error") or "")
        accepted = int(frame.get("accepted") or 0)
        flags_found = int(frame.get("flags_found") or 0)
        if error and "start failed" in error:
            status = "failed"
        elif error:
            status = "failed"
        elif accepted > 0 and accepted == flags_found and flags_found > 0:
            status = "solved"
        else:
            status = "done"
        path = run.get("path") or ""
        flags = self._read_flags(path)
        self._fifo.put({
            "t": "close", "run": run,
            "body": {"run_id": run["run_id"], "worker_id": run.get("worker_id"),
                     "status": status, "error": error[:_ERR_MAX] or None,
                     "turns": int(frame.get("turns") or 0) or None,
                     "flags_found": flags_found or None,
                     "flags_accepted": flags or None,
                     "ended_at": frame.get("updated_at") or None},
        })
        log.info("obs run close: %s code=%s status=%s%s", run["run_id"], run["code"],
                 status, f" note={note}" if note else "")

    def _read_flags(self, transcript_path: str) -> list[str]:
        if not transcript_path:
            return []
        d = os.path.dirname(transcript_path)
        for name in _FLAG_FILES:
            p = os.path.join(d, name)
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    lines = [ln.strip() for ln in f if ln.strip()]
                return lines[:_FLAG_MAX_LINES]
            except OSError:
                continue
        return []

    # ── 引擎与发送 ──

    def _engine(self) -> None:
        q = self._bus.subscribe()
        try:
            # 注册行:worker 上线即一份 idle 快照(平台 live_state 建档 + /api/status 有值)
            snap = dict(self._live.snapshot()) if self._live else {}
            snap.setdefault("worker_id", self._worker_id)
            self._fifo.put_live({**snap, "kind": "lifecycle"})
            next_live = time.monotonic() + _LIVE_CADENCE
            next_tail = time.monotonic() + _TAIL_CADENCE
            next_ping = time.monotonic() + _PING_CADENCE
            next_roster = time.monotonic() + _ROSTER_CADENCE
            while not self._stop.is_set():
                try:
                    frame = q.get(timeout=0.2)
                    if frame is not None:
                        try:
                            self._on_frame(frame)
                        except Exception:
                            log.exception("obs frame handling error")
                        self._fifo.put_live(frame)  # 每帧进最新槽(节拍发送)
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now >= next_live:
                    next_live = now + _LIVE_CADENCE
                    self._fifo.ship_live()
                if now >= next_tail:
                    next_tail = now + _TAIL_CADENCE
                    run = self._run
                    if run and run.get("path") and not run.get("closed") \
                            and self._fifo.depth() < 500:  # 平台长 down:暂停续读,队列不膨胀
                        try:
                            with self._file_lock:
                                self._tail_once(run)
                        except Exception:
                            log.debug("obs tail error", exc_info=True)
                if now >= next_ping:
                    next_ping = now + _PING_CADENCE
                    self._fifo.put({"t": "ping", "worker_id": self._worker_id})
                if now >= next_roster:
                    next_roster = now + _ROSTER_CADENCE
                    try:
                        self._roster_tick()
                    except Exception:
                        log.exception("obs roster tick error")
        finally:
            try:
                self._bus.unsubscribe(q)
            except Exception:
                pass

    def _roster_tick(self) -> None:
        if self._poller is None:
            try:
                from drivers.roster import RosterPoller
                self._poller = RosterPoller(self._workdir)
                self._poller.start()
            except Exception as e:
                log.warning("obs roster poller unavailable: %s", e)
                return
        try:
            snap = self._poller.snapshot()
            if snap:
                self._fifo.put({"t": "roster", "worker_id": self._worker_id,
                                "snapshot": snap})
        except Exception as e:
            log.warning("obs roster snapshot failed: %s", e)

    def _sender(self) -> None:
        backoff = 2.0
        warned: dict[str, float] = {}
        with httpx.Client(timeout=5.0) as client:
            headers = {"X-Observability-Token": self._token} if self._token else {}
            while not self._stop.is_set():
                try:
                    msg = self._fifo._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    if msg["t"] == "events":
                        r = client.post(self._url + "/api/internal/events",
                                        json={"run_id": msg["run"]["run_id"],
                                              "worker_id": msg["run"].get("worker_id"),
                                              "challenge_code": msg["run"]["code"],
                                              "model": msg["run"].get("model") or "",
                                              "events": msg["rows"]}, headers=headers)
                    elif msg["t"] == "live":
                        r = client.post(self._url + "/api/internal/live",
                                        json={"worker_id": msg["worker_id"],
                                              "kind": msg.get("kind", "lifecycle"),
                                              "snapshot": msg["frame"]}, headers=headers)
                    elif msg["t"] == "close":
                        r = client.post(self._url + "/api/internal/run_close",
                                        json=msg["body"], headers=headers)
                    elif msg["t"] == "roster":
                        r = client.post(self._url + "/api/internal/roster",
                                        json={"worker_id": msg["worker_id"],
                                              "snapshot": msg["snapshot"]}, headers=headers)
                    elif msg["t"] == "ping":
                        r = client.post(self._url + "/api/internal/ping",
                                        json={"worker_id": msg["worker_id"]}, headers=headers)
                    else:
                        continue
                    if r.status_code < 400:
                        backoff = 2.0
                        self._sent.set()
                        continue
                    detail = r.text[:200]
                    if detail and (time.monotonic() - warned.get(detail, 0.0)) > 60:
                        log.warning("obs POST %s HTTP %d: %s", msg["t"], r.status_code, detail)
                        warned[detail] = time.monotonic()
                except Exception as e:
                    if time.monotonic() - warned.get("exc", 0.0) > 60:
                        log.warning("obs platform unreachable (%s) — retrying, events buffered", e)
                        warned["exc"] = time.monotonic()
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 10.0)


def maybe_start_relay(live, bus, workdir: str | None = None) -> ObsRelay | None:
    """OBSERVABILITY_URL 未设返回 None —— 宿主裸跑等场景零行为变化。
    token 可选:平台侧未配 token 时返回 503(响亮),relay 记 warn + 退避重试。"""
    url = os.getenv("OBSERVABILITY_URL", "").strip().rstrip("/")
    if not url:
        log.info("obs relay disabled (OBSERVABILITY_URL unset)")
        return None
    token = os.getenv("OBSERVABILITY_TOKEN", "").strip() or None
    workdir = workdir or os.getenv("ADAPTER_WORKDIR", "/work")
    relay = ObsRelay(live, bus, workdir, url, token,
                     worker_id=os.getenv("WORKER_ID", "worker-1"))
    relay.start()
    return relay
