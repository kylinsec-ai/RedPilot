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
        # reverse: 纯静态逆向 / 自定义 VM / 字节码解释器（比 pwn 更匹配 .elf 逆向题）
        (r"revers|逆向|disassembl|反汇编|bytecode|字节码|virtual.?machine|虚拟机|"
         r"vm\b|deobfuscat|反混淆|unpack|脱壳|crackme|校验器|凭据输出|内部执行机制", 3.0, "reverse"),
        (r"\.elf$|\.bin$|\.exe$", 2.0, "reverse"),
        (r"buffer.?overflow|format.?string|heap|stack|rop|ret2|shellcode|pwn", 3.0, "pwn"),
        (r"rsa|aes|des|cipher|encrypt|decrypt|hash|md5|sha|crypto", 3.0, "crypto"),
        (r"lateral|pivot|内网|横向|提权|privilege.?escal|credential|渗透", 3.0, "pentest"),
        (r"forensic|memory.?dump|内存镜像|内存分析|镜像取证|volatility|carv|stego|隐写|取证|流量", 3.0, "forensics"),
        (r"aws|s3|iam|cloud|容器逃逸|metadata|云", 3.0, "cloud"),
        (r"bypass|evas|waf|antivirus|obfuscat|检测|规避|对抗", 3.0, "evasion"),
        # java-exploit: Java 生态应用利用（4.0 压过 web，Java 特征必进 top-2）
        (r"java.?反序列化|反序列化.{0,4}java", 3.5, "java-exploit"),
        (r"fastjson|shiro|rememberme|log4j|jndi|autotype|templateimpl|actuator|heapdump|jolokia|spel|weblogic|jackson|struts|spring|commons.?collections|gadget|ysoserial|cc\d|hessian|内存马", 4.0, "java-exploit"),
        # web-deep: Web 利用深度（指纹/绕过矩阵/原语放大）
        (r"sql.?inject|注入|盲注|union|load_file|outfile|xp_cmdshell|gopher|tamper|waf|上传|upload|后缀|bypass|命令注入|命令执行|file.?upload|xxe|xml|ssti|lfi|rfi", 2.5, "web-deep"),
        # web-attack: 进阶 Web 攻击
        (r"cors|jwt|graphql|prototype.?pollution|race.?condition|条件竞争|cache.?poison|request.?smuggl|host.?header|websocket|subdomain.?takeover|idor|限流|rate.?limit", 3.0, "web-attack"),
        # post-exploit: 后渗透
        (r"post.?exploit|提权|privilege.?escal|lateral|横向移动|container.?escape|容器逃逸|credential.?harvest|凭证|persist|持久化|kerberoast|pass.?the.?hash|DCSync|IMDS|metadata", 3.0, "post-exploit"),
        # ad: Active Directory 域渗透
        (r"active.?directory|kerberos|域控|domain.?controller|bloodhound|ADCS|kerberoast|ASREP|委派|delegation|golden.?ticket|silver.?ticket|域信任|domain.?trust|LDAP|SMB.*域|gpp.?password|cpassword|Shadow.?Credential|RBCD", 3.0, "ad"),
        # cicd: CI/CD 管线攻击
        (r"github.?actions|jenkins|gitlab.?ci|pipeline|workflow|CI.?CD|supply.?chain|artifact|runner|OIDC.?token|workflow.?inject|script.?console|cicd", 3.0, "cicd"),
        # llm: LLM/AI 安全
        (r"prompt.?inject|jailbreak|LLM|大模型|system.?prompt|越狱|chatbot|RAG|excessive.?agency|模型.*安全|AI.*安全|DAN.?mode|tool.?calling", 3.0, "llm"),
        # ebpf: eBPF 内核级攻击
        (r"ebpf|bpf|kprobe|tracepoint|XDP|内核.*后门|rootkit|bpftrace|bpftool|CAP_BPF|内核.*凭证|SSL_read.*hook|网络.*拦截.*内核", 3.0, "ebpf"),
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
