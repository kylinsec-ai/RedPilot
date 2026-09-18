"""pi 求解引擎的传输层：print(json 一次性) 与 rpc(常驻 JSONL)。

为什么单独一层（设计依据见 `docs/pi-rpc-migration-research.md`）：

- 两种传输只是"怎么起进程 / 怎么发 prompt / 什么时候算完"不同；
- **事件处理循环（`pi_agent.solve` 的 500 行分支）对它们完全无感** —— 这是本次
  替换的关键：把差异挡在这一层，避免把那份分支复制一遍造成行为漂移。

RPC 的三个语义差异全部在本层被吸收，不让它们污染事件循环：

1. **stdout 混入协议帧**：RPC 会同时吐 `response` 与 `extension_ui_request`。
   本层只把**事件**（及无法解析的诊断行）交给上层；`response` 丢弃，
   `extension_ui_request` 由本层自动应答（无人值守一律"拒绝/取消"）。
2. **进程不自己退出**：RPC 常驻，必须靠 `agent_settled` 收尾；`shutdown()`
   负责关停。`terminates_on_settled=True` 把这个语义告诉上层。
3. **prompt 走 stdin 而非 argv**：`build_cmd(include_prompt=False)` + 启动后发
   `{"type":"prompt"}`。

分帧遵循官方要求：**严格按 `\\n` 切**（`readline` 会误切 `U+2028/2029`），
增量 UTF-8 解码。stdout/stderr 合并（与 print 传输一致）——非 JSON 行进
`junk_tail`，是非零退出时唯一的诊断线索（`test_provider_failure_guard` 依赖）。
"""

from __future__ import annotations

import codecs
import json
import logging
import os
import queue
import subprocess
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("adapter.solver.pi.transport")

# RPC 关闭时的宽限：先 abort 让 turn 收尾，再升级到进程树回收
_ABORT_GRACE_S = 3.0


class PrintTransport:
    """`pi --mode json --print` 一次性传输（原行为，逐字保留）。"""

    terminates_on_settled = False

    def __init__(self, cmd: list[str], *, prompt: str, workdir: str,
                 env: dict, stop_fn: Callable,
                 identity: dict | None = None, **_ignored):
        # identity 是 Popen 的身份参数（user/group/extra_groups）；空 dict = 不降权。
        # 设计：docs/solver-isolation-design.md §4。
        self.proc = subprocess.Popen(
            cmd + [prompt], cwd=workdir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=False, bufsize=0, start_new_session=True,
            **(identity or {}),
        )
        self._stop_fn = stop_fn

    def wait(self, timeout: float) -> bool:
        import select
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        return bool(ready)

    def read(self) -> str:
        try:
            return os.read(self.proc.stdout.fileno(), 65536).decode("utf-8", "replace")
        except OSError:
            return ""

    def send(self, _obj: dict) -> None:      # print 无双向通道
        return

    def stop(self, *, force: bool = False) -> None:
        if self.proc.poll() is None:
            self._stop_fn(self.proc, force=force)

    def shutdown(self) -> None:
        # 一次性进程在 EOF 时已自行退出；此处只在异常路径兜底
        if self.proc.poll() is None:
            self._stop_fn(self.proc, force=True)

    def close(self) -> None:
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
        except Exception:
            pass


