#!/usr/bin/env python3
"""worker/dashboard — 本地实时源仪表板(stdlib 无依赖,worker 容器 :8080)。

数据不来自 SQLite,而来自注入的协作者(LiveState/LiveBus/RosterPoller 构造
注入、鸭子类型使用 —— 本模块绝不 import平台 obs 侧,worker 的 driver 负责装配)。

依赖说明(架构边界):本模块是 worker 自己的诊断工具,只依赖 redpilot.contracts
与 stdlib;它不 import obs.store/db/schema/ingest(无数据依赖)。本地仪表板
不是数据链路:远端证据链只走 HTTP relay → obs ingest。

与 obs/read.py 的协议契约(路由/SSE 帧/snapshot kind/资产表)单源
redpilot.contracts —— :8080 与平台读端的字节兼容由测试强制而非注释约定。

路由:
  GET /               -> web/index.html(无则 404 说明)
  GET /api/status     -> 最新 LiveState 快照 JSON
  GET /api/events     -> text/event-stream(连接即写快照首帧,15s 心跳)
  GET /api/transcript?code=<id>&tail=<n> -> transcript.jsonl 尾部
  GET /api/roster     -> 题目总览(poller 快照;失败回退本地扫描)
  GET /api/challenge?code=<id> -> 单题详情
  GET /api/timeline?code=<id>&after=<seq> -> 人读时间线(digest 折叠)

失败隔离:本线程任何异常只记日志,绝不影响 worker 求解循环。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from redpilot.contracts.assets import ASSET_RX, ASSET_TYPES
from redpilot.contracts.paths import (FLAG_FILES, FLAG_MAX_LINES,
                                       LIVE_DIR, TRANSCRIPT_FILENAME, safe_code)
from redpilot.contracts.vocabulary import ACTIVE_PHASES, SNAPSHOT_KIND, \
    SSE_HEARTBEAT_S, strip_out_of_band

log = logging.getLogger("worker.dashboard")

_VALID_RX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _valid_code(code: str) -> bool:
    return bool(_VALID_RX.match(code or ""))


def _valid_rel(name: str) -> bool:
    """映射后目录名的遍历守卫:safe_code 产物(含 hash 后缀,超 64 字符)放行"""
    return bool(name) and "/" not in name and "\\" not in name \
        and "\x00" not in name and name not in (".", "..")


def read_flag_lines(dpath: str, names: tuple[str, ...] = FLAG_FILES) -> list[str]:
    """读题目目录的 FLAG 候选:逐个试名,首个有实质内容的去空行后最多 FLAG_MAX_LINES 条;
    空文件跳过(占位空 FLAG 不得挡住后面的 flag.txt 候选);全部缺失/全空返回 []。"""
    for name in names:
        try:
            with open(os.path.join(dpath, name), encoding="utf-8", errors="ignore") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if lines:
                return lines[:FLAG_MAX_LINES]
        except OSError:
            continue
    return []


# ── 本地目录扫描(纯 stdlib;RosterPoller 未起/失败时的兜底视图) ──

_SKIP_NAMES = {".", "..", "__pycache__", LIVE_DIR}


def _dir_maps_to_code(dirname: str):
    """目录名是否可能是某 code 的 safe_code 映射(可逆的纯 sanitize 名返回原名)"""
    if not dirname or dirname.startswith((".", "_")) or dirname in _SKIP_NAMES:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", dirname):
        return dirname
    return None


def _scan_artifacts(dpath: str, max_files: int = 20) -> list[str]:
    """非 dot/非 _ 开头的普通文件(transcript/FLAG/CLAUDE.md/core.* 单列,不入此表)"""
    out: list[str] = []
    try:
        for name in sorted(os.listdir(dpath)):
            if name.startswith((".", "_")):
                continue
            if name in (TRANSCRIPT_FILENAME, "FLAG", "CLAUDE.md") or name.startswith("core."):
                continue
            p = os.path.join(dpath, name)
            if os.path.isfile(p) and os.path.getsize(p) <= 500_000:
                out.append(name)
            if len(out) >= max_files:
                break
    except OSError:
        pass
    return out


def scan_local_dir(workdir: str, dirname: str) -> dict:
    """单个题目目录的本地痕迹(stat 级,不读大文件)"""
    dpath = os.path.join(workdir, dirname)
    out: dict = {
        "dir": dirname,
        "flag": False,
        "transcript_bytes": 0,
        "transcript_mtime": 0.0,
        "crashed": False,
        "artifacts": [],
        "last_activity": 0.0,
    }
    try:
        for name in os.listdir(dpath):
            p = os.path.join(dpath, name)
            if not os.path.isfile(p):
                continue
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            if name == "FLAG":
                out["flag"] = True
            elif name == TRANSCRIPT_FILENAME:
                out["transcript_bytes"] = os.path.getsize(p)
                out["transcript_mtime"] = mtime
            elif name.startswith("core."):
                out["crashed"] = True
            out["last_activity"] = max(out["last_activity"], mtime)
        out["artifacts"] = _scan_artifacts(dpath)
    except OSError:
        pass
    return out


def scan_local(workdir: str) -> dict[str, dict]:
    """扫描 workdir 顶层全部题目目录 -> {code: local}(code 用可反解目录名)"""
    result: dict[str, dict] = {}
    try:
        for name in sorted(os.listdir(workdir)):
            if name in _SKIP_NAMES or name.startswith((".", "_")):
                continue
            if not os.path.isdir(os.path.join(workdir, name)):
                continue
            code = _dir_maps_to_code(name)
            if code is None:
                continue  # hash 后缀映射名无法归属平台题号,跳过(平台行会自带映射)
            result[code] = scan_local_dir(workdir, name)
    except OSError:
        pass
    return result


def challenge_detail(workdir: str, code: str, platform_row) -> dict:
    """单题详情:平台行 + 本地痕迹 + FLAG 内容 + 文件明细。code 需先经调用方守卫。"""
    dpath = os.path.join(workdir, safe_code(code))
    local = scan_local_dir(workdir, os.path.basename(dpath)) if os.path.isdir(dpath) else {}
    # 详情只认规范名 FLAG(与 roster 快照/obs 兜底不同,不扩散到 flag.txt 候选)
    flags = read_flag_lines(dpath, names=("FLAG",))
    row = dict(platform_row or {})
    row.update({"unique_code": code, "local": local, "flags": flags})
    return row


def local_challenge_row(code: str, local: dict | None = None) -> dict:
    """local_only 挑战行模板(poller 兜底/无 poller 仪表板共用)"""
    row = {"unique_code": code, "local_only": True, "description": "", "difficulty": "",
           "total_score": 0, "flag_count": 0, "correct_flag_count": 0,
           "is_completed": False, "container_status": "", "container_addr": [],
           "level": 0}
    if local is not None:
        row["local"] = local
    return row


def empty_roster_snapshot(*, platform_disabled: bool = True) -> dict:
    """5 键空快照(poller 初值/无 poller 兜底共用;obs store 保同构字面量)。"""
    return {"fetched_at": 0.0, "stale": True, "platform_error": "",
            "platform_disabled": platform_disabled, "challenges": {}}


def _make_handler(live, bus, workdir: str, web_dir: str, poller=None, digest=None):
    """handler 工厂:per-server 配置走实例属性,不污染类状态。

    live/bus/poller/digest 均为注入的协作者(鸭子类型):
      live.snapshot() -> dict;bus.subscribe()/unsubscribe() -> queue.Queue
      poller.snapshot() -> dict;digest.timeline(code, after=, live=) -> dict
    """
    index_cache: dict = {"mtime": 0.0, "body": b""}
    assets_cache: dict[str, dict] = {}  # 绝对路径 -> {"mtime": float, "body": bytes}

    def _live_code_phases() -> tuple[str, bool]:
        """当前是否正在求解某题(用于 timeline 的 live 判定:进行中的题不标 abrupt)"""
        try:
            if live is None:
                return "", False
            snap = live.snapshot()
            code = snap.get("challenge_code") or ""
            return code, snap.get("phase") in ACTIVE_PHASES
        except Exception:
            return "", False

    class Handler(BaseHTTPRequestHandler):
        server_version = "RedPilotStatus/1"

        def log_message(self, fmt, *args):  # 降噪:走 logging
            log.debug(fmt, *args)

        def _send_bytes(self, body: bytes, content_type: str, code: int = 200) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, obj, code: int = 200) -> None:
            self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                             "application/json; charset=utf-8", code)

        def do_GET(self) -> None:  # noqa: N802
            try:
                u = urlparse(self.path)
                if u.path == "/api/status":
                    self._send_json(live.snapshot() if live else {})
                elif u.path == "/api/events":
                    self._sse()
                elif u.path == "/api/transcript":
                    self._transcript(parse_qs(u.query))
                elif u.path == "/api/roster":
                    self._roster()
                elif u.path == "/api/challenge":
                    self._challenge(parse_qs(u.query))
                elif u.path == "/api/timeline":
                    self._timeline(parse_qs(u.query))
                elif u.path in ("/", "/index.html"):
                    self._index()
                elif u.path.startswith("/assets/"):
                    self._asset(u.path)
                else:
                    self.send_error(404, "not found")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                log.exception("localserver handler error")
                try:
                    self.send_error(500, "internal error")
                except Exception:
                    pass

        def _index(self) -> None:
            p = os.path.join(web_dir, "index.html")
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                self.send_error(404, "web/index.html not baked (see Dockerfile COPY web)")
                return
            if mtime != index_cache["mtime"]:
                try:
                    with open(p, "rb") as f:
                        index_cache["body"] = f.read()
                    index_cache["mtime"] = mtime
                except OSError:
                    log.exception("index read failed")
                    self.send_error(500, "read failed")
                    return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(index_cache["body"])))
            self.send_header("Cache-Control", "no-store")  # 开发期热更:禁止浏览器缓存 index
            self.end_headers()
            self.wfile.write(index_cache["body"])

        def _asset(self, path: str) -> None:
            """静态构建产物(web/assets/*)。名字只允许 URL 安全平铺名,杜绝穿越。"""
            name = path[len("/assets/"):]
            ext = os.path.splitext(name)[1].lower()
            if (not name or "/" in name or "\\" in name or "\x00" in name
                    or not ASSET_RX.fullmatch(name) or ext not in ASSET_TYPES):
                self.send_error(404, "asset not found")
                return
            p = os.path.join(web_dir, "assets", name)  # name 无分隔符 => 必在 web_dir 内
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                self.send_error(404, "web/assets/" + name
                                + " not baked (run `cd frontend && npm run build`; commit web/)")
                return
            entry = assets_cache.get(p)
            if entry is None or entry["mtime"] != mtime:
                try:
                    with open(p, "rb") as f:
                        body = f.read()
                except OSError:
                    log.exception("asset read failed: %s", name)
                    self.send_error(500, "read failed")
                    return
                assets_cache[p] = {"mtime": mtime, "body": body}
            self.send_response(200)
            self.send_header("Content-Type", ASSET_TYPES[ext])
            self.send_header("Content-Length", str(len(assets_cache[p]["body"])))
            self.send_header("Cache-Control", "no-store")  # 开发期热更:与 index 一致不缓存
            self.end_headers()
            self.wfile.write(assets_cache[p]["body"])

        def _sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = bus.subscribe() if bus else queue.Queue()
            try:
                # 读端超时:慢/死的 SSE 订阅者最多占线程 60s,随后 finally 释放订阅
                try:
                    self.request.settimeout(60)
                except Exception:
                    pass
                if live:  # 首帧快照永不空白;bus 本身无 replay
                    snap = live.snapshot()
                    snap["kind"] = SNAPSHOT_KIND
                    snap.setdefault("ts", time.time())
                    self.wfile.write(f"data: {json.dumps(snap, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=SSE_HEARTBEAT_S)
                        if isinstance(ev, dict):
                            # 带外元数据(_ 前缀,如 closing 帧附带的 _accepted_flags 明文)
                            # 只走 relay→平台链路,绝不向仪表板广播(契约见 vocabulary)
                            ev = strip_out_of_band(ev)
                        self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except socket.timeout:
                pass
            finally:
                if bus:
                    try:
                        bus.unsubscribe(q)
                    except Exception:
                        pass

        def _transcript(self, qs: dict) -> None:
            code = (qs.get("code") or [""])[0]
            try:
                tail = max(1, min(int((qs.get("tail") or ["200"])[0]), 2000))
            except ValueError:
                tail = 200
            # 与 driver 同映射:合法 code 查精确目录;其余只查 sanitize 映射目录
            # (400 仅当两种查法都不可能时返回,点/冒号题不再误杀)
            cands = []
            if _valid_code(code):
                cands.append(os.path.join(workdir, code, TRANSCRIPT_FILENAME))
            mapped = safe_code(code)
            if mapped != code and _valid_rel(mapped):
                cands.append(os.path.join(workdir, mapped, TRANSCRIPT_FILENAME))
            if not cands:
                self.send_error(400, "bad code")
                return
            path = next((p for p in cands if os.path.isfile(p)), None)
            if path is None:
                self._send_json({"code": code, "lines": []})
                return
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    lines = list(deque(f, maxlen=tail))
                self._send_json({"code": code, "lines": [ln.rstrip("\n") for ln in lines]})
            except Exception:
                log.exception("transcript read failed")
                self.send_error(500, "read failed")

        def _roster(self) -> None:
            """题目总览:poller 快照(平台+本地);异常时回退一次本地扫描,绝不让页面空"""
            snap = None
            try:
                if poller is not None:
                    snap = dict(poller.snapshot())
            except Exception:
                log.exception("roster snapshot failed")
            if not snap:  # poller 缺省/失败 → 空快照(页面仍可渲染,带 stale 标记)
                snap = empty_roster_snapshot()
            if not snap.get("challenges"):
                try:
                    local = scan_local(workdir)
                    if local:
                        rows = {code: local_challenge_row(code, local=lc)
                                for code, lc in local.items()}
                        snap = dict(snap, challenges=rows)
                except Exception:
                    log.exception("roster local fallback failed")
            self._send_json(snap)

        def _challenge(self, qs: dict) -> None:
            code = (qs.get("code") or [""])[0]
            if not _valid_code(code):
                mapped = safe_code(code)
                if not (mapped != code and _valid_rel(mapped)):
                    self.send_error(400, "bad code")
                    return
            try:
                platform_row = {}
                if poller is not None:
                    platform_row = (poller.snapshot().get("challenges") or {}).get(code) or {}
                self._send_json(challenge_detail(workdir, code, platform_row))
            except Exception:
                log.exception("challenge detail failed")
                self.send_error(500, "read failed")

        def _timeline(self, qs: dict) -> None:
            code = (qs.get("code") or [""])[0]
            if not _valid_code(code):
                mapped = safe_code(code)
                if not (mapped != code and _valid_rel(mapped)):
                    self.send_error(400, "bad code")
                    return
            try:
                after = max(0, int((qs.get("after") or ["0"])[0]))
            except ValueError:
                after = 0
            if digest is None:
                self._send_json({"next_seq": 0, "meta": {}, "entries": []})
                return
            live_code, live_phase = _live_code_phases()
            try:
                out = digest.timeline(code, after=after, live=(live_code == code and live_phase))
                self._send_json(out)
            except Exception:
                log.exception("timeline digest failed")
                self.send_error(500, "digest failed")

    return Handler


def serve_forever_in_thread(live, bus, port: int, *,
                            workdir: str | None = None,
                            web_dir: str | None = None,
                            poller=None, digest=None,
                            host: str = "0.0.0.0") -> threading.Thread | None:
    """起守护线程服务;port<=0 则禁用(回归:求解不受影响)。

    协作者全部构造注入(本模块零 worker import,装配责任在调用方):
      live/bus —— worker driver 传入(LiveState/LiveBus,鸭子类型);
      poller   —— 题目总览轮询(worker 的 RosterPoller 或 None);
      digest   —— transcript 时间线折叠壳;worker 侧 TranscriptDigest 已删除,
                  恒 None(本地仪表板 timeline 返回空态,时间线走 obs 平台侧)。
    worker 的 main() 负责共享同一个 poller(避免双 60s 轮询线程双写 roster.json)。

    host: 监听地址。仪表板数据无鉴权(实时 FLAG/完整实录),默认应由调用方传入
    回环地址;本函数缺省 0.0.0.0 仅为 docker-proxy 转发兼容(容器需全网卡监听)。
    """
    if not port or port <= 0:
        log.info("local status server disabled (STATUS_PORT=%s)", port)
        return None

    workdir = workdir or os.getenv("ADAPTER_WORKDIR", "/work")
    if web_dir is None:
        # 环境变量优先(compose/镜像设 OBSERVABILITY_WEB 或按 /app/web 布局);
        # 回退按本文件相对宿主源树定位(redpilot/worker → ../../web)
        web_dir = (os.getenv("OBSERVABILITY_WEB", "").strip()
                   or os.getenv("STATUS_WEB_DIR", "").strip()
                   or os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "..", "..", "web")))
    try:
        srv = ThreadingHTTPServer(
            (host, port), _make_handler(live, bus, workdir, web_dir, poller, digest))
    except Exception:
        log.exception("local status server bind %s:%d failed (solving continues)", host, port)
        return None

    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 1},
                         daemon=True, name="status-server")
    t.start()
    log.info("local status server on :%d (web + SSE)", port)
    return t
