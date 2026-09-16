"""合规打码口径 —— **唯一定义处**。

为什么单独一个模块（B61，2026-09-11）：
打码这件事现在有**两条消费路径** ——
  1. driver 的清理路径（`_scrub_flag_plaintext`，B52 建立）：给会回灌进下一场的
     文本打码，重发与解完两条分支共用；
  2. 控制台的**只读展示**路径（题目工作区的观察面板）：`.heimdall.json` 的
     `why` / `evidence` 字段是**工具输出摘录**，可能含答案明文，不能原样进浏览器。

两条路径各写一份正则 = 迟早漂移。B52 的根因正是这类漂移：清洗器历史上只认
`flag{...}` 信封，而框架自己的账本 `.unverified_flags` 存的是**去信封、小写归一**
的 body —— 同一批 body 落进 MEMORY.md / _blackboard.json / tried_commands.md
时全部漏过（某题实测三文件合计命中 body 14 次，而 `flag{` 命中 0 次）。

★**别再拿 `grep 'flag{'` 判断 workdir 干净与否，会得到假阴性。**

本模块只依赖标准库，不 import driver（避免环）。
"""

from __future__ import annotations

import os
import re

__all__ = [
    "REDACTION",
    "flag_bodies",
    "flag_plaintext_rx",
    "MATCH_RX",
    "count_redactions",
    "scrub_text",
]

#: 打码后的替换串。与 driver 侧历史输出保持一致（日志里有按此串比对的地方）。
REDACTION = "[REDACTED-FLAG]"

#: 信封形式：`flag{...}`。这是**平台契约**的形态，任何题都适用。
# ★[B64b] 闭分支必须**排除换行**：`[^}]` 会把 `\n` 也吃进去，于是一个**截断**的
# `flag{` 会一路跨行吃到几百字符外某个毫不相干的 `}`（实测吞掉 204 字符、
# 中间一整行 `submit resp: {'correct': True, ...}` 整条消失）。flag 不含换行，
# 所以按行收口既是正确的、也不损失任何合法匹配。
_ENVELOPE_RX = r"flag\{[^}\n]{1,200}\}"


def _derive_body_class() -> tuple:
    """[B64] 从 `verify` 的**唯一字符集定义**派生可内联的字符类。

    `_FLAG_BODY_RX` 是锚定的（`^[...]{3,200}$`），这里拆成能嵌进任意位置的
    `[...]`。返回 `(字符类, 是否派生成功)` —— 第二个值给测试断言用：
    verify 改了结构导致派生失败时，**测试要响**，而运行时只是降级。
    """
    try:
        from adapter.verify import _FLAG_BODY_RX as _rx
        m = re.match(r"^\^(\[[^\]]+\])\{\d+,\d+\}\$$", _rx.pattern)
        if m:
            return m.group(1), True
    except Exception:
        pass
    # 兜底 = verify 的**同宽**字面量：行为与「改之前 + 本条修复」一致。
    # ★刻意不用宽松类 `[^\s...]`：面板会把它渲染给用户看，宽松类会吃掉中文散文；
    #   而「派生失败」是可被测试逮住的工程状态，不该用污染显示来兜。
    return r"[A-Za-z0-9_\-.:/]", False


_BODY_CLASS, _BODY_CLASS_DERIVED = _derive_body_class()

#: [B64] **截断信封**：`flag{` + body 串，尾 `}` 已丢。
#: 需要它的理由见模块头。字符集与 verify 同源，故中文说明（非 body 字符集）
#: 天然不匹配 —— 只认 body 形态，不放宽成「任意字符」。
_ENVELOPE_OPEN_RX = r"flag\{%s{3,200}" % _BODY_CLASS

#: 账本 body 的最短长度门槛。短于这个长度的 body 逐一匹配会误伤正常文本
#: （8 字符的词在中文技术文本里撞车概率不低），故不参与打码。
_MIN_BODY = 8


def flag_bodies(workdir: str) -> set:
    """账本 `.unverified_flags` 里的 bare body 集合（去信封、小写归一）。

    读不到就返回空集 —— 不是错误：新题第一次清理时这个文件还不存在。
    """
    try:
        with open(os.path.join(workdir, ".unverified_flags"), encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if len(ln.strip()) >= _MIN_BODY}
    except (OSError, UnicodeDecodeError):
        return set()


def flag_plaintext_rx(workdir: str):
    """[B52] 本题的「答案明文」正则：信封形式 + 账本里的 bare body。

    body 用词边界夹住并按**最长优先**排序：前者防短 body 误伤正常文本，
    后者防「一条 body 是另一条前缀」时先替短的留下残尾。
    整条正则 IGNORECASE —— body 是归一化小写的，若原文是混合大小写，
    区分大小写就会漏。
    """
    # ★闭分支在前：交替左优先，否则完整信封会被开分支吃出残尾 '}'
    alts = [_ENVELOPE_RX, _ENVELOPE_OPEN_RX]
    for b in sorted(flag_bodies(workdir), key=len, reverse=True):
        alts.append(r"(?<![0-9A-Za-z_])%s(?![0-9A-Za-z_])" % re.escape(b))
    return re.compile("|".join(alts), re.IGNORECASE)


#: 只认信封的兜底正则 —— 读不到 `.unverified_flags` 时的口径。
#: 取它做「该题账本不可用，只能按平台契约打码」的降级，**不是**去掉打码。
MATCH_RX = re.compile("|".join([_ENVELOPE_RX, _ENVELOPE_OPEN_RX]),
                      re.IGNORECASE)


def scrub_text(text: str, rx) -> tuple:
    """按 `rx` 打码，返回 `(打码后的文本, 命中处数)`。

    命中 **0** 处时返回的文本与输入**逐字相同**（`re.sub` 在无命中时不改动
    字符串对象的内容），所以调用方可以拿 `n == 0` 判断「显示有没有偏离
    原始内容」—— 面板要靠这个标注"已打码"。
    """
    if not text:
        return text, 0
    out, n = rx.subn(REDACTION, text)
    return out, n


def count_redactions(text: str) -> int:
    """数一段文本里已有多少处打码痕迹（含 driver 历史写入的）。"""
    return text.count(REDACTION) if text else 0
