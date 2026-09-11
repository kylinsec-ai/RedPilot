"""obs_relay — worker 最小观测中继:订阅 LiveBus,把 run 生命周期 / transcript 事件行 /
live 快照 / roster / ping 推送到本地观测平台(obs)的 POST /api/internal/*。

零司机语义改动:只用 LiveBus 帧 + transcript 文件字节续读驱动;OBSERVABILITY_URL 未设
则整体禁用(零行为变化)。事件与 run_close 走同一 FIFO,保证同 worker 全序:
run_close 恒在事件之后落库;平台幂等(UNIQUE run_id+seq)兜底重发。
"""

from __future__ import annotations

import collections
import json
import logging
import os
import queue
import threading
import time
import uuid

import httpx

from ghost_contracts.paths import FLAG_MAX_LINES
from ghost_contracts.vocabulary import (MESSAGE_UPDATE, PHASES,
                                            RUN_CLOSE_STATUSES, strip_for_snapshot)

log = logging.getLogger("ghost_worker.relay")

# 触发"旧 run 未关先切题"防御的 phase 集(active 全集;closing/idle/done/error 走关闭分支)
ACTIVE_RUN_PHASES = tuple(p for p in PHASES if p in ("starting", "solving", "submitting"))

_LIVE_CADENCE = 0.6     # live 快照推送节拍(≥0.5s,与 _live_set 1s 节流同量级)
_TAIL_CADENCE = 1.0     # transcript 字节续读节拍
_PING_CADENCE = 30.0    # 心跳(平台 stale_after=150s,余量 5x)
_ROSTER_CADENCE = 60.0  # roster 快照全量(与 worker 轮询同节奏)
_BATCH_MAX = 64         # 单批事件行数
_ERR_MAX = 2000
# 积压上限:平台长 down 时 events/run_close 继续留队(零丢失);live 帧只占槽位
# (恒最新一帧)、续读暂停、ping/roster 队满即丢下节拍再生 —— 队列因此有界
# (put_droppable 门控可再生消息;requeue 只循环同批消息不增长)
_QUEUE_CAP = 500
# 「平台回了话却持续拒收」的重试上限(见 _sender 的失败分类)。只用于 HTTP 5xx/429
# 这类**有响应**的失败;连接异常/超时是平台 down,不限次(零丢失)。
_POISON_TRIES = 5


class _Fifo:
    """无界 FIFO + 控制消息;单发送线程顺序 POST(与事件同队,保单 worker 全序)。

    失败消息经 requeue 放回队首重试:同一 run 的 events 恒先于 run_close 送达,
    平台长时间 down 时事件留在 FIFO(无界),恢复后续传零丢失。

    队首重试的代价是队首即全局闸门 —— 一条平台**回话拒收**的毒消息会把它后面的
    live/events/ping 全堵死(平台据此 stale_after 判 worker 离线,而 worker 其实在
    正常解题,且只有重启进程一条恢复路径)。故有响应的失败按 _POISON_TRIES 设终点;
    无响应的失败(平台 down)仍不限次,两条不变量在此互不干扰。
    """

    def __init__(self) -> None:
        self._q: collections.deque = collections.deque()
        self._cv = threading.Condition()
        self._pending_live: dict | None = None  # 最新 live 槽:同刻多帧只留最新

    def put(self, msg: dict) -> None:
        with self._cv:
            self._q.append(msg)
            self._cv.notify()

    def requeue(self, msg: dict) -> None:
        """失败消息放回队首(保序重试,不落尾)。"""
        with self._cv:
            self._q.appendleft(msg)
            self._cv.notify()

    def put_droppable(self, msg: dict) -> bool:
        """可再生消息用:队满直接丢(下节拍再生),ping/roster 走此通道保队列有界。
        events/run_close 为零丢失保留无界 put(队满时续读已暂停,积压只来自
        同批待重试消息与零星 close)。"""
        with self._cv:
            if len(self._q) >= _QUEUE_CAP:
                return False
            self._q.append(msg)
            self._cv.notify()
            return True

    def put_live(self, frame: dict) -> None:
        """live 槽合并:队列里若已有未发 live,替换为最新(绝不堆积)。
        队满也保留最新帧(只占槽位不进队);ship_live 在积压排空后的下节拍补送 ——
        旧帧永不盖掉新帧,恢复后平台先看到最新 phase。
        槽内只存纯状态快照 —— 信封键(kind/ts)与带外元数据(_ 前缀)剥掉,
        契约:LiveIn.snapshot = LiveState 纯快照(读端首帧 kind 缺省 'snapshot')。"""
        with self._cv:
            self._pending_live = {"t": "live", "worker_id": frame.get("worker_id"),
                                  "kind": frame.get("kind", "lifecycle"),
                                  "frame": strip_for_snapshot(frame)}

    def ship_live(self) -> bool:
        """引擎节拍把最新槽移交发送队列;未发帧静默丢旧,绝不堆积。
        队满时保留槽位,待积压排空后的下一节拍再送。"""
        with self._cv:
            if self._pending_live is None or len(self._q) >= _QUEUE_CAP:
                return False
            self._q.append(self._pending_live)
            self._pending_live = None
            self._cv.notify()
        return True

    def get(self, timeout: float) -> dict:
        with self._cv:
            while not self._q:
                if not self._cv.wait(timeout):
                    raise queue.Empty
            return self._q.popleft()

    def depth(self) -> int:
        with self._cv:
            return len(self._q)


