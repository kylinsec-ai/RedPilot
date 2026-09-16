"""Heimdall —— 观察者 Agent：读思路、画图、**不攻击**。

另一路 Agent。它不执行任何攻击动作，只读主 Agent 的思考与工具结果，把它们
对照成一张 `<heimdall-map>`，经 LLM 旁路注入后续会话的 prompt。

▌它解决什么
主 Agent 在长会话里会陷入几类典型退化：死胡同、习惯性爆破、死路重测、
幻觉循环。同题换一场会话，这些状态**不跟着走** —— 新会话从 MEMORY.md 里
只看到结论，看不到"当时为什么放弃"。Heimdall 把这一层补上。

▌四条红线（改这个文件前先读）
1. **只举镜子，不发指令**。没有 NEXT，没有祈使句。图上任何一条都可能错，
   主 Agent 有权推翻 —— 提示词里必须写明这一点。
2. **DEAD/LOCK 可撤销**。误标的死路会让人永远错失解题方向，比不标更糟。
   每条都必须带依据（依据是它可被推翻的唯一凭据）；新证据可撤销旧结论，
   撤销要在下一场的图上**显式可见**（"曾判死、已撤销"本身就是信息）。
3. **TENSION 只并置，不裁决**。主 Agent 自己两句互相打架的话摆在一起，
   附 sha1 指纹，由主 Agent 决定废哪一句。观察者不替它选。
4. **失败绝不外溢**。观察者超时/报错/输出不可解析 → 一律静默降级为「本场
   没有图」，绝不影响解题主流程。它是旁观者，不是关卡。

▌与黑板的区别
`adapter/blackboard.py` 是**机械**抽取（正则扫工具输出，抓凭据/端点），
driver 自己跑，不占主 Agent 注意力。Heimdall 是**语义**层 —— 机械抽取器
做不了"这两句话互相矛盾"或"这一类打法已经证死了"。

开关：`ADAPTER_HEIMDALL=1`（默认关）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Optional

log = logging.getLogger("adapter.heimdall")

# ── 预算 ──────────────────────────────────────────────────────
_DIGEST_MAX = 12000        # 喂给观察者的转录摘要上限（字符）
_MAP_MAX = 1800            # 注入主 Agent 的图上限（字符）—— 镜像不该抢占注意力
_KEEP_TENSION = 3          # 最多并置几组矛盾
_KEEP_PER_KIND = 6         # LOCK/DEAD/ANGLES 各自显示上限
_STALE_SESSIONS = 6        # 连续这么多场未被重新确认的节点自动退场（防旧图僵化）

_SYS = """你是一面镜子。你观察一个自主安全测试 Agent 的工作过程，把它的状态
反射回去。你不给建议、不下命令、不指方向。

▌绝对禁止
- 祈使句（"应该…" / "试试…" / "建议…" / "下一步…"）。你没有"下一步"这个字段。
- 任何形式的行动指令。你只描述你**看到**的东西。
- 编造。图上每一条都必须能在下面给你的材料里找到出处。

▌要产出五类观察

LOCK —— 这题在考什么、难在哪里、flag 大概率在哪。
        写你的**推断**，并给出推断所依据的具体观察（哪条命令的输出、哪个响应的形状）。
        这是推断，不是事实，措辞上要体现（例如"响应里同时出现 X 与 Y，像是……"）。

DEAD —— 当时失败过的**一整类**做法，不是单条命令。
        "试过 XX 爆破，三次都是统一 404，且服务端日志无记录" 是一类；
        "curl 那条命令" 不是。必须带依据，因为被误标的一类会让人永远不再碰它。

ANGLES —— 还没认真打过的突破类。
        只写**方向**，不写怎么打。判据是"材料里找不到真的试过它的痕迹"。
        注意：材料是**截断过的**，省略处有标注。对可能落在省略区里的东西，
        宁可不下判断 —— "没看见"不等于"没试过"，把后者写成 ANGLES 会让主
        Agent 以为那是块处女地。

TENSION —— 主 Agent 自己两句互相打架的话。原文引用，不要转述。
        只并置，不裁决。由它自己决定废弃哪一句。

POSTPONE —— 材料里出现过、但明显是耗时工程的动作（大字典爆破、全端口慢扫、
        大规模枚举）。标明它，不是禁止它 —— 只是让主 Agent 知道这事的开销量级。

