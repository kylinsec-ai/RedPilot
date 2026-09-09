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

import hashlib
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("adapter.verify")

# 占位词/诱饵特征
_PLACEHOLDER_RX = re.compile(
    r"(?:example|placeholder|test|dummy|sample|xxxx|0000|1234|abcd)"
    r"|^flag\{[a-f0-9]{32}\}$",  # 纯 md5 哈希
    re.IGNORECASE,
)
# flag body 合法字符（防命令注入 payload 误提取）
_FLAG_BODY_RX = re.compile(r"^[A-Za-z0-9_\-.:/]{3,200}$")


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

    @property
    def body(self) -> str:
        """提取 flag{} 内的主体"""
        m = re.match(r"flag\{(.+)\}", self.flag, re.IGNORECASE)
        return m.group(1) if m else self.flag


def normalize_flag_body(flag: str) -> str:
    """标准化 flag 用于去重"""
    m = re.match(r"flag\{(.+)\}", flag, re.IGNORECASE)
    body = m.group(1) if m else flag
    return body.strip().lower()


def _entropy(s: str) -> float:
    """计算字符串的 Shannon 熵"""
    if not s:
        return 0.0
    freq = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    total = len(s)
    return -sum((n / total) * math.log2(n / total) for n in freq.values())


def flag_confidence(flag: str, observed_output: str, tool_outputs: list = None) -> Claim:
    """
    评估候选 flag 的置信度。

    返回 Claim 对象，含 grounding 结果和初步置信度。
    """
    claim = Claim(flag=flag)

    # 1. 格式检查
    if not re.match(r"^flag\{.+\}$", flag, re.IGNORECASE):
        claim.reject_reason = "invalid_format"
        claim.confidence = 0.0
        return claim

    body = claim.body

    # 2. body 字符集检查（引号/空格/命令字符 → 非法）
    if not _FLAG_BODY_RX.match(body):
        claim.reject_reason = "invalid_body_chars"
        claim.confidence = 0.0
        return claim

    # 3. 占位词 / 低熵检查
    if _PLACEHOLDER_RX.search(body):
        claim.reject_reason = "placeholder_pattern"
        claim.confidence = 0.1
        return claim

    if len(body) > 4 and _entropy(body) < 1.5:
        claim.reject_reason = "low_entropy"
        claim.confidence = 0.15
        return claim

    # 3. Grounding: 只在真实命令输出（tool_outputs）中逐字查找，且必须区分
    #   「系统观测」与「agent 自造」。注意：observed_output 含助手文本（思考/推测），
    #   LLM 幻觉的 flag 会被自己的文本"grounded"化导致误放行 → 排除文本部分。
    #
    #   自造判据（框架能力，不改 agent）：若包含该 flag 的命令行里【本身含有】
    #   这个 flag/body 字符串（echo/printf/heredoc/cat>FLAG/./validator 'flag{...}'），
    #   说明是 agent 把自己的猜测敲进命令（写了再读、或把猜测塞给本地验证器），
    #   不是系统产出 —— 不算取证证据。真实观测（f1 socket probe、f2-07 curl /check
    #   等）命令里没有该 flag，flag 只出现在【输出】里 → 才算 observed grounding。
    #   另：读 agent 自己写的假设文件（FLAG/SOURCE/MEMORY/黑板/todolist/transcript/
    #   tried_commands/.pi-home）不算系统观测 —— agent 把"结论"写进这些文件后再 cat，
    #   与 echo 自造同源，不能当作平台产出的证据。真实 flag 总有独立系统观测作证。
    #   ★第二轮严格化：evidence 必须【完整 flag{...} 信封】逐字出现在输出里
    #   （大小写无关）。只出现【裸 body】= agent 从别处抠到裸值自己包了信封（如 f2-05
    #   从 r2 输出的 movabs rax,0x6d6a031f1105170c 包成 flag{0x6d6a...}），不算取证。
    tool_text = ""
    evidence = []          # (cmd, out)：真正系统观测（完整信封）→ 提交证据
    authored = []          # (cmd, out)：agent 自造 / 读自己写的假设文件 → 非证据
    _AGENT_FILE_RX = re.compile(r"(?:^|[/\s>])(?:FLAG[\.\w]*|SOURCE|MEMORY\.?md?"
                                r"|_?blackboard[\w._-]*|todolist[\w._-]*"
                                r"|tried_commands[\w._-]*|_transcripts|notes[\w._-]*)", re.IGNORECASE)
    _ENV_RX = re.compile(r"flag{" + re.escape(body) + r"}", re.IGNORECASE)
    if tool_outputs:
        for _tool, _args, out in tool_outputs:
            text = str(out or "")
            tool_text += "\n" + text
            cmd = str(_args.get("command", _args) if isinstance(_args, dict) else _args or _tool).strip()
            if cmd in ("{}", "[]", "None", "()", ""):
                cmd = ""
            if not text:
                continue
            if not _ENV_RX.search(text):
                continue                      # 输出里没有完整 flag{body} 信封 → 非取证
            if cmd and _ENV_RX.search(cmd):
                authored.append((cmd, text))      # 命令里含完整信封 → agent 自造
            elif cmd and _AGENT_FILE_RX.search(cmd):
                authored.append((cmd, text))      # 读/写 agent 自己的假设文件 → 自造同源
            else:
                evidence.append((cmd, text))       # 只出现在输出 → 系统观测

    if evidence:
        claim.grounded = True
        claim.confidence = 0.95
        c, o = evidence[0]
        if c:
            claim.source_cmd = c[:200]
            claim.source_output = o[:500]
        else:
            # 输出含 flag 但拿不到命令出处 → 无法证明是系统观测，保守降级
            claim.grounded = False
            claim.confidence = 0.4
            claim.reject_reason = "no_source_cmd"
    elif authored:
        # flag 只出现在 agent 自己敲的命令（echo/printf/validate自己的猜测）
        claim.grounded = False
        claim.confidence = 0.2
        claim.reject_reason = "agent_authored"
    else:
        # 未在任何真实命令输出中找到 → 疑似幻觉/猜测
        claim.grounded = False
        claim.confidence = 0.3
        claim.reject_reason = "not_grounded"

    return claim


