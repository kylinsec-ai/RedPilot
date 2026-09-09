"""
事实图谱 (Blackboard)
带来源标注的事实存储与查询，支持 ATT&CK 目标链追踪。

每条事实包含:
- kind: 类别 (recon/credential/vuln/foothold/flag/network/service)
- content: 内容
- source: 来源命令
- confidence: 置信度
- iter: 所属会话轮次
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .solver.base import is_valid_flag

log = logging.getLogger("adapter.blackboard")


@dataclass
class Fact:
    """单条事实"""
    kind: str
    content: str
    source: str = ""
    confidence: float = 0.5
    iter: int = 0
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "content": self.content,
            "source": self.source,
            "confidence": self.confidence,
            "iter": self.iter,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Fact":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Goal:
    """目标链节点"""
    id: str
    description: str
    satisfied: bool = False


# 默认目标链
_DEFAULT_GOALS = [
    Goal("recon", "侦察: 发现目标服务、端口和技术栈"),
    Goal("vuln", "漏洞: 识别可利用的安全缺陷"),
    Goal("foothold", "立足: 获得目标系统的初始访问"),
    Goal("escalate", "提权: 提升权限或横向移动"),
    Goal("flag", "获取: 找到并提取 flag"),
]


def goals_for_category(category: str = "") -> list[Goal]:
    """根据类别返回适合的目标链"""
    cat = (category or "").lower()
    if cat in ("crypto", "misc", "forensics", "reverse"):
        return [
            Goal("analyze", "分析: 理解题目结构和加密/编码方式"),
            Goal("solve", "求解: 实施解题算法或逆向"),
            Goal("flag", "获取: 提取 flag"),
        ]
    if cat == "pentest":
        return [
            Goal("recon", "侦察: 端口扫描和服务枚举"),
            Goal("vuln", "漏洞: 识别攻击面"),
            Goal("foothold", "入口: 获得初始 shell"),
            Goal("credential", "凭证: 获取有效凭据"),
            Goal("lateral", "横向: 移动到其他主机"),
            Goal("escalate", "提权: 获取 root/admin"),
            Goal("flag", "获取: 提取所有 flag"),
        ]
    return [Goal(g.id, g.description) for g in _DEFAULT_GOALS]


# 事实提取正则
_IP_PORT_RX = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):?(\d{1,5})?\b")
_SERVICE_RX = re.compile(r"\b(http|ssh|ftp|mysql|redis|smtp|dns|smb|rdp|vnc|mssql|postgresql)\b", re.I)
# 凭证线索（B14）：旧正则 \S+ 把 HTML 表单字段（login ==）、SQLi payload
# （password='）、状态码（admin: 500）统统当凭证——实测 c-05 攒了 61 条
# credential 事实，注入下一场的却是 "login ==; pass ==; admin ==; login: 500;
# admin: 500"（actionable_assets 只取前 5 条，噪音把信号挤没）。
# 收紧：值必须是 3~64 位的引号串或字母数字/常见符号串；分隔符只用空格/制表符
# （\s 含换行，会把 "root:\ncss" 拼成一条凭证）；键前 (?<![A-Za-z0-9]) 挡掉
# bypass:/username: 这类词内误匹配，但放行 my_password= 这种下划线前缀键。
_CRED_RX = re.compile(
    r"(?<![A-Za-z0-9])(?:user|login|admin|root|password|passwd|pwd|pass)[ \t]*[:=][ \t]*"
    r"(?:\"([^\"\n]{3,64})\"|'([^'\n]{3,64})'|([A-Za-z0-9!@#$%^&*_\-\.]{3,64}))",
    re.I,
)
# 整条事实必须「键=值」到底（B14 复核）：_CRED_RX 是前缀匹配，旧数据里
# "passwd: HTTPConnectionPool(host='10.0.190.241'," 会取到 HTTPConnectionPool
# 而通过判定。净化存量/收新事实时都要求全串匹配，尾部残留（括号/引号/换行）
# 即判噪音。允许 markdown 加粗的尾部 **。
_CRED_FULL_RX = re.compile(
    r"[ \t]*(?<![A-Za-z0-9])(?:user|login|admin|root|password|passwd|pwd|pass)"
    r"[ \t]*[:=][ \t]*"
    r"(?:\"([^\"\n]{3,64})\"|'([^'\n]{3,64})'|([A-Za-z0-9!@#$%^&*_\-\.]{3,64}))"
    r"[ \t]*\**[ \t]*$",
    re.I,
)
# 占位符 / 常见爆破 payload：作为「已发现凭证」注入只会误导下一场
_CRED_PLACEHOLDERS = {
    "test", "testing", "tests", "guest", "zzz", "wrongpass", "example",
    "changeme", "foo", "bar", "baz", "asdf", "qwerty", "123456", "letmein",
    "hello", "world", "password", "passwd", "pwd", "pass", "secret",
    "placeholder", "yourpassword", "your_password", "xxx", "abcd", "abcd1234",
    "none", "null", "undefined", "todo", "tbd", "string", "value", "demo",
    "sample", "flag", "admin", "root", "user", "login",
}


def _cred_value_ok(val: str, quoted: bool) -> bool:
    """值是否像真凭证（B14 复核）。

    实测漏网噪音值：css / final / enable_queue / HTTPConnectionPool / zzzzz。
    规则：占位符与爆破 payload 丢弃；单字符重复（zzzzz/11111）丢弃；至少含
    一个字母（状态码/端口丢弃）；未加引号时还要求含数字或符号，或长度 ≥ 12
    ——纯字母短词在本项目数据里全是页面文本（css/final/enable），而真凭证
    admin:Admin@123、admin:hunter2xyz、john.doe 都含数字或符号。
    """
    v = (val or "").strip()
    if not v or v.lower() in _CRED_PLACEHOLDERS:
        return False
    if len(set(v)) <= 2:
        return False
    if not any(ch.isalpha() for ch in v):
        return False
    if quoted:
        return True
    return any(not ch.isalpha() for ch in v) or len(v) >= 12


def _cred_quality_ok(content: str) -> bool:
    """凭证事实质量判定（B14）：能否作为「已发现凭证」注入下一场。

    旧正则在 HTML 表单字段 / SQLi payload / 状态码上攒了大量垃圾，注入时
    只取前 5 条 → 真信号被挤没。要求：整条事实全串匹配「键=值」、值通过
    _cred_value_ok()。
    """
    m = _CRED_FULL_RX.match(content or "")
    if not m:
        return False
    quoted = bool(m.group(1) or m.group(2))
    val = next((g for g in m.groups() if g), "")
    return _cred_value_ok(val, quoted)


_FLAG_RX = re.compile(r"flag\{[^}]{1,200}\}", re.I)


class Blackboard:
    """事实图谱存储"""

    def __init__(self, persist_path: Optional[str] = None):
        self.persist_path = persist_path
        self.facts: list[Fact] = []
        self.goals: list[Goal] = []
        self.objective: str = ""
        self._seen: set = set()

        if persist_path and os.path.isfile(persist_path):
            self._load()

    def _load(self):
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.facts = [Fact.from_dict(d) for d in data.get("facts", [])]
            # B14：净化存量噪音凭证（旧正则攒下的表单字段/爆破 payload）。
            # 放在 _load 而非改数据文件：运行中的 driver 持有内存版 board，
            # add() 会把内存整体写回；加载时净化则随重载自然生效，无竞态。
            kept = [f for f in self.facts
                    if f.kind != "credential" or _cred_quality_ok(f.content)]
            if len(kept) != len(self.facts):
                log.info("blackboard: 剔除 %d 条噪音凭证事实（B14）",
                         len(self.facts) - len(kept))
                self.facts = kept
                self._save()
            self._seen = {f"{f.kind}:{f.content}" for f in self.facts}
        except Exception as e:
            log.warning("blackboard load failed: %s", e)

    def _save(self):
        if not self.persist_path:
            return
        try:
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
            with open(self.persist_path, "w", encoding="utf-8") as f:
                json.dump({"facts": [fa.to_dict() for fa in self.facts]}, f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("blackboard save failed: %s", e)

    def seed_goals(self, goals: list[Goal]):
        """设置目标链"""
        self.goals = goals

    def add(self, fact: Fact) -> bool:
        """添加事实 (去重)"""
        key = f"{fact.kind}:{fact.content}"
        if key in self._seen:
            return False
        self._seen.add(key)
        fact.timestamp = time.time()
        self.facts.append(fact)
        self._save()
        return True

    def observe(self, tool: str, args: dict, output: str, *, iter: int = 0) -> int:
        """
        从工具调用中自动抽取事实。

        返回新增事实数量。
        """
        if not output or len(output.strip()) < 3:
            return 0

        added = 0
        cmd = str(args.get("command", "")) if isinstance(args, dict) else str(args)
        source = f"{tool}: {cmd[:100]}"

        # IP + 端口
        for m in _IP_PORT_RX.finditer(output[:2000]):
            ip = m.group(1)
            port = m.group(2) or ""
            content = f"{ip}:{port}" if port else ip
            if self.add(Fact(kind="network", content=content, source=source,
                             confidence=0.8, iter=iter)):
                added += 1

        # 服务发现
        for m in _SERVICE_RX.finditer(output[:2000]):
            svc = m.group(1).lower()
            if self.add(Fact(kind="service", content=svc, source=source,
                             confidence=0.7, iter=iter)):
                added += 1

        # 凭证线索
        for m in _CRED_RX.finditer(output[:3000]):
            cred = m.group(0).strip()[:120]
            if not _cred_quality_ok(cred):
                continue                      # 占位符/爆破 payload/纯数字
            if self.add(Fact(kind="credential", content=cred, source=source,
                             confidence=0.6, iter=iter)):
                added += 1

        # Flag 候选
        for m in _FLAG_RX.finditer(output):
            if is_valid_flag(m.group(0)):
                if self.add(Fact(kind="flag", content=m.group(0), source=source,
                                 confidence=0.9, iter=iter)):
                    added += 1

        return added

    def query(self, kind: Optional[str] = None) -> list[Fact]:
        """查询事实"""
        if kind is None:
            return list(self.facts)
        return [f for f in self.facts if f.kind == kind]

    def next_open_goal(self) -> Optional[Goal]:
        """返回下一个未完成的目标"""
        for g in self.goals:
            if not g.satisfied:
                return g
        return None

    def actionable_assets(self) -> str:
        """生成可操作资产摘要"""
        parts = []
        creds = self.query("credential")
        if creds:
            parts.append("已发现凭证: " + "; ".join(f.content for f in creds[:5]))
        nets = self.query("network")
        if nets:
            parts.append("已发现网络: " + ", ".join(f.content for f in nets[:10]))
        svcs = self.query("service")
        if svcs:
            parts.append("已发现服务: " + ", ".join(f.content for f in svcs[:10]))
        footholds = self.query("foothold")
        if footholds:
            parts.append("已获立足点: " + "; ".join(f.content for f in footholds[:3]))
        return "\n".join(parts)

    def summary(self) -> str:
        """生成图谱摘要"""
        counts = {}
        for f in self.facts:
            counts[f.kind] = counts.get(f.kind, 0) + 1
        return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
