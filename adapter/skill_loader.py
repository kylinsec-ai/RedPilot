"""
Skill 加载器 — 渐进式披露 (Progressive Disclosure)

灵感来自 Pi Agent 的 Skills 系统，但实现完全原创：
- 启动时只读 SKILL.md 的 frontmatter（name + description），不读正文
- 匹配时按关键词加权打分，只加载得分最高的 skill 全文
- 避免把所有战术一股脑灌进 prompt，节省上下文

与 hxbai 的区别：
- hxbai 在 playbooks.py 里硬编码了 11 类战术，全量注入
- 我们用独立的 SKILL.md 文件，按需加载，人可读可改
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("adapter.skills")


@dataclass
class SkillMeta:
    """Skill 元信息（启动时加载，只有描述）"""
    name: str
    description: str
    path: str                    # SKILL.md 完整路径
    fingerprints: list[str] = field(default_factory=list)  # 快速匹配关键词
    desc_bigrams: frozenset = frozenset()   # 描述字符二元组（扫描时预计算）
    body: Optional[str] = None              # 正文（首次 load_skill 时惰性缓存）


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter，返回 (meta_dict, body)"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm_text = text[3:end].strip()
    body = text[end + 4:].strip()
    meta = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, body


def _extract_fingerprints(description: str) -> list[str]:
    """从描述中提取指纹关键词"""
    # 去掉常见停用词，保留有区分度的词
    stop = {"the", "and", "for", "with", "this", "that", "used", "when",
            "from", "into", "使用", "进行", "通过", "适用", "用于", "对于"}
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,3}", description)
    return [w.lower() for w in words if w.lower() not in stop][:15]


def _bigrams(text: str) -> frozenset:
    """计算文本的字符二元组集合（用于描述-目标相关度匹配）"""
    t = text or ""
    return frozenset(t[i:i + 2].lower() for i in range(max(0, len(t) - 1)))


class SkillStore:
    """
    Skill 仓库

    扫描 skills/ 目录，解析 SKILL.md frontmatter，提供按需加载。
    """

    # 加权匹配规则：(关键词, 权重, 关联skill名)
    _DOMAIN_SIGNALS = [
        # 文件扩展名 → 技能映射
        (r"\.py$|\.php$|\.jsp$|\.asp", 2.0, "web"),
        (r"\.elf$|\.bin$|\.exe$", 2.0, "pwn"),
        (r"\.pcap$|\.pcapng$", 2.0, "forensics"),
        (r"\.pem$|\.key$|\.crt$", 1.5, "crypto"),
        (r"\.apk$|\.dex$|\.ipa$", 2.0, "mobile"),
        (r"\.sol$", 2.0, "blockchain"),
        # 端口号 → 技能映射
        (r"\b(?:80|443|8080|8443|3000|5000)\b", 1.5, "web"),
        (r"\b(?:22|2222)\b", 1.0, "pentest"),
        (r"\b(?:3306|5432|6379|27017)\b", 1.0, "web"),
        (r"\b(?:445|139|135)\b", 1.5, "pentest"),
        # 关键词 → 技能映射
        (r"sql.?inject|xss|ssrf|csrf|lfi|rfi|upload|deseriali|webshell", 3.0, "web"),
        (r"buffer.?overflow|format.?string|heap|stack|rop|ret2|shellcode|pwn", 3.0, "pwn"),
        (r"rsa|aes|des|cipher|encrypt|decrypt|hash|md5|sha|crypto", 3.0, "crypto"),
        (r"lateral|pivot|内网|横向|提权|privilege.?escal|credential|渗透", 3.0, "pentest"),
        (r"forensic|memory.?dump|volatility|carv|stego|隐写|取证|流量", 3.0, "forensics"),
        (r"aws|s3|iam|cloud|容器逃逸|metadata|云", 3.0, "cloud"),
        (r"bypass|evas|waf|antivirus|obfuscat|检测|规避|对抗", 3.0, "evasion"),
    ]

    def __init__(self, skills_dir: str = None):
        self._dir = skills_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
        self._skills: dict[str, SkillMeta] = {}
        self._scan()

    def _scan(self):
        """扫描 skills/ 目录，只读 frontmatter"""
        if not os.path.isdir(self._dir):
            log.warning("skills dir not found: %s", self._dir)
            return
        for entry in os.listdir(self._dir):
            skill_dir = os.path.join(self._dir, entry)
            skill_md = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(skill_md):
                # 也支持 skills/xxx.md 单文件形式
                if entry.endswith(".md") and os.path.isfile(os.path.join(self._dir, entry)):
                    skill_md = os.path.join(self._dir, entry)
                    entry = entry[:-3]
                else:
                    continue
            try:
                with open(skill_md, "r", encoding="utf-8") as f:
                    text = f.read()
                meta, _ = _parse_frontmatter(text)
                name = meta.get("name", entry)
                desc = meta.get("description", "")
                fps = _extract_fingerprints(desc)
                self._skills[name] = SkillMeta(
                    name=name, description=desc,
                    path=skill_md, fingerprints=fps,
                    desc_bigrams=_bigrams(desc),
                )
            except Exception as e:
                log.warning("failed to load skill %s: %s", entry, e)
        log.info("loaded %d skills: %s", len(self._skills),
                 ", ".join(self._skills.keys()))

    def list_skills(self) -> list[dict]:
        """返回所有 skill 的概要（不含正文）"""
        return [{"name": s.name, "description": s.description}
                for s in self._skills.values()]

    def load_skill(self, name: str) -> Optional[str]:
        """按需加载 skill 全文（首次读文件，之后返回缓存）"""
        meta = self._skills.get(name)
        if meta is None:
            return None
        if meta.body is None:
            try:
                with open(meta.path, "r", encoding="utf-8") as f:
                    text = f.read()
                _, body = _parse_frontmatter(text)
                meta.body = body
            except Exception as e:
                log.warning("failed to load skill body %s: %s", name, e)
                return None
        return meta.body

    def match_skills(self, objective: str, targets: list[str] = None,
                     files: list[str] = None, *, top: int = 2) -> list[dict]:
        """
        根据题目信息匹配最相关的 skill。

        用加权关键词打分，不是简单的 if-else 路由。
        返回得分最高的 top 个 skill 元信息。
        """
        haystack = (objective or "").lower()
        if targets:
            haystack += " " + " ".join(str(t) for t in targets).lower()
        if files:
            haystack += " " + " ".join(str(f) for f in files).lower()

        scores: dict[str, float] = {name: 0.0 for name in self._skills}

        # 1. 领域信号匹配
        for pattern, weight, skill_name in self._DOMAIN_SIGNALS:
            if skill_name in scores and re.search(pattern, haystack, re.I):
                scores[skill_name] += weight

        # 2. Skill 自身 fingerprint 匹配
        for name, meta in self._skills.items():
            for fp in meta.fingerprints:
                if fp in haystack:
                    scores[name] += 1.0

        # 3. 描述与目标的 bigram 交集（描述 bigram 扫描时已预计算）
        obj_bigrams = _bigrams(objective)
        for name, meta in self._skills.items():
            overlap = len(obj_bigrams & meta.desc_bigrams)
            if overlap > 3:
                scores[name] += overlap * 0.2

        # 排序取 top
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        result = []
        for name, score in ranked[:top]:
            if score > 0:
                result.append({
                    "name": name,
                    "description": self._skills[name].description,
                    "score": score,
                })
        return result

    def skill_summary_xml(self) -> str:
        """生成 Pi Agent 风格的 XML 摘要，嵌入系统提示词"""
        lines = ["<available_skills>"]
        for s in self._skills.values():
            lines.append(f'  <skill name="{s.name}">{s.description}</skill>')
        lines.append("</available_skills>")
        return "\n".join(lines)