▌撤销
如果你上一轮画出的图上某条，被这一轮的新材料**证伪**了（例如某类打法其实
打出过有效响应、某条推断与新的输出矛盾），把它写进 retract，并说明是哪条
新材料推翻的。宁可撤销，也不要让一条错的死路一直挂着。

▌输出
只回一个 JSON 对象，不要别的话：
{
  "lock":     [{"claim": "...", "evidence": "..."}],
  "dead":     [{"class": "...", "evidence": "..."}],
  "angles":   [{"class": "..."}],
  "tension":  [{"a": "原文一", "b": "原文二"}],
  "postpone": [{"op": "...", "why": "..."}],
  "retract":  [{"id": "上一轮图上的节点 id", "why": "被哪条新材料推翻"}]
}
没有内容的类别给空数组。宁可少写：图上多一条错的，比少一条对的更糟。"""

# 注入主 Agent 的图的固定头 —— 红线 1 的落点：必须写明"可推翻"
_HEAD = (
    "<heimdall-map 场次={session}>\n"
    "这是对你自己前几场行为的一面镜子，**不是指令**。它是推断，会错。\n"
    "每一项都附了它的依据，你可以据此判断可信度，也可以直接推翻其中任何一条。\n"
)
_TAIL = "</heimdall-map>"

_KIND_LABEL = {
    "lock": "LOCK 这题在考什么 / 难在哪 / flag 大概率在哪",
    "dead": "DEAD 当时失败过的一整类",
    "angles": "ANGLES 还没认真打过的突破类",
    "postpone": "POSTPONE 开销较大的动作（不是禁止）",
}

# pi 对 message_update 用了**带空格**的序列化，其余事件是紧凑的。
# type 永远是行首第一个键，故只查前 64 字符：既快，又不会误伤正文里的同名字符串。
_HEAD_CHARS = 64


def _is_stream_delta(line: str) -> bool:
    return "message_update" in line[:_HEAD_CHARS]


# ══════════════════════════════════════════════════════════════
# 转录 → 摘要
# ══════════════════════════════════════════════════════════════
def _text_of(result) -> str:
    """tool_execution_end.result 是 dict，正文埋在 content[].text 里。"""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        parts = []
        for blk in (result.get("content") or []):
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text") or ""))
        return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)
    return str(result)


def _tail_fit(chunks, budget: int, *, sep: str = "\n---\n") -> tuple:
    """从**最近**往前塞，塞不下就丢最旧的。返回 (文本, 丢了几条)。

    为什么不整块 `blob[-N:]`：那样会把段头一起切掉，观察者拿到的是一坨
    无结构文本 —— 分不清哪句是主 Agent 的**思考**、哪段是**工具输出**，
    而 TENSION 要引"它自己的话"、DEAD 要"命令 + 结果"配对，全靠这个区分。
    """
    kept, used = [], 0
    for c in reversed(chunks):
        if kept and used + len(c) + len(sep) > budget:
            break
        kept.append(c)
        used += len(c) + len(sep)
    kept.reverse()
    return sep.join(kept), len(chunks) - len(kept)


def transcript_digest(path: str, *, max_chars: int = _DIGEST_MAX) -> str:
    """从 pi 会话转录里抽出「思考 + 工具 + 结果」，压成给观察者的材料。

    跳过 message_update（流式增量，占全文件 99%），只留 message_end 的
    思考/文本块与工具执行的两端。返回空串表示无可观察内容。

    预算按段分（思考 ~45% / 工具 ~55%），每段各自从**尾部**保留最近的，
    并**保留段头**。有省略时显式标注省略了多少条 —— 否则观察者会把"被截断
    没看见"误判成"没试过"，进而产出一条错的 ANGLES（红线 2 那类误标的来源）。
    """
    think, calls, outs = [], {}, {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or _is_stream_delta(line):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                t = ev.get("type")
                if t == "message_end":
                    m = ev.get("message") or {}
                    if m.get("role") != "assistant":
                        continue
                    for blk in (m.get("content") or []):
                        if not isinstance(blk, dict):
                            continue
                        if blk.get("type") == "thinking":
                            s = str(blk.get("thinking") or "").strip()
                            if s:
                                think.append(s)
                        elif blk.get("type") == "toolCall":
                            args = blk.get("arguments") or {}
                            calls[blk.get("id")] = str(
                                args.get("command") or args)[:400]
                elif t == "tool_execution_start":
                    calls[ev.get("toolCallId")] = str(
                        (ev.get("args") or {}).get("command")
                        or ev.get("args"))[:400]
                elif t == "tool_execution_end":
                    outs[ev.get("toolCallId")] = (
                        ("[ERR] " if ev.get("isError") else "")
                        + _text_of(ev.get("result"))[:600])
    except OSError:
        return ""

    if not think and not calls:
        return ""

    parts = []
    think_budget = int(max_chars * 0.45)
    tool_budget = max_chars - think_budget
    if think:
        body, dropped = _tail_fit([x[:1200] for x in think[-40:]], think_budget)
        if dropped:
            body = f"（更早的 {dropped} 段思考已省略）\n" + body
        parts.append("## 主 Agent 的思考（按时间）\n" + body)
    if calls:
        rows = [f"$ {cmd}\n  → {outs.get(cid, '(无输出记录)')}"
                for cid, cmd in list(calls.items())[-40:]]
        body, dropped = _tail_fit(rows, tool_budget, sep="\n")
        if dropped:
            body = f"（更早的 {dropped} 条工具调用已省略）\n" + body
        parts.append("## 工具调用与结果（按时间）\n" + body)
    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════════════
# 状态：节点表 + 撤销
# ══════════════════════════════════════════════════════════════
def _fp(s: str) -> str:
    return hashlib.sha1(str(s).encode("utf-8")).hexdigest()[:8]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _key(kind: str, text: str) -> str:
    """节点 id：由内容决定，所以同一个观察重复出现时是同一个节点（可续期）。"""
    return f"{kind[:1]}{_fp(_norm(text))[:6]}"


def empty_state() -> dict:
    return {"version": 1, "nodes": [], "tensions": [], "session": -1, "updated_at": 0.0}


def load_state(workdir: str) -> dict:
    p = os.path.join(workdir, ".heimdall.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("nodes"), list):
            d.setdefault("tensions", [])
            return d
    except (OSError, ValueError):
        pass
    return empty_state()


def _save_state(workdir: str, st: dict) -> None:
    p = os.path.join(workdir, ".heimdall.json")
    try:
        st["updated_at"] = time.time()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, p)          # 原子写，同 stoploss._save
    except OSError:
        pass


def merge(state: dict, fresh: dict, session: int) -> dict:
    """把观察者的新输出并进状态表。**纯函数**，便于测试。

    并入规则：
      · 同 (kind, 规范化文本) → 同 id → 续期（last_seen 前移、miss 清零）
      · 新出现 → 新节点
      · 观察者显式 retract 的 id → status=retracted，并记 gone_at（本场可见）
      · 旧 active 节点这轮没出现 → 记一次 miss，连续 _STALE_SESSIONS 场未再
        出现才退场（单场漏报不该抹掉一条结论）
    """
    st = json.loads(json.dumps(state))          # 深拷贝，不动入参
    st["session"] = session
    st.setdefault("nodes", [])
    by_id = {n["id"]: n for n in st["nodes"]}
    seen = set()

    src = {
        "lock": [(x.get("claim"), x.get("evidence")) for x in (fresh.get("lock") or [])],
        "dead": [(x.get("class"), x.get("evidence")) for x in (fresh.get("dead") or [])],
        "angles": [(x.get("class"), None) for x in (fresh.get("angles") or [])],
        "postpone": [(x.get("op"), x.get("why")) for x in (fresh.get("postpone") or [])],
    }
    for kind, items in src.items():
        for text, why in items[: _KEEP_PER_KIND * 2]:
            text = str(text or "").strip()
            if not text:
                continue
            nid = _key(kind, text)
            seen.add(nid)
            n = by_id.get(nid)
            if n is None:
                n = {"id": nid, "kind": kind, "text": text[:300],
                     "why": str(why or "")[:400], "status": "active",
                     "first_seen": session, "last_seen": session, "miss": 0}
                st["nodes"].append(n)
                by_id[nid] = n
            else:
                n["text"] = text[:300]
                if why:
                    n["why"] = str(why or "")[:400]
                n["last_seen"] = session
                n["miss"] = 0
                if n["status"] == "retracted":
                    # 撤销过的又出现了 —— 只记录，不复活（避免来回抖）
                    n["status"] = "reaffirmed"
                else:
                    n["status"] = "active"

    # ── 红线 2：显式撤销。观察者对着上一轮的 id 说"这条被证伪了" ──
    for r in (fresh.get("retract") or []):
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "").strip()
        n = by_id.get(rid)
        if n is None or n.get("status") not in ("active", "reaffirmed"):
            continue
        n["status"] = "retracted"
        n["retract_why"] = str(r.get("why") or "")[:400]
        n["gone_at"] = session

    # 本轮没出现的 active 节点：记一次 miss，够次数就退场
    for n in st["nodes"]:
        if n["id"] in seen or n["status"] != "active":
            continue
        n["miss"] = int(n.get("miss", 0)) + 1
        if n["miss"] >= _STALE_SESSIONS:
            n["status"] = "stale"
            n["gone_at"] = session

    # TENSION 单独处理：成对出现，用两句原文的 sha1 定 id（红线 3：附指纹）
    tens = []
    old_t = {t["id"]: t for t in (st.get("tensions") or []) if isinstance(t, dict)}
    for x in (fresh.get("tension") or [])[:_KEEP_TENSION]:
        if not isinstance(x, dict):
            continue
        a, b = str(x.get("a") or "").strip(), str(x.get("b") or "").strip()
        if not a or not b:
            continue
        tid = "t" + _fp(_norm(a) + "|" + _norm(b))[:6]
        t = old_t.get(tid) or {"id": tid, "fp_a": _fp(a), "fp_b": _fp(b),
                               "first_seen": session}
        t["a"], t["b"] = a[:240], b[:240]
        t["last_seen"] = session
        t["status"] = "active"
        tens.append(t)
    # 上一轮的 TENSION 若本轮缺席 → 直接退场（矛盾是即时的，没有续期价值）
    st["tensions"] = tens
    return st


# ══════════════════════════════════════════════════════════════
# 渲染：状态 → 注入主 Agent 的 <heimdall-map>
# ══════════════════════════════════════════════════════════════
def render(state: dict, *, max_chars: int = _MAP_MAX) -> str:
    """状态表 → 注入文本。**纯函数**。没有任何内容时返回空串。"""
    all_nodes = state.get("nodes") or []
    nodes = [n for n in all_nodes if n.get("status") == "active"]
    tens = [t for t in (state.get("tensions") or []) if t.get("status") == "active"]
    # 刚退场的：本场被撤销 / 本场判为陈旧 —— 撤销本身是信息（红线 2）
    gone = [n for n in all_nodes if n.get("gone_at") == state.get("session")]
    if not nodes and not tens and not gone:
        return ""

    lines = [_HEAD.format(session=state.get("session", "?"))]
    for kind in ("lock", "dead", "angles", "postpone"):
        items = [n for n in nodes if n.get("kind") == kind][:_KEEP_PER_KIND]
        if not items:
            continue
        lines.append("\n" + _KIND_LABEL[kind])
        for n in items:
            lines.append(f"  {n['id']} {n['text']}")
            if n.get("why"):
                lines.append(f"      依据：{n['why']}")
    if tens:
        lines.append("\nTENSION 你自己两句互相打架的话（由你决定废哪一句）")
        for t in tens:
            lines.append(f"  {t['id']} “{t['a']}”  ⟷  “{t['b']}”")
            lines.append(f"      （sha1 {t['fp_a']} / {t['fp_b']}）")
    if gone:
        # 「被证伪撤销」和「没再被确认而淡出」是两件事，措辞必须分开 ——
        # 前者是"这条错了"，后者只是"这条没再出现"，可信度不同。
        # 段头也各自按需输出：没内容就不该挂一个空标题。
        retracted = [n for n in gone if n.get("status") == "retracted"]
        if retracted:
            lines.append("\n曾标出但已撤销（不再被当前证据支持）：")
            for n in retracted[:_KEEP_PER_KIND]:
                why = f" —— {n['retract_why']}" if n.get("retract_why") else ""
                lines.append(f"  {n['id']} {n['text']}{why}")
        faded = [n for n in gone if n.get("status") != "retracted"]
        if faded:
            lines.append("\n已淡出（连续多场未再被观察到，不一定是错的）：")
            for n in faded[:_KEEP_PER_KIND]:
                lines.append(f"  {n['id']} {n['text']}")
    lines.append(_TAIL)

    out = "\n".join(lines)
    if len(out) > max_chars:
        # 超预算就砍尾部（保留 LOCK/DEAD/TENSION —— 它们排前面）
        out = out[:max_chars].rsplit("\n", 1)[0] + "\n" + _TAIL
    return out


# ══════════════════════════════════════════════════════════════
# 观察者本体
# ══════════════════════════════════════════════════════════════
class Heimdall:
    """一次观察 = 一次 LLM 旁路调用。失败一律返回 None（红线 4）。"""

    def __init__(self, llm, *, timeout: float = 45.0, min_session: int = 1):
        self.llm = llm
        self.timeout = timeout
        self.min_session = min_session      # 首场没有"前几场"可照，跳过

    def enabled(self, session_idx: int) -> bool:
        return self.llm is not None and session_idx >= self.min_session

    @staticmethod
    def _parse(text: str) -> Optional[dict]:
        m = re.search(r"\{.*\}", text or "", re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except ValueError:
            return None
        if not isinstance(d, dict):
            return None
        return {k: (v if isinstance(v, list) else [])
                for k, v in d.items()
                if k in ("lock", "dead", "angles", "tension", "postpone", "retract")}

    def observe(self, digest: str, prior_map: str, session_idx: int) -> Optional[dict]:
        """读摘要 → 产出观察。任何异常/超时 → None。"""
        if not digest:
            return None
        prompt = ("## 本次要观察的过程材料\n" + digest
                  + ("\n\n## 你上一轮画出的图（供对照；不要求沿用）\n" + prior_map
                     if prior_map else ""))
        box: dict = {}

        def _run():
            try:
                box["r"] = self.llm.chat(
                    [{"role": "system", "content": _SYS},
                     {"role": "user", "content": prompt}],
                    # B53 教训：本网关是推理模型，思维链与正文共享 max_tokens，
                    # 预算给小了正文必空（当时 200 → 6/6 全空）。下面是**本调用点**
                    # 的实测定标，别照搬 verify 侧那个值 —— 裁判吃的是短候选，
                    # 观察者吃的是最长 12000 字符的摘要，输入规模差一个量级：
                    #   取现场最长 digest（11497 字符）跑真实调用，
                    #   4096 → 4/6（1 次思维链吃满、正文为空：tokens 精确 4096）；
                    #   8192 → 6/6，中位 15.9s / 最慢 21.1s，峰值仅用 4396；
                    #   12288 → 同样全中但中位 23.0s（多花 7s 换零收益）。
                    # 注：首次定标曾用一份 2655 字符的 digest，比现场**最小的**
                    # 还小（现场区间 3496~11497），轻负载下结论系统性偏乐观——
                    # 定标样本必须卡在现场负载的分布上。
                    # thinking=False：观察者要做稳定判断，不要思维链；
                    # reasoning_effort 实测有害，别打开。
                    max_tokens=8192, thinking=False)
            except Exception as e:                       # noqa: BLE001
                box["e"] = e

        t = threading.Thread(target=_run, daemon=True, name="heimdall")
        t.start()
        t.join(self.timeout)
        if t.is_alive():
            log.warning("[B61] 观察者超时 (%.0fs) — 本场无图，不影响解题", self.timeout)
            return None
        if "e" in box:
            log.warning("[B61] 观察者调用失败：%s — 本场无图，不影响解题", box["e"])
            return None
        got = self._parse(getattr(box.get("r"), "text", "") or "")
        if got is None:
            log.info("[B61] 观察者未产出可解析的图（场次 %d）", session_idx)
        return got


def review(heimdall: "Heimdall", workdir: str, transcript_path: str,
           session_idx: int) -> str:
    """一次完整的观察：抽摘要 → 调观察者 → 并入状态 → 返回可注入的图。

    这是 driver 唯一需要调用的入口。任何失败都返回 ""（红线 4）。
    """
    try:
        if not heimdall.enabled(session_idx):
            return ""
        st = load_state(workdir)
        prior = render(st)
        digest = transcript_digest(transcript_path)
        if not digest:
            return prior
        fresh = heimdall.observe(digest, prior, session_idx)
        if fresh is None:
            return prior
        st = merge(st, fresh, session_idx)
        _save_state(workdir, st)
        out = render(st)
        if out:
            cnt = lambda k: sum(1 for n in st["nodes"]      # noqa: E731
                                if n["kind"] == k and n["status"] == "active")
            log.info("[B61] 观察者已出图（%s 场次 %d）：LOCK %d / DEAD %d / "
                     "ANGLE %d / TENSION %d", os.path.basename(workdir), session_idx,
                     cnt("lock"), cnt("dead"), cnt("angles"),
                     len(st.get("tensions") or []))
        return out
    except Exception as e:                               # noqa: BLE001
        log.warning("[B61] 观察者异常（已忽略，不影响解题）：%s", e)
        return ""