class Verifier:
    """
    三重校验门验证器

    - grounding: 代码校验 (始终执行)
    - skeptic:   否定式质疑 (有 LLM 时)
    - followup:  追问式复核 (有 LLM 时)
    """

    def __init__(self, llm=None, *, skeptic_votes: int = 1):
        self.llm = llm
        self.skeptic_votes = max(1, skeptic_votes)

    def verify(self, claim: Claim) -> Claim:
        """
        提交门：只放行【系统观测】grounding 的 flag（fail-closed）。

        真实解全部是 observed-grounded（f1 socket probe、f2-07 curl /check 输出里的
        UUID）；agent 自造猜测（echo/printf/./validator 自己敲的 flag、未取证幻觉）
        一律不自动提交 → 消灭平台「答题失败」刷屏。代价最多是少提交一次试探性猜测，
        不会丢真解（真解都来自观测，下一场仍会重观测到）。
        """
        # 格式检查
        if not claim.flag or not claim.flag.startswith("flag{") or not claim.flag.endswith("}"):
            claim.reject_reason = "invalid_format"
            log.info("  verify REJECT (invalid format): %s", claim.flag[:30])
            return claim

        # 只提交 observed 取证（confidence>=0.9 是观测证据的唯一出口）
        if claim.grounded and claim.confidence >= 0.9:
            claim.verified = True
            log.info("  verify PASS (grounded observed, conf=%.2f): %s",
                     claim.confidence, claim.flag[:30])
            return claim

        # 其余一律拒绝：agent 自造 / 未取证 / 无法定位来源，都不自动提交
        claim.verified = False
        if not claim.reject_reason:
            claim.reject_reason = "not_grounded_or_authored"
        log.info("  verify REJECT (ungrounded/authored, conf=%.2f, reason=%s): %s",
                 claim.confidence, claim.reject_reason, claim.flag[:40])
        return claim

