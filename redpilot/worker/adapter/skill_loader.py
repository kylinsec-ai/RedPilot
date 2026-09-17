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


class SkillStore:
    """
    Skill 仓库

    扫描 skills/ 目录，解析 SKILL.md frontmatter，提供按需加载。
    """

    # 加权匹配规则：(关键词, 权重, 关联skill名)
    #
    # ⚠️ 2026-09：技能面已**整体替换**为 SpecterOps/skills（Apache-2.0 / MIT，见
    #    skills/NOTICE.md）。下方映射同步重指向新技能名 —— 旧名（web/pwn/forensics/
    #    crypto/pentest/reverse/cloud/evasion/java-exploit/web-deep/web-attack/
    #    post-exploit/ad/cicd/llm/ebpf）**已全部不存在**；不重指向则匹配循环里的
    #    `if skill_name in scores` 会静默跳过每一条，预选器退化成仅靠描述 bigram，
    #    而 `test_skills_root::test_taskprompt_injects_a_matched_skill` 仍会「偶然通过」
    #    （webapp-review 的描述含 web）—— 静默错配比测试变红更难发现。
    _DOMAIN_SIGNALS = [
        # 文件扩展名 → 技能映射
        (r"\.py$|\.pyw$", 2.0, "openssf-python-review"),
        (r"\.php$|\.jsp$|\.asp", 2.0, "webapp-review"),
        (r"\.go$", 2.0, "go-review"),
        (r"\.(?:c|cc|cpp|h|hpp)$", 2.0, "cpp-core-guidelines"),
        (r"\.elf$|\.bin$|\.exe$", 2.0, "binary-ninja-mcp-analysis"),
        (r"\.tf$|\.hcl$|\.tfvars$", 2.0, "iac-attack-surface"),
        # 端口号 → 技能映射
        (r"\b(?:80|443|8080|8443|3000|5000)\b", 1.5, "webapp-review"),
        (r"\b(?:22|2222)\b", 1.0, "ssh-ops"),
        (r"\b(?:3306|5432|6379|27017)\b", 1.0, "security-review"),
        (r"\b(?:445|139|135)\b", 1.5, "bloodhound-ad-analysis"),
        # 版本控制 / 代码审计
        (r"\bgit\b|commit|merge|branch|diff|staged|worktree", 2.0, "git-preflight"),
        (r"code.?review|代码审计|pull.?request|merge.?readiness|审查变更", 3.0, "code-review"),
        (r"\bcwe\b|弱点|缺陷分类|弱点类型", 3.0, "cwe-code-review"),
        (r"owasp|top.?10", 3.0, "owasp-security-code-review"),
        (r"secret|密钥泄露|api.?key|凭证泄露|token.?leak", 3.0, "secret-scan"),
        # Web 应用
        (r"sql.?inject|xss|ssrf|csrf|lfi|rfi|upload|deseriali|webshell|idor|oauth|jwt|graphql", 3.0, "webapp-review"),
        (r"web.?app|web 应用|前端|浏览器|playwright|渲染|console.?error", 3.0, "webapp-qa"),
        # 二进制 / 逆向
        (r"revers|逆向|disassembl|反汇编|decompil|反编译|bytecode|字节码|"
         r"virtual.?machine|虚拟机|deobfuscat|反混淆|unpack|脱壳|crackme|固件|firmware", 3.0, "ghidra-mcp-analysis"),
        (r"binary.?ninja|binja|\bhlil\b|\bmlil\b|\bssa\b|反编译视图", 3.0, "binary-ninja-mcp-analysis"),
        (r"buffer.?overflow|format.?string|\bheap\b|\bstack\b|\brop\b|ret2|shellcode|\bpwn\b", 3.0, "binary-ninja-mcp-analysis"),
        (r"symbolic|符号执行|angr|\bz3\b|约束求解|补丁分析", 2.5, "binary-ninja-mcp-analysis"),
        # 侦察 / OSINT
        (r"\bnmap\b|端口扫描|服务枚举|service.?enum|greppable|scan\.xml", 3.0, "nmap-parse"),
        (r"osint|开源情报|被动侦察|被动信息|子域枚举|\bdns\b", 3.0, "osint-recon"),
        (r"shodan|暴露面|internet.?facing|外部资产|联网设备", 3.0, "shodan"),
        (r"source.?research|资料检索|第一手来源|引用出处", 2.0, "source-research"),
        # 基础设施 / 横向
        (r"\bssh\b|隧道|\btunnel\b|pivot|内网|横向移动|代理转发|socks", 3.0, "ssh-ops"),
        (r"proxychains|socks5|动态转发", 2.0, "proxychains-tunnel"),
        (r"firewall|nftables|iptables|白名单|放行来源|allow.?source", 3.0, "nftables-allow-source"),
        (r"terraform|\biac\b|基础设施即代码|cloudformation|arm.?template", 3.0, "iac-attack-surface"),
        # AD / 云身份图
        (r"active.?directory|kerberos|域控|domain.?controller|\badcs\b|kerberoast|asrep|"
         r"委派|delegation|golden.?ticket|域信任|\bldap\b|dcsync|\brbcd\b", 3.0, "bloodhound-ad-analysis"),
        (r"bloodhound|cypher|攻击路径|attack.?path|tier.?zero|最短路径|shortest.?path", 3.0, "bloodhound-analysis"),
        (r"azurehound|\bazure\b|entra", 3.0, "azurehound-analysis"),
        (r"opengraph|openhound|graph.?schema", 3.0, "bloodhound-opengraph"),
        (r"\bokta\b", 3.0, "openhound-okta"),
        (r"\bjamf\b|macos.?mdm", 3.0, "openhound-jamf"),
        (r"github.*(?:资产|身份|仓库)|github.?hound", 2.5, "openhound-github"),
        # SCCM
        (r"\bsccm\b|\bmecm\b|configuration.?manager|sccmhunter", 3.5, "sccm-recon"),
        (r"sccm.*(?:takeover|relay|接管|中继)", 3.5, "sccm-takeover-relay"),
        # C2 / 载荷
        (r"mythic|implant|植入体|payload.?type|listener.?profile", 3.0, "mythic-implant-development"),
        (r"cobalt.?strike|aggressor|beacon|malleable", 3.0, "cobalt-strike-aggressor-development"),
        (r"\bbof\b|beacon.?object|object.?file", 3.0, "beacon-object-file-development"),
        (r"outflank|\boc2\b", 3.0, "oc2-bof-script-development"),
        (r"electron|squirrel|\basar\b|桌面应用打包", 3.0, "electron-app-audit"),
        # 系统 tradecraft
        (r"windows|win32|com.?hijack|注册表劫持", 2.5, "com-proxy-triage"),
        (r"macos|\bosx\b|launchd|\btcc\b", 2.5, "macos-initial-access"),
        (r"linux.*(?:注入|injection|提权|持久化)|进程注入|\bptrace\b", 2.5, "linux-process-injection"),
        # 社工 / 报告 / 时间线 / 观测
        (r"phish|钓鱼|诱饵|话术|pretext|社工", 3.0, "phishing-pretext"),
        (r"vishing|语音社工|电话社工", 3.0, "vishing-pretext"),
        (r"报告|\breport\b|\bfinding\b|交付物|修复建议|remediation", 3.0, "finding-report"),
        (r"ghostwriter|oplog|操作日志", 3.0, "ghostwriter-oplog"),
        (r"timeline|时间线|时间轴|复盘", 3.0, "timeline-workflow"),
        (r"\botel\b|opentelemetry|telemetry|遥测", 3.0, "opentelemetry-codex"),
        (r"ludus|靶场编排|cyber.?range", 3.0, "ludus-development"),
    ]

    def __init__(self, skills_dir: str = None):
        # 默认目录由 `redpilot.contracts.paths.skills_root` 定位（env → /app/skills →
        # 从本文件上溯）。此前是 `dirname(dirname(__file__))/skills`：那在朋友的
        # 目录布局里对，搬进 redpilot/worker/adapter/ 之后指向不存在的路径
        # —— 扫描退化成 0 个技能且只打一条 warning。
        from redpilot.contracts.paths import skills_root
        self._dir = skills_dir or skills_root(__file__, extra="/app/skills")
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
        """按需加载 skill 全文"""
        meta = self._skills.get(name)
        if meta is None:
            return None
        try:
            with open(meta.path, "r", encoding="utf-8") as f:
                text = f.read()
            _, body = _parse_frontmatter(text)
            return body
        except Exception as e:
            log.warning("failed to load skill body %s: %s", name, e)
            return None

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

        # 3. 描述与目标的 bigram 交集
        obj_bigrams = set()
        for i in range(len(objective or "") - 1):
            obj_bigrams.add((objective or "")[i:i+2].lower())
        for name, meta in self._skills.items():
            desc_bigrams = set()
            for i in range(len(meta.description) - 1):
                desc_bigrams.add(meta.description[i:i+2].lower())
            overlap = len(obj_bigrams & desc_bigrams)
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
