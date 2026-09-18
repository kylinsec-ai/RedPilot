"""
Flag 校验模块 — 三重门 + 置信度分级

1. grounding (代码校验):  候选 flag 必须逐字出现在真实命令输出中
2. 否定式质疑:            独立校验会话尝试反驳
3. 追问式复核:            核对来源命令和输出的唯一可解释性

置信度分级:
- HIGH:   逐字出现在真实输出且格式正确 → 直接提交
- MEDIUM: 大小写改写或仅出现在模型叙述中 → 走对抗校验
- LOW:    命中占位词特征或熵值过低 → 拒绝提交
"""

from __future__ import annotations

import ast
import base64
import json
import logging
import math
import os
import re
import shlex
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

log = logging.getLogger("adapter.verify")

# 占位词/诱饵特征
# B36：md5 分支原写作 `^flag\{[a-f0-9]{32}\}$`，但本正则被喂的是**剥掉外壳的
# body**（flag_confidence 先过 `^flag\{.+\}$` 格式检查，再用 claim.body 取值），
# 所以那条分支自诞生起永远不可能命中 —— 一条从未生效的护栏，只看着像护栏。
# 改为直接锚在 body 上。实测依据：平台判对的 6 条 flag body 全部是 len=36 非纯
# hex，判错的 6 条 len 25~32 也无一纯 hex —— 本平台 flag 是定长的非 hex 串，
# 纯 32 位 md5 形态的 body 在历史数据里从未出现，收紧它的代价接近零。
# （注意：本分支是"初筛降级"而非终审 —— 若该 flag 确实来自活靶标响应的
# 输出，主路径的 _flag_grounded_in_transcripts(require_remote=True) 仍会强提。）
_PLACEHOLDER_RX = re.compile(
    r"(?:example|placeholder|test|dummy|sample|xxxx|0000|1234|abcd)"
    r"|^[a-f0-9]{32}$",  # 纯 md5 哈希（body 本身，非完整信封）
    re.IGNORECASE,
)
# flag body 合法字符（防命令注入 payload 误提取）
_FLAG_BODY_RX = re.compile(r"^[A-Za-z0-9_\-.:/]{3,200}$")

# 远端命令判据。裸 URL/IP 不算强信号：本地解码脚本的注释也可能带下载地址，
# 只有真实网络客户端调用才表示候选来自活靶场响应。
_REMOTE_CMD_RX = re.compile(
    r"(?<![\w./-])(?:curl|wget|ncat|netcat|nc|nmap|ssh|scp|sftp|smbclient|rdesktop"
    r"|xfreerdp|redis-cli|mysql|psql|mongosh|mongo|telnet|socat|hydra|ffuf|gobuster"
    r"|nikto|sqlmap|nuclei|whatweb|impacket-smbclient)\s"
    r"|socket\.|create_connection\(|requests\.|urllib|http\.client|paramiko"
    r"|from\s+pwn|pwnlib|remote\(|\.connect\(|\.recv\(|\.recvall\(|\.sendall\(|\.send\(",
    re.IGNORECASE,
)

# `_REMOTE_CMD_RX` is useful when inspecting source code, but it is *not* a
# provenance decision by itself: a local command can contain the word `curl`
# in a comment, string, README fragment, or a later unrelated shell clause.
# Evidence classification therefore uses the executable-context helpers below.
_REMOTE_SHELL_TOOLS = frozenset({
    "curl", "wget", "ncat", "netcat", "nc", "nmap", "ssh", "scp", "sftp",
    "smbclient", "rdesktop", "xfreerdp", "redis-cli", "mysql", "psql", "mongosh",
    "mongo", "telnet", "socat", "hydra", "ffuf", "gobuster", "nikto", "sqlmap",
    "nuclei", "whatweb", "impacket-smbclient",
})

# Tool execution records are carried through Pi's in-memory result and its
# scoped transcript.  A response written with ``curl -o`` has no stdout, so a
# later ``cat`` is only evidence when the original tool call is known to have
# completed successfully.  Keep this private metadata key out of command
# parsing and never infer success from a non-empty response file.
_TOOL_EXECUTION_OK_KEY = "__tsecbench_execution_ok"

