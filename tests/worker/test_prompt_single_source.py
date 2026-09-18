"""提示面单源：同一份约束不许走两条路进同一个上下文。

## 为什么需要这条

`docs/architecture/TARGET_ARCHITECTURE.md` §2.1 与 §3.1 立过一条规则：
**一份内容只允许出现在一处**。这文件是它的执行点。

本仓吃过同型的亏：`_ISOLATION_CONSTRAINT`（1834 字符）曾**既**被
`write_context_md()` 写进逐题 `CLAUDE.md`，**又**被 `build_task_prompt()`
直接 append 进 prompt —— 同一常量、每次会话白烧 1834 字符，而**没有任何
测试会因此变红**（两份内容一字不差，行为上看不出区别）。这类"重复但正确"
的劣化只能靠结构断言抓。

2026-09 死码清扫收敛到**只走 CLAUDE.md 一条路**，理由不只是省 token：
`CLAUDE.md` 是 pi 的项目指令文件，Agent 可随时重读，且**子 Agent 进程**
（同 cwd 的独立 pi）也会加载它 —— 而父会话的 prompt 不会传给子 Agent。
隔离红线恰恰必须对子 Agent 也生效。

## 断言的两个方向

1. **在**：`CLAUDE.md` 里必须有且只有一份（漏了 = 红线没了，且因为它在文件里，
   比 prompt 更难被发现）；
2. **不在**：prompt 里不许再有第二份（回来 = 重复注入回来了）。

`_OFFLINE_CONSTRAINT` 仍走 prompt 一侧，一并钉住 —— 免得将来"单源化"时
把两条路对调，结果两个都在或两个都没了。
"""

from __future__ import annotations

from redpilot.worker.adapter.task import AgentTask
from redpilot.worker.adapter.taskprompt import build_task_prompt, write_context_md

# 取每段正文里足够独特、又必然随正文一起被注入的短语
_ISOLATION_MARK = "战场隔离红线"
_OFFLINE_MARK = "禁止联网下载"


def _claude_md(tmp_path) -> str:
    return open(write_context_md(str(tmp_path)), encoding="utf-8").read()


def _prompt() -> str:
    task = AgentTask(objective="probe", category="web",
                     targets=["http://127.0.0.1:1/"], files=[])
    return build_task_prompt(task)


def test_isolation_constraint_lives_in_claude_md_exactly_once(tmp_path):
    claude = _claude_md(tmp_path)
    assert claude.count(_ISOLATION_MARK) == 1, (
        "隔离红线在 CLAUDE.md 里出现了 %d 次（应恰好 1 次）—— "
        "它是写进文件的那一份，漏了就等于红线没了" % claude.count(_ISOLATION_MARK)
    )


def test_isolation_constraint_is_not_duplicated_into_the_prompt(tmp_path):
    prompt = _prompt()
    assert _ISOLATION_MARK not in prompt, (
        "隔离红线又回到了 prompt 里 —— 它与 CLAUDE.md 那份逐字重复（见本文件 docstring）。"
        "单源规则：只走 CLAUDE.md 一条路。"
    )


def test_offline_constraint_still_rides_the_prompt(tmp_path):
    """反面：离线约束**应该**在 prompt 里（它没有 CLAUDE.md 那一份）。

    这条与上面两条成对存在，防止"单源化"被做成"两边都删"。
    """
    assert _OFFLINE_MARK in _prompt()
