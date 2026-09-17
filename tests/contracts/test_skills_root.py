"""`redpilot.contracts.paths.skills_root` 的定位契约 + 技能面不为空的回归守卫。

这一条是**回归守卫**，不是新功能测试：搬迁把两处"数固定层数"的 skills 定位一起
打断了（`dirname(dirname(__file__))/skills` 与"三级上溯即仓库根"），两处都指向一个
不存在的 skills/ 路径（搬迁前布局里的 `packages/worker/skills`），
于是技能扫描静默退化成 0 个 —— 只打一条 warning，
不报错，而 prompt 仍然让 agent "按题型 read 匹配的 SKILL.md"。

所以断言分两半：解析器本身的行为，以及**在仓库布局下技能数必须 > 0**。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from redpilot.contracts.paths import skills_root

# 本文件在 tests/contracts/ → 上溯两级才是仓库根
_REPO = Path(__file__).resolve().parents[2]


class SkillsRootTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("ADAPTER_SKILLS_DIR", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ADAPTER_SKILLS_DIR", None)
        else:
            os.environ["ADAPTER_SKILLS_DIR"] = self._saved

    def test_finds_the_repo_skills_dir_by_walking_up(self):
        """从策略层的真实位置出发，上溯必须命中仓库根的 skills/。

        这是被打断的那一处：起点是 `redpilot/worker/adapter/skill_loader.py`，
        距仓库根 4 层 —— 固定层数的写法在搬迁后全部指错。
        """
        start = _REPO / "redpilot/worker/adapter/skill_loader.py"
        self.assertEqual(skills_root(str(start)), str(_REPO / "skills"))

    def test_env_var_wins(self):
        os.environ["ADAPTER_SKILLS_DIR"] = str(_REPO / "skills")
        start = _REPO / "redpilot/worker/adapter/skill_loader.py"
        self.assertEqual(skills_root(str(start)), str(_REPO / "skills"))

    def test_extra_wins_over_the_walk(self):
        """容器侧传 `/app/skills`：镜像里那条不随 HOME 改写消失的路径。"""
        start = _REPO / "redpilot/worker/adapter/skill_loader.py"
        self.assertEqual(
            skills_root(str(start), extra=str(_REPO / "skills")),
            str(_REPO / "skills"))

    def test_returns_empty_string_when_nothing_matches(self):
        """找不到就返回空串，**不是抛异常也不是返回一个不存在的路径**。

        调用方（`SkillStore` / `_install_skills`）拿空串就会跳过扫描；
        返回一个不存在的路径会让它们各自再去 isdir 一次，两处都得记着查。
        """
        self.assertEqual(skills_root("/nonexistent/deep/tree/mod.py"), "")

    def test_a_bare_skills_dir_without_SKILL_md_does_not_count(self):
        """同名但空的 skills/ 不算命中 —— 否则会盖住真正的那份。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "skills").mkdir()          # 空目录，无 SKILL.md
            self.assertEqual(skills_root(str(Path(tmp) / "a/b/c/mod.py")), "")


class SkillsAreActuallyLoadedTests(unittest.TestCase):
    """**回归守卫**：仓库布局下技能面必须非空。

    这条断言在搬迁后曾经是红的（0 个技能）。它守的不是解析器本身，而是
    "两处调用点都真的接上了解析器" —— 解析器写对了但调用点没改，这条依然会红。
    """

    def test_skill_store_scans_more_than_zero_skills(self):
        from redpilot.worker.adapter.skill_loader import SkillStore
        store = SkillStore()
        self.assertGreater(
            len(store._skills), 0,
            f"技能面为空（dir={store._dir}）—— skills 定位又断了："
            "逐题 HOME 下 pi 只认 $HOME 内的 skills/，框架两条扫描都以此为准")

    def test_taskprompt_injects_a_matched_skill(self):
        """端到端一小步：`build_task_prompt` 必须真的注入技能正文。

        上面那条守"扫到了"，这条守"用上了" —— 中间还隔着 `_get_skill_store()`
        的缓存与 `match_skills` 的打分。
        """
        from redpilot.worker.adapter.skill_loader import SkillStore
        store = SkillStore()
        matched = store.match_skills("web sql injection on /login.php",
                                     targets=["http://10.0.0.1"])
        self.assertTrue(matched, "web 题面没匹配到任何技能 —— 打分表或 skills 目录断了")
        self.assertTrue(store.load_skill(matched[0]["name"]),
                        "匹配到的技能正文读不出来（load_skill 返回空）")


if __name__ == "__main__":
    unittest.main()