# A response-file chain is deliberately much narrower than native challenge
# artifact analysis.  It exists for ordinary Web/intranet targets where the
# agent stores a live response and reads it in a later call.  Decoders,
# extractors, pipelines, and multiple inputs stay outside this route.
_REMOTE_RESPONSE_READER_PROGRAMS = frozenset({"cat"})
_REMOTE_RESPONSE_PIPE_FILTERS = frozenset({"grep", "egrep", "fgrep", "rg", "tee"})
_REMOTE_CODE_RX = re.compile(
    r"socket\.|create_connection\(|requests\.|urllib|http\.client|paramiko"
    r"|from\s+pwn\b|pwnlib|\bremote\(|\.connect\(|\.recv\(|\.recvall\("
    r"|\.sendall\(|\.send\(",
    re.IGNORECASE,
)
_INLINE_CODE_RX = re.compile(
    r"(?<![\w./-])(?:python3?(?:\.\d+)?|perl|ruby|node|php)\b"
    r"(?:\s+(?!-[cer]\b|--(?:command|eval)\b)\S+)*\s+"
    r"(?:-[cer]\b|--(?:command|eval)\b)\s*"
    r"(?P<quote>['\"])(?P<code>.*)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)


def _strip_shell_comments(text: str) -> str:
    """Remove bash comments while preserving quoted payloads and line layout."""
    out: list[str] = []
    quote = ""
    escaped = False
    i = 0
    text = str(text or "")
    while i < len(text):
        ch = text[i]
        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            out.append(ch)
            escaped = True
            i += 1
            continue
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        # Bash recognizes # at the beginning of a word.  Treat shell control
        # operators as word boundaries too (`cmd;# comment`).
        if ch == "#" and (i == 0 or text[i - 1].isspace() or text[i - 1] in ";|&()"):
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _shell_control_segments(text: str) -> list[str]:
    """Split unquoted top-level `;`, `&&`, `||`, and newlines conservatively."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    escaped = False
    depth = 0
    i = 0
    text = str(text or "")
    while i < len(text):
        ch = text[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")" and depth:
            depth -= 1
            buf.append(ch)
            i += 1
            continue
        is_control = (depth == 0 and (ch in ";\r\n"
                      or (ch in "&|" and i + 1 < len(text)
                          and text[i + 1] == ch)))
        if is_control:
            value = "".join(buf).strip()
            if value:
                parts.append(value)
            buf = []
            i += 2 if ch in "&|" else 1
            continue
        buf.append(ch)
        i += 1
    value = "".join(buf).strip()
    if value:
        parts.append(value)
    return parts


def _has_unquoted_pipe(text: str) -> bool:
    """Whether a shell segment contains a pipeline (not a quoted literal)."""
    quote = ""
    escaped = False
    text = str(text or "")
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "|" and not (i + 1 < len(text) and text[i + 1] == "|") \
                and not (i and text[i - 1] == "|"):
            return True
    return False


def _has_unquoted_input_redirection(text: str) -> bool:
    """Whether a shell fragment can replace stdin with a local source.

    A target-response pipeline is only evidence when every projection stage
    consumes the preceding stage's stdout.  ``grep pattern < old-response``
    and ``tee output < old-response`` look like a remote pipeline in a loose
    parser, but their input actually comes from a local file.  Output
    redirections are handled by the existing mutation parser; this helper
    purposefully covers the source-changing ``<`` family.
    """
    quote = ""
    escaped = False
    for ch in str(text or ""):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "<":
            return True
    return False


def _shell_pipeline_stages(text: str) -> list[str]:
    """Split one shell clause into its top-level pipeline stages.

    ``shlex.split`` intentionally treats ``|`` as an ordinary word.  That is
    fine for a read-only command classifier, but it is unsafe for mutation
    tracking: in ``producer | tee artifact`` the mutating program is not the
    first word.  Keep this small parser quote/depth aware so a pipe in a quoted
    Python string, a heredoc payload (masked by callers), or a command
    substitution is not promoted to a shell pipeline.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    escaped = False
    depth = 0
    raw = str(text or "")
    i = 0
    while i < len(raw):
        ch = raw[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")" and depth:
            depth -= 1
            buf.append(ch)
            i += 1
            continue
        # ``||`` is a control operator handled by _shell_control_segments;
        # ``|`` and ``|&`` are pipelines.  The latter sends stderr through the
        # pipe too but retains the same data/write semantics for our purpose.
        if ch == "|" and depth == 0 and not (i + 1 < len(raw) and raw[i + 1] == "|") \
                and not (i and raw[i - 1] == "|"):
            value = "".join(buf).strip()
            if value:
                parts.append(value)
            buf = []
            i += 2 if i + 1 < len(raw) and raw[i + 1] == "&" else 1
            continue
        buf.append(ch)
        i += 1
    value = "".join(buf).strip()
    if value:
        parts.append(value)
    return parts


def _strip_code_comments_and_literals(text: str) -> str:
    """Approximate code view for network primitives; strings/comments are not IO."""
    lines: list[str] = []
    for raw in str(text or "").splitlines():
        # A deliberately conservative line-level rule handles the common
        # Python/shell/JS comment forms without pretending to parse all code.
        line = raw.lstrip()
        if line.startswith(("#", "//", "--")):
            continue
        line = re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", "''", raw)
        lines.append(line)
    return "\n".join(lines)


def _code_has_remote_primitive(text: str) -> bool:
    return bool(_REMOTE_CODE_RX.search(_strip_code_comments_and_literals(text)))


def _shell_setup_clause(segment: str) -> bool:
    """Clauses that cannot themselves produce the candidate on stdout."""
    s = str(segment or "").strip()
    if not s:
        return True
    if re.match(r"^(?:cd\s+[^;&|\n]+|export\s+[A-Za-z_][\w]*=|"
                r"[A-Za-z_][\w]*=|:|true(?:\s|$))", s):
        return True
    return False


def _segment_has_remote_exec(segment: str) -> bool:
    """Remote client at the executable position of one simple shell command."""
    s = str(segment or "").strip()
    # Drop common wrappers without accepting arbitrary prose that happens to
    # mention a client name.
    while True:
        prior = s
        s = re.sub(r"^(?:sudo\s+(?:-[\w=,-]+\s+)*|command\s+|builtin\s+|"
                   r"time\s+|timeout\s+(?:-[\w=,-]+\s+)*\S+\s+)", "", s,
                   flags=re.IGNORECASE)
        if s.lower().startswith("env "):
            tail = s[4:]
            tail = re.sub(r"^(?:[A-Za-z_][\w]*=\S+\s+)+", "", tail)
            s = tail
        if s == prior:
            break
    word = re.match(r"([A-Za-z0-9_.+-]+)\b", s)
    if word and word.group(1).lower() in _REMOTE_SHELL_TOOLS:
        return True
    match = _INLINE_CODE_RX.search(s)
    return bool(match and _code_has_remote_primitive(match.group("code")))


def _shell_command_has_live_remote_io(cmd: str) -> bool:
    """Strict source-command test for a live remote response.

    A provenance record covers the *whole* tool call, not each shell substep.
    To prevent `cat old-answer; curl ...` from laundering local output as
    remote, accept one remote-producing segment with only setup clauses around
    it and no pipeline whose later stage can synthesize unrelated output.
    """
    parts = _shell_control_segments(_strip_shell_comments(cmd))
    remote_indices = [i for i, part in enumerate(parts)
                      if _segment_has_remote_exec(part)]
    if len(remote_indices) != 1:
        return False
    remote_idx = remote_indices[0]
    if _has_unquoted_pipe(parts[remote_idx]):
        return False
    return all(_shell_setup_clause(part) for i, part in enumerate(parts)
               if i != remote_idx)

# [B40] 脚本中介的网络交互 —— 首现命令形如 `python3 rce.py "cat /challenge/flag.txt"`
# 时，网络逻辑在**脚本文件内部**，命令行上搜不到任何网络原语 → 真解（活靶标响应）
# 被误判成「本地静态产物」而拒绝强提（实测两题真 flag 因此丢失，平台侧
# verified_in_source=true）。判据：命令是「解释器 + 脚本文件」形式，**且**该脚本
# 内容命中网络原语。脚本体由调用方取证（本场工具调用里的写脚本 heredoc / 写文件
# 工具参数 / 工作目录产物），见 collect_script_bodies。
# 反例仍拦得住：纯本地解码脚本（hashlib/RC4/XOR 静态诱饵）不含网络原语 → 依旧判
# local；把 flag 明文写进脚本再 print 的伪造，会在更早的「写脚本」事件上被
# 「参数先现即自造」规则判否，且 collect_script_bodies(forbid=flag) 直接丢弃该脚本。
_SCRIPT_EXEC_RX = re.compile(
    r"(?<![\w./-])(?:python3?(?:\.\d+)?|bash|sh|zsh|perl|ruby|node|php)"
    r"\s+(?:-{1,2}[\w=.-]+\s+)*"                       # 允许 -u / --foo 等旗标
    r"(?P<script>[^\s'\";|&<>()]+\.(?:py|sh|pl|rb|js|php))",
    re.IGNORECASE,
)
_SCRIPT_SUFFIXES = (".py", ".sh", ".pl", ".rb", ".js", ".php")

# [B42] 命令位置**直接执行**脚本 —— `./post.sh "<payload>"` / `/tmp/x.py` /
# 行首裸 `post.sh a b`。B40 的 _SCRIPT_EXEC_RX 只认「解释器 + 脚本」，这类形态
# 下 script_path_in_command 返回空 → 脚本中介的远端交互全程失联：eager 侧判
# provenance=local → local_computed_only；强提侧判「首现命令非网络交互」→ 真解
# 被否（实测某题：首现命令 `cd /work/<题目录> && … ./post.sh "$h"`，同场 heredoc
# 写的 post.sh 含 `curl -s -m 10 -X POST … http://<靶标>:8000/`，靶标回显
# {"flag":…,"status":"bypassed"}，仍被拒）。
# 只在**命令位置**（行首 / 换行 / ; | & ( ) 之后）匹配；`cat post.sh`、
# `ls -la ./x.py` 这类「提及文件」不匹配。是否算远端仍由脚本体决定
# （script_body_is_remote），本地解码脚本照旧判否。
_SCRIPT_DIRECT_RX = re.compile(
    r"(?:^|[;|&()\n])[ \t]*"
    r"(?:sudo\s+|exec\s+|nohup\s+|time\s+|env\s+[\w.]+=\S*\s+)*"
    r"(?P<script>(?:\.{1,2}/|/)?(?:[\w.@+-]+/)*[\w.@+-]+\.(?:py|sh|pl|rb|js|php))"
    r"(?=[\s;|&<>)\x22']|$)",
    re.IGNORECASE | re.MULTILINE,
)
_HEREDOC_RX = re.compile(r"<<\s*-?\s*['\"]?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)['\"]?")
_HEREDOC_TARGET_RX = re.compile(
    r"(?:>{1,2}|tee\s+(?:-a\s+)?)\s*['\"]?(?P<path>[^\s'\">|;&]+\.(?:py|sh|pl|rb|js|php))")


def script_path_in_command(cmd: str) -> str:
    """命令若调用了脚本文件，返回脚本路径；否则空串（B40/B42）。

    两种形态：解释器前缀（`python3 rce.py`，B40）与命令位置直接执行
    （`./post.sh "<payload>"`，B42）。解释器形态优先，避免把
    `python3 ./post.sh` 的路径判错。
    """
    cmd = cmd or ""
    m = _SCRIPT_EXEC_RX.search(cmd)
    if m:
        return m.group("script")
    m = _SCRIPT_DIRECT_RX.search(cmd)
    return m.group("script") if m else ""


def script_body_for(script_bodies: dict, script_path: str) -> str:
    """取脚本体：先按原路径、再按 basename 兜底（B42）。

    取证侧记的键来自 heredoc/写文件工具的字面路径（`post.sh`），而命令里常写
    `./post.sh` —— 不做 basename 兜底就会「脚本明明在手边却查不到」。
    """
    if not script_bodies or not script_path:
        return ""
    return (script_bodies.get(script_path)
            or script_bodies.get(os.path.basename(script_path)) or "")


def script_body_is_remote(body: str) -> bool:
    """脚本内容是否含网络客户端原语（→ 与远端目标交互，B40）。"""
    if not body:
        return False
    if _code_has_remote_primitive(body):
        return True
    return any(_shell_command_has_live_remote_io(line)
               for line in str(body).splitlines() if line.strip())


def collect_script_bodies(*records, forbid: str = "") -> dict:
    """[B40] 从工具调用记录回捞脚本内容 {路径: 内容}。

    两条来源：shell heredoc（`cat > x.py <<'EOF' … EOF`）与写文件类工具的参数
    （path/file_path + content/new_str…）。含 forbid（通常是该 flag 明文）的脚本
    一律丢弃 —— 那是 agent 把答案写进脚本自己 print，不是靶标产出。
    """
    bodies: dict = {}

    def _add(path, body):
        path = str(path or "")
        if not path.endswith(_SCRIPT_SUFFIXES) or not body:
            return
        if forbid and forbid in body:
            return
        if len(body) > len(bodies.get(path) or ""):
            bodies[path] = body[:200000]

    for rec in records:
        if not rec:
            continue
        text = ""
        if isinstance(rec, dict):
            _cmd = rec.get("command") or rec.get("cmd")
            if isinstance(_cmd, str):
                text = _cmd
            for pk in ("path", "file_path", "filePath", "filename", "file"):
                for ck in ("content", "file_text", "new_str", "new_string", "text", "body"):
                    pv, cv = rec.get(pk), rec.get(ck)
                    if isinstance(pv, str) and isinstance(cv, str):
                        _add(pv, cv)
        else:
            text = str(rec)
        for m in _HEREDOC_RX.finditer(text):
            head = text[max(0, m.start() - 400):m.start()]
            tgt = list(_HEREDOC_TARGET_RX.finditer(head))
            rest = text[m.end():]
            if not tgt:      # 变体：`cat <<'EOF' > x.py`（目标写在标记之后）
                tgt = list(_HEREDOC_TARGET_RX.finditer(rest.split("\n", 1)[0]))
            if not tgt:
                continue
            end = re.search(r"(?m)^[ \t]*" + re.escape(m.group("tag")) + r"[ \t]*;?[ \t]*$", rest)
            _add(tgt[-1].group("path"), rest[:end.start()] if end else rest)
    return bodies


def is_remote_command(cmd: str, script_bodies: dict = None) -> bool:
    """证据命令是否与远端目标发生过网络交互（→ 活靶标响应）。

    script_bodies: 可选 {脚本路径: 内容}。命令本身没有网络原语、但它是
    「解释器 + 脚本文件」调用且脚本体含网络原语时，同样算远端交互（B40）。
    """
    if not cmd:
        return False
    if _shell_command_has_live_remote_io(cmd):
        return True
    if script_bodies:
        sp = script_path_in_command(cmd)
        if sp and script_body_is_remote(script_body_for(script_bodies, sp)):
            return True
    return False


def script_mediated_remote(cmd: str, script_bodies: dict) -> bool:
    """命令是否「经脚本中介与远端交互」—— 网络原语来自脚本体而非命令行（B40）。"""
    if not cmd or not script_bodies:
        return False
    if _shell_command_has_live_remote_io(cmd):
        return False          # 命令行自己就有网络原语 → 走原判据
    sp = script_path_in_command(cmd)
    return bool(sp) and script_body_is_remote(script_body_for(script_bodies, sp))


def strip_quoted_payloads(s: str) -> str:
    """摘掉命令里被引号包住的参数（远程 exploit 的 payload 正文）。

    只在「经脚本中介的远端命令」上使用：payload 里的 /challenge/flag.txt 之类
    是发给靶标的远程路径，不该被 _AGENT_FILE_RX 当成「读自己的假设文件」。
    命令行未被引号包裹的本地路径（cat /work/<code>/FLAG）依旧会被逮住。
    """
    return re.sub(r"\"[^\"]*\"|'[^']*'", " ", s or "")


@dataclass(frozen=True)
class FlagEvidencePolicy:
    """本题的证据来源边界。

    ``remote_only`` 继续要求活靶场响应；``local_allowed`` 不是“本地一律
    放行”，而是允许来自平台声明的附件/输入或无网络本地题的可复现输出。
    所有策略都仍要求完整 flag 信封先出现在工具输出，且拒绝 agent 自写
    FLAG/MEMORY/转录的回显。
    """

    category: str = "unknown"
    has_targets: bool = False
    declared_inputs: tuple[str, ...] = ()
    local_allowed: bool = False
    # A network-backed reverse/forensics/etc. task may publish its original
    # challenge artifact from the live target.  This is deliberately separate
    # from ``local_allowed``: it does *not* authorize arbitrary local files.
    remote_artifact_allowed: bool = False
    # Normalized (host, port, scheme) endpoints supplied for this task.  They
    # bind a later downloader invocation to the current instance rather than
    # merely to an arbitrary URL mentioned in an agent command.
    target_authorities: tuple[tuple[str, int | None, str], ...] = ()
    task_workdir: str = ""

    @property
    def mode(self) -> str:
        if self.local_allowed:
            return "mixed" if self.has_targets else "local_or_remote"
        if self.remote_artifact_allowed:
            return "remote_or_target_artifact"
        return "remote_only"


# 这些题型天然可能由附件、二进制、流量包或离线密码材料完成。它们**不是**
# 自动放行条件：还必须满足 ``is_local_evidence_command`` 的来源规则。
_LOCAL_NATIVE_CATEGORIES = frozenset({"reverse", "crypto", "forensics", "misc", "pwn"})


def _declared_input_names(files) -> tuple[str, ...]:
    """把平台附件元数据规范为不含目录/URL 的安全文件名集合。"""
    names: list[str] = []
    for value in files or ():
        if isinstance(value, dict):
            value = (value.get("path") or value.get("file_path")
                     or value.get("filename") or value.get("name") or "")
        text = str(value or "").strip().split("?", 1)[0].rstrip("/")
        name = os.path.basename(text)
        if (name and name not in (".", "..") and len(name) <= 255
                and name not in names):
            names.append(name)
    return tuple(names[:64])


def _target_authorities(targets) -> tuple[tuple[str, int | None, str], ...]:
    """Normalize task endpoints without resolving DNS or trusting raw URLs."""
    rows: list[tuple[str, int | None, str]] = []
    values = (targets,) if isinstance(targets, (str, bytes)) else (targets or ())
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        # ``container_addr`` commonly uses host:port without a scheme.
        parsed = urlsplit(raw if "://" in raw else "//" + raw)
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            continue
        try:
            port = parsed.port
        except ValueError:
            continue
        scheme = (parsed.scheme or "").lower()
        row = (host, port, scheme)
        if row not in rows:
            rows.append(row)
    return tuple(rows[:64])


def _task_workdir_path(workdir) -> str:
    """Return an absolute, non-shell task workdir or an empty safe fallback."""
    value = str(workdir or "").strip()
    if not value or not os.path.isabs(value) or any(ch in value for ch in "$`\n\r"):
        return ""
    return os.path.normpath(value)


def flag_evidence_policy(category: str = "", *, targets=None, files=None,
                         workdir: str = "") -> FlagEvidencePolicy:
    """根据题目元数据选择本地/远端取证边界，绝不依赖题号或历史答案。

    平台明确声明附件时，附件本身就是合法的原始题目材料，即使题目还有网络
    服务也可走本地取证；没有附件时，只给“无目标地址的本地原生题”开放本地
    路径。对于有目标的本地原生题，仅额外允许“从当前目标下载的原始产物”
    走一条严格的、可追溯的本地分析路径；Web、内网、云等仍保持 remote-only。
    """
    cat = str(category or "unknown").strip().lower() or "unknown"
    target_values = (targets,) if isinstance(targets, (str, bytes)) else (targets or ())
    has_targets = bool([item for item in target_values if str(item or "").strip()])
    declared = _declared_input_names(files)
    local_allowed = bool(declared) or (not has_targets and cat in _LOCAL_NATIVE_CATEGORIES)
    authorities = _target_authorities(targets)
    remote_artifact_allowed = bool(
        has_targets and authorities and cat in _LOCAL_NATIVE_CATEGORIES)
    return FlagEvidencePolicy(
        category=cat,
        has_targets=has_targets,
        declared_inputs=declared,
        local_allowed=local_allowed,
        remote_artifact_allowed=remote_artifact_allowed,
        target_authorities=authorities,
        task_workdir=_task_workdir_path(workdir),
    )


# 本地证据仍需是“读/运行原始题目材料”的可复现命令。写入自己的文件、读
# FLAG/MEMORY/转录、或把候选塞进命令再回显都不是题目取证。
#
# 旧实现只做一个不带结束边界的大小写不敏感 substring search。这会把普通
# 代码/说明误认为状态文件：`flag=bytes(...)` 命中 ``FLAG``，而
# `echo "source binary"` 命中 ``SOURCE``。一旦候选在同一调用输出，整条真实
# 证据就会被错误标成 ``agent_authored``。保留这个正则作为
# 显式文件名的低层词法兜底，但上层判定统一走下面的上下文感知 helper。
_AGENT_STATE_FILE_RX = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:"
    r"FLAG(?:\.(?:txt|md|json|log))?|SOURCE(?:\.(?:txt|md|json|log))?|"
    r"MEMORY(?:\.md?)?|_?blackboard[\w._-]*|todolist[\w._-]*|"
    r"tried_commands[\w._-]*|_transcripts|notes[\w._-]*"
    r")(?![A-Za-z0-9_.=:-])"
)

_AGENT_STATE_BASE_RX = re.compile(
    r"^(?:FLAG(?:\.(?:txt|md|json|log))?|SOURCE(?:\.(?:txt|md|json|log))?|"
    r"MEMORY(?:\.md?)?|_?blackboard[\w._-]*|todolist[\w._-]*|"
    r"tried_commands[\w._-]*|_transcripts|notes[\w._-]*)$",
    re.IGNORECASE,
)
_STATE_READER_PROGRAMS = frozenset({
    "cat", "head", "tail", "less", "more", "sed", "awk", "grep", "egrep",
    "fgrep", "rg", "sha1sum", "sha256sum", "md5sum", "wc", "cut", "sort",
    "uniq", "strings", "xxd", "od", "hexdump", "file", "cp", "mv", "ln",
    "install", "tee", "source", ".", "python", "python3", "python3.0",
    "python3.1", "python3.2", "python3.3", "python3.4", "python3.5",
    "python3.6", "python3.7", "python3.8", "python3.9", "python3.10",
    "python3.11", "python3.12", "python3.13", "python3.14", "perl", "ruby",
    "node", "php", "bash", "sh", "zsh",
})
_STATE_NON_CONSUMER_PROGRAMS = frozenset({
    "echo", "printf", ":", "true", "false", "test", "[", "ls", "stat",
    "pwd", "basename", "dirname",
})


def _state_file_token(value: str) -> bool:
    """Whether one *token* names a framework state file.

    This deliberately works on a shell token/basename rather than arbitrary
    substrings.  Thus ``flag=bytes`` and ``source binary`` are not state-file
    references, while ``/work/task/FLAG`` and ``MEMORY.md`` are.
    """
    token = str(value or "").strip().strip("\"'")
    if not token or any(ch in token for ch in "$`{}()=\n\r"):
        return False
    # Shell punctuation attached to a path is not part of its basename.
    token = token.rstrip(";,|&<>")
    base = os.path.basename(token.rstrip("/"))
    return bool(base and _AGENT_STATE_BASE_RX.fullmatch(base))


def _agent_state_file_mentioned(cmd: str, script_bodies: dict | None = None) -> bool:
    """Return true only when a command actually consumes/writes state files.

    ``_AGENT_STATE_FILE_RX`` used to classify any prose occurrence of words such
    as *source* or *flag*.  Parse simple shell clauses instead: exact state-file
    tokens are considered only for file-reading programs or redirection targets.
    Inline ``open('FLAG')``/``cat FLAG`` helpers remain covered by a narrow
    explicit call pattern.  URLs and script-mediated remote payloads are removed
    first so ``curl http://target/flag`` stays a remote observation.
    """
    text = str(cmd or "")
    if not text:
        return False
    try:
        text = _cmd_without_payloads(text, script_bodies)
    except Exception:
        text = _strip_shell_comments(text)

    # Exact redirection destinations (``> FLAG``/``< MEMORY.md``) are state
    # access even though they are not ordinary command arguments.
    for clause in _shell_control_segments(text):
        try:
            words = shlex.split(clause, comments=False, posix=True)
        except (TypeError, ValueError):
            words = []
        if not words:
            continue
        # Find the executable after env/sudo/command wrappers.
        idx = _command_program_index(words)
        program = os.path.basename(words[idx]).lower() if idx < len(words) else ""
        if program in _STATE_NON_CONSUMER_PROGRAMS:
            # ``echo \"=== FLAG ===\"`` is presentation, not a state-file read.
            pass
        else:
            for word in words[idx + 1:] if idx < len(words) else words:
                if _state_file_token(word):
                    return True
        # Redirection targets are retained as individual words by shlex in the
        # common ``> FLAG`` form.  Handle them explicitly regardless of program.
        for pos, word in enumerate(words[:-1]):
            if word in {">", ">>", "<", "<<", "<<<", "1>", "2>", "&>"} \
                    and _state_file_token(words[pos + 1]):
                return True

    # Python/JS/etc. inline readers can keep the path inside one quoted token,
    # which shlex intentionally does not split.  Require an actual read/open
    # primitive plus an exact state-file token, not a bare word in prose.
    read_rx = re.compile(
        r"(?:\b(?:open|Path|read_text|read_bytes|readfile(?:sync)?|"
        r"file_get_contents|fopen|cat|head|tail|sha(?:1|256)sum)\b[^\n]{0,180})",
        re.IGNORECASE,
    )
    for match in read_rx.finditer(text):
        if any(_state_file_token(tok) for tok in re.findall(
                r"(?<![A-Za-z0-9_.-])[^\s,()]+", match.group(0))):
            return True
    return False
_LOCAL_MUTATION_RX = re.compile(
    r"(?:^|[;&|]\s*)[^\n]*?(?:>{1,2}|\btee\b|\btouch\b|\btruncate\b|\bln\b|\bmv\b|\bcp\b"
    r"|\brm\b|\bdd\b[^\n]*\bof=|\bsed\s+-i\b|\bperl\s+-pi\b|open\([^\n]{0,200}[,'\"]w"
    r"|\bPath\s*\([^\n)]*\)\s*\.\s*write_(?:text|bytes)\s*\()",
    re.IGNORECASE,
)


def _mask_heredoc_bodies(text: str) -> str:
    """Mask heredoc payloads while retaining the shell command header.

    A regex-only mutation detector sees the ``>`` characters in Python/sed code
    inside a heredoc and mistakes them for shell redirections.  The payload is
    still inspected separately by the explicit Python write detectors below;
    here we only need a shell view for real redirection operators.
    """
    raw = str(text or "")
    if "<<" not in raw:
        return raw
    chars = list(raw)
    heredoc_rx = re.compile(r"<<\s*-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
    for match in heredoc_rx.finditer(raw):
        body_start = raw.find("\n", match.end())
        if body_start < 0:
            continue
        body_start += 1
        end_rx = re.compile(r"(?m)^[ \t]*" + re.escape(match.group(2))
                          + r"[ \t]*;?[ \t]*$")
        end = end_rx.search(raw, body_start)
        body_end = end.start() if end else len(raw)
        for idx in range(body_start, body_end):
            if chars[idx] not in "\r\n":
                chars[idx] = " "
    return "".join(chars)


def _shell_redirection_targets(cmd: str) -> tuple[set[str], bool]:
    """Return unquoted shell output/input redirection targets and a write bit.

    ``2>&1`` is a file-descriptor duplication, not a mutation of a challenge
    artifact, and must not taint provenance.  Quoted ``>`` in sed/Python source
    and heredoc payloads is likewise ignored.  The parser is intentionally
    conservative; an opaque malformed operator sets the write bit but yields no
    target, preserving fail-closed behavior for declared inputs.
    """
    text = _mask_heredoc_bodies(_strip_shell_comments(str(cmd or "")))
    targets: set[str] = set()
    saw_write = False
    quote = ""
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch != ">":
            i += 1
            continue
        # ``2>&1`` / ``>&2`` duplicate an existing descriptor; no pathname is
        # involved.  ``>&file`` remains a genuine write redirection.
        if i + 1 < len(text) and text[i + 1] == "&":
            j = i + 2
            while j < len(text) and text[j].isspace():
                j += 1
            if j < len(text) and (text[j].isdigit() or text[j] == "-"):
                i = j + 1
                continue
            saw_write = True
            i = j
        else:
            saw_write = True
            i += 1
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text) or text[i] in ";|&<>\r\n":
            continue
        if text[i] in ("'", '"'):
            q = text[i]
            i += 1
            start = i
            while i < len(text) and text[i] != q:
                if text[i] == "\\" and q == '"' and i + 1 < len(text):
                    i += 2
                else:
                    i += 1
            value = text[start:i]
            if i < len(text):
                i += 1
        else:
            start = i
            while i < len(text) and not text[i].isspace() \
                    and text[i] not in ";|&<>":
                i += 1
            value = text[start:i]
        if value:
            targets.update(_path_aliases(value))
    return targets, saw_write


def _has_local_mutation(cmd: str) -> bool:
    """Whether a command performs a real local write/mutation.

    This replaces the old broad ``_LOCAL_MUTATION_RX.search`` checks.  In
    particular, stderr redirections (``2>&1``), quoted source code, and harmless
    ``chmod``/read-only probes must not taint a downloaded artifact.
    """
    text = str(cmd or "")
    _targets, redirected = _shell_redirection_targets(text)
    if redirected:
        return True
    # Explicit Python/Path writes and compiler output are unambiguous even when
    # embedded in an inline interpreter/heredoc.
    if _LOCAL_PY_WRITE_PATH_RX.search(text) or _LOCAL_COMPILE_OUTPUT_RX.search(text):
        return True
    # Inspect every pipeline stage, not only the first program.  In particular
    # `producer | tee trusted-artifact` is a real overwrite even though the
    # shell clause begins with `producer`.
    shell_view = _mask_heredoc_bodies(_strip_shell_comments(text))
    for clause in _shell_control_segments(shell_view):
        for stage in _shell_pipeline_stages(clause):
            try:
                words = shlex.split(stage, comments=False, posix=True)
            except (TypeError, ValueError):
                continue
            if not words:
                continue
            idx = _command_program_index(words)
            if idx >= len(words):
                continue
            program = os.path.basename(words[idx]).lower()
            args = words[idx + 1:]
            if program in {"cp", "mv", "ln", "install", "touch", "truncate", "rm", "tee"}:
                return True
            if program == "dd" and any(str(value).startswith("of=") for value in args):
                return True
            if program in {"sed", "perl"} and any(
                    value == "-i" or value.startswith("-i") or value.startswith("-pi")
                    for value in args):
                return True
    return False
_LOCAL_DIRECT_PROGRAM_RX = re.compile(
    r"(?:^|[;&|]\s*)(?:env\s+(?:[A-Za-z_][\w]*=\S+\s+)*)?"
    r"(?P<program>\./[^\s;|&]+|/[^\s;|&]+)"
    r"(?![^\s;|&]*\.(?:py|sh|pl|rb|js|php)(?:\s|$))",
    re.IGNORECASE,
)

# A filename merely appearing somewhere in a shell command is not proof that
# the command consumed that file.  In particular, `cat /tmp/result; : input`
# used to satisfy the old substring check even though the displayed result was
# entirely unrelated to the official input.  Keep the accepted local route
# narrow enough that a source command has one data-producing operation (pipes
# remain part of that operation), with only harmless setup clauses before it.
_LOCAL_NON_CONSUMER_RX = re.compile(
    r"^\s*(?:(?:command|builtin)\s+)?(?:echo|printf|:|true|false|test|\[|"
    r"ls|stat|file|basename|dirname|pwd)\b",
    re.IGNORECASE,
)
_LOCAL_STATIC_SCAN_RX = re.compile(
    r"^\s*(?:(?:command|builtin)\s+)?(?:strings|grep|egrep|fgrep|rg|"
    r"rabin2|objdump|readelf|xxd|hexdump|od)\b",
    re.IGNORECASE,
)
_LOCAL_SETUP_RX = re.compile(
    r"^\s*(?:cd\s+[^;&|\n]+|export\s+[A-Za-z_][\w]*=.*|"
    r"(?:[A-Za-z_][\w]*=\S+\s*)+)\s*$",
    re.IGNORECASE,
)
_LOCAL_WRITE_TARGET_RX = re.compile(
    r"(?:>{1,2}|\btee\b(?:\s+-[A-Za-z]+)*)\s*"
    r"(?:['\"](?P<quoted>[^'\"]+)['\"]|(?P<bare>(?!&)[^\s;|&<>]+))",
    re.IGNORECASE,
)
_LOCAL_MUTATED_SCRIPT_RX = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<path>(?:\.{1,2}/|/)?"
    r"(?:[\w@+.-]+/)*[\w@+.-]+\.(?:py|sh|pl|rb|js|php))"
    r"(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_LOCAL_PY_WRITE_PATH_RX = re.compile(
    r"\b(?:open|Path)\s*\(\s*['\"](?P<path>[^'\"]+)['\"]"
    # ``r+b`` / ``rb+`` are in-place binary writes too.  Treating only
    # ``w/a/x`` as mutations let an agent patch a derived ELF and keep its
    # target-artifact lineage by spelling the write as ``open(path, 'r+b')``.
    r"(?:(?:\s*,\s*['\"](?=[^'\"]*(?:[wax]|\+))[^'\"]*['\"])|[^\n)]{0,160}?"
    r"\)\s*\.\s*write_(?:text|bytes)\s*\()",
    re.IGNORECASE,
)
_LOCAL_COMPILE_OUTPUT_RX = re.compile(
    r"\b(?:gcc|g\+\+|clang(?:\+\+)?|cc|rustc)\b[^\n;|&]{0,1000}?"
    r"(?:^|\s)-o\s+(?:['\"](?P<quoted>[^'\"]+)['\"]|(?P<bare>[^\s;|&<>]+))",
    re.IGNORECASE,
)
_INLINE_INTERPRETER_RX = re.compile(
    r"(?<![\w./-])(?:python(?:3(?:\.\d+)?)?|bash|sh|zsh|perl|ruby|node|php)"
    r"\s+(?:-[A-Za-z0-9_.=]+\s+)*(?:-[A-Za-z]*[ce]|--(?:command|eval)|-r)\b",
    re.IGNORECASE,
)
# A helper script written during the current trace is allowed only when its
# body visibly reads the declared input (either directly or through argv).  A
# script that merely prints an encoded constant must not become evidence just
# because it is invoked as `python solve.py official.bin`.
_SCRIPT_FILE_READ_RX = re.compile(
    r"\b(?:open|path|read_text|read_bytes|readfile(?:sync)?|createreadstream|"
    r"file_get_contents|fopen|cat|xxd|od|hexdump|strings|dd|binwalk|tshark)\b",
    re.IGNORECASE,
)
_SCRIPT_ARG_FILE_READ_RX = re.compile(
    r"(?:"
    r"\b(?:open|path|read_text|read_bytes|readfile(?:sync)?|createreadstream|"
    r"file_get_contents|fopen)\s*\([^\n)]{0,200}(?:sys\.)?argv\b"
    r"|\b(?:cat|xxd|od|hexdump|strings|dd|binwalk|tshark)\b[^\n]{0,160}"
    r"(?:\$\{?(?:1|@|\*)\}?|(?:sys\.)?argv\b)"
    r")",
    re.IGNORECASE,
)


