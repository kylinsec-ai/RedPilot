"""flag 提取与校验 — worker 内单一来源。

此前 adapter/solver/base.py 持实现,driver 又从 solver.base 导入并在自己的
on_fact 里重复提取;归拢到本模块,solver 会话内提取与编排层提取同源。
正则维持硬编码 flag{...}(ADAPTER_FLAG_FORMAT 只渲染进 prompt,不参数化提取)。
"""

from __future__ import annotations

import re

# flag 提取正则
_FLAG_RX = re.compile(r"flag\{[^}]{1,200}\}", re.IGNORECASE)
_FINAL_ANSWER_RX = re.compile(r"<FinalAnswer>(.*?)</FinalAnswer>", re.DOTALL)
# flag body 合法字符：字母数字 + 常见分隔符（防命令注入 payload 误提取）
_FLAG_BODY_RX = re.compile(r"^[A-Za-z0-9_\-.:/]{3,200}$")
# 占位符/省略号 body（如 prompt 模板里的 flag{...}）必须含至少一个字母数字
_ALNUM_RX = re.compile(r"[A-Za-z0-9]")


def flag_body(flag: str) -> str:
    """提取 flag{} 内主体；无完整外壳时原样返回"""
    m = re.match(r"flag\{(.+)\}", flag, re.IGNORECASE)
    return m.group(1) if m else flag


def normalize_flag_body(flag: str) -> str:
    """去外壳 + 去空白 + 小写：跨会话去重与提交前归一化比较"""
    return flag_body(flag).strip().lower()


def is_valid_flag(flag: str) -> bool:
    """校验 flag 整体合法性：外壳完整 + body 无引号/空格/命令字符 + 非纯标点占位符"""
    body = flag_body(flag)
    return body != flag and bool(_FLAG_BODY_RX.match(body)) and bool(_ALNUM_RX.search(body))


def extract_flags(text: str) -> list[str]:
    """从文本中提取所有 flag{...} 格式的候选（过滤非法 body）"""
    if not text:
        return []
    found = set()
    for m in _FLAG_RX.finditer(text):
        f = m.group(0)
        if is_valid_flag(f):
            found.add(f)
    for m in _FINAL_ANSWER_RX.finditer(text):
        for fm in _FLAG_RX.finditer(m.group(1)):
            if is_valid_flag(fm.group(0)):
                found.add(fm.group(0))
    return list(found)