class RpcTransport:
    """`pi --mode rpc` 常驻传输。

    读线程**持续抽干 stdout**（否则写 stdin 会被管道背压死锁），
    过滤协议帧后按行入队；上层 `wait()/read()` 从队列取。
    """

    terminates_on_settled = True

    def __init__(self, cmd: list[str], *, prompt: str, workdir: str,
                 env: dict, stop_fn: Callable,
                 identity: dict | None = None, **_ignored):
        self.proc = subprocess.Popen(
            cmd, cwd=workdir, env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=False, bufsize=0, start_new_session=True,
            **(identity or {}),
        )
        self._stop_fn = stop_fn
        self._q: "queue.Queue[str]" = queue.Queue()
        self._pending: Optional[str] = None
        self._ui_answered = 0
        self._eof = threading.Event()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="pi-rpc-reader")
        self._reader.start()
        # 协议第一步：发 prompt。RPC 进程可能已立刻死亡（假 pi / 缺依赖），
        # 写管道会 BrokenPipeError —— 与 print 模式"参数错误立刻非零退出"等价，
        # 交给上层的 junk_tail/退出码护栏处理，这里只吞掉。
        self.send({"type": "prompt", "message": prompt})

    # ── 读线程：严格 LF 分帧 + 协议帧过滤 ──
    def _read_loop(self) -> None:
        buf = ""
        try:
            fd = self.proc.stdout.fileno()
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += self._decoder.decode(chunk)
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._on_line(line)
            buf += self._decoder.decode(b"", final=True)
            if buf:
                self._on_line(buf)
        except Exception as _e:          # noqa: BLE001 —— 守护线程绝不能把异常抛给主流程
            log.warning("[rpc] 读线程异常退出：%r", _e)
        finally:
            self._eof.set()

    def _on_line(self, line: str) -> None:
        line = line[:-1] if line.endswith("\r") else line
        if not line.strip():
            return
        try:
            ev = json.loads(line)
        except ValueError:
            self._q.put(line)          # 非 JSON = 诊断文本，交给 junk_tail
            return
        etype = ev.get("type")
        if etype == "response":
            return                     # 请求/响应簿记，不是事件
        if etype == "extension_ui_request":
            self._answer_ui(ev)
            return
        self._q.put(line)

    def _answer_ui(self, ev: dict) -> None:
        """无人值守：一律"拒绝/取消"。扩展若弹窗等待回应，不答就会整场挂死。"""
        method = ev.get("method")
        rid = ev.get("id")
        if rid is None:
            return
        if method == "confirm":
            self.send({"type": "extension_ui_response", "id": rid, "confirmed": False})
        elif method in ("select", "input", "editor"):
            self.send({"type": "extension_ui_response", "id": rid, "cancelled": True})
        # notify / setStatus / setWidget / setTitle / set_editor_text 属于
        # fire-and-forget，按协议不需要回应。
        if method in ("confirm", "select", "input", "editor"):
            self._ui_answered += 1
            log.info("[rpc] 已自动拒绝扩展弹窗 method=%s（无人值守）", method)

    # ── 上层接口 ──
    def wait(self, timeout: float) -> bool:
        try:
            self._pending = self._q.get(timeout=timeout)
            return True
        except queue.Empty:
            return False

    def read(self) -> str:
        item = getattr(self, "_pending", None)
        if item is not None:
            self._pending = None
            return item + "\n"
        return ""

    def send(self, obj: dict) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass                       # 进程已死：退出码护栏负责报错

    def stop(self, *, force: bool = False) -> None:
        if self.proc.poll() is not None:
            return
        # 先 abort（等 turn 收尾），再升级到进程树回收
        self.send({"type": "abort"})
        end = time.monotonic() + (0.5 if force else _ABORT_GRACE_S)
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                return
            time.sleep(0.1)
        self._stop_fn(self.proc, force=force)

    def shutdown(self) -> None:
        if self.proc.poll() is not None:
            return
        # agent_settled 之后 agent 已空闲：关 stdin 让它自然退出，超时再杀
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=_ABORT_GRACE_S)
        except subprocess.TimeoutExpired:
            self.send({"type": "abort"})
            self._stop_fn(self.proc, force=True)

    def close(self) -> None:
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
        except Exception:
            pass


# ── 能力探测：老版 pi 无 --mode rpc 时安全回退到 print ──
_RPC_PROBE_CACHE: dict[str, bool] = {}


def rpc_available(cmd: str) -> bool:
    """`pi --help` 是否列出 rpc 模式。结果按可执行路径缓存（每进程一次）。"""
    key = os.path.abspath(cmd) if os.path.exists(cmd) else cmd
    if key in _RPC_PROBE_CACHE:
        return _RPC_PROBE_CACHE[key]
    ok = False
    try:
        out = subprocess.run([cmd, "--help"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=20)
        text = out.stdout.decode("utf-8", "replace")
        ok = "rpc" in text.lower() and "--mode" in text
    except Exception as e:                       # noqa: BLE001
        log.warning("[rpc] 能力探测失败（回退 print）：%s", e)
    _RPC_PROBE_CACHE[key] = ok
    return ok


def make_transport(*, transport: str, cmd_base: list[str], prompt: str,
                   workdir: str, env: dict, stop_fn: Callable,
                   cmd_path: str = "", identity: dict | None = None,
                   ) -> PrintTransport | RpcTransport:
    """按名字建传输；rpc 不可用（老 pi）时**静默降级**为 print 并记一条 warn。

    降级是刻意的：宁可退回旧行为，也不要让一个"模式不存在"把整条求解链路打死。
    identity 是两个传输共用的降权身份（空 dict = 保持当前 uid），
    不因传输降级而丢失 —— 否则\"老 pi 回退 print" 会静默地把隔离一起退掉。
    """
    want_rpc = (transport or "").strip().lower() == "rpc"
    if want_rpc and not cmd_path:
        cmd_path = cmd_base[0] if cmd_base else "pi"
    if want_rpc and not rpc_available(cmd_path):
        log.warning("[rpc] 该 pi 不支持 --mode rpc，回退 print（ADAPTER_PI_TRANSPORT）")
        want_rpc = False
    if want_rpc:
        log.info("[rpc] 使用 RPC 传输（常驻 JSONL）")
        return RpcTransport(cmd_base, prompt=prompt, workdir=workdir, env=env,
                            stop_fn=stop_fn, identity=identity)
    return PrintTransport(cmd_base, prompt=prompt, workdir=workdir, env=env,
                          stop_fn=stop_fn, identity=identity)