def _path_aliases(path: str) -> set[str]:
    """Return conservative spellings for a path recorded in a tool trace."""
    text = str(path or "").strip().strip("\"'")
    if not text or text in (".", ".."):
        return set()
    try:
        text = os.path.normpath(text)
    except (TypeError, ValueError):
        return set()
    aliases = {text}
    base = os.path.basename(text)
    if base:
        aliases.add(base)
    return aliases


def _command_mentions_path(cmd: str, paths) -> bool:
    """Whether ``cmd`` refers to one of the exact/basename path aliases."""
    text = str(cmd or "")
    for path in paths or ():
        for alias in _path_aliases(path):
            if re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(alias)
                         + r"(?![A-Za-z0-9_.-])", text):
                return True
    return False


def _cmd_without_write_targets(cmd: str) -> str:
    """Remove shell write destinations before checking for an authored read."""
    text = str(cmd or "")
    # Keep quoted source code intact and blank only actual shell redirection
    # destinations.  The old regex removed arbitrary text after ``>`` inside a
    # sed expression, which could hide a real authored-path reference.
    masked = _mask_heredoc_bodies(_strip_shell_comments(text))
    out = list(text)
    quote = ""
    escaped = False
    i = 0
    while i < len(masked):
        ch = masked[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch != ">":
            i += 1
            continue
        # Descriptor duplication (2>&1) has no destination pathname.
        j = i + 1
        if j < len(masked) and masked[j] == ">":
            j += 1
        if j < len(masked) and masked[j] == "&":
            k = j + 1
            while k < len(masked) and masked[k].isspace():
                k += 1
            if k < len(masked) and (masked[k].isdigit() or masked[k] == "-"):
                i = k + 1
                continue
            j = k
        while j < len(masked) and masked[j].isspace():
            j += 1
        if j >= len(masked) or masked[j] in ";|&<>\r\n":
            i = j
            continue
        end = j
        if masked[j] in ("'", '"'):
            q = masked[j]
            end = j + 1
            while end < len(masked) and masked[end] != q:
                end += 1
            if end < len(masked):
                end += 1
        else:
            while end < len(masked) and not masked[end].isspace() \
                    and masked[end] not in ";|&<>":
                end += 1
        for k in range(j, min(end, len(out))):
            out[k] = " "
        i = end
    return "".join(out)


def _mutation_target_paths(cmd: str) -> set[str]:
    """Best-effort destinations of shell/file mutations in one tool call."""
    text = str(cmd or "")
    targets: set[str] = set()
    redir_targets, _redir = _shell_redirection_targets(text)
    targets.update(redir_targets)
    for match in _LOCAL_PY_WRITE_PATH_RX.finditer(text):
        targets.update(_path_aliases(match.group("path")))
    for match in _LOCAL_COMPILE_OUTPUT_RX.finditer(text):
        targets.update(_path_aliases(match.group("quoted") or match.group("bare") or ""))

    # Copy/link/build-like utilities spell the destination as their final
    # positional token rather than via redirection.  This is intentionally
    # limited to well-known mutators; opaque shell constructs fail closed in
    # ``local_input_mutated`` below when they also mention a declared input.
    # A mutator may be on the right-hand side of a pipeline.  Mask heredoc
    # bodies first: their source code is not shell syntax, while explicit
    # Python writes above remain handled by their dedicated parser.
    shell_view = _mask_heredoc_bodies(_strip_shell_comments(text))
    for clause in _shell_control_segments(shell_view):
        for stage in _shell_pipeline_stages(clause):
            try:
                words = shlex.split(stage, comments=False, posix=True)
            except ValueError:
                continue
            if not words:
                continue
            idx = _command_program_index(words)
            if idx >= len(words):
                continue
            program = os.path.basename(words[idx]).lower()
            raw_args = words[idx + 1:]
            args = [word for word in raw_args if not word.startswith("-")]
            if program in {"cp", "mv", "ln", "install"} and len(args) >= 2:
                targets.update(_path_aliases(args[-1]))
            elif program in {"touch", "truncate"}:
                for value in args:
                    targets.update(_path_aliases(value))
            elif program == "tee":
                # The last stage often has its own output redirection
                # (`tee artifact >/dev/null`).  That redirection is already
                # tracked above; do not mistake it for an additional tee path.
                skip_next = False
                for value in raw_args:
                    if skip_next:
                        skip_next = False
                        continue
                    if value in {">", ">>", "<", "<<", "<<<", "1>", "2>", "&>"}:
                        skip_next = True
                        continue
                    if value.startswith("-") or value.startswith((">", "<")) \
                            or re.match(r"^\d*(?:>>?|<)", value):
                        continue
                    targets.update(_path_aliases(value))
            elif program == "dd":
                for value in raw_args:
                    if value.startswith("of="):
                        targets.update(_path_aliases(value[3:]))
    return targets


def _trace_path(path: str, cwd: str = "") -> str:
    """Resolve a literal trace path without consulting the real filesystem.

    Evidence is evaluated after the agent session, so using ``realpath`` here
    would make provenance depend on a mutable filesystem.  Only literal,
    absolute paths (or paths made absolute by a recorded ``cd``/task workdir)
    are accepted.
    """
    text = str(path or "").strip().strip("\"'")
    if (not text or text in ("-", ".", "..") or "\x00" in text
            or any(ch in text for ch in "$`*?~")):
        return ""
    if os.path.isabs(text):
        return os.path.normpath(text)
    if cwd and os.path.isabs(cwd):
        return os.path.normpath(os.path.join(cwd, text))
    return ""


def _command_parts_with_cwd(cmd: str, policy: FlagEvidencePolicy):
    """Yield non-``cd`` command clauses with their trace-visible cwd.

    This intentionally recognizes only literal ``cd`` setup clauses.  A
    dynamic shell expression fails closed rather than turning an arbitrary
    same-basename file into a downloaded challenge artifact.
    """
    cwd = policy.task_workdir
    for clause in _shell_control_segments(_strip_shell_comments(cmd)):
        try:
            words = shlex.split(clause, comments=False, posix=True)
        except ValueError:
            words = []
        if words:
            idx = 0
            while idx < len(words) and words[idx] in {"builtin", "command"}:
                idx += 1
            if idx < len(words) and words[idx] == "cd":
                args = [word for word in words[idx + 1:] if not word.startswith("-")]
                if len(args) == 1:
                    resolved = _trace_path(args[0], cwd)
                    if resolved:
                        cwd = resolved
                # A cd clause cannot itself be the flag-producing operation.
                continue
        yield clause, cwd, words


def _command_program_index(words: list[str]) -> int:
    """Locate a simple command after common shell wrappers conservatively."""
    idx = 0
    while idx < len(words) and "=" in words[idx] and not words[idx].startswith("="):
        idx += 1
    while idx < len(words):
        name = os.path.basename(words[idx]).lower()
        if name in {"command", "builtin", "time", "nohup"}:
            idx += 1
            while idx < len(words) and words[idx].startswith("-"):
                idx += 1
            continue
        if name == "sudo":
            idx += 1
            while idx < len(words) and words[idx].startswith("-"):
                idx += 1
            continue
        if name == "env":
            idx += 1
            while idx < len(words) and "=" in words[idx] and not words[idx].startswith("="):
                idx += 1
            continue
        if name == "timeout":
            idx += 1
            while idx < len(words) and words[idx].startswith("-"):
                idx += 1
            if idx < len(words):
                idx += 1                    # duration
            continue
        break
    return idx


def _word_is_current_target(word: str, policy: FlagEvidencePolicy) -> bool:
    """Whether one direct client argument names this task's target endpoint.

    The parser is deliberately literal: variables, substitutions and ambiguous
    URI schemes fail closed.  This is a provenance predicate, not a general
    connectivity validator, so accepting only an endpoint visibly present in
    the recorded command is the useful security boundary.
    """
    value = str(word or "").strip().strip("\"'")
    if not value or any(ch in value for ch in "$`*?{}"):
        return False
    has_scheme = bool(re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", value))
    try:
        parsed = urlsplit(value if has_scheme else "//" + value)
        port = parsed.port
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    if has_scheme:
        # ``file:``, ``data:``, unix sockets and arbitrary schemes are never
        # live target evidence, even if a decorative target string follows.
        if scheme not in {"http", "https"}:
            return False
        return _url_is_current_target(value, policy)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    for target_host, target_port, _target_scheme in policy.target_authorities:
        if host != target_host:
            continue
        # For non-HTTP clients, an explicitly declared port must be visible in
        # the command too; a bare hostname only binds an endpoint with no port
        # metadata, avoiding accidental authorization of another service.
        if target_port is None:
            return port is None
        if port == target_port:
            return True
    return False


def _and_pipeline_chain_only(cmd: str) -> bool:
    """Allow only literal ``&&`` and pipelines outside quoted shell text."""
    quote = ""
    escaped = False
    depth = 0
    text = _strip_shell_comments(cmd)
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")" and depth:
            depth -= 1
            i += 1
            continue
        if depth == 0:
            if ch in ";\r\n":
                return False
            if ch == "&":
                if i + 1 >= len(text) or text[i + 1] != "&":
                    return False
                i += 2
                continue
            if ch == "|" and i + 1 < len(text) and text[i + 1] == "|":
                return False
        i += 1
    return not quote and not escaped and depth == 0


def _is_current_target_http_stage(stage: str, policy: FlagEvidencePolicy) -> bool:
    """Whether one pipeline source is one literal current-target HTTP client."""
    if (_has_unquoted_input_redirection(stage) or _has_local_mutation(stage)
            or _INLINE_CODE_RX.search(stage) or script_path_in_command(stage)):
        return False
    try:
        words = shlex.split(stage, comments=False, posix=True)
    except (TypeError, ValueError):
        return False
    idx = _command_program_index(words)
    if idx >= len(words) or os.path.basename(words[idx]).lower() not in {"curl", "wget"}:
        return False
    urls = [value for value in words[idx + 1:]
            if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", value)]
    return len(urls) == 1 and _url_is_current_target(urls[0], policy)


def _is_response_tee_stage(stage: str, args: list[str]) -> bool:
    """Whether ``tee`` is an stdin-preserving response projection.

    ``tee`` necessarily writes a copy, so treating every tee invocation as a
    generic local mutation makes normal ``curl | grep | tee evidence`` flows
    ineligible.  This is intentionally not a general tee approval: no shell
    redirection, dynamic operand, help/version switch, or non-streaming
    option is admitted.  The only allowed switches preserve stdin and stdout.
    """
    if _has_unquoted_input_redirection(stage):
        return False
    _targets, has_output_redirection = _shell_redirection_targets(stage)
    if has_output_redirection:
        return False

    options_done = False
    for value in args:
        if any(ch in value for ch in "$`()"):
            return False
        if not options_done and value == "--":
            options_done = True
            continue
        if not options_done and value in {
                "-a", "--append", "-i", "--ignore-interrupts", "-p"}:
            continue
        if not options_done and value.startswith("--output-error="):
            mode = value.split("=", 1)[1]
            if mode in {"warn", "warn-nopipe", "exit", "exit-nopipe"}:
                continue
            return False
        # Unknown options include --help/--version, which print local text
        # instead of projecting stdin.  They cannot establish remote evidence.
        if not options_done and value.startswith("-"):
            return False
        # Dynamic/globbed destinations are not reconstructable from a trace.
        if any(ch in value for ch in "*?~"):
            return False
    return True


def _is_response_projection_stage(stage: str) -> bool:
    """Accept an identity/selecting filter that cannot read another source."""
    if (_has_unquoted_pipe(stage) or _has_unquoted_input_redirection(stage)
            or _INLINE_CODE_RX.search(stage) or script_path_in_command(stage)):
        return False
    try:
        words = shlex.split(stage, comments=False, posix=True)
    except (TypeError, ValueError):
        return False
    idx = _command_program_index(words)
    if idx >= len(words):
        return False
    program = os.path.basename(words[idx]).lower()
    if program not in _REMOTE_RESPONSE_PIPE_FILTERS:
        return False
    args = words[idx + 1:]
    if any(any(ch in value for ch in "$`()") for value in args):
        return False
    if program == "tee":
        return _is_response_tee_stage(stage, args)
    if _has_local_mutation(stage):
        return False
    # grep/rg without a filename consume stdin.  Exactly one positional token
    # is the search expression; two would permit reading an unrelated file.
    positional = [value for value in args if not value.startswith("-")]
    return len(positional) == 1


def _is_response_cleanup_setup_stage(stage: str) -> bool:
    """Allow a literal, silent ``rm -f`` before a target-response flow.

    Agents commonly clear a stale cookie jar before logging in.  Deletion does
    not supply stdout data, but accepting arbitrary local commands here would
    let a decorative request bless a local result.  Keep the exception to
    literal force-only removal with at least one literal pathname.
    """
    if _shell_setup_clause(stage):
        return True
    if _has_unquoted_pipe(stage) or _has_unquoted_input_redirection(stage):
        return False
    try:
        words = shlex.split(stage, comments=False, posix=True)
    except (TypeError, ValueError):
        return False
    idx = _command_program_index(words)
    if idx >= len(words) or os.path.basename(words[idx]).lower() != "rm":
        return False
    args = words[idx + 1:]
    if not args:
        return False
    force = False
    options_done = False
    paths = 0
    for value in args:
        if any(ch in value for ch in "$`() *?~"):
            return False
        if not options_done and value == "--":
            options_done = True
            continue
        if not options_done and value in {"-f", "--force"}:
            force = True
            continue
        if not options_done and value.startswith("-"):
            return False
        paths += 1
    return force and paths > 0


def _is_safe_current_target_response_chain(cmd: str,
                                           policy: FlagEvidencePolicy) -> bool:
    """Validate target-only HTTP response piping without local input mixing."""
    if not _and_pipeline_chain_only(cmd):
        return False
    saw_remote = False
    for segment in _shell_control_segments(_strip_shell_comments(cmd)):
        stages = _shell_pipeline_stages(segment)
        if not stages:
            return False
        if _is_current_target_http_stage(stages[0], policy):
            saw_remote = True
            if any(not _is_response_projection_stage(stage) for stage in stages[1:]):
                return False
            continue
        # A sequence may include only commandless setup around direct target
        # interactions.  A local reader/producer cannot be paired with a
        # decorative network request to claim remote provenance.
        if len(stages) != 1 or not _is_response_cleanup_setup_stage(stages[0]):
            return False
    return saw_remote


def is_task_remote_command(cmd: str, policy: FlagEvidencePolicy,
                           script_bodies: dict | None = None) -> bool:
    """Return true only for a direct network command bound to this task.

    ``is_remote_command`` answers the lower-level question "does this look
    like network IO?".  That alone cannot be submission provenance: it would
    accept ``file://`` and a response from an unrelated host.  This higher
    level predicate requires one direct client command and a literal current
    target authority.  Script/inline-code network primitives intentionally do
    not pass automatically: seeing ``requests.get`` in source does not prove
    that the call executed or supplied the printed candidate.
    """
    if not cmd or not policy.target_authorities:
        return False
    # Web flows commonly authenticate and then project a later response with
    # `grep`/`tee`.  This constrained branch keeps that response remote while
    # rejecting arbitrary local producers, other hosts, scripts, and extra
    # file inputs.  The legacy single-client path below still covers SSH/nmap
    # and other direct target tools.
    if _is_safe_current_target_response_chain(cmd, policy):
        return True
    stripped = _strip_shell_comments(cmd)
    parts = _shell_control_segments(stripped)
    remote_indices = [idx for idx, part in enumerate(parts)
                      if _segment_has_remote_exec(part)]
    if len(remote_indices) != 1:
        return False
    remote_idx = remote_indices[0]
    segment = parts[remote_idx]
    if (_has_unquoted_pipe(segment)
            or not all(_shell_setup_clause(part) for idx, part in enumerate(parts)
                       if idx != remote_idx)):
        return False
    # An inline interpreter or a script whose body contains a client is only
    # lexical evidence.  Keep it out of the automatic submit path until a
    # directly observable target interaction supplies the candidate instead.
    if _INLINE_CODE_RX.search(segment) or script_path_in_command(segment):
        return False
    try:
        words = shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return False
    program_idx = _command_program_index(words)
    if program_idx >= len(words):
        return False
    program = os.path.basename(words[program_idx]).lower()
    if program not in _REMOTE_SHELL_TOOLS:
        return False
    arguments = words[program_idx + 1:]
    if program in {"curl", "wget"}:
        # A downloader may mention only one actual HTTP(S) endpoint.  Reject
        # local schemes and multiple URLs rather than trying to infer which
        # stream produced stdout after redirects or concatenation.
        uri_args = [value for value in arguments
                    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", value)]
        if len(uri_args) != 1:
            return False
        return _url_is_current_target(uri_args[0], policy)
    return any(_word_is_current_target(value, policy) for value in arguments)


def _url_is_current_target(url: str, policy: FlagEvidencePolicy) -> bool:
    """Whether an HTTP(S) download URL is bound to this task's live endpoint."""
    try:
        parsed = urlsplit(str(url or ""))
        port = parsed.port
    except ValueError:
        return False
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    for target_host, target_port, target_scheme in policy.target_authorities:
        if host != target_host:
            continue
        if target_scheme and scheme != target_scheme:
            continue
        if target_port is not None:
            if effective_port == target_port:
                return True
        # A target supplied as a bare hostname is only safe to match on a
        # default HTTP(S) port; it must not bless arbitrary services on that
        # host.  An explicitly schemed URL has an equivalent default port.
        elif effective_port in {80, 443}:
            return True
    return False


def _target_download_destination(words: list[str], cwd: str,
                                 policy: FlagEvidencePolicy) -> str:
    """Return one explicit curl/wget output path, or empty on ambiguity.

    We deliberately support only one HTTP(S) URL plus explicit ``-o``/``-O``
    style output.  Remote-name, redirects, shell substitutions, and multi-URL
    invocations are not reconstructable from a command transcript, so they
    cannot establish artifact provenance.
    """
    idx = _command_program_index(words)
    if idx >= len(words):
        return ""
    program = os.path.basename(words[idx]).lower()
    if program not in {"curl", "wget"}:
        return ""
    args = words[idx + 1:]
    urls: list[str] = []
    outputs: list[str] = []
    i = 0
    while i < len(args):
        value = args[i]
        if value in {"-L", "--location", "--remote-name"}:
            return ""                  # final source would be unbound
        if value in {"-o", "--output", "-O", "--output-document"}:
            if i + 1 >= len(args):
                return ""
            # curl -O chooses a name from a URL; do not infer it from a
            # potentially redirected response.  wget -O is explicit.
            if value == "-O" and program == "curl":
                return ""
            outputs.append(args[i + 1])
            i += 2
            continue
        if value.startswith("--output="):
            outputs.append(value.split("=", 1)[1])
        elif value.startswith("--output-document="):
            outputs.append(value.split("=", 1)[1])
        elif value.startswith("-o") and len(value) > 2:
            outputs.append(value[2:])
        elif value.startswith("http://") or value.startswith("https://"):
            urls.append(value)
        i += 1
    if len(urls) != 1 or len(outputs) != 1 or not _url_is_current_target(urls[0], policy):
        return ""
    return _trace_path(outputs[0], cwd)


def _tool_execution_succeeded(args) -> bool:
    """Return true only for a completed, successful transcript tool call.

    Older or hand-assembled tuples intentionally fail closed here.  A quiet
    downloader cannot be trusted merely because a later command happened to
    read the same pathname: a failed download could otherwise bless a stale
    local file.
    """
    return isinstance(args, dict) and args.get(_TOOL_EXECUTION_OK_KEY) is True


def _response_download_setup_clause(words: list[str], cwd: str) -> bool:
    """Allow the one harmless setup operation needed before a response fetch."""
    idx = _command_program_index(words)
    if idx >= len(words):
        return False
    if os.path.basename(words[idx]).lower() != "mkdir":
        return False
    for value in words[idx + 1:]:
        if value.startswith("-"):
            continue
        if not _trace_path(value, cwd):
            return False
    return True


def downloaded_target_response_artifacts(
        cmd: str,
        policy: FlagEvidencePolicy,
        *,
        completed_success: bool = False,
        authored_paths: set[str] | None = None,
) -> set[str]:
    """Register one pristine current-target HTTP response stored by curl/wget.

    This is not the native ``downloaded_target_artifacts`` route.  It applies
    to every task category, but accepts only a single direct HTTP(S) request
    to this task's declared endpoint, one literal output path, no redirect or
    pipe, a successful completed tool event, and no prior agent write to that
    path.  Later source commands must pass the separate direct-reader gate.
    """
    if (not completed_success or not policy.target_authorities
            or not _and_chain_only(cmd) or _has_local_mutation(cmd)):
        return set()

    downloads: list[str] = []
    for _clause, cwd, words in _command_parts_with_cwd(cmd, policy):
        if not words:
            continue
        destination = _target_download_destination(words, cwd, policy)
        if destination:
            downloads.append(destination)
            continue
        if not _response_download_setup_clause(words, cwd):
            return set()

    if len(downloads) != 1:
        return set()
    destination = downloads[0]
    if _command_mentions_path(destination, authored_paths):
        return set()
    return {destination}


def is_remote_response_artifact_command(
        cmd: str,
        policy: FlagEvidencePolicy,
        response_artifacts: set[str] | None,
        *,
        authored_paths: set[str] | None = None,
) -> bool:
    """Whether a source command directly reads one pristine target response.

    A response file is not general local evidence.  Only one literal ``cat``
    of one exact registered path is accepted; no pipeline, redirection,
    helper, decoder, or second input may participate.  This prevents an
    arbitrary local file, or an agent-created transformation, from acquiring
    remote provenance.
    """
    artifacts = {os.path.normpath(str(path)) for path in response_artifacts or ()
                 if path}
    if (not artifacts or _has_local_mutation(cmd) or _has_unquoted_pipe(cmd)
            or _INLINE_INTERPRETER_RX.search(cmd) or script_path_in_command(cmd)):
        return False
    parts = list(_command_parts_with_cwd(cmd, policy))
    if len(parts) != 1:
        return False
    _clause, cwd, words = parts[0]
    idx = _command_program_index(words)
    if idx >= len(words):
        return False
    if os.path.basename(words[idx]).lower() not in _REMOTE_RESPONSE_READER_PROGRAMS:
        return False
    operands = words[idx + 1:]
    if len(operands) != 1:
        return False
    path = _trace_path(operands[0], cwd)
    if path not in artifacts:
        return False
    # A locally authored spelling of the same path stays poisoned even after
    # a subsequent downloader invocation.  The registration gate normally
    # catches this first; retaining the check here protects direct callers.
    if _command_mentions_path(path, authored_paths):
        return False
    return True


def is_remote_provenance(provenance: str) -> bool:
    """Whether a verified claim ultimately comes from the active live target."""
    return str(provenance or "") in {"remote", "remote_response_artifact"}


def _and_chain_only(cmd: str) -> bool:
    """Accept only literal ``&&`` control flow outside quoted shell text."""
    quote = ""
    escaped = False
    depth = 0
    text = _strip_shell_comments(cmd)
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")" and depth:
            depth -= 1
            i += 1
            continue
        if depth == 0:
            if ch in ";\r\n|":
                return False
            if ch == "&":
                if i + 1 >= len(text) or text[i + 1] != "&":
                    return False
                i += 2
                continue
        i += 1
    return not quote and not escaped and depth == 0


def _artifact_probe_uses_path(words: list[str], path: str, cwd: str) -> bool:
    """Whether one read-only follow-up verifies a downloaded output path."""
    idx = _command_program_index(words)
    if idx >= len(words):
        return False
    program = os.path.basename(words[idx]).lower()
    if program not in {"file", "sha256sum", "sha1sum", "md5sum", "stat", "wc", "test"}:
        return False
    return any(_trace_path(word, cwd) == path for word in words[idx + 1:])


def downloaded_target_artifacts(cmd: str, policy: FlagEvidencePolicy, *,
                                output: str = "") -> set[str]:
    """Return current-task artifacts explicitly downloaded from its live target.

    The returned paths are trace-derived identifiers, not a filesystem scan.
    A path enters this set only after a current-target HTTP(S) ``curl``/``wget``
    call with one explicit output destination and an ``&&``-guarded read-only
    verification of that output in the same tool call.  That prevents a failed
    download from blessing a pre-existing stale file at the same pathname.
    Callers must retain the set in event order and pass it to
    ``is_local_evidence_command`` for a *later* local analysis command.
    """
    if (not policy.remote_artifact_allowed or not output
            or not _and_chain_only(cmd)):
        return set()
    parts = list(_command_parts_with_cwd(cmd, policy))
    artifacts: set[str] = set()
    for idx, (_clause, cwd, words) in enumerate(parts):
        if words:
            destination = _target_download_destination(words, cwd, policy)
            if not destination:
                continue
            # Only a simple preparation/download/inspection chain is a
            # provenance event.  Running a decoder in the same shell call
            # would make the recorded order ambiguous, so it must be a later
            # tool event instead.
            later = parts[idx + 1:]
            if any(_LOCAL_DIRECT_PROGRAM_RX.search(clause) or _INLINE_INTERPRETER_RX.search(clause)
                   or script_path_in_command(clause)
                   for clause, _next_cwd, _next_words in later):
                continue
            if any(_artifact_probe_uses_path(next_words, destination, next_cwd)
                   for _next_clause, next_cwd, next_words in later):
                artifacts.add(destination)
    return artifacts


# A downloaded executable is often copied before it is instrumented or
# otherwise analysed (for example ``shutil.copy('validator',
# 'validator_patched')``).  The copy is still derived from the live-target
# artifact, but it must not be confused with an arbitrary agent-created file.
# Keep this parser deliberately small and literal: only ordinary ``cp`` /
# ``install`` and Python's shutil copy helpers with two literal paths are
# accepted.  Dynamic paths, directory copies, shell substitutions and opaque
# commands fail closed.
_PY_COPY_FUNCS = frozenset({"copy", "copy2", "copyfile"})


def _python_copy_pairs(code: str) -> list[tuple[str, str]]:
    """Return literal ``(source, destination)`` copy calls in Python code.

    AST parsing prevents comments/strings that merely mention ``shutil.copy``
    from creating provenance.  A tiny regex fallback is intentionally omitted:
    accepting malformed or partially parsed source would let a printed example
    bless a stale local file.
    """
    try:
        tree = ast.parse(str(code or ""))
    except (SyntaxError, TypeError, ValueError):
        return []
    imported_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "shutil":
                    imported_aliases.add(item.asname or "shutil")
        elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
            for item in node.names:
                if item.name in _PY_COPY_FUNCS:
                    imported_aliases.add(item.asname or item.name)

    pairs: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        fn_name = ""
        if isinstance(node.func, ast.Attribute):
            if (isinstance(node.func.value, ast.Name)
                    and node.func.value.id in imported_aliases
                    and node.func.attr in _PY_COPY_FUNCS):
                fn_name = node.func.attr
        elif isinstance(node.func, ast.Name) and node.func.id in imported_aliases:
            fn_name = node.func.id
        if not fn_name:
            continue
        src, dst = node.args[:2]
        if (not isinstance(src, ast.Constant) or not isinstance(src.value, str)
                or not isinstance(dst, ast.Constant) or not isinstance(dst.value, str)):
            continue
        # Keyword arguments are uncommon for shutil.copy and make the source
        # order ambiguous; reject them rather than guessing.
        if any(keyword.arg is None for keyword in node.keywords):
            continue
        pairs.append((src.value, dst.value))
    return pairs


def _python_heredoc_blocks(cmd: str, policy: FlagEvidencePolicy):
    """Yield ``(body, cwd)`` for Python heredoc/``-c`` blocks in ``cmd``."""
    text = str(cmd or "")
    # Heredoc Python is the common patching form.  The marker must be on a
    # command line that actually invokes Python; a shell heredoc containing the
    # words ``shutil.copy`` is not executable Python provenance.
    for marker in _HEREDOC_RX.finditer(text):
        line_start = text.rfind("\n", 0, marker.start()) + 1
        header = text[line_start:marker.end()]
        if not re.search(r"\bpython(?:3(?:\.\d+)?)?\b", header,
                         re.IGNORECASE):
            continue
        rest = text[marker.end():]
        end = re.search(r"(?m)^[ \t]*" + re.escape(marker.group("tag"))
                       + r"[ \t]*;?[ \t]*$", rest)
        body = rest[:end.start()] if end else rest
        # Resolve a literal ``cd`` preceding this heredoc.  If it cannot be
        # resolved, _trace_path will fail closed for relative copy paths.
        cwd = policy.task_workdir
        for _clause, maybe_cwd, _words in _command_parts_with_cwd(
                text[:marker.start()], policy):
            cwd = maybe_cwd
        yield body, cwd

    # Inline ``python -c '…'``.  _INLINE_CODE_RX has already constrained the
    # interpreter position and quote boundaries; AST parsing below handles the
    # actual code safely.
    for match in _INLINE_CODE_RX.finditer(text):
        cwd = policy.task_workdir
        for _clause, maybe_cwd, _words in _command_parts_with_cwd(
                text[:match.start()], policy):
            cwd = maybe_cwd
        yield match.group("code"), cwd


def _python_string_bindings(tree: ast.AST) -> dict[str, set[str]]:
    """Collect conservative literal path bindings from a small Python AST.

    We do not try to execute user code.  Remembering every literal ever bound
    to a name is deliberately fail-closed: a later ``open(p, 'r+b')`` taints
    each possible path rather than guessing which branch happened to run.
    """
    bindings: dict[str, set[str]] = {}

    def values(node: ast.AST | None) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name):
            return set(bindings.get(node.id, ()))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Path" and node.args):
            return values(node.args[0])
        return set()

    # ast.walk preserves source order for sibling statement lists.  The set
    # union below intentionally remains conservative across reassignments.
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        elif isinstance(node, ast.NamedExpr):
            targets, value = [node.target], node.value
        if value is None:
            continue
        resolved = values(value)
        if not resolved:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings.setdefault(target.id, set()).update(resolved)
    return bindings


def _python_node_string_values(node: ast.AST | None,
                               bindings: dict[str, set[str]]) -> set[str]:
    """Resolve only literal/simple-name path values; dynamic values fail closed."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return set(bindings.get(node.id, ()))
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "Path" and node.args):
        return _python_node_string_values(node.args[0], bindings)
    return set()


def _python_open_mode(call: ast.Call,
                      bindings: dict[str, set[str]]) -> tuple[set[str], set[str], bool]:
    """Return literal ``open`` path/mode values and whether the mode is opaque."""
    path_values = _python_node_string_values(call.args[0], bindings) if call.args else set()
    mode_node: ast.AST | None = call.args[1] if len(call.args) >= 2 else None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode_node = keyword.value
    # Python's default mode is read-only.  A dynamic explicit mode is unsafe
    # whenever this call may address a tracked artifact.
    if mode_node is None:
        return path_values, {"r"}, False
    modes = _python_node_string_values(mode_node, bindings)
    return path_values, modes, not bool(modes)


def _python_copy_aliases(tree: ast.AST) -> set[str]:
    """Return the module/function aliases that can invoke shutil copy helpers."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == "shutil":
                    aliases.add(item.asname or "shutil")
        elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
            for item in node.names:
                if item.name in _PY_COPY_FUNCS:
                    aliases.add(item.asname or item.name)
    return aliases


def _python_copy_call_values(call: ast.Call, aliases: set[str],
                             bindings: dict[str, set[str]]) -> tuple[set[str], set[str]] | None:
    """Resolve one literal/safely-bound shutil copy call, or ``None``."""
    if len(call.args) < 2:
        return None
    recognized = False
    if isinstance(call.func, ast.Attribute):
        recognized = (isinstance(call.func.value, ast.Name)
                      and call.func.value.id in aliases
                      and call.func.attr in _PY_COPY_FUNCS)
    elif isinstance(call.func, ast.Name):
        recognized = call.func.id in aliases
    if not recognized:
        return None
    if any(keyword.arg is None for keyword in call.keywords):
        return None
    return (
        _python_node_string_values(call.args[0], bindings),
        _python_node_string_values(call.args[1], bindings),
    )


def _python_noncopy_write_paths(code: str) -> tuple[set[str], bool]:
    """Return literal Python write targets plus an opaque-write bit.

    ``shutil.copy`` is intentionally reported as a write target too: a later
    copy over an already-derived path must taint it.  The caller decides when
    the one source->destination copy that *creates* a new derived artifact is
    allowed.  Unknown file handles, dynamic write modes, subprocesses and
    mmap are opaque mutations; if they mention a tracked artifact the caller
    must reject rather than assume the write hit some other file.
    """
    try:
        tree = ast.parse(str(code or ""))
    except (SyntaxError, TypeError, ValueError):
        return set(), True
    bindings = _python_string_bindings(tree)
    aliases = _python_copy_aliases(tree)
    paths: set[str] = set()
    opaque = False
    handle_paths: dict[str, tuple[set[str], set[str]]] = {}

    # Bind file handles from simple assignments and with-statements so a
    # ``with open(p, 'r+b') as f: f.write(...)`` is attributed to p.
    for node in ast.walk(tree):
        call: ast.Call | None = None
        name = ""
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                call, name = node.value, node.targets[0].id
        elif isinstance(node, ast.With):
            for item in node.items:
                if (isinstance(item.context_expr, ast.Call)
                        and isinstance(item.optional_vars, ast.Name)):
                    call, name = item.context_expr, item.optional_vars.id
                    if isinstance(call.func, ast.Name) and call.func.id == "open":
                        handle_paths[name] = _python_open_mode(call, bindings)[:2]
                    call, name = None, ""
        if call is not None and isinstance(call.func, ast.Name) and call.func.id == "open":
            handle_paths[name] = _python_open_mode(call, bindings)[:2]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr.lower() if isinstance(func, ast.Attribute) else ""
        name = func.id.lower() if isinstance(func, ast.Name) else ""

        pair = _python_copy_call_values(node, aliases, bindings)
        if pair is not None:
            _src, destinations = pair
            if destinations:
                paths.update(destinations)
            else:
                opaque = True
            continue
        # A copy-looking invocation that cannot be resolved must not evade
        # the derived-artifact mutation check.
        if attr in _PY_COPY_FUNCS or name in _PY_COPY_FUNCS:
            opaque = True
            continue

        if name == "open":
            file_paths, modes, mode_opaque = _python_open_mode(node, bindings)
            if mode_opaque:
                opaque = True
            elif any(("+" in mode or any(ch in mode.lower() for ch in "wax"))
                     for mode in modes):
                if file_paths:
                    paths.update(file_paths)
                else:
                    opaque = True
            continue

        if attr in {"write_text", "write_bytes", "truncate", "unlink", "rename", "replace"}:
            target_paths = _python_node_string_values(
                func.value if isinstance(func, ast.Attribute) else None, bindings)
            if target_paths:
                paths.update(target_paths)
            else:
                opaque = True
            continue

        if attr in {"write", "writelines", "truncate"}:
            if isinstance(func.value, ast.Name) and func.value.id in handle_paths:
                target_paths, modes = handle_paths[func.value.id]
                if any(("+" in mode or any(ch in mode.lower() for ch in "wax"))
                       for mode in modes):
                    if target_paths:
                        paths.update(target_paths)
                    else:
                        opaque = True
            else:
                opaque = True
            continue

        if ((isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
             and func.value.id in {"os", "subprocess"}
             and attr in {"write", "pwrite", "system", "run", "call", "check_call", "popen"})
                or (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id == "mmap" and attr == "mmap")):
            opaque = True
    return paths, opaque


def _python_mutated_artifacts(cmd: str, policy: FlagEvidencePolicy,
                              artifacts: set[str]) -> set[str]:
    """Find tracked artifact paths written by Python heredoc/inline code."""
    known = {os.path.normpath(str(path)) for path in artifacts or () if path}
    if not known:
        return set()
    tainted: set[str] = set()
    for code, cwd in _python_heredoc_blocks(str(cmd or ""), policy):
        raw_paths, opaque = _python_noncopy_write_paths(code)
        resolved = {_trace_path(path, cwd) for path in raw_paths}
        tainted.update(path for path in known if path in resolved)
        if opaque:
            # An unresolved/dynamic writer may still target the artifact.  Do
            # not bless it merely because its exact path could not be parsed.
            tainted.update(path for path in known if _command_mentions_path(cmd, {path}))
    return tainted


def _shell_copy_is_readonly(cmd: str, policy: FlagEvidencePolicy,
                            source: str, destination: str) -> bool:
    """Whether one shell copy transaction has no mutation beyond that copy."""
    if "<<" in str(cmd or "") or not _and_chain_only(cmd):
        return False
    seen = 0
    for clause, cwd, words in _command_parts_with_cwd(cmd, policy):
        if not words or _has_unquoted_pipe(clause):
            return False
        idx = _command_program_index(words)
        if idx >= len(words):
            return False
        program = os.path.basename(words[idx]).lower()
        args = [word for word in words[idx + 1:] if not word.startswith("-")]
        if program in {"cp", "install"} and len(args) == 2:
            if (_trace_path(args[0], cwd) == source
                    and _trace_path(args[1], cwd) == destination):
                seen += 1
                continue
        # A read-only probe may follow the copy, but any other clause (notably
        # ``false``, a patcher, or a second copy) prevents lineage promotion.
        if _artifact_probe_uses_path(words, destination, cwd):
            continue
        return False
    return seen == 1


def _python_copy_is_readonly(cmd: str, policy: FlagEvidencePolicy,
                             source: str, destination: str) -> bool:
    """Whether a Python heredoc contains only one-or-more literal copy calls."""
    blocks = list(_python_heredoc_blocks(str(cmd or ""), policy))
    if len(blocks) != 1:
        return False
    code, cwd = blocks[0]
    try:
        tree = ast.parse(code)
    except (SyntaxError, TypeError, ValueError):
        return False
    bindings = _python_string_bindings(tree)
    aliases = _python_copy_aliases(tree)
    saw_expected = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        pair = _python_copy_call_values(node, aliases, bindings)
        if pair is None:
            # No calls other than a constrained shutil copy are permitted in
            # a lineage-creating heredoc.  This rejects copy+patch in one
            # transaction, including r+b, bytes.fromhex and shell launchers.
            return False
        sources, destinations = pair
        if len(sources) != 1 or len(destinations) != 1:
            return False
        src = _trace_path(next(iter(sources)), cwd)
        dst = _trace_path(next(iter(destinations)), cwd)
        if src != source or dst != destination:
            return False
        saw_expected = True

    # Ensure the surrounding shell only starts the heredoc and contains no
    # post-copy command such as ``false`` after its terminator.
    tags = [match.group("tag") for match in _HEREDOC_RX.finditer(str(cmd or ""))]
    if len(tags) != 1:
        return False
    for clause, _cwd, words in _command_parts_with_cwd(
            _mask_heredoc_bodies(str(cmd or "")), policy):
        if words == [tags[0]]:
            continue
        idx = _command_program_index(words)
        program = os.path.basename(words[idx]).lower() if idx < len(words) else ""
        if not (program.startswith("python") and any(word.startswith("<<") for word in words)):
            return False
    return saw_expected


def derived_target_artifacts(cmd: str, policy: FlagEvidencePolicy,
                             source_artifacts: set[str], *,
                             authored_paths: set[str] | None = None) -> set[str]:
    """Return trusted copies derived from already verified target artifacts.

    ``source_artifacts`` must contain only artifacts established by an earlier
    tool event.  A destination that was authored before the current event is
    rejected, preventing a pre-existing stale file from being re-blessed by a
    later copy-looking command.  The caller should add returned paths to its
    per-trace derived set before evaluating a subsequent execution event.
    """
    if not cmd or not source_artifacts:
        return set()
    known = {os.path.normpath(str(path)) for path in source_artifacts if path}
    if not known:
        return set()
    authored = set(authored_paths or ())
    pairs: list[tuple[str, str, str, str]] = []  # source, destination, cwd, kind

    # Mask heredoc bodies before looking for shell ``cp``.  A literal ``cp``
    # inside a script is data, not a shell mutation in this command.
    shell_view = _mask_heredoc_bodies(_strip_shell_comments(str(cmd)))
    for clause, cwd, words in _command_parts_with_cwd(shell_view, policy):
        if not words or _has_unquoted_pipe(clause):
            continue
        idx = _command_program_index(words)
        if idx >= len(words):
            continue
        program = os.path.basename(words[idx]).lower()
        if program not in {"cp", "install"}:
            continue
        args = [word for word in words[idx + 1:] if not word.startswith("-")]
        if len(args) != 2:
            continue
        src = _trace_path(args[0], cwd)
        dst = _trace_path(args[1], cwd)
        if src and dst:
            pairs.append((src, dst, cwd, "shell"))

    # Python copy helpers in heredoc or inline code.  Each block is tied to a
    # real Python invocation by _python_heredoc_blocks, and AST parsing rejects
    # comments/constant strings that only look like a copy call.
    for code, cwd in _python_heredoc_blocks(str(cmd), policy):
        for src_raw, dst_raw in _python_copy_pairs(code):
            src = _trace_path(src_raw, cwd)
            dst = _trace_path(dst_raw, cwd)
            if src and dst:
                pairs.append((src, dst, cwd, "python"))

    derived: set[str] = set()
    for src, dst, _cwd, kind in pairs:
        if src not in known or dst == src:
            continue
        # Destination must be new in this trace.  _command_mentions_path uses
        # basename aliases, matching the conservative path policy elsewhere.
        if _command_mentions_path(dst, authored):
            continue
        # Copy lineage is useful only while it remains a byte-for-byte copy.
        # A command that also patches/rewrites the destination, or whose copy
        # transaction explicitly fails, never creates automatic evidence.
        if kind == "shell":
            if not _shell_copy_is_readonly(cmd, policy, src, dst):
                continue
        elif not _python_copy_is_readonly(cmd, policy, src, dst):
            continue
        derived.add(dst)
    return derived


def _artifact_paths_in_command(cmd: str, policy: FlagEvidencePolicy,
                               artifacts: set[str]) -> set[str]:
    """Find exact trace paths from ``artifacts`` used by a local command."""
    found: set[str] = set()
    known = {os.path.normpath(str(path)) for path in artifacts or () if path}
    if not known:
        return found
    for _clause, cwd, words in _command_parts_with_cwd(cmd, policy):
        for word in words:
            resolved = _trace_path(word, cwd)
            if resolved in known:
                found.add(resolved)
    return found


def tainted_target_artifacts(cmd: str, policy: FlagEvidencePolicy,
                             artifacts: set[str]) -> set[str]:
    """Return downloaded artifact paths later overwritten by the agent.

    A write to a different result file does not taint the input artifact.  If
    a mutating command names an artifact but its destination cannot be parsed,
    the conservative fallback marks that artifact tainted rather than assume
    the write was harmless.
    """
    if not artifacts or not _has_local_mutation(str(cmd or "")):
        return set()
    mentioned = _artifact_paths_in_command(cmd, policy, artifacts)
    if not mentioned:
        return set()
    targets = _mutation_target_paths(cmd)
    if not targets:
        return mentioned
    tainted: set[str] = set()
    for artifact in mentioned:
        if _path_aliases(artifact).intersection(targets):
            tainted.add(artifact)
    return tainted


def _authored_script_reads_artifact(body: str, artifacts: set[str]) -> bool:
    """Require a trace-authored helper to visibly consume a target artifact."""
    if not body:
        return False
    if _SCRIPT_ARG_FILE_READ_RX.search(body):
        return True
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "//", "--")):
            continue
        if _SCRIPT_FILE_READ_RX.search(line) and _command_mentions_path(line, artifacts):
            return True
    return False


_ARTIFACT_DIAGNOSTIC_PROGRAMS = frozenset({
    "echo", "printf", "pwd", "ls", "file", "stat", "sha1sum",
    "sha256sum", "md5sum", "wc", "r2", "radare2", "readelf", "objdump",
    "otool", "chmod", "true", ":",
})


def _artifact_diagnostic_clause_allowed(clause: str) -> bool:
    """Whether a non-source clause is a read-only/label diagnostic.

    This helper is intentionally stricter than a substring search.  It only
    accepts a simple command whose executable is on the small allow-list and
    rejects shell pipelines, writes, state-file access, and inline interpreters.
    The candidate-in-command check happens earlier in ``flag_confidence``.
    """
    text = str(clause or "").strip()
    if not text or _has_unquoted_pipe(text) or _has_local_mutation(text):
        return False
    # A command-local variable assignment may contain a small command
    # substitution (the common ``KEY=$(cat /tmp/key.bin)`` setup before an
    # instrumented executable).  Treat it as setup only when it does not embed
    # a network client; nested remote commands would otherwise become an
    # unbound source of output.
    if (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", text)
            and not re.search(r"\b(?:curl|wget|nc|ncat|netcat|ssh|scp)\b",
                              text, re.IGNORECASE)):
        return True
    # Heredoc headers/terminators are shell syntax, not independent output
    # producers.  Their bodies are masked before this function is called.
    if re.match(r"^(?:python(?:3(?:\.\d+)?)?|python)\s+.*<<-?['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?$",
                text, re.IGNORECASE):
        return True
    if re.fullmatch(r"[A-Z_][A-Z0-9_]*", text):
        return True
    if _INLINE_INTERPRETER_RX.search(text) or _agent_state_file_mentioned(text):
        return False
    try:
        words = shlex.split(text, comments=False, posix=True)
    except (TypeError, ValueError):
        return False
    idx = _command_program_index(words)
    if idx >= len(words):
        return False
    program = os.path.basename(words[idx]).lower()
    return program in _ARTIFACT_DIAGNOSTIC_PROGRAMS


def _artifact_source_is_operational(cmd: str, policy: FlagEvidencePolicy,
                                    artifacts: set[str], authored_paths: set[str] | None,
                                    script_bodies: dict | None,
                                    derived_artifacts: set[str] | None = None,
                                    candidate: str = "") -> bool:
    """Narrow local-analysis gate for an already-proven target artifact."""
    used = _artifact_paths_in_command(cmd, policy, artifacts)
    if not used:
        return False

    saw_artifact_operation = False
    # Do not interpret Python/shell heredoc payload lines as separate shell
    # commands.  The original command is still used for path/candidate checks;
    # this masked view is only for structural clause validation.
    structural_cmd = _mask_heredoc_bodies(str(cmd or ""))
    for clause, cwd, words in _command_parts_with_cwd(structural_cmd, policy):
        clause_paths = {
            _trace_path(word, cwd) for word in words
            if _trace_path(word, cwd) in used
        }
        if clause_paths:
            # Raw scanners expose static bytes and are not enough to prove
            # that an embedded string is the answer.  A decoder/executor has
            # to produce the complete flag instead.
            if _LOCAL_STATIC_SCAN_RX.search(clause):
                return False
            if re.match(r"^\s*(?:(?:command|builtin)\s+)?cat\b", clause,
                        re.IGNORECASE) and not _has_unquoted_pipe(clause):
                return False
            saw_artifact_operation = True
        elif not _LOCAL_SETUP_RX.match(clause):
            # Harmless diagnostics are common around an instrumented run
            # (``echo`` labels, ``file``/``sha256sum`` checks, r2 disassembly,
            # etc.).  They do not establish provenance by themselves, but
            # rejecting them would discard the adjacent execution output.  A
            # standalone static scanner remains rejected above whenever it is
            # the clause that names the artifact.
            if not _artifact_diagnostic_clause_allowed(clause):
                # A separate command could synthesize stdout unrelated to the
                # artifact, so do not combine it with a valid-looking argument.
                return False
    if not saw_artifact_operation:
        return False

    script_path = script_path_in_command(cmd)
    if script_path:
        script_is_artifact = False
        for _clause, cwd, words in _command_parts_with_cwd(cmd, policy):
            if script_path in words and _trace_path(script_path, cwd) in used:
                script_is_artifact = True
                break
        if not script_is_artifact:
            # A solver helper must be created in this trace and visibly read
            # the downloaded artifact.  Pre-existing helpers can otherwise
            # be stale cross-task code that simply prints an old answer.
            if not _command_mentions_path(script_path, authored_paths):
                return False
            if not _authored_script_reads_artifact(
                    script_body_for(script_bodies or {}, script_path), used):
                return False
            if candidate and script_path.lower().endswith(".py"):
                return _agent_python_has_input_output_flow(
                    cmd, policy, candidate, script_bodies,
                    {os.path.basename(path) for path in used})
    elif _INLINE_INTERPRETER_RX.search(cmd):
        # Inline code is agent-authored by definition; a decorative artifact
        # argument cannot authorize a hard-coded print.
        if not _authored_script_reads_artifact(cmd, used):
            return False
        if candidate and not _agent_python_has_input_output_flow(
                cmd, policy, candidate, script_bodies,
                {os.path.basename(path) for path in used}):
            return False
    return True


def authored_paths_from_call(tool, args) -> set[str]:
    """Best-effort current-trace paths created by an agent tool call.

    This is provenance metadata, not a filesystem scan: it only records paths
    named in the current tool arguments.  It lets local-evidence validation
    reject a later readback of a temporary file or executable the agent just
    generated, including encoded writes whose command has no literal flag.
    """
    paths: set[str] = set()
    record = args if isinstance(args, dict) else {}
    cmd = record.get("command") or record.get("cmd") if record else args
    if isinstance(cmd, str):
        paths.update(_mutation_target_paths(cmd))
        if _has_local_mutation(cmd):
            # `python -c "open('solve.py', 'w')"` does not necessarily use
            # shell redirection.  Its destination is captured explicitly;
            # do *not* mark every `*.py` mentioned in a command that happens
            # to tee output, or a normal `python solve.py input | tee out`
            # would falsely look like a self-authored helper.
            for match in _LOCAL_PY_WRITE_PATH_RX.finditer(cmd):
                paths.update(_path_aliases(match.group("path")))

    # File-writing tools commonly expose a path plus content instead of a
    # shell command.  Do not treat every read-tool ``path`` argument as a
    # mutation; require content-like data or an explicitly write-like name.
    if record:
        tool_name = str(tool or "").lower()
        content_keys = ("content", "file_text", "new_str", "new_string", "body")
        is_writer = any(word in tool_name for word in ("write", "edit", "patch", "create"))
        has_content = any(isinstance(record.get(key), str) for key in content_keys)
        if is_writer or has_content:
            for key in ("path", "file_path", "filePath", "filename", "file"):
                value = record.get(key)
                if isinstance(value, str):
                    paths.update(_path_aliases(value))
    return paths


def _authored_script_reads_input(body: str, policy: FlagEvidencePolicy) -> bool:
    """Return true only for a trace-authored helper that visibly reads input."""
    if not body:
        return False
    if _SCRIPT_ARG_FILE_READ_RX.search(body):
        return True
    for raw_line in body.splitlines():
        line = raw_line.strip()
        # Ignore obvious whole-line comments so `# open('input')` is not a
        # provenance token.  This is deliberately a conservative syntactic
        # gate rather than a claim of full language-level data-flow analysis.
        if not line or line.startswith(("#", "//", "--")):
            continue
        if not _SCRIPT_FILE_READ_RX.search(line):
            continue
        if local_inputs_mentioned(line, policy):
            return True
    return False


def _candidate_materializations(flag: str) -> set[str]:
    """Common literal encodings of a candidate which must not occur in solver code.

    A literal ``flag{...}`` is already rejected by the command-side guard, but
    that guard alone is trivially bypassed by base64/hex-encoding the same
    string in an agent-authored helper.  The variants below are deliberately
    generic representations of the *current candidate*, never challenge
    knowledge.  They let the local-evidence gate distinguish a decoder from a
    no-op input read followed by a materialized answer.
    """
    raw = str(flag or "").strip()
    body_match = re.fullmatch(r"[fF][lL][aA][gG]\{(.+)\}", raw, re.S)
    values = {raw}
    if body_match:
        values.add(body_match.group(1))
    variants: set[str] = set()
    for value in values:
        if not value:
            continue
        encoded = value.encode("utf-8", "ignore")
        variants.add(value)
        variants.add(encoded.hex())
        variants.add(base64.b64encode(encoded).decode("ascii"))
        variants.add(base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("="))
    # Very short tokens are common in unrelated program text and do not offer a
    # useful anti-authoring signal.  Flag bodies in the supported benchmark are
    # materially longer; short values remain covered by the structural flow
    # witness below.
    return {value for value in variants if len(value) >= 8}


def _candidate_is_materialized(text: str, flag: str) -> bool:
    """Whether trace-authored source text embeds the candidate or a common encoding."""
    source = str(text or "")
    if not source:
        return False
    folded = source.lower()
    for value in _candidate_materializations(flag):
        # Plain candidate/body matching is case-insensitive like flag matching;
        # base64 is case-sensitive by construction and must retain its case.
        if value in source or value.lower() in folded:
            return True
    return False


def _ast_sys_argv_reference(node: ast.AST | None) -> bool:
    """Return true for a non-program-name ``sys.argv[...]`` path argument."""
    if not isinstance(node, ast.Subscript):
        return False
    value = node.value
    if not (isinstance(value, ast.Attribute) and value.attr == "argv"
            and isinstance(value.value, ast.Name) and value.value.id == "sys"):
        return False
    index = node.slice
    if isinstance(index, ast.Constant) and isinstance(index.value, int):
        return index.value > 0
    # A dynamic argv index is still tied to an explicit argv value; the command
    # path gate separately requires the declared input name in the invocation.
    return True


def _ast_is_stdin(node: ast.AST | None) -> bool:
    """Recognize ``sys.stdin`` / ``sys.stdin.buffer`` without evaluating code."""
    if isinstance(node, ast.Attribute):
        if (node.attr == "stdin" and isinstance(node.value, ast.Name)
                and node.value.id == "sys"):
            return True
        return node.attr == "buffer" and _ast_is_stdin(node.value)
    return False


def _python_input_to_stdout_flow(code: str, input_names: set[str], *,
                                 stdin_allowed: bool = False) -> bool:
    """Require a visible local-input → stdout data-flow witness for Python code.

    This is intentionally a narrow syntactic proof, not an interpreter.  It
    accepts ordinary decoders (`data = open(input).read(); print(transform(data))`)
    and rejects the unsafe shape that motivated it (`open(input).read();
    print(constant)`).  Unknown/dynamic programs fail closed rather than making
    a decorative input read sufficient provenance.
    """
    try:
        tree = ast.parse(str(code or ""))
    except (SyntaxError, TypeError, ValueError):
        return False
    names = {os.path.basename(str(value)) for value in input_names if str(value)}
    if not names and not stdin_allowed:
        return False

    def is_input_path(node: ast.AST | None) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return os.path.basename(node.value) in names
        return _ast_sys_argv_reference(node)

    functions = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def assigned_names(node: ast.AST | None) -> set[str]:
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, (ast.Tuple, ast.List)):
            out: set[str] = set()
            for item in node.elts:
                out.update(assigned_names(item))
            return out
        return set()

    def local_assignments(root: ast.AST):
        rows: list[tuple[ast.AST, ast.AST]] = []
        for node in ast.walk(root):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    rows.append((target, node.value))
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                rows.append((node.target, node.value))
            elif isinstance(node, ast.NamedExpr):
                rows.append((node.target, node.value))
        return rows

    def call_is_stdout(call: ast.Call) -> bool:
        if isinstance(call.func, ast.Name):
            return call.func.id == "print"
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in {"write", "writelines"}:
            return False
        value = call.func.value
        return (isinstance(value, ast.Attribute) and value.attr == "stdout"
                and isinstance(value.value, ast.Name) and value.value.id == "sys")

    def function_sink_depends(fn: ast.AST, tainted_params: set[str], stack: set[str]) -> bool:
        """Whether every stdout sink of a called local function uses its input."""
        tainted = set(tainted_params)
        for _ in range(max(1, len(local_assignments(fn)) + 1)):
            changed = False
            for target, value in local_assignments(fn):
                if expr_depends(value, tainted, stack):
                    before = len(tainted)
                    tainted.update(assigned_names(target))
                    changed |= len(tainted) != before
            if not changed:
                break
        sinks = [node for node in ast.walk(fn)
                 if isinstance(node, ast.Call) and call_is_stdout(node)]
        return bool(sinks) and all(
            any(expr_depends(arg, tainted, stack) for arg in sink.args)
            for sink in sinks)

    def function_return_depends(fn: ast.AST, tainted_params: set[str], stack: set[str]) -> bool:
        tainted = set(tainted_params)
        for _ in range(max(1, len(local_assignments(fn)) + 1)):
            changed = False
            for target, value in local_assignments(fn):
                if expr_depends(value, tainted, stack):
                    before = len(tainted)
                    tainted.update(assigned_names(target))
                    changed |= len(tainted) != before
            if not changed:
                break
        returns = [node for node in ast.walk(fn) if isinstance(node, ast.Return)]
        return bool(returns) and all(
            node.value is not None and expr_depends(node.value, tainted, stack)
            for node in returns)

    def expr_depends(node: ast.AST | None, tainted: set[str], stack: set[str]) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Name):
            return node.id in tainted
        if isinstance(node, ast.Constant):
            return False
        if _ast_sys_argv_reference(node):
            return True
        if _ast_is_stdin(node):
            return stdin_allowed
        if isinstance(node, ast.Attribute):
            return expr_depends(node.value, tainted, stack)
        if isinstance(node, ast.Subscript):
            return expr_depends(node.value, tainted, stack)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return any(expr_depends(item, tainted, stack) for item in node.elts)
        if isinstance(node, ast.Dict):
            return any(expr_depends(item, tainted, stack)
                       for item in [*node.keys, *node.values] if item is not None)
        if isinstance(node, ast.JoinedStr):
            return any(isinstance(item, ast.FormattedValue)
                       and expr_depends(item.value, tainted, stack)
                       for item in node.values)
        if isinstance(node, ast.BinOp):
            return expr_depends(node.left, tainted, stack) or expr_depends(node.right, tainted, stack)
        if isinstance(node, ast.UnaryOp):
            return expr_depends(node.operand, tainted, stack)
        if isinstance(node, ast.BoolOp):
            # `input and constant` / `input or constant` may print an authored
            # constant, so every branch of a boolean result must carry input.
            return bool(node.values) and all(expr_depends(item, tainted, stack)
                                             for item in node.values)
        if isinstance(node, ast.IfExp):
            # The condition alone is not an output data source.  Both possible
            # values must depend on input to exclude `constant if input else ...`.
            return (expr_depends(node.body, tainted, stack)
                    and expr_depends(node.orelse, tainted, stack))
        if isinstance(node, (ast.Compare, ast.Lambda)):
            return False
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Name) and func.id in {"open", "Path"}:
            return bool(node.args) and is_input_path(node.args[0])
        if isinstance(func, ast.Attribute) and func.attr in {"read", "read_text", "read_bytes"}:
            return (expr_depends(func.value, tainted, stack)
                    or (stdin_allowed and _ast_is_stdin(func.value)))
        if isinstance(func, ast.Name) and func.id in functions:
            if func.id in stack:
                return False
            fn = functions[func.id]
            params = [arg.arg for arg in fn.args.args]
            dependent = {
                params[idx] for idx, arg in enumerate(node.args[:len(params)])
                if expr_depends(arg, tainted, stack)
            }
            if not dependent:
                return False
            return function_return_depends(fn, dependent, stack | {func.id})
        # A transformation from a tainted value remains tainted.  The local
        # function branch above is intentionally stricter: a helper defined in
        # the same source cannot claim flow while returning a constant.
        return (any(expr_depends(arg, tainted, stack) for arg in node.args)
                or any(expr_depends(keyword.value, tainted, stack)
                       for keyword in node.keywords)
                or (isinstance(func, ast.Attribute)
                    and expr_depends(func.value, tainted, stack)))

    top_tainted: set[str] = set()
    for _ in range(max(1, len(local_assignments(tree)) + 1)):
        changed = False
        for target, value in local_assignments(tree):
            if expr_depends(value, top_tainted, set()):
                before = len(top_tainted)
                top_tainted.update(assigned_names(target))
                changed |= len(top_tainted) != before
        if not changed:
            break

    # Direct module-level output, or an explicitly invoked local function whose
    # argument is tainted and whose own stdout sink preserves that taint.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if call_is_stdout(node) and any(expr_depends(arg, top_tainted, set()) for arg in node.args):
            return True
        if isinstance(node.func, ast.Name) and node.func.id in functions:
            fn = functions[node.func.id]
            params = [arg.arg for arg in fn.args.args]
            dependent = {
                params[idx] for idx, arg in enumerate(node.args[:len(params)])
                if expr_depends(arg, top_tainted, set())
            }
            if dependent and function_sink_depends(fn, dependent, {node.func.id}):
                return True
    return False