class ObsRelay:
    """单引擎线程:LiveBus 订阅帧驱动状态机;顺带跑 live/tail/ping/roster 节拍。
    sender 线程负责 HTTP;平台 down 时事件留在 FIFO(无界),恢复后续传零丢失。"""

    def __init__(self, live, bus, workdir: str, url: str, token: str | None,
                 worker_id: str = "worker-1", roster_poller=None):
        self._live = live
        self._bus = bus
        self._workdir = workdir
        self._url = url.rstrip("/")
        self._token = token
        self._worker_id = (worker_id or "worker-1").strip() or "worker-1"
        self._fifo = _Fifo()
        self._run: dict | None = None          # {run_id, code, model, path, base, seq, emitted}
        self._assignment_context: dict[str, str] = {}
        self._file_lock = threading.Lock()     # 序列化文件续读(引擎 tick 与 driver flush 共用)
        self._roster_poller = roster_poller    # 共享 RosterPoller(main() 注入);None → 不推 roster
        self._stop = threading.Event()
        self._sent = threading.Event()         # sender 有进展(测试/健康用)

    # ── 生命周期 ──

    def stop(self) -> None:
        """停引擎与 sender 线程(幂等;测试/进程关停用)。"""
        self._stop.set()

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

    def bind_attempt(
        self,
        *,
        evaluation_id: str | None = None,
        job_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        """把下一次 relay run 绑定到控制面的 assignment。"""

        self._assignment_context = {
            key: value
            for key, value in {
                "run_id": attempt_id,
                "evaluation_id": evaluation_id,
                "job_id": job_id,
                "attempt_id": attempt_id,
            }.items()
            if value
        }

    def clear_attempt(self) -> None:
        self._assignment_context = {}

    def send_accepted_flags(self, flags: list[str]) -> None:
        """把已接受的 flag 明文补给平台(非权威,加性,只补 runs.flags_accepted 一列)。

        为何需要:assignment 模式下 relay **有意跳过** run_close(canonical 拥有生命
        周期权威),而 flags_accepted 此前只经 run_close 写入 —— 平台主推的模式反而
        看不到已获得的 flag(/api/challenge 恒返回 [])。

        为何走独立窄端点而不是塞进 canonical 事件:canonical 会持久化进 core 的
        platform_events,而 ARCHITECTURE.md §7.1 规定 core 只存 SHA-256 不存明文。

        零丢失语义与 events 相同(走无界 put):这是小体量一次性消息,丢了就永久缺一列。
        """
        if not flags or self._stop.is_set():
            return
        run_id = ((self._run or {}).get("run_id")
                  or self._assignment_context.get("run_id"))
        if not run_id:
            log.debug("accepted flags dropped: no bound run yet")
            return
        self._fifo.put({"t": "flags", "run_id": run_id, "flags": list(flags)})

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
                    if typ == MESSAGE_UPDATE:
                        continue
                    rows.append({"seq": run["seq"], "type": typ, "payload": line})
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

        if run and phase in ACTIVE_RUN_PHASES and code:
            if run["code"] != code and not run.get("closed"):
                # 换题帧没走 closing?防御:先关旧 run(平台侧另有 idle/switch 守卫);
                # 已 closed 的 run 再关 = 重复 run_close(平台终态幂等,纯噪音),跳过
                self._close_run(run, frame, note="switch without close")

        if phase == "starting" and code and (run is None or run["code"] != code
                                              or run.get("closed")):
            self._run = {"run_id": uuid.uuid4().hex, "code": code, "model": "",
                         "path": None, "base": 0, "seq": 0, "emitted": False,
                         "worker_id": worker, **self._assignment_context}
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

        # 关闭分支与原实现一致:仅 closing/idle 关闭(done/error 只是状态更新,
        # done 帧后仍可能有 submitting/更多提交;真正收尾由 closing 帧携带 turns/accepted)
        if run and not run.get("closed") and phase in ("closing", "idle"):
            self._close_run(run, frame)

    def _close_run(self, run: dict, frame: dict, note: str = "") -> None:
        """drain 全部事件(同步续读到 EOF 并入队)后 POST run_close —— FIFO 保证全序。

        assignment 模式(run 绑定 attempt_id):生命周期归 canonical outbox
        (attempt.completed)所有,此处只排干事件不发 run_close,避免 relay 的
        乐观 done 抢赢 canonical 的 authoritative interrupted(首写获胜导致
        仪表板把中断显示成 done)。legacy 模式(无 attempt_id)仍由 relay 关闭。
        """
        run["closed"] = True
        try:
            with self._file_lock:
                self._tail_once(run, force=True)
        except Exception:
            log.debug("obs close drain failed", exc_info=True)
        if run.get("attempt_id"):
            log.info("obs run close skipped (canonical owns lifecycle): %s code=%s%s",
                     run["run_id"], run["code"], f" note={note}" if note else "")
            return
        error = str(frame.get("error") or "")
        accepted = int(frame.get("accepted") or 0)
        flags_found = int(frame.get("flags_found") or 0)
        if error:
            status = "failed"
        elif accepted > 0:
            status = "solved"
        else:
            status = "done"
        assert status in RUN_CLOSE_STATUSES  # 契约:close 状态 ⊆ contracts 终态集
        path = run.get("path") or ""
        raw = frame.get("_accepted_flags")
        if isinstance(raw, list):
            # driver 随 closing 帧附带的平台确认 accepted 明文(FLAG 文件含被拒候选)
            flags = [str(f) for f in raw][:FLAG_MAX_LINES]
        else:
            flags = self._read_flags(path)  # 旧帧/测试兜底:回退 FLAG 文件
        self._fifo.put({
            "t": "close", "run": run,
            "body": {"run_id": run["run_id"], "worker_id": run.get("worker_id"),
                      "evaluation_id": run.get("evaluation_id"),
                      "job_id": run.get("job_id"),
                      "attempt_id": run.get("attempt_id"),
                      "status": status, "error": error[:_ERR_MAX] or None,
                     "turns": int(frame.get("turns") or 0) or None,
                     "flags_found": flags_found or None,
                     "flags_accepted": flags or None,
                     "ended_at": frame.get("updated_at") or None},
        })
        log.info("obs run close: %s code=%s status=%s%s", run["run_id"], run["code"],
                 status, f" note={note}" if note else "")

    def _read_flags(self, transcript_path: str) -> list[str]:
        """回退读 FLAG 候选文件(读法单源:drivers.roster.read_flag_lines)。"""
        if not transcript_path:
            return []
        from .roster import read_flag_lines
        return read_flag_lines(os.path.dirname(transcript_path))

    # ── 引擎与发送 ──

    def _engine(self) -> None:
        q = self._bus.subscribe()
        try:
            # 注册行:worker 上线即一份 idle 快照(平台 live_state 建档 + /api/status 有值)。
            # 必须立即 ship —— 若只进 0.6s 节拍槽,重启后首个 'starting' 帧会先到平台,
            # 平台崩溃守卫将看不到 idle 帧:同 code 崩溃重启的僵尸 running run 无人关闭
            snap = dict(self._live.snapshot()) if self._live else {}
            snap.setdefault("worker_id", self._worker_id)
            self._fifo.put_live({**snap, "kind": "lifecycle"})
            self._fifo.ship_live()
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
                            and self._fifo.depth() < _QUEUE_CAP:  # 平台长 down:暂停续读,队列不膨胀
                        try:
                            with self._file_lock:
                                self._tail_once(run)
                        except Exception:
                            log.debug("obs tail error", exc_info=True)
                if now >= next_ping:
                    next_ping = now + _PING_CADENCE
                    # ping 可再生:队满即丢(下节拍再生),不参与无界积压
                    self._fifo.put_droppable({"t": "ping", "worker_id": self._worker_id})
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
        # poller 恒由 driver 装配注入(observability_url 或 status_port>0 时创建共享
        # 实例,与 localserver 同一 60s 轮询);未注入(裸测试/直连)则不推 roster。
        # 绝不在此自建 RosterPoller —— driver 是唯一装配根。
        if self._roster_poller is None:
            return
        try:
            snap = self._roster_poller.snapshot()
            if snap:
                # roster 全量可再生:队满即丢(60s 后再生),不参与无界积压
                self._fifo.put_droppable({"t": "roster", "worker_id": self._worker_id,
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
                    msg = self._fifo.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    if msg["t"] == "events":
                        r = client.post(self._url + "/api/internal/events",
                                        json={"run_id": msg["run"]["run_id"],
                                              "worker_id": msg["run"].get("worker_id"),
                                              "challenge_code": msg["run"]["code"],
                                              "model": msg["run"].get("model") or "",
                                              "evaluation_id": msg["run"].get("evaluation_id"),
                                              "job_id": msg["run"].get("job_id"),
                                              "attempt_id": msg["run"].get("attempt_id"),
                                              "events": msg["rows"]}, headers=headers)
                    elif msg["t"] == "live":
                        # 帧已在 put_live 剥好信封/带外键 —— 原样 POST(勿二次剥)。
                        # 契约:LiveIn.snapshot = LiveState 纯快照
                        r = client.post(self._url + "/api/internal/live",
                                        json={"worker_id": msg["worker_id"],
                                              "kind": msg.get("kind", "lifecycle"),
                                              "snapshot": msg["frame"]}, headers=headers)
                    elif msg["t"] == "flags":
                        r = client.post(self._url + "/api/internal/accepted_flags",
                                        json={"run_id": msg["run_id"],
                                              "flags": msg["flags"]}, headers=headers)
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
                    if r.status_code == 503 and "token not configured" in detail:
                        # obs 侧根本没配 token:重试到天荒地老也不会成功,响亮丢弃。
                        log.error("obs POST %s HTTP 503 (server token not configured) — "
                                  "message dropped, set OBSERVABILITY_TOKEN server-side", msg["t"])
                    elif (r.status_code < 500 and r.status_code != 429):
                        # 4xx = 配置/载荷错误:重试不会成功,响亮丢弃(避免无界积压)
                        log.error("obs POST %s HTTP %d — permanent error, message dropped: %s",
                                  msg["t"], r.status_code, detail)
                    elif detail and (time.monotonic() - warned.get(detail, 0.0)) > 60:
                        log.warning("obs POST %s HTTP %d: %s", msg["t"], r.status_code, detail)
                        warned[detail] = time.monotonic()
                    retryable = (r.status_code >= 500 or r.status_code == 429) \
                        and "token not configured" not in detail
                    if retryable and msg["t"] != "live":
                        # 平台**回了话**(收到 HTTP 响应)却持续拒收 → 这条载荷本身是毒
                        # 消息。队首即全局闸门,无上限重试会把它后面的 live/events/ping
                        # 永久堵死(平台进而 stale_after 判 worker 离线,而 worker 其实
                        # 在正常解题),故给一个终点。连接异常走 except:那是平台 down,
                        # 仍不限次 —— 保住「长 down 零丢失 + 队首保序」。
                        tries = msg.get("_tries", 0) + 1
                        msg["_tries"] = tries
                        if tries >= _POISON_TRIES:
                            log.error("obs POST %s HTTP %d — 连续 %d 次被拒,丢弃该消息"
                                      "(避免毒消息永久堵死队首): %s",
                                      msg["t"], r.status_code, tries, detail)
                        else:
                            self._fifo.requeue(msg)
                except Exception as e:
                    if time.monotonic() - warned.get("exc", 0.0) > 60:
                        log.warning("obs platform unreachable (%s) — retrying, message requeued", e)
                        warned["exc"] = time.monotonic()
                    if msg["t"] != "live":
                        self._fifo.requeue(msg)
                    # live 帧不重放:最新槽会随下一节拍补送,旧帧重放反而滞后
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 10.0)


def maybe_start_relay(live, bus, *, settings=None, roster_poller=None) -> ObsRelay | None:
    """OBSERVABILITY_URL 未设返回 None —— 宿主裸跑等场景零行为变化。
    token 可选:平台侧未配 token 时返回 503(响亮),relay 记 warn + 退避重试。
    settings:WorkerSettings(driver 装配;None 时回退自读 env,测试/独立使用兼容)。
    roster_poller:driver 与 localserver 共享的 RosterPoller(main() 注入;None 不推 roster)。"""
    if settings is not None:
        url = settings.observability_url
        token = settings.observability_token or None
        workdir = settings.workdir
        worker_id = settings.worker_id
    else:
        url = os.getenv("OBSERVABILITY_URL", "").strip().rstrip("/")
        token = os.getenv("OBSERVABILITY_TOKEN", "").strip() or None
        workdir = os.getenv("ADAPTER_WORKDIR", "/work")
        worker_id = os.getenv("WORKER_ID", "worker-1")
    if not url:
        log.info("obs relay disabled (OBSERVABILITY_URL unset)")
        return None
    relay = ObsRelay(live, bus, workdir, url, token,
                     worker_id=worker_id, roster_poller=roster_poller)
    relay.start()
    return relay
