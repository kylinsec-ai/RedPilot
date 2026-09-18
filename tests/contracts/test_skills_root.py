"""`redpilot.contracts.paths.skills_root` 的定位契约 + 技能面不为空的回归守卫。

这一条是**回归守卫**，不是新功能测试：搬迁把两处"数固定层数"的 skills 定位一起
打断了（`dirname(dirname(__file__))/skills` 与"三级上溯即仓库根"），两处都指向一个
不存在的 skills/ 路径（搬迁前布局里的 `packages/worker/skills`），
于是技能扫描静默退化成 0 个 —— 只打一条 warning，
不报错，而 prompt 仍然让 agent "按题型 read 匹配的 SKILL.md"。

所以断言分两半：解析器本身的行为，以及**在仓库布局下技能数必须 > 0**。

2026-09-16 技能库整体换成上游 `yaklang/hack-skills`（见 `skills/PROVENANCE.md`），
带进来一类新的静默失败：**扫得到、但读不对**。上游的 `description` 一律是 YAML
块标量（`>-` 折行），只认单行的解析器会把 103 条描述全读成字面量 `'>-'` ——
技能数非空、日志正常，而名录里一条描述都没有。所以这里补上"描述必须解析出来"
与"装载面判据一致"两条。
"""

from __future__ import annotations

import os
import re
import tempfile
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

    def test_walks_up_from_the_deepest_call_site(self):
        """**最深**的调用方也要命中 —— 它比上面那条多一层。

        `adapter/solver/pi_agent.py` 是**最深**的调用方。搬迁前的三包布局里它距
        仓库根 6 层，而当时的上溯上限恰好是 6 —— 最深调用方用满最后一层、
        **零余量**，再加一层包目录（或再挪一次文件）就静默归零。那个上限现在
        已经去掉（`skills_root` 走到文件系统根为止），所以这条守卫钉住的是：
        以后无论再搬几次，最深调用方都必须命中。
        上面那条从较浅的 skill_loader 出发，测不出这一点。
        """
        start = _REPO / "redpilot/worker/adapter/solver/pi_agent.py"
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

    def test_every_skill_description_parses_non_empty(self):
        """端到端一小步：`build_task_prompt` 的名录里描述必须读得出来。

        上面那条守"扫到了"，这条守"读对了" —— 上游 hack-skills 的 description
        **一律**是 YAML 块标量（`>-` 折行，103/103）。旧解析器只认单行 `key: value`，
        于是每条描述都被读成字面量 `'>-'`、正文首行从 `description: >-` 开始：
        技能扫得到 103 个，但名录里一条描述都没有 —— 又一次"看起来在跑"的静默失败。
        """
        from redpilot.worker.adapter.skill_loader import SkillStore
        store = SkillStore()
        empty = [s.name for s in store._skills.values() if not s.description]
        self.assertEqual(
            empty, [],
            f"这些技能没解析出描述（frontmatter 形态又变了）：{empty}")
        # 名录形态：pi 与兜底路径都靠 name/description/path 三件套决策
        xml = store.skill_summary_xml()
        self.assertIn("<available_skills>", xml)
        self.assertIn('path="', xml)
        sample = next(iter(store._skills.values()))
        self.assertNotIn(">-", sample.description,
                         "描述被读成了块标量标记本身")

    def test_catalogue_xml_escapes_external_text(self):
        """名录里的外部文本必须转义：这是喂给 Agent 的结构化面，坏一个字符整份废。

        描述来自外部（上游技能库），出现 `"` `<` `&` 只是时间问题
        —— 实测 `ghost-bits-cast-attack` 的描述里就有一个字面 `"`。
        发射点若不转义，属性会被截断、结构塌掉，而调用方毫无察觉。

        这里不引 XML 解析器（stdlib 的解析器默认带 XXE 面，而这份断言只需要
        看"有没有漏网的裸字符"）：扫所有文本/属性位置即可。
        """
        from redpilot.worker.adapter.skill_loader import SkillStore
        store = SkillStore()
        xml = store.skill_summary_xml()

        # 已知带 `"` 的技能：必须被转义，不能裸着出现在属性值里
        escaped = [m for m in store._skills.values() if '"' in m.description]
        self.assertTrue(escaped, "上游描述形态又变了：没有含引号的样本可断言")
        self.assertIn("&quot;", xml)

        # 结构完好：每个 <skill ...> 开标签的属性区里不该有裸的 `<`/`&`(除转义实体)
        for line in xml.splitlines()[1:-1]:
            self.assertTrue(line.startswith("  <skill "), line)
            self.assertTrue(line.endswith("</skill>"), line)
            attrs = line[line.index("<") + 1: line.index(">")]   # 去掉开头的 '<'
            self.assertNotIn("<", attrs, attrs)
            self.assertEqual(
                len(re.findall(r"&(?!amp;|lt;|gt;|quot;)", attrs)), 0,
                f"属性里有未转义的 & —— {attrs}")

    def test_loader_and_pi_agree_on_what_counts_as_a_skill(self):
        """装载面契约：名录侧与 pi 软链侧必须给出**同一个**技能集合。

        判据本身已单源（两处都调 `redpilot.contracts.paths.is_skill_dir`，其行为由
        下面那条用例守），但"两处都调了它"不等于"两处结果一致" —— 一边若改了
        过滤/遍历方式仍会错位。所以这里真的各跑一遍，比对集合。
        """
        from redpilot.worker.adapter.skill_loader import SkillStore
        from redpilot.worker.adapter.solver.pi_agent import _install_skills

        store = SkillStore()
        from_loader = {os.path.basename(os.path.dirname(m.path))
                       for m in store._skills.values()}
        self.assertTrue(from_loader, "名录为空 —— skills 定位又断了")

        with tempfile.TemporaryDirectory() as pi_home:
            self.assertEqual(_install_skills(pi_home), len(from_loader))
            dest = os.path.join(pi_home, ".pi", "agent", "skills")
            self.assertEqual(set(os.listdir(dest)), from_loader,
                             "名录里有、pi 软链里没有（或反过来）—— 两条装载面错位")

        for meta in store._skills.values():
            self.assertTrue(os.path.isfile(meta.path), meta.path)
            self.assertEqual(os.path.basename(meta.path), "SKILL.md")
            self.assertEqual(os.path.dirname(os.path.dirname(meta.path)),
                             store._dir)

    def test_is_skill_dir_is_the_single_predicate(self):
        """判据单源：`is_skill_dir` 只认"目录 + 含 SKILL.md"。"""
        from redpilot.contracts.paths import is_skill_dir
        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, "plain")
            skill = os.path.join(tmp, "skill")
            os.makedirs(plain)
            os.makedirs(skill)
            self.assertFalse(is_skill_dir(plain), "空目录不算技能")
            with open(os.path.join(plain, "README.md"), "w", encoding="utf-8") as f:
                f.write("x")
            self.assertFalse(is_skill_dir(plain), "别的 .md 不算技能")
            with open(os.path.join(skill, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: t\n---\n")
            self.assertTrue(is_skill_dir(skill))
            self.assertFalse(is_skill_dir(os.path.join(tmp, "nope")), "不存在的路径")


if __name__ == "__main__":
    unittest.main()