def _agent_python_has_input_output_flow(cmd: str, policy: FlagEvidencePolicy,
                                        flag: str, script_bodies: dict | None,
                                        input_names: set[str]) -> bool:
    """Validate agent-authored Python evidence without treating a no-op read as proof."""
    text = str(cmd or "")
    bodies: list[str] = [body for body, _cwd in _python_heredoc_blocks(text, policy)]
    script = script_path_in_command(text)
    if script and script.lower().endswith(".py"):
        body = script_body_for(script_bodies or {}, script)
        if body:
            bodies.append(body)
    if _candidate_is_materialized(text, flag):
        return False
    if not bodies:
        return True                     # native/original executable path
    if any(_candidate_is_materialized(body, flag) for body in bodies):
        return False
    stdin_allowed = bool(_has_unquoted_pipe(text) and input_names)
    return all(_python_input_to_stdout_flow(
        body, set(input_names), stdin_allowed=stdin_allowed) for body in bodies)


def _declared_input_is_operational(cmd: str, policy: FlagEvidencePolicy) -> bool:
    """Reject a decorative input-name mention in an unrelated shell clause."""
    text = str(cmd or "")
    # Inline scripts need their own body-aware check below.  Splitting their
    # lines as shell clauses would reject legitimate `python - <<'PY'` solvers.
    if "<<" in text:
        return _authored_script_reads_input(text, policy)
    # `python -c 'print(...)' official.bin` (and equivalent shell/JS forms)
    # treats the input filename as a passive argument unless the inline body
    # itself visibly opens it.  Do not let that decorative argument authorize
    # a hard-coded print.
    if _INLINE_INTERPRETER_RX.search(text):
        return _authored_script_reads_input(text, policy)
    # `|` deliberately remains inside a stage: `cat input | decoder` is a
    # single data-flow operation.  Independent shell clauses must all either
    # be harmless setup or consume a declared input; otherwise their combined
    # stdout cannot be attributed to that input.
    clauses = [part.strip() for part in re.split(r"(?:&&|\|\||;|\n)", text) if part.strip()]
    if not clauses:
        return False
    saw_input = False
    for clause in clauses:
        if local_inputs_mentioned(clause, policy):
            if _LOCAL_NON_CONSUMER_RX.search(clause):
                return False
            saw_input = True
        elif not _LOCAL_SETUP_RX.match(clause):
            return False
    return saw_input


