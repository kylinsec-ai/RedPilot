#!/usr/bin/env python3
"""只读监视服务 — stdlib 无依赖：摘要快照 + SSE 推送 + transcript 尾部。

路由：
  GET /               -> web/index.html（无则 404 说明）
  GET /api/status     -> 最新 LiveState 快照 JSON
  GET /api/events     -> text/event-stream（连接即写快照首帧，15s 心跳）
  GET /api/transcript?code=<id>&tail=<n> -> transcript.jsonl 尾部

失败隔离：本线程任何异常只记日志，绝不影响 driver 求解循环。
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

from drivers.roster import RosterPoller, TranscriptDigest, challenge_detail, scan_local

log = logging.getLogger("adapter.status")

_VALID_RX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# ── 前端构建产物 (vite 多文件 dist → web/assets/*) ──
# 名字只允许 URL 安全平铺名(vite 只发 <hash>.js/.css),杜绝路径穿越
_ASSET_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}
_ASSET_RX = re.compile(r"[A-Za-z0-9._-]{1,120}")


def _valid_code(code: str) -> bool:
    return bool(_VALID_RX.match(code or ""))


def _valid_rel(name: str) -> bool:
    """映射后目录名的遍历守卫:_safe_code 产物(含 hash 后缀,超 64 字符)放行"""
    return bool(name) and "/" not in name and "\\" not in name \
        and "\x00" not in name and name not in (".", "..")


def _map_code(code: str) -> str:
    """与 driver 一致的 code->目录映射（sanitize+hash 后缀）；失败回退 sanitize 值"""
    try:
        from drivers.benchmark_driver import _safe_code
        return _safe_code(code)
    except Exception:
        # 回退必须与 _safe_code 逐字一致（含 hash 后缀），否则查到错误目录
        import hashlib
        raw = str(code)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:64] or "chal"
        return safe if safe == raw else f"{safe}-{hashlib.sha1(raw.encode()).hexdigest()[:6]}"


def _make_handler(live, bus, workdir: str, web_dir: str,
                  poller: RosterPoller | None = None,
                  digest: TranscriptDigest | None = None):
    """handler 工厂：per-server 配置走实例属性，不污染类状态"""
    index_cache: dict = {"mtime": 0.0, "body": b""}
    assets_cache: dict[str, dict] = {}  # 绝对路径 -> {"mtime": float, "body": bytes}

    def _live_code_phases() -> tuple[str, bool]:
        """当前是否正在求解某题（用于 timeline 的 live 判定：进行中的题不标 abrupt）"""
        try:
            if live is None:
                return "", False
            snap = live.snapshot()
            code = snap.get("challenge_code") or ""
            return code, snap.get("phase") in ("starting", "solving", "submitting", "closing")
        except Exception:
            return "", False

    class Handler(BaseHTTPRequestHandler):
        server_version = "TsecBenchStatus/1"

        def log_message(self, fmt, *args):  # 降噪：走 logging
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
                log.exception("status_server handler error")
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
            self.send_header("Cache-Control", "no-store")  # 开发期热更：禁止浏览器缓存 index
            self.end_headers()
            self.wfile.write(index_cache["body"])

        def _asset(self, path: str) -> None:
            """静态构建产物（web/assets/*）。名字只允许 URL 安全平铺名，杜绝穿越。"""
            name = path[len("/assets/"):]
            ext = os.path.splitext(name)[1].lower()
            if (not name or "/" in name or "\\" in name or "\x00" in name
                    or not _ASSET_RX.fullmatch(name) or ext not in _ASSET_TYPES):
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
            self.send_header("Content-Type", _ASSET_TYPES[ext])
            self.send_header("Content-Length", str(len(assets_cache[p]["body"])))
            self.send_header("Cache-Control", "no-store")  # 开发期热更：与 index 一致不缓存
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
                if live:  # 首帧快照永不空白；bus 本身无 replay
                    snap = live.snapshot()
                    snap["kind"] = "snapshot"
                    snap.setdefault("ts", time.time())
                    self.wfile.write(f"data: {json.dumps(snap, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15)
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
            # 与 driver 同映射：合法 code 查精确目录；其余只查 sanitize 映射目录
            # (400 仅当两种查法都不可能时返回,点/冒号题不再误杀)
            cands = []
            if _valid_code(code):
                cands.append(os.path.join(workdir, code, "transcript.jsonl"))
            mapped = _map_code(code)
            if mapped != code and _valid_rel(mapped):
                cands.append(os.path.join(workdir, mapped, "transcript.jsonl"))
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
            """题目总览：poller 快照（平台+本地）；异常时回退一次本地扫描，绝不让页面空"""
            try:
                snap = dict(poller.snapshot()) if poller else {
                    "fetched_at": 0.0, "stale": True, "platform_error": "",
                    "platform_disabled": True, "challenges": {}}
            except Exception:
                log.exception("roster snapshot failed")
                snap = {"fetched_at": 0.0, "stale": True, "platform_error": "",
                        "platform_disabled": True, "challenges": {}}
            if not snap.get("challenges"):
                try:
                    local = scan_local(workdir)
                    if local:
                        rows = {}
                        for code, lc in local.items():
                            rows[code] = {"unique_code": code, "local_only": True,
                                          "difficulty": "", "total_score": 0, "flag_count": 0,
                                          "correct_flag_count": 0, "is_completed": False,
                                          "container_status": "", "container_addr": [],
                                          "level": 0, "description": "", "local": lc}
                        snap = dict(snap, challenges=rows)
                except Exception:
                    log.exception("roster local fallback failed")
            self._send_json(snap)

        def _challenge(self, qs: dict) -> None:
            code = (qs.get("code") or [""])[0]
            if not _valid_code(code):
                mapped = _map_code(code)
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
                mapped = _map_code(code)
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
                            web_dir: str | None = None) -> threading.Thread | None:
    """起守护线程服务；port<=0 则禁用（回归：求解不受影响）。"""
    if not port or port <= 0:
        log.info("status server disabled (STATUS_PORT=%s)", port)
        return None

    workdir = workdir or os.getenv("ADAPTER_WORKDIR", "/work")
    if web_dir is None:
        web_dir = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "..", "web"))
    # 监视台数据层：题目总览轮询 + transcript 合流 digest（各自失败隔离，不起服务不阻塞）
    poller = digest = None
    try:
        poller = RosterPoller(workdir)
        poller.start()
    except Exception:
        log.exception("roster poller start failed (dashboard shows local-only)")
        poller = None
    try:
        digest = TranscriptDigest(workdir)
    except Exception:
        log.exception("digest init failed (timeline unavailable)")
        digest = None
    try:
        srv = ThreadingHTTPServer(
            ("0.0.0.0", port), _make_handler(live, bus, workdir, web_dir, poller, digest))
    except Exception:
        log.exception("status server bind :%d failed (solving continues)", port)
        return None

    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 1},
                         daemon=True, name="status-server")
    t.start()
    log.info("status server on :%d (web + SSE)", port)
    return t
