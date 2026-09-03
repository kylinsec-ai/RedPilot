"""
事实图谱 (Blackboard)
带来源标注的事实存储与查询。

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
from dataclasses import dataclass
from typing import Optional

from .solver.base import extract_flags

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


# 事实提取正则（flag 提取复用 solver.base.extract_flags）
_IP_PORT_RX = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):?(\d{1,5})?\b")
_SERVICE_RX = re.compile(r"\b(http|ssh|ftp|mysql|redis|smtp|dns|smb|rdp|vnc|mssql|postgresql)\b", re.I)
_CRED_RX = re.compile(r"(?:user|login|admin|root|password|passwd|pwd|pass)\s*[:=]\s*\S+", re.I)


# 事实落盘节流窗口（秒）：高频 add 期间只记脏标记，窗口过后才整写一次
_SAVE_INTERVAL = 5.0


class Blackboard:
    """事实图谱存储"""

    def __init__(self, persist_path: Optional[str] = None):
        self.persist_path = persist_path
        self.facts: list[Fact] = []
        self.objective: str = ""
        self._seen: set = set()
        self._dirty = False
        self._last_save = 0.0

        if persist_path and os.path.isfile(persist_path):
            self._load()

    def _load(self):
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.facts = [Fact.from_dict(d) for d in data.get("facts", [])]
            self._seen = {f"{f.kind}:{f.content}" for f in self.facts}
        except Exception as e:
            log.warning("blackboard load failed: %s", e)

    def _save(self, *, force: bool = False):
        """落盘事实：节流窗口内仅置脏标记，到期或 flush() 才真正整写"""
        if not self.persist_path or not self._dirty:
            return
        if not force and time.monotonic() - self._last_save < _SAVE_INTERVAL:
            return
        self._dirty = False
        self._last_save = time.monotonic()
        try:
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
            with open(self.persist_path, "w", encoding="utf-8") as f:
                json.dump({"facts": [fa.to_dict() for fa in self.facts]}, f,
                          ensure_ascii=False, indent=2)
        except Exception as e:
            log.warning("blackboard save failed: %s", e)

    def flush(self):
        """强制落盘（访问结束/进程退出前调用，防丢节流窗口内的事实）"""
        self._save(force=True)

    def add(self, fact: Fact) -> bool:
        """添加事实 (去重)"""
        key = f"{fact.kind}:{fact.content}"
        if key in self._seen:
            return False
        self._seen.add(key)
        fact.timestamp = time.time()
        self.facts.append(fact)
        self._dirty = True
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
            if self.add(Fact(kind="credential", content=cred, source=source,
                             confidence=0.6, iter=iter)):
                added += 1

        # Flag 候选
        for flag in extract_flags(output):
            if self.add(Fact(kind="flag", content=flag, source=source,
                             confidence=0.9, iter=iter)):
                added += 1

        return added

    def query(self, kind: Optional[str] = None) -> list[Fact]:
        """查询事实"""
        if kind is None:
            return list(self.facts)
        return [f for f in self.facts if f.kind == kind]

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