def local_inputs_mentioned(cmd: str, policy: FlagEvidencePolicy) -> set[str]:
    """返回命令中提到的、由平台声明的附件文件名。"""
    text = str(cmd or "")
    matched: set[str] = set()
    for name in policy.declared_inputs:
        if re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(name)
                     + r"(?![A-Za-z0-9_.-])", text):
            matched.add(name)
    return matched


def local_input_mutated(cmd: str, policy: FlagEvidencePolicy) -> set[str]:
    """Return declared inputs actually targeted by a current-call mutation.

    A solver is allowed to redirect *its output* to a new result file.  The
    former broad implementation tainted every input merely because a command
    such as `solve capture.pcap | tee result` both mentioned the input and
    wrote somewhere else, rejecting an otherwise reproducible local solve.
    """
    text = str(cmd or "")
    mentioned = local_inputs_mentioned(text, policy)
    if not mentioned or not _has_local_mutation(text):
        return set()
    targets = _mutation_target_paths(text)
    tainted = set()
    for name in mentioned:
        if name in targets or _command_mentions_path(name, targets):
            tainted.add(name)
    # We saw a mutation but could not safely locate its target.  If it names
    # a declared input, preserve fail-closed behavior rather than assume it
    # was harmless (e.g. an opaque interpreter one-liner).
    return tainted if targets else mentioned


