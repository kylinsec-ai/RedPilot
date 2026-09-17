"""taskprompt 单源守卫：包级导出必须指向**真正在跑**的那一份。

这一条是**回归守卫**。修之前，`ghost_worker/taskprompt.py`（143 行的最小单会话
形态）与 `ghost_worker/adapter/taskprompt.py`（705 行的竞技场版）同名并存，
而 orchestrator 只 import 后者：

    ghost_worker.build_task_prompt()          → 前者（**死代码**）
    orchestrator 实际调用的                    → 后者

死的那份还挂在 `ghost_worker/__init__.py` 的 `__all__` 上，所以
`from ghost_worker import build_task_prompt` 拿到的是一个**没人跑**的组装器 ——
不报错、不警告，只是拼出来的 prompt 少一整段（实测：技能库指引只写进了死的那份，
agent 侧一个字都收不到）。

根因是**两个同名模块共享一个包名空间**，谁改错文件都不会有任何反馈。
"""

from __future__ import annotations

import unittest

import ghost_worker
import ghost_worker.adapter.taskprompt as live
from ghost_worker import build_task_prompt, write_context_md


class TaskPromptIsSingleSourcedTests(unittest.TestCase):
    def test_package_exports_are_the_live_adapter(self):
        """包级导出必须就是 orchestrator 用的那一份（同一个函数对象）。"""
        self.assertIs(build_task_prompt, live.build_task_prompt)
        self.assertIs(write_context_md, live.write_context_md)
        self.assertEqual(build_task_prompt.__module__,
                         "ghost_worker.adapter.taskprompt",
                         "包级 build_task_prompt 又指向了别的模块 —— "
                         "同名双份的坑复活了")

    def test_no_shadowing_root_module(self):
        """包根下不得再出现能遮蔽 adapter.taskprompt 的 taskprompt.py。"""
        import os
        root = os.path.dirname(ghost_worker.__file__)
        stray = os.path.join(root, "taskprompt.py")
        self.assertFalse(
            os.path.exists(stray),
            f"{stray} 又出现了：它会被 `from .taskprompt import ...` 优先命中，"
            "而 orchestrator 用的是 adapter 那份 —— 两份 prompt 组装器并存")

    def test_skill_rule_ships_in_the_written_claude_md(self):
        """端到端一小步：技能库指引必须真的写进每题目录的 CLAUDE.md。

        死的那份才带这条规则；真跑的那份没有 —— 于是"技能库怎么用"从没到过
        agent 手上，而技能装载的开关、软链、名录全都正常，谁都不会发现。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as workdir:
            text = open(write_context_md(workdir), encoding="utf-8").read()
        self.assertIn("技能库", text,
                      "写入的 CLAUDE.md 里没有技能库指引 —— 规则又写丢/写歪了")
        self.assertIn("`hack`", text, "三层路由的入口没提")


if __name__ == "__main__":
    unittest.main()
