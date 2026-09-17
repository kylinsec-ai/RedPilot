"""
Skill 名录装载器

与 hxbai 的区别：hxbai 在 playbooks.py 里硬编码了 11 类战术、全量注入 prompt；
本模块只读技能目录的 frontmatter，产出一份**名录**（名字 + 描述 + 路径）。

**框架不再自己挑技能**（2026-09-16）：昔日这里有一张 `_DOMAIN_SIGNALS`
「正则 → 技能名」表做 top-2 预选、由 taskprompt 注入技能正文。技能库换成上游
`yaklang/hack-skills`（103 个技能，自带 hack → 分类入口 → 深度题面三层路由）之后，
那张表既覆盖不了题面、又要跟着每次同步手改——路由交回给 pi 的原生渐进披露
（pi_agent._install_skills 把 skills/ 软链进每题 HOME，系统提示只放
`<available_skills>`，Agent 按题目分析自己 read），框架只负责把名录喂给 pi
与 README 说的那条兜底路径（ADAPTER_SKILL_AGENT=0）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ghost_contracts.paths import is_skill_dir, skills_root

log = logging.getLogger("adapter.skills")


@dataclass
class SkillMeta:
    """Skill 元信息（启动时加载，只有名字/描述/路径，不含正文）"""
    name: str
    description: str
    path: str                    # SKILL.md 完整路径


def _xml_escape(text: str) -> str:
    """转义成可放进 XML 文本/属性的字面量。名录是**外部输入的投影**——
    描述里出现 `<` `&` `"` 是迟早的事，一处转义胜过在每个格式化点设防。"""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def _parse_frontmatter(text: str) -> dict:
    """解析 YAML frontmatter，返回 meta 字典。

    只认**扁平键值**：单行 `key: value`，以及 YAML 块标量 `key: >-` / `key: |`
    （缩进多行收敛成一行）。不引入 yaml 依赖——技能文件是外部输入，
    钉死一个极小的子集比拖进一个解析器更好审。

    ⚠️ 块标量这条是硬需求：上游 hack-skills 的 `description` **一律**是
    `>-` 折行形式（103/103）。旧解析器只认单行，于是每条描述都被读成字面量
    `'>-'`、正文首行也从 `description: >-` 开始 —— 名录里 103 条描述全空。
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fm_lines = text[3:end].splitlines()

    meta: dict[str, str] = {}
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        key, sep, value = line.partition(":")
        if not sep or line[:1].isspace():
            # 续行/缩进行：没有所属键就被丢（我们只认扁平结构）
            i += 1
            continue
        key, value = key.strip(), value.strip()
        if value in (">", ">-", ">+", "|", "|-", "|+"):
            block: list[str] = []
            i += 1
            while i < len(fm_lines) and (not fm_lines[i].strip()
                                         or fm_lines[i][:1].isspace()):
                block.append(fm_lines[i].strip())
                i += 1
            # 块标量语义：`>` 折行成空格、`|` 按行保留；两者都吃掉尾部空行
            joined = " ".join(p for p in block if p) if value.startswith(">") \
                else "\n".join(block).strip()
            meta[key] = joined.strip()
            continue
        meta[key] = value.strip().strip('"').strip("'")
        i += 1
    return meta


class SkillStore:
    """
    技能名录仓库

    扫描 skills/ 目录，解析每个 SKILL.md 的 frontmatter，产出名录。
    不读取、不匹配、不注入正文——那是 Agent 自己的事（见模块 docstring）。
    """

    def __init__(self, skills_dir: str = None):
        # 默认目录由 `ghost_contracts.paths.skills_root` 定位（env → /app/skills →
        # 从本文件上溯）。此前是 `dirname(dirname(__file__))/skills`：那在朋友的
        # 目录布局里对，搬进 ghost_worker/adapter/ 之后指向不存在的
        # `packages/worker/skills` —— 扫描退化成 0 个技能且只打一条 warning。
        self._dir = skills_dir or skills_root(__file__, extra="/app/skills")
        self._skills: dict[str, SkillMeta] = {}
        self._scan()

    def _scan(self):
        """扫描 skills/ 目录，只读 frontmatter"""
        if not os.path.isdir(self._dir):
            log.warning("skills dir not found: %s", self._dir)
            return
        for entry in sorted(os.listdir(self._dir)):
            skill_dir = os.path.join(self._dir, entry)
            # pi 的发现规则：含 SKILL.md 的目录才算技能（判据单源，与 _install_skills 同）
            if not os.path.isdir(skill_dir) or not is_skill_dir(skill_dir):
                continue
            skill_md = os.path.join(skill_dir, "SKILL.md")
            try:
                with open(skill_md, "r", encoding="utf-8") as f:
                    text = f.read()
                meta = _parse_frontmatter(text)
                name = meta.get("name") or entry
                self._skills[name] = SkillMeta(
                    name=name,
                    description=meta.get("description", ""),
                    path=skill_md,
                )
            except Exception as e:
                log.warning("failed to load skill %s: %s", entry, e)
        missing = [s.name for s in self._skills.values() if not s.description]
        log.info("loaded %d skills (dir=%s)%s", len(self._skills), self._dir,
                 f"；{len(missing)} 个描述为空: {missing}" if missing else "")

    def skill_summary_xml(self) -> str:
        """生成 Pi Agent 风格的 XML 名录（名字 + 路径 + 首句描述）

        与 pi 原生 `<available_skills>` 同构：Agent 拿它就能决定 read 哪一个。
        只取描述的**首句** —— 上游 hack-skills 的描述平均 221 字符、最长 615
        （103 条全文 XML 35KB，能占掉 prompt 的一半），而当路由判据的门面只需要
        "这技能是干嘛的"那一句；首句平均 42 字符，整份名录降到 ~16KB。
        """
        lines = ["<available_skills>"]
        for s in self._skills.values():
            desc = _xml_escape(s.description or "(无描述)")
            head, _, _ = desc.partition(". ")
            if head:
                desc = head
            lines.append(
                f'  <skill name="{_xml_escape(s.name)}" path="{_xml_escape(s.path)}">'
                f"{desc}</skill>")
        lines.append("</available_skills>")
        return "\n".join(lines)