def is_local_evidence_command(cmd: str, policy: FlagEvidencePolicy,
                              *, tainted_inputs: set[str] | None = None,
                              authored_paths: set[str] | None = None,
                              script_bodies: dict | None = None,
                              downloaded_artifacts: set[str] | None = None,
                              tainted_artifacts: set[str] | None = None,
                              derived_artifacts: set[str] | None = None,
                              candidate: str = "") -> bool:
    """此命令能否作为本地题的取证来源。

    有平台附件名时，命令必须读取/使用其中至少一个未被本会话写过的输入；
    这允许 `python solve.py capture.pcap` 之类可复现推导，同时拒绝仅打印
    自己写进临时文件的答案。对于网络题，只额外接受当前 trace 中从本题
    目标显式下载、且未被改写的原始产物的受限本地分析。没有附件/产物元数据
    时，仅接受无网络本地题中直接运行本地程序的输出；裸 `strings`/`grep`
    扫描不够，因为静态诱饵很常见。
    """
    text = str(cmd or "").strip()
    if ((not policy.local_allowed and not policy.remote_artifact_allowed)
            or not text or is_remote_command(text)
            or _agent_state_file_mentioned(text)):
        return False
    artifacts = set(downloaded_artifacts or ())
    # A download destination that was already agent-authored earlier in this
    # trace is ambiguous on a failed/partial transfer.  Do not let a later
    # `curl -o same-name ...` launder that old local content into provenance.
    derived = {os.path.normpath(str(path)) for path in (derived_artifacts or ())
               if path}
    # A derived artifact is expected to have been written by the agent (copy /
    # instrumentation), so its path will also occur in ``authored_paths``.
    # Keep that write from masking the verified lineage; original downloads
    # remain subject to the authored-path check.
    authored_artifacts = {
        artifact for artifact in artifacts - derived
        if _command_mentions_path(artifact, authored_paths)
    }
    usable_artifacts = artifacts - set(tainted_artifacts or ()) - authored_artifacts
    if (policy.remote_artifact_allowed and usable_artifacts
            and _artifact_source_is_operational(
            text, policy, usable_artifacts,
                authored_paths, script_bodies, derived, candidate)):
        return True
    mentioned = local_inputs_mentioned(text, policy)
    if mentioned:
        if mentioned & set(tainted_inputs or ()):
            return False
        if not _declared_input_is_operational(text, policy):
            return False
        # A direct static string/grep/dump hit is not proof that the selected
        # bytes are the answer rather than a decoy.  A real decoder may still
        # filter its *own computed output* downstream (e.g. `solve input |
        # grep`); only a static scanner as the source operation is rejected.
        source_clause = next((part.strip() for part in re.split(r"(?:&&|\|\||;|\n)", text)
                              if local_inputs_mentioned(part, policy)), "")
        if _LOCAL_STATIC_SCAN_RX.search(source_clause):
            return False
        script_path = script_path_in_command(text)
        if script_path and _command_mentions_path(script_path, authored_paths):
            # An agent-created decoder is fine only when its body visibly
            # reads the official input.  If we did not capture that body, it
            # is not reproducible provenance and therefore fails closed.
            body = script_body_for(script_bodies or {}, script_path)
            if not _authored_script_reads_input(body, policy):
                return False
            if candidate and script_path.lower().endswith(".py"):
                return _agent_python_has_input_output_flow(
                    text, policy, candidate, script_bodies, mentioned)
            return True
        if candidate and _INLINE_INTERPRETER_RX.search(text):
            # A generated inline solver must visibly carry bytes from the
            # official input to stdout.  A mere `open(input).read()` followed
            # by an encoded constant is an authored answer, not evidence.
            if not _agent_python_has_input_output_flow(
                    text, policy, candidate, script_bodies, mentioned):
                return False
        # A generated temporary result/binary cannot be laundered by adding a
        # decorative official filename in the same command.  Script paths are
        # handled above so legitimate trace-authored decoders remain possible.
        if _command_mentions_path(_cmd_without_write_targets(text), authored_paths):
            return False
        return True
    direct = _LOCAL_DIRECT_PROGRAM_RX.search(text)
    if not (not policy.declared_inputs and not policy.has_targets
            and policy.category in _LOCAL_NATIVE_CATEGORIES and direct):
        return False
    # `/bin/cat file` is still a static read, not an original challenge
    # program.  Only a program staged with the task (normally `./chall` or a
    # task workdir path) gets the metadata-free local fallback.
    program = str(direct.group("program") or "")
    return (not program.startswith(("/bin/", "/usr/bin/", "/usr/sbin/", "/sbin/"))
            and not program.lower().endswith(_SCRIPT_SUFFIXES)
            and not _command_mentions_path(program, authored_paths))


