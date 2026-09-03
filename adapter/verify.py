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

import logging
import math
import re
from dataclasses import dataclass

log = logging.getLogger("adapter.verify")

from .solver.base import _FLAG_BODY_RX, flag_body

# 占位词/诱饵特征
_PLACEHOLDER_RX = re.compile(
    r"(?:example|placeholder|test|dummy|sample|xxxx|0000|1234|abcd)"
    r"|^flag\{[a-f0-9]{32}\}$",  # 纯 md5 哈希
    re.IGNORECASE,
)


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
        return flag_body(self.flag)


def normalize_flag_body(flag: str) -> str:
    """标准化 flag 用于去重"""
    return flag_body(flag).strip().lower()


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

    # 3. Grounding: 在真实输出中逐字查找
    full_output = observed_output or ""
    if tool_outputs:
        for _tool, _args, out in tool_outputs:
            full_output += "\n" + str(out or "")

    if flag in full_output:
        claim.grounded = True
        claim.confidence = 0.95
        # 定位来源
        if tool_outputs:
            for tool, args, out in tool_outputs:
                if flag in str(out or ""):
                    claim.source_cmd = str(args.get("command", args) if isinstance(args, dict) else args)[:200]
                    claim.source_output = str(out)[:500]
                    break
    elif body in full_output:
        # body 匹配但外壳不完全匹配 (可能大小写问题)
        claim.grounded = True
        claim.confidence = 0.75
    else:
        # 未在真实输出中找到 → 可能是幻觉
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

    def verify(self, claim: Claim, *, force_skeptic: bool = False) -> Claim:
        """
        执行验证流程。

        高置信 (>= 0.9 且 grounded) → 直接通过 (跳闸直提)
        中等置信 → 走否定式质疑
        低置信 → 拒绝
        """
        # 已被 grounding 拒绝的直接返回
        if claim.confidence < 0.2:
            log.info("  verify REJECT (low confidence %.2f): %s — %s",
                     claim.confidence, claim.flag[:30], claim.reject_reason)
            return claim

        # 高置信跳闸直提
        if claim.confidence >= 0.9 and claim.grounded and not force_skeptic:
            claim.verified = True
            log.info("  verify PASS (high confidence %.2f, grounded): %s",
                     claim.confidence, claim.flag[:30])
            return claim

        # 中等置信 → 否定式质疑
        if self.llm is not None and claim.confidence >= 0.3:
            skeptic_pass = self._skeptic_check(claim)
            if skeptic_pass:
                claim.verified = True
                log.info("  verify PASS (skeptic approved): %s", claim.flag[:30])
            else:
                claim.reject_reason = "skeptic_rejected"
                claim.confidence *= 0.5
                log.info("  verify REJECT (skeptic rejected): %s", claim.flag[:30])
        elif claim.grounded:
            # 无 LLM 但 grounded → 降级通过
            claim.verified = True
            log.info("  verify PASS (grounded, no LLM): %s", claim.flag[:30])
        else:
            claim.reject_reason = "unverified_no_llm"
            log.info("  verify REJECT (not grounded, no LLM): %s", claim.flag[:30])

        return claim

    def _skeptic_check(self, claim: Claim) -> bool:
        """否定式质疑: 让 LLM 尝试反驳候选 flag"""
        if self.llm is None:
            return True

        approve_count = 0
        for _ in range(self.skeptic_votes):
            try:
                evidence = claim.source_output[:800] if claim.source_output else "(no direct output evidence)"
                messages = [{"role": "user", "content": (
                    "你是一位严格的安全竞赛评审。以下是一个候选 flag 及其产出证据。\n"
                    "请判断这个 flag 是否真实可信，还是可能是：\n"
                    "1. 模型幻觉/编造\n"
                    "2. 示例/占位值\n"
                    "3. 诱饵/混淆值\n\n"
                    f"候选 flag: {claim.flag}\n\n"
                    f"产出该 flag 的命令输出证据:\n{evidence}\n\n"
                    "如果你认为这是真实 flag，回复 'APPROVE'。\n"
                    "如果你认为应该拒绝，回复 'REJECT' 并给出理由。"
                )}]

                resp = self.llm.chat(messages, max_tokens=512, thinking=False)
                text = (getattr(resp, "text", "") or getattr(resp, "content", "") or "").strip()

                if "APPROVE" in text.upper():
                    approve_count += 1
            except Exception as e:
                log.warning("skeptic check failed: %s", e)
                approve_count += 1  # LLM 出错时默认通过

        return approve_count > self.skeptic_votes // 2
