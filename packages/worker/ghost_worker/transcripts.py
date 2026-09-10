"""transcript 文件后处理 — 编排层所有(非 solver 能力)。

compress_transcript 此前在 adapter/solver/pi_agent.py,driver 绕过 ABC 直接
import(边界渗漏);归拢到本模块:pi_agent 不再导出,编排层经本模块调用。
"""

from __future__ import annotations

import logging
import os

from ghost_contracts.vocabulary import MESSAGE_UPDATE

log = logging.getLogger("ghost_worker.transcripts")


def compress_transcript(path: str) -> None:
    """session 结束后压缩 transcript：删除 message_update 增量事件。

    message_update 占 transcript 体积 ~92%（流式 token 增量），
    message_end 已包含完整消息，增量对历史回放无用。
    原子写回（.tmp + os.replace），失败仅 warn 不抛异常。
    调用时序契约:编排层必须先 relay.flush_run()(排干中继未读字节)再压缩,
    否则行内信息随重写丢失。
    """
    if not path or not os.path.isfile(path):
        return
    tmp = path + ".compressing"
    try:
        before = os.path.getsize(path)
        if before < 1024:  # <1KB 不值得压缩
            return
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        kept = [ln for ln in lines if not _is_message_update(ln)]
        removed = len(lines) - len(kept)
        if removed == 0:
            return
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, path)
        after = os.path.getsize(path)
        log.info("transcript compressed: %d → %d bytes (%d message_update removed, -%.0f%%)",
                 before, after, removed, (1 - after / before) * 100 if before else 0)
    except Exception as e:
        log.warning("transcript compress failed (%s): %s", path, e)
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _is_message_update(line: str) -> bool:
    """快速判断一行 JSONL 是否为 message_update 事件（避免完整 JSON 解析）"""
    # message_update 的 type 字段总是前几个字符，用 str.find 避免 json.loads 开销
    idx = line.find('"type"')
    if idx < 0:
        return False
    # 跳过 "type": 后找值
    rest = line[idx + 6:].lstrip()
    return rest.startswith(': "%s"' % MESSAGE_UPDATE) or rest.startswith(':"%s"' % MESSAGE_UPDATE)