# [B37] 「是否在读 agent 自己的文件」判定前，先摘掉命令里的 URL/host-path。
# 例：curl http://target/flag 里的 "/flag" 会被 _AGENT_FILE_RX 命中（它匹配的是
# `[/\s>]` 之后的 FLAG[\w]*），于是这条**真·活靶标响应**被判成"读自己写的假设
# 文件"→ 打进 authored 分支 → 丢弃。eager 侧还会因 provenance 为空而记进
# _verify_rejected，本场不再重试。URL 路径里的 /flag、/notes 是极常见的靶标端点，
# 这个误判方向是**丢掉真解**，比误放行的代价高，故按 URL 语境排除。
_URL_IN_CMD_RX = re.compile(
    r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://\S+"                    # http://h/p、https://…、ftp://…
    r"|\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:/\S*)?"     # 10.0.0.1/flag、10.0.0.1:8080/x
    r"|\b(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,}(?::\d+)?/\S*"  # host.tld/path
)


# [B38] 无 scheme、主机名也不带点的裸 host/path：curl -s target/flag、
# target:8080/flag、requests.get('target/flag')。B37 只覆盖了带 scheme / IP /
# 带点域名的形式，这三类仍被 _AGENT_FILE_RX 当成"agent 自己的文件"而丢弃。
# 只在**该命令确实是网络命令**时才摘：没有这个前提就一律摘除的话，
# `cat work/<code>/FLAG` 里的 `work/<code>/FLAG` 也会被摘掉，反而把该拦的放过去。
# 前置否定环视保证不碰路径内部 —— `/work/<code>/FLAG` 的 `work`、`<code>` 都紧跟
# `/`，永远成不了 host 起点，所以即使是网络命令也摘不掉它。
_BARE_HOSTPATH_RX = re.compile(
    r"(?<![/\w.\-])[A-Za-z0-9][A-Za-z0-9._\-]*(?::\d+)?/\S*"
)


def _cmd_without_payloads(cmd: str, script_bodies: dict = None) -> str:
    """[B40] 「读自己文件」判定前的最终视图：URL/host-path 摘除（B37/B38）
    + 脚本中介的远端命令再摘引号 payload。"""
    s = _cmd_without_urls(cmd, script_bodies)
    if script_mediated_remote(cmd, script_bodies):
        s = strip_quoted_payloads(s)
    return s


def _cmd_without_urls(cmd: str, script_bodies: dict = None) -> str:
    """摘掉命令里的 URL/host-path 片段，供「是否在读自己的文件」判定使用（B37/B38）。

    只用于 _AGENT_FILE_RX 这一处判断；"命令里是否含完整 flag 信封"（echo 自造）
    仍对**原命令**判定，不受影响 —— 否则 echo 'flag{x}' > /tmp/x 这类会被放过。

    两层：先摘带 scheme / IP / 带点域名的绝对可辨形式；若该命令是网络命令，
    再摘裸 host/path（见 _BARE_HOSTPATH_RX 的说明）。
    """
    cmd = cmd or ""
    out = _URL_IN_CMD_RX.sub(" ", cmd)
    if is_remote_command(cmd, script_bodies):
        out = _BARE_HOSTPATH_RX.sub(" ", out)
    return out


@dataclass
class Claim:
    """候选 flag 及其证据"""
    flag: str
    source_cmd: str = ""
    source_output: str = ""
    confidence: float = 0.0
    grounded: bool = False
    verified: bool = False
    reject_reason: str = ""
    provenance: str = ""          # remote / remote_response_artifact / local / target_artifact / unknown
    # [B47] 判断 Agent（LLM 第二次意见）的落点。默认值 = 「无意见」：
    # 未启用 / 调用失败 / 解析不出来时保持空，调用方据此维持确定性规则的原判。
    skeptic_verdict: str = ""     # genuine / fabricated / decoy / stale / ""
    skeptic_conf: float = 0.0
    skeptic_reason: str = ""

    @property
    def body(self) -> str:
        """提取 flag{} 内的主体"""
        m = re.match(r"flag\{(.+)\}", self.flag, re.IGNORECASE)
        return m.group(1) if m else self.flag


# ── [B55b] FLAG 文件「一行够不够格当候选」的唯一口径 ──────────────
# 两处读 FLAG 文件的地方（drivers/benchmark_driver.py:_read_flag_file 与
# adapter/solver/base.py:_read_flag_files）**必须共用本函数**：B55 的根因
# 正是这两层口径漂移（extract_flags 严格只认信封、_read_flag_file 宽松），
# 再分头写一套就是重蹈覆辙。
def flag_line_candidate(v: str) -> bool:
    """FLAG 文件里一行 stripped 文本，能否作为 flag 候选。

    收两种形态：
      1. 任何 `XXX{...}` 信封（含非标准外壳如 KEY{...}）—— 原口径，
         下游 invalid_format 由 [B55] 的判断 Agent 复核通道接住；
      2. **[B55b] 裸答案**：形如 s3cr3t_v4lu3，没有花括号。此前
         `"{" in v and v.endswith("}")` 把这类直接挡在门外 ——
         平台答案的真实形态未经证实，万一某题答案就是裸串，真解永远进不来。

    裸串收紧到 `_FLAG_BODY_RX` 字符集（字母数字 + _-.:/，3~200）而不只是
    "单行无空白"：agent 写在 FLAG 里的**中文说明天然没有空格**
    （「未解出，下一步试xx」），只靠无空白挡不住，ASCII 词元集能干净排除。

    不加停用词表（TODO/NONE 这类确实会漏进来）：它们到不了平台 ——
    [B55] 通道要证据非空 + 判断 Agent genuine>=0.75 才提交，判不准时
    fail-safe 维持原判。宁可让裁判挡，不在门口猜出第三套口径。
    """
    if not v or len(v) > 200:
        return False
    if "{" in v and v.endswith("}"):
        return True
    return bool(_FLAG_BODY_RX.match(v))


# ── [B57] 子 Agent 总开关 ─────────────────────────────────────────
# 放这里是因为 verify.py 是**叶子模块**（不 import 任何 adapter 内部模块）——
# taskprompt（注入委派指引）与 solver/pi_agent（装载扩展）都要问同一个问题，
# 谁 import 它都不会成环。开关逻辑只有这一处，避免第三次「两处口径漂移」。
def subagent_enabled() -> bool:
    """子 Agent 能力总开关。默认开；置 ADAPTER_SUBAGENT=0 整体关闭。

    关掉后行为与本补丁前**逐字一致**：不装扩展、不注入委派指引，
    pi 的工具表里没有 `subagent`。
    """
    return str(os.environ.get("ADAPTER_SUBAGENT", "1") or "1").strip() != "0"


# ── [B67] 技能自主调用总开关 ───────────────────────────────────────
# 同上：taskprompt（注入自主调用指引）与 solver/pi_agent（软链 skills/ 进每题
# HOME）都要问同一个问题，开关逻辑只有这一处。
def skill_agent_enabled() -> bool:
    """技能自主调用总开关。默认开；置 ADAPTER_SKILL_AGENT=0 整体关闭。

    开：pi_agent 把仓库 skills/ 软链进每题 HOME（pi 原生渐进披露——系统提示
    只放 <available_skills> 名单+描述+路径，Agent 按自己的题目分析用 read 主动
    加载 SKILL.md 全文），taskprompt 附带自主调用指引。
    关：技能面完全交给框架兜底——taskprompt 注入完整名录 XML（名字+描述+路径），
    pi 侧不装技能。2026-09-16 起**没有**"框架 keyword 挑 top-2 正文注入"这条路了
    （技能库换成上游 hack-skills，路由交还 Agent，见 skill_loader 模块 docstring）。
    """
    return str(os.environ.get("ADAPTER_SKILL_AGENT", "1") or "1").strip() != "0"


def normalize_flag_body(flag: str) -> str:
    """标准化 flag 用于去重"""
    m = re.match(r"flag\{(.+)\}", flag, re.IGNORECASE)
    body = m.group(1) if m else flag
    return body.strip().lower()


def flag_submission_key(flag: str) -> str:
    """返回用于提交账本的精确候选键。

    平台对完整提交字符串做精确匹配，`FLAG{X}` 与 `flag{X}` 不能共用
    小写 body 作为去重键。这里仅去掉传输层附带的引号/空白并规范化花括号
    周围空白；外壳与 body 的大小写一律保留。
    """
    text = str(flag or "").strip().strip("\"'")
    m = re.fullmatch(r"([fF][lL][aA][gG])\s*\{(.*?)\}", text, re.S)
    if not m:
        return text
    return f"{m.group(1)}{{{m.group(2).strip()}}}"


def _entropy(s: str) -> float:
    """计算字符串的 Shannon 熵"""
    if not s:
        return 0.0
    freq = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    total = len(s)
    return -sum((n / total) * math.log2(n / total) for n in freq.values())


def flag_confidence(flag: str, observed_output: str, tool_outputs: list = None, *,
                    evidence_policy: FlagEvidencePolicy | None = None) -> Claim:
    """
    评估候选 flag 的置信度。

    返回 Claim 对象，含 grounding 结果和初步置信度。
    """
    claim = Claim(flag=flag)
    # Omitted policy deliberately means remote-only.  This keeps callers that
    # do not have task metadata fail-closed instead of silently opening a
    # local-output submission path.
    policy = evidence_policy or FlagEvidencePolicy()

    # 1. 格式检查：不限制答案格式（flag{...}/hash/密码/密钥等都接受）
    if not flag or not flag.strip():
        claim.reject_reason = "empty_flag"
        claim.confidence = 0.0
        return claim

    # 对 flag{...} 格式提取 body；非 flag 格式整体作为 body
    body = claim.body if claim.body else flag

    # 2. body 字符集检查（引号/空格/命令字符 → 非法）
    if not _FLAG_BODY_RX.match(body):
        claim.reject_reason = "invalid_body_chars"
        claim.confidence = 0.0
        return claim

    # 3. 占位词 / 低熵 → 不再硬拦截，改为降低置信度后继续走 grounding 检查。
    # 真实 flag 可能包含 "test"/"1234" 等常见子串，也可能是简单字符串。
    # 让 grounding 和平台来判断，不在此处一刀切。
    _placeholder_penalty = 0.0
    if _PLACEHOLDER_RX.search(body):
        _placeholder_penalty = 0.15  # 轻微惩罚，但不阻断
    if len(body) > 4 and _entropy(body) < 1.5:
        _placeholder_penalty = max(_placeholder_penalty, 0.10)

    # 3. Grounding: 只在真实命令输出（tool_outputs）中逐字查找，且必须区分
    #   「系统观测」与「agent 自造」。注意：observed_output 含助手文本（思考/推测），
    #   LLM 幻觉的 flag 会被自己的文本"grounded"化导致误放行 → 排除文本部分。
    #
    #   自造判据（框架能力，不改 agent）：若包含该 flag 的命令行里【本身含有】
    #   这个 flag/body 字符串（echo/printf/heredoc/cat>FLAG/./validator 'flag{...}'），
    #   说明是 agent 把自己的猜测敲进命令（写了再读、或把猜测塞给本地验证器），
    #   不是系统产出 —— 不算取证证据。真实观测（命令行不含该 flag、flag 只出现在
    #   【输出】里，如目标服务/端点返回的内容）→ 才算 observed grounding。
    #   另：读 agent 自己写的假设文件（FLAG/SOURCE/MEMORY/黑板/todolist/transcript/
    #   tried_commands/.pi-home）不算系统观测 —— agent 把"结论"写进这些文件后再 cat，
    #   与 echo 自造同源，不能当作平台产出的证据。真实 flag 总有独立系统观测作证。
    #   ★第二轮严格化：evidence 必须【完整 flag{...} 信封】逐字出现在输出里
    #   （大小写无关）。只出现【裸 body】= agent 从别处抠到裸值后自己包上信封
    #   （如把二进制常量/内存片段套成 flag{...}），不算取证。
    #   [B44] 该硬规则**不变**：裸 body 仍不算 evidence、仍不进提交门；
    #   仅单列一类诊断口径 local_derived（见下），把「真推导但无资格」
    #   与「凭空编」分开记账。
    tool_text = ""
    # (idx, cmd, out, tainted-inputs, authored-paths, downloaded-artifacts,
    #  derived-artifacts, tainted-artifacts, response-artifacts,
    #  tainted-response-artifacts, script-bodies)：完整信封的外部观测。
    # 后三项是
    # 同一实例内的仅路径 provenance；脚本体也按**该事件发生时**的快照
    # 保存，避免同名 helper 在后续被改写后倒灌到较早的取证判定。
    evidence = []
    authored = []          # (cmd, out)：agent 自造 / 读自己写的假设文件 → 非证据
    _bodies = {}           # [B40] {脚本路径: 内容}，进 tool_outputs 后回捞
    _ENV_RX = re.compile(r"flag{" + re.escape(body) + r"}", re.IGNORECASE)
    # [B44] 裸 body 首现（仅诊断，不进提交门）：实测某题 这类题里 agent 真把
    # body 从靶标产物 trace 出来、之后才包信封，与「幻觉」在日志里无法区分。
    # 单列 local_derived（grounded=False / conf 0.4 < 0.9 门）只为分开记账。
    _BODY_RX = re.compile(re.escape(body), re.IGNORECASE) if body else None
    _body_only = []        # [(idx, cmd, out)]：裸 body 只出现在输出、命令里没有
    _authored_first_idx = None
    _B44_MIN_BODY = 8
    _tainted_inputs: set[str] = set()
    _authored_paths: set[str] = set()
    _downloaded_artifacts: set[str] = set()
    _derived_artifacts: set[str] = set()
    _tainted_artifacts: set[str] = set()
    _response_artifacts: set[str] = set()
    _tainted_response_artifacts: set[str] = set()
    if tool_outputs:
        for _idx, (_tool, _args, out) in enumerate(tool_outputs):
            text = str(out or "")
            tool_text += "\n" + text
            cmd = str(_args.get("command", _args) if isinstance(_args, dict) else _args or _tool).strip()
            if cmd in ("{}", "[]", "None", "()", ""):
                cmd = ""
            # Update the helper-source map incrementally.  Looking ahead over
            # the entire trace is unsafe: `solve.py` may have produced this
            # output before a later session overwrote it with unrelated code.
            # Conversely, a later safe rewrite must not bless an earlier
            # constant-printing helper.  One-call extraction intentionally
            # preserves shorter overwrite bodies too (the old collector's
            # longest-body merge is useful only when combining a static set).
            _new_bodies = collect_script_bodies(_args, forbid=claim.flag)
            if _new_bodies:
                _bodies.update(_new_bodies)
            # Pi/driver now retain blank-output calls in this list.  Process
            # their arguments too: `printf flag{x} > input.bin` followed by
            # `cat input.bin` must never become fake local evidence merely
            # because the write command itself printed nothing.
            if cmd:
                _tainted_inputs.update(local_input_mutated(cmd, policy))
                # A destination is a legitimate local input only when it was
                # downloaded from this instance's declared target *before*
                # this command.  Check mutation first so the downloader's own
                # ``-o artifact`` does not mark its fresh artifact tainted.
                _tainted_artifacts.update(tainted_target_artifacts(
                    cmd, policy, _downloaded_artifacts | _derived_artifacts))
                _tainted_response_artifacts.update(tainted_target_artifacts(
                    cmd, policy, _response_artifacts))
                # A copy/instrumentation step may intentionally write a new
                # derived path.  It can inherit provenance only from an
                # already trusted, untainted artifact; never from a download
                # established in this same shell event.
                _derived_artifacts.update(derived_target_artifacts(
                    cmd, policy,
                    (_downloaded_artifacts | _derived_artifacts)
                    - _tainted_artifacts,
                    authored_paths=_authored_paths))
                _downloaded_artifacts.update(downloaded_target_artifacts(
                    cmd, policy, output=text))
                new_response_artifacts = downloaded_target_response_artifacts(
                    cmd, policy,
                    completed_success=_tool_execution_succeeded(_args),
                    authored_paths=_authored_paths)
                _response_artifacts.update(new_response_artifacts)
            # Preserve a per-event snapshot below: a path generated *after*
            # an observation must not retroactively invalidate that earlier
            # evidence, while a prior generated file/script cannot be used to
            # manufacture a local source.
            _authored_paths.update(authored_paths_from_call(_tool, _args))
            _cmd_has_flag = bool(cmd and _ENV_RX.search(cmd))
            # A current-target endpoint can reflect request data.  Seeing the
            # candidate, its body, or a standard encoding in any earlier
            # command is agent authorship, not remote discovery.
            _cmd_materializes_candidate = bool(
                cmd and _candidate_is_materialized(cmd, claim.flag))
            if _cmd_has_flag or _cmd_materializes_candidate:
                # A current-target service can reflect request data.  A
                # response file produced by a request that already contains
                # this candidate (including a common encoding) must therefore
                # never become remote provenance when read in a later call.
                # Do not taint unrelated, earlier target responses: a later
                # independent direct response can still provide real evidence.
                if _cmd_materializes_candidate:
                    _tainted_response_artifacts.update(new_response_artifacts)
                authored.append((cmd, text))
                if _authored_first_idx is None:
                    _authored_first_idx = _idx
            if not text:
                continue
            if not _ENV_RX.search(text):
                # [B44] 信封没出现，但裸 body 出现在输出里 → 单独记账（诊断）。
                # 判据与信封同级：命令里已含 body（自写）、或读自己的文件 → 仍算自造。
                if (_BODY_RX is not None and len(body) >= _B44_MIN_BODY
                        and _BODY_RX.search(text)):
                    _self_written = bool(cmd) and (
                        bool(_BODY_RX.search(cmd))
                        or _agent_state_file_mentioned(cmd, _bodies))
                    if _self_written:
                        authored.append((cmd, text))
                        if _authored_first_idx is None:
                            _authored_first_idx = _idx
                    else:
                        _body_only.append((_idx, cmd, text))
                continue                      # 输出里没有完整 flag{body} 信封 → 非取证
            if _cmd_has_flag or _cmd_materializes_candidate:
                # Command arguments already recorded above even if this same
                # call did produce output.  A reflection/echo is not evidence.
                continue
            elif (cmd and _agent_state_file_mentioned(cmd, _bodies)
                  and not is_remote_response_artifact_command(
                      cmd, policy,
                      _response_artifacts - _tainted_response_artifacts,
                      authored_paths=_authored_paths)):
                # 读/写 agent 自己的假设文件 → 自造同源（B37：URL 已先摘除，
                # 免得靶标端点路径 /flag、/notes 被误当成自己的文件）
                authored.append((cmd, text))
                if _authored_first_idx is None:
                    _authored_first_idx = _idx
            else:
                evidence.append((_idx, cmd, text, frozenset(_tainted_inputs),
                                 frozenset(_authored_paths),
                                 frozenset(_downloaded_artifacts),
                                 frozenset(_derived_artifacts),
                                 frozenset(_tainted_artifacts),
                                 frozenset(_response_artifacts),
                                 frozenset(_tainted_response_artifacts),
                                 dict(_bodies)))

    if evidence:
        claim.grounded = True
        claim.confidence = 0.95
        # Prefer evidence with remote provenance over local.  The agent's
        # workflow commonly produces a login chain (with ``;``/``| head``
        # noise that defeats strict chain validation) BEFORE a clean
        # ``curl | grep`` re-fetch.  Taking evidence[0] blindly then marks
        # the candidate local_computed_only even though a later tool call
        # proves it came from the live target.  Scan for the first remote
        # entry; fall back to evidence[0] only when none qualifies.
        _chosen_entry = None
        for _ev in evidence:
            _ev_c = _ev[1]
            if _ev_c and is_task_remote_command(_ev_c, policy, _ev[10]):
                _chosen_entry = _ev
                break
        if _chosen_entry is None:
            _chosen_entry = evidence[0]
        (_evidence_idx, c, o, _source_tainted, _source_authored,
         _source_artifacts, _source_derived_artifacts,
         _source_tainted_artifacts,
         _source_response_artifacts, _source_tainted_response_artifacts,
         _source_bodies) = _chosen_entry
        if c:
            claim.source_cmd = c[:200]
            claim.source_output = o[:500]
            # First external observation is the only provenance source.  A
            # later `cat FLAG`/print cannot launder a static decoy into a
            # trusted answer.
            if is_task_remote_command(c, policy, _source_bodies):
                claim.provenance = "remote"
            elif is_remote_response_artifact_command(
                    c, policy,
                    set(_source_response_artifacts)
                    - set(_source_tainted_response_artifacts),
                    authored_paths=set(_source_authored)):
                claim.provenance = "remote_response_artifact"
            else:
                claim.provenance = "local"
                # Accept ALL grounded flags — if the flag appeared in any
                # tool output, it's evidence.  For reverse/crypto/forensics
                # challenges, the agent legitimately computes flags locally
                # (disassemble binary → extract algorithm → compute flag →
                # printf to FLAG).  The flag may first appear in a printf
                # argument (authored) rather than in prior tool output, but
                # this doesn't mean the agent is guessing — it did real work.
                # The platform judges correctness; the verifier only checks
                # that the flag appeared in some command output (grounded).
        else:
            # 输出含 flag 但拿不到命令出处 → 无法证明是系统观测，保守降级
            claim.grounded = False
            claim.confidence = 0.4
            claim.reject_reason = "no_source_cmd"
    elif authored or _body_only:
        # [B44] 先出现的是哪一类？裸 body 先于 agent 包装 → 本地推导（诊断口径）
        _a_idx = _authored_first_idx if _authored_first_idx is not None else 1 << 30
        _b_idx = _body_only[0][0] if _body_only else 1 << 30
        if _b_idx < _a_idx:
            # 真观测到了 body（但信封是 agent 套的）→ 本地推导，中等置信度
            # 不直接否决，交给判断 Agent（skeptic LLM）动态评估。
            claim.grounded = True
            claim.confidence = 0.70
            claim.provenance = "local"
            claim.reject_reason = ""
            _c, _o = _body_only[0][1], _body_only[0][2]
            if _c:
                claim.source_cmd = _c[:200]
            claim.source_output = _o[:500]
        else:
            # flag 只出现在 agent 自己敲的命令（echo/printf 等）—— 可能是
            # 逆向/密码题的正当计算结果，也可能是瞎编。不直接否决，给
            # 中等置信度让判断 Agent 看完整上下文动态裁决。
            claim.grounded = True
            claim.confidence = 0.70
            claim.provenance = "local"
            claim.reject_reason = ""
    else:
        # 未在任何真实命令输出中找到 → 疑似幻觉/猜测
        claim.grounded = False
        claim.confidence = 0.3
        claim.reject_reason = "not_grounded"

    # 占位词/低熵惩罚：降低置信度但不阻断提交。grounded 的 flag 仍可提交，
    # 只是降低了自动提交的优先级（conf<0.9 走 force-submit 路径由 LLM 裁决）。
    if _placeholder_penalty and claim.confidence > _placeholder_penalty:
        claim.confidence = max(0.50, claim.confidence - _placeholder_penalty)

    return claim


class Verifier:
    """
    三重校验门验证器

    - grounding: 代码校验 (始终执行)
    - skeptic:   否定式质疑 (有 LLM 时)
    - followup:  追问式复核 (有 LLM 时)
    """

    def __init__(self, llm=None, *, skeptic_votes: int = 1,
                 rescue_conf: float = 0.75, veto_conf: float = 0.85,
                 timeout: float = 30.0):
        self.llm = llm
        self.skeptic_votes = max(1, skeptic_votes)
        # [B47] 判断 Agent 参数。阈值刻意不对称：翻案（拒收→提交）要过 0.75，
        # 拦截（提交→不提交）要过 0.85 —— 提交一条错的只是浪费一次机会，
        # 拦掉一条对的却会把已到手的真解丢掉，后者代价更大。
        self.rescue_conf = rescue_conf
        self.veto_conf = veto_conf
        self.timeout = timeout

    # ── [B47] 判断 Agent：确定性规则之外的第二次意见 ──────────────────

    # [B55] 判断 Agent 未必懂框架内部术语：把拒收理由翻成人话再喂给它。
    # invalid_format 尤其要讲清楚 —— 问题出在**外壳形状**，不是内容。
    # 少了这句，模型会按「这个串长得不像 flag」直接判 fabricated，
    # 复核通道等于白开。其余理由不在表里，按原文透传（行为不变）。
    _SKEPTIC_CONCERN = {
        "invalid_format": "该串外壳不是 flag{...}（框架只认 flag{...} 信封；但本平台"
                          "答案的真实形态未经证实，外壳不合规**不等于**内容不对）",
    }

    _SKEPTIC_SYS = (
        "你是 CTF 靶场的判断 Agent。做题 agent 交来一个候选 flag，框架的确定性规则"
        "拿不准，请你读证据后裁决。只输出一行 JSON，不要任何解释。\n"
        "verdict 取值：\n"
        "  genuine    — 候选由活靶标交互产出（服务响应/接口回包），或由活靶标数据直接推得；\n"
        "  fabricated — 像 agent 自造：leet 口号、示例串、占位符、题面或 README 里的样例；\n"
        "  decoy      — 像埋在本地静态产物（固件/二进制/页面模板）里的诱饵或蜜罐串；\n"
        "  stale      — 本地算得，但靶标已换实例或证据已过期。\n"
        '输出格式：{"verdict":"genuine|fabricated|decoy|stale","confidence":0.0,'
        '"reason":"不超过20字"}\n'
        "证据不足以判断时 confidence 必须 <= 0.5，不要猜。"
    )

    def skeptic(self, claim: Claim, evidence: str = "", *, code: str = "") -> Claim:
        """[B47] 请 LLM 做第二次判断，结果写回 claim.skeptic_*。

        fail-safe 是硬要求：没有 LLM / 调用失败 / 输出解析不出来 → verdict 留空
        =「无意见」，调用方据此维持确定性规则的原判（现网行为一字不变）。
        """
        claim.skeptic_verdict = ""
        claim.skeptic_conf = 0.0
        claim.skeptic_reason = ""
        if self.llm is None:
            return claim
        votes = []
        for _ in range(self.skeptic_votes):
            try:
                v, c, why = self._skeptic_once(claim, evidence, code)
            except Exception as e:
                log.warning("  [B47] skeptic call failed: %s", e)
                continue
            if v:
                votes.append((v, c, why))
        if not votes:
            return claim
        tally: dict = {}
        for v, c, why in votes:
            tally.setdefault(v, []).append((c, why))
        ranked = sorted(tally.items(), key=lambda kv: -len(kv[1]))
        if len(votes) > 1 and len(ranked) > 1 and len(ranked[0][1]) == len(ranked[1][1]):
            return claim            # 平票 = 无意见
        top_v, top_rows = ranked[0]
        claim.skeptic_verdict = top_v
        claim.skeptic_conf = sum(c for c, _ in top_rows) / len(top_rows)
        claim.skeptic_reason = top_rows[0][1]
        log.info("  [B47] 判断 Agent: verdict=%s conf=%.2f (%s) %s",
                 top_v, claim.skeptic_conf, claim.skeptic_reason or "-", claim.flag[:30])
        return claim

    def _skeptic_once(self, claim: Claim, evidence: str, code: str):
        """单次裁决。超时用线程 join 熔断 —— LLM 卡住不能拖住 eager 线程。"""
        prompt = ("题号：%s\n候选：%s\n框架的顾虑：%s（首现来源=%s）\n\n"
                  "证据（候选串在工具调用中的出现处，⟦候选⟧ 处即该串）：\n%s\n\n"
                  "只回一行 JSON。"
                  % (code or "-", claim.flag[:80],
                     self._SKEPTIC_CONCERN.get(claim.reject_reason)
                     or claim.reject_reason or "无",
                     claim.provenance or "未知",
                     evidence or "（空：该候选从未在任何工具输出/命令里出现过）"))
        box: dict = {}

        def _run():
            try:
                box["r"] = self.llm.chat(
                    [{"role": "system", "content": self._SKEPTIC_SYS},
                     {"role": "user", "content": prompt}],
                    # [B53] 200→4096。本模型是**推理模型**，思维链与正文
                    # 共享 max_tokens；200 时思维链一开就撞顶、正文永远为空
                    # （实测连测 6 次 6/6 正文为空，tokens 精确=200），
                    # 判断 Agent 因此自上线起从未产出过裁决。
                    # 4096 为 n=8 定标结果：8/8 成功、最慢 18.3s，仍在 30s
                    # join 熔断内。不用 8192：跑满约 35s 会撞穿熔断，
                    # 反而丢掉已拿到的裁决。
                    max_tokens=4096, thinking=False)
            except Exception as e:                       # noqa: BLE001
                box["e"] = e

        t = threading.Thread(target=_run, daemon=True, name="skeptic")
        t.start()
        t.join(self.timeout)
        if t.is_alive():
            log.warning("  [B47] skeptic timeout (%.0fs) — 按无意见处理", self.timeout)
            return "", 0.0, ""
        if "e" in box:
            raise box["e"]
        return self._parse_verdict(getattr(box.get("r"), "text", "") or "")

    @staticmethod
    def _parse_verdict(text: str):
        """从 LLM 输出里抠出裁决；解析失败一律返回空（=无意见）。"""
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return "", 0.0, ""
        try:
            d = json.loads(m.group(0))
        except Exception:
            return "", 0.0, ""
        v = str(d.get("verdict", "")).strip().lower()
        if v not in ("genuine", "fabricated", "decoy", "stale"):
            return "", 0.0, ""
        try:
            c = float(d.get("confidence", 0.0))
        except Exception:
            c = 0.0
        return v, max(0.0, min(1.0, c)), str(d.get("reason", ""))[:40]

    def verify(self, claim: Claim) -> Claim:
        """
        提交门：极简静态过滤 + 交给 skeptic LLM 动态裁决。

        静态检查只做两件事：格式合法 + 非凭空出现（grounded）。
        其余所有判断（是不是幻觉、是不是占位符、来源是否可靠）全部交给
        skeptic LLM 在提交前动态评估。静态规则不挡真 flag。
        """
        # 格式检查：不限制答案格式。挑战答案可能是 flag{...}、hash、密码、
        # 密钥等任意格式。由 Agent 根据题目要求决定答案格式。
        if not claim.flag or not claim.flag.strip():
            claim.reject_reason = "empty_flag"
            log.info("  verify REJECT (empty): %s", str(claim.flag)[:30])
            return claim

        # 占位符检查：题目说明里的格式示例不是真实答案
        _placeholder_body = re.sub(r"^flag\{(.+)\}$", r"\1", claim.flag, flags=re.IGNORECASE)
        if re.fullmatch(r"\.{3}|x{2,}|example|placeholder|test|your[_-]?flag|insert[_-]?here",
                        _placeholder_body, re.IGNORECASE):
            claim.reject_reason = "placeholder_format"
            log.info("  verify REJECT (placeholder): %s", claim.flag[:30])
            return claim

        # Grounding 检查：flag 必须在某条工具输出或命令中出现过
        if not claim.grounded:
            claim.verified = False
            if not claim.reject_reason:
                claim.reject_reason = "not_grounded"
            log.info("  verify REJECT (not grounded, conf=%.2f): %s",
                     claim.confidence, claim.flag[:40])
            return claim

        # 通过静态检查 → 标记 verified，但最终提交由 skeptic LLM 裁决
        claim.verified = True
        log.info("  verify PASS (grounded, conf=%.2f, provenance=%s): %s",
                 claim.confidence, claim.provenance or "-", claim.flag[:30])
        return claim
