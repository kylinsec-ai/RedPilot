# 图工程设计（RedPilot / RedPilot worker）

> 前置：`docs/autonomous-offensive-agent-comparison.md`（谱系）、`docs/top10-offensive-agents-deep-dive.md`
> （源码级对照）。本文件只回答一个问题：**怎么在本仓库做图工程**。
> 结论先行：本仓库已经存在**三块半成品图**，缺的不是"引入图数据库"，而是**把已有的
> 事实、观察、证据出身三者用边连起来，并把消费者从"读列表"改成"读图视图"**。

---

## 0. 什么是「图工程」

图工程不是"用 Neo4j"。它是一组关于**如何让 Agent 的记忆可追溯、可查询、可撤销**的设计纪律。
从本次调研的十个项目里能提炼出九条：

| # | 原则 | 正例 | 反例（本仓库现状） |
|---|---|---|---|
| 1 | **事件日志是真相源，图是派生视图** | PentestAgent：Notes → ShadowGraph → Insights；Heimdall：transcript → nodes | `_blackboard.json` 既是写入目标又是真相源，改提取器就得改历史数据 |
| 2 | **每条节点/边必须带出身（origin）** | CAI 自主等级、PentestGPT canonical vs noncanonical | `Fact` 无出身字段，`confidence=0.6` 是拍脑袋常量 |
| 3 | **每条节点/边必须带证据（evidence）** | PentestGPT `evidence_sequences` 指向 trace 事件 | `Fact.source` 只是命令字符串前 100 字，无法定位到输出行 |
| 4 | **出身不可混淆，消费者权限不同** | `hallucination.py` 已把幻觉族/推导族分开 | 分离只在 `verify.py` 内部，图里混在一起 |
| 5 | **节点 id 内容决定、确定性** | Heimdall `_key(kind, text) = kind[:1]+sha1[:6]` | Blackboard 用 `kind:content` 字符串，尚可但未归一化 |
| 6 | **生命周期显式**（active/retracted/stale/reaffirmed） | Heimdall `merge()` 已有完整生命周期 | Blackboard 只有增，没有撤、没有陈旧 |
| 7 | **面向 prompt 的视图必须有预算、可丢弃、只读** | Heimdall `_MAP_MAX=1800` + `_KEEP_PER_KIND=6` | `actionable_assets()` 无预算概念，只取前 N |
| 8 | **写入在旁路，不占主 Agent 注意力** | Blackboard 机械抽取、Heimdall 语义观察 | 已满足 |
| 9 | **图坏掉不影响解题（失败隔离）** | Heimdall 红线 4、`hallucination.py` 第三安全性质 | 已满足，须保持 |

一句话：**图的难点从来不是存，而是"凭什么说这是真的"，以及"读者有没有资格用它"。**

---

## 1. 现状：三块半成品图 + 一个被浪费的出身分级

### 1.1 `adapter/blackboard.py` —— 机械事实，无边

```
Fact(kind, content, source, confidence, iter, timestamp)
query(kind) / actionable_assets() / summary() / next_open_goal()
```

- `kind ∈ {recon, credential, vuln, foothold, network, service}`；
- 写入只发生在 `Blackboard.observe()`（正则抽取），由 `_observe_qualified_tool_facts()`
  经 `_ProgressEvidenceGate` 过滤后调用；
- **没有边**：`network=10.1.1.5:22` 与 `credential=admin:Admin@123` 之间没有 `AUTH_ACCESS`；
  `foothold` 与它赖以成立的 `vuln` 之间没有 `EXPLOITED_VIA`。
- **没有出身**：`source="Bash: nmap ..."` 无法回答"这是靶场回的包，还是 agent 自己 echo 的"。

### 1.2 `adapter/heimdall.py` —— 语义节点，有生命周期，仍无边

```
Node(id, kind ∈ {lock,dead,angles,postpone}, text, why, status, miss, first/last_seen)
Tension(id, a, b, fp_a, fp_b)
merge() → active / retracted / stale / reaffirmed
```

这是本仓库最接近"图"的模块，但它只连自己：
`DEAD("试过 XX 爆破")` 与 `LOCK("flag 大概率在 /admin")` 之间没有边，
`TENSION(a,b)` 是唯一一种"关系"，且是成对平铺的。**没有跨模块的边**。

### 1.3 `_ProgressEvidenceGate` / `verify.py` —— 出身分级，进不了图

这是最可惜的一块。`_ProgressEvidenceGate.observe()` 已经把每个工具事件分成了三类：

```
"current_target"          远程命令直接交互（活靶场响应）
"current_target_response" 由成功的 curl -o 落盘、以后续读取的响应产物
"task_artifact"           题目自带的本地产物
""                        不合格（失败/空/authored/tainted）
```

而这正是图节点最需要的 `origin`！它现在只用来决定"这次输出能不能喂给黑板 / 能不能强提 flag"，
用完即弃。`hallucination.py` 的幻觉族/推导族之分同理。

### 1.4 由此产生的四个具体病症

| 病症 | 现状位置 | 后果 |
|---|---|---|
| **真答案被接地门拒掉** | `hallucination.py` 顶部：29 条被拒中 19 条实为正确答案 | 推导族（`local_computed_only` / `local_derived`）无法转化成"待验证假设"，直接进未验证账本 |
| **止损判据是命令字符串相似度** | `stoploss.py` `last_commands` 快照 | "换汤不换药"识别不准；同一假设的新 payload 被误判为进展 |
| **阶段提示靠启发式** | `blackboard.next_open_goal()` 用"有 network 事实就算 recon 完成" | 目标链从不真正推进，多段题每场都说还在 recon |
| **跨场记忆只有散文** | `MEMORY.md`（≤4000 字符）+ Heimdall 图 | "为什么放弃"在 Heimdall 里，但"放弃了哪条边、还剩哪些边"无处可查 |

---

## 2. 数据模型：一张证据图

### 2.1 词汇表（**封闭且小**，新增需评审）

**节点 kind（11 个）**

| kind | 含义 | 主要来源 |
|---|---|---|
| `host` | IP / 主机名 | 机械抽取 |
| `endpoint` | scheme://host:port/path 或 host:port | 机械抽取 |
| `service` | 服务名 + 版本 | 机械抽取 |
| `credential` | 主体（用户名 + 密码/哈希/token） | 机械抽取（复用 B14 质量门） |
| `artifact` | 落盘产物（下载/派生/生成） | `_ProgressEvidenceGate` 的 artifact 集合 |
| `vuln` | 弱点（可带 CWE/类型） | 机械 + 语义 |
| `foothold` | 立足点 / 会话 | 机械 + 语义 |
| `flag` | **仅平台确认后** | `_record_confirmed_submission` |
| `hypothesis` | 待验证断言（Heimdall LOCK、推导族候选） | 语义 + verify 推导族 |
| `technique` | 打法类别（一类做法，非单条命令） | 语义（Heimdall DEAD/ANGLES） |
| `goal` | 目标（来自 `goals_for_category`） | 驱动 seed |

**边 kind（12 个）** —— 每条边也是"事实"，同样需要 origin/evidence

| 边 | 语义 | 例 |
|---|---|---|
| `HAS_SERVICE` | host → service | `10.1.1.5` → `http(nginx/1.24)` |
| `EXPOSES` | service → endpoint | `http` → `http://10.1.1.5/admin` |
| `RESOLVES_TO` | hostname → host | `app.internal` → `10.1.1.5` |
| `AUTH_ACCESS` | credential → host/service | `admin:Admin@123` → `10.1.1.5:22` |
| `RECOVERED` | credential → artifact | `admin:...` ← `hash.txt` |
| `VULNERABLE_TO` | endpoint/service → vuln | `/admin` → `CWE-89` |
| `EXPLOITED_VIA` | foothold → vuln | `www-data@…` → `CWE-89` |
| `ON_HOST` | foothold → host | `www-data@…` → `10.1.1.5` |
| `LATERAL_TO` | host → host | `10.1.1.5` → `10.1.1.20` |
| `DERIVED_FROM` | node → node（计算派生） | 解码后的明文 ← 密文 artifact |
| `EVIDENCED_BY` | 任意 → evidence | 每个节点/边的**必填**出边 |
| `CONTRADICTS` | node ↔ node | Heimdall TENSION 两句话 |
| `SATISFIES` | node → goal | `flag` → `goal:flag` |
| `BLOCKED_BY` | technique → node | `爆破` → `WAF` |

> 有意**不设** `TRIED` 为独立边：一次尝试用 `technique` 节点的 `attempts` 计数字段 +
> `EVIDENCED_BY` 表达即可，避免边爆炸。

### 2.2 出身（origin）——**这张表的权限列是本次设计的核心**

```python
class Origin(StrEnum):
    ASSUMED   = "assumed"    # 题目描述直接给定（目标地址、给定凭据）
    OBSERVED  = "observed"   # 出现在合格工具输出里（gate ∈ {current_target, current_target_response, task_artifact}）
    DERIVED   = "derived"    # 由 OBSERVED 计算得出（解码/解密/推导），可复现
    INFERRED  = "inferred"   # LLM 语义推断（Heimdall LOCK、hypothesis）
    AUTHORED  = "authored"   # agent 自己写进命令/文件（echo、ML 猜测）—— 永不是证据
    CONFIRMED = "confirmed"  # 平台判定正确（仅 flag）
```

**权限矩阵**（消费者按 origin 决定能用节点做什么）：

| origin | 可满足 goal | 可作为 flag 证据 | 可重置止损干场 | 可进入 prompt | 可被撤销 |
|---|---|---|---|---|---|
| `ASSUMED` | 否 | 否 | 否 | 是 | 是（题目变更） |
| `OBSERVED` | 是 | **是**（需 remote） | **是** | 是 | 是（实例重建） |
| `DERIVED` | 否 | 间接（须先转为 OBSERVED 复现） | 否 | 是 | 是 |
| `INFERRED` | **否** | 否 | 否 | 是（且必须标"推断"） | 是 |
| `AUTHORED` | 否 | **否**（现行硬规则） | 否 | 是（标"你自己写的"） | 是 |
| `CONFIRMED` | 是 | 是 | 是 | 是 | 否（终态） |

这张表把三处现有逻辑**收敛成一处**：
- `hallucination.py` 的"推导族永不计数" = `DERIVED/INFERRED` 不能重置止损；
- `verify._flag_grounded_in_transcripts` 的"参数先现即否决" = flag 证据边必须 `OBSERVED`；
- `blackboard.next_open_goal` 的"不把推断写成事实" = `INFERRED` 不满足 goal。

### 2.3 证据引用（evidence）——**存 id 不存明文**

```python
@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str      # sha1(scope|transcript_basename|line_no|toolCallId)[:16]
    source_kind: str      # current_target | current_target_response | task_artifact | authored
    session: int
    excerpt_sha1: str     # 摘录的 sha1（用于"同一证据"判定），不存明文
    excerpt_len: int      # 长度，便于预算与去重
```

- `evidence_id` 由**已有**的 trace scope 生成（`_transcript_evidence_signature` 同一作用域口径），
  因此**跨实例不可伪造**——与 `_ProgressEvidenceGate` "不持久化、新实例新 gate" 同一条不变量。
- **持久化文件里不写明文**：flag 明文、命令输出、凭据值都不进 `.graph.jsonl`。
  需要渲染给主 Agent 时，用 `evidence_id` **回读** transcript（transcript 本身已有访问控制：
  `OBSERVABILITY_READ_TOKEN` 才可读）。这是 `compliance.py` 口径的延续，不新增第二套打码。
- 凭据值特殊处理：`credential` 节点的 `content` 需要注入 prompt（否则没用），
  沿用 `blackboard.py` B14 的做法——**存值但过质量门**，并在 `_purge_plaintext_artifacts` 时随题清掉。

---

## 3. 存储与生命周期

### 3.1 两个文件：事件日志（真相）+ 材质化视图（缓存）

```
<workdir>/.graph.jsonl    追加式 delta 日志（真相源；只 append，永不改写）
<workdir>/.graph.json     材质化视图（可从 .jsonl 完整重建；读方用）
```

- `.graph.jsonl` 每行一个 `GraphDelta`：`{op, node|edge, origin, evidence, session, ts}`；
- `.graph.json` 是 `replay(deltas)` 的结果，`os.replace` 原子写，与 `stoploss._save`、
  Heimdall `_save_state` 同款；
- **单写入者**：driver 进程内串行写（加 `threading.Lock`）；编排层已有 `_advisory_lock`
  处理跨进程题目租约，图不跨 worker 共享（per-workdir）；
- **可重放**：`.graph.json` 损坏 → 从 `.jsonl` 重建；`.jsonl` 是 append-only，
  损坏只可能丢尾行。

### 3.2 delta 操作（幂等）

```python
@dataclass(frozen=True)
class GraphDelta:
    op: Literal["upsert_node", "upsert_edge", "retract_node", "retract_edge", "touch"]
    node_id: str
    kind: str
    origin: str
    evidence: EvidenceRef | None      # upsert 必填；retract 填撤销依据
    attrs: dict                       # 类型专属（port/version/cwe/attempts…）
    session: int
    ts: float
```

- `node_id` **内容决定**：`f"{kind[:1]}{sha1(normalize(key_text))[:10]}"`
  （沿用 Heimdall `_key` 思路，长度从 6 提到 10 以降碰撞）；
- 同 id 再 upsert = 续期（`last_seen` 前移、`miss=0`），origin **只升不降**
  （`INFERRED → DERIVED → OBSERVED → CONFIRMED`；反向需显式 `retract`），
  这条防"先推断、后被观测证实又被降级"的反复抖动；
- 同 id 不同 evidence = 保留多条 `EVIDENCED_BY` 边（证据可累加）。

### 3.3 生命周期（复用 Heimdall 的成熟语义）

| 状态 | 触发 | 消费者行为 |
|---|---|---|
| `active` | 本场被 upsert / 再出现 | 正常参与 |
| `reaffirmed` | 曾被 retract，又被新证据支持 | 参与，但 prompt 里标"曾撤销后重新确认" |
| `retracted` | 显式撤销（新证据证伪） | 不参与判定；prompt 里显式可见"曾判 X，已撤销" |
| `stale` | 连续 `_STALE_SESSIONS`(6) 场 miss | 不参与判定；prompt 里"已淡出（不一定是错的）" |
| `expired` | 题目实例重建（task epoch 变更） | 硬清除（`_ProgressEvidenceGate` 同款边界） |

**实例边界（最重要的一条）**：`_activate_task_epoch` 换 epoch 时，所有
`origin=OBSERVED` 且证据 scope 属于旧实例的节点必须 `expired`——因为
`_ProgressEvidenceGate` 的整个存在理由就是"新的 target instance 不继承任何 lineage"。
图不能成为绕过这条不变量的后门。`INFERRED`/`technique`（打法类）可以跨 epoch 保留，
因为它们不声称"这台机器上存在什么"。

### 3.4 容量与淘汰

- 硬顶 `ADAPTER_GRAPH_MAX_NODES`（默认 2000）、`ADAPTER_GRAPH_MAX_EDGES`（默认 6000）；
- 淘汰优先级（从先淘汰到后淘汰）：
  `INFERRED + stale` > `AUTHORED + 无入边` > `DERIVED + 无入边` > `OBSERVED + 无入边`；
  `CONFIRMED`、`goal`、`technique` 永不淘汰，`flag`/`credential`/`foothold` 高水位保留；
- 淘汰写 `retract_node(reason="capacity")` 而非删除——日志仍可重放。

---

## 4. 写入面：三个 writer，一个 merge

### 4.1 机械抽取（旁路，不占注意力）—— 升级 `blackboard.observe`

```python
# adapter/graph/extract.py
def observe_mechanical(g, *, tool, args, output, source_kind, evidence_id, session) -> int:
    """在 _ProgressEvidenceGate 已判定合格后调用。"""
```

由 `_observe_qualified_tool_facts()` 改造而来：**gate 判定结果直接映射 origin**：

| gate 返回 | origin |
|---|---|
| `current_target` / `current_target_response` | `OBSERVED` |
| `task_artifact` | `OBSERVED`（本地题）/ `DERIVED`（题目分类决定，复用 `flag_evidence_policy`） |
| `""` | **不写入** |

抽取内容在现有正则（IP/端口/服务/凭据）基础上补两类**边**：
- `host → endpoint`（`EXPOSES` 需要 service 中介，IP:port 直接建 `endpoint` 节点）；
- `credential → host`（仅当同一次输出里二者同时出现且凭据过 B14 质量门 → `AUTH_ACCESS`）。

> 保守原则：机械层**只建它敢建确定的边**。`VULNERABLE_TO`/`EXPLOITED_VIA` 这类需要语义判断的
> 留给语义层。宁可少建边，不可错建边（错边比缺边更毒——它会污染路径搜索）。

### 4.2 语义观察（旁路，一次 LLM 调用）—— Heimdall 变成 writer

Heimdall 现在写 `.heimdall.json`。改为**同时**（或取代）发 delta：

| Heimdall 输出 | 图 delta |
|---|---|
| `lock[].claim` | `upsert_node(kind=hypothesis, origin=INFERRED, evidence=…)` |
| `dead[].class` | `upsert_node(kind=technique, attrs={state:"dead"})` + `BLOCKED_BY`/证据边 |
| `angles[].class` | `upsert_node(kind=technique, attrs={state:"untried"})` |
| `tension{a,b}` | `upsert_edge(CONTRADICTS)`（保留 sha1 指纹） |
| `postpone[].op` | `technique.attrs.cost = "high"` |
| `retract{id}` | `retract_node(id, evidence=…)` |

关键：Heimdall 的 `merge()` 生命周期逻辑**整体保留**，只是从"节点表"变成"图的语义子图"。
`.heimdall.json` 可保留为兼容层，由 `.graph.json` 派生，避免一次性迁移风险。

### 4.3 驱动事件（确定性）—— 唯一能产出 CONFIRMED/ASSUMED 的 writer

| 驱动事件 | delta |
|---|---|
| 题目 start / 描述解析 | `upsert_node(target, origin=ASSUMED)`、`goal` seed、给定凭据 `ASSUMED` |
| 平台提交返回 correct | `upsert_node(flag, origin=CONFIRMED)` + `SATISFIES(goal)` |
| 平台提交返回 duplicate | 标记为已存在（不新增 flag 节点） |
| task epoch 变更 | `expire` 旧 OBSERVED 子图 |
| 题目 solved / purge | 清除明文（凭据值、证据摘录） |

### 4.4 一个 merge 函数（纯函数，可单测）

```python
def merge(view: GraphView, deltas: Iterable[GraphDelta]) -> GraphView:
    """纯函数。同 Heimdall.merge 的定位：全部规则可离线测。"""
```

规则：内容 id 去重 → origin 单调升 → evidence 累加 → 生命周期迁移 →
`CONTRADICTS` 不自动裁决（红线：只并置）→ `INFERRED` 永不被 merge 提升为 `OBSERVED`
（必须是带新 evidence 的独立 upsert）。

---

## 5. 接地集成：把 flag 验证变成一次图查询

这是本次设计**收益最直接**的一处。现有逻辑散在
`_flag_grounded_in_transcripts` / `_remote_grounded` / `flag_confidence` 里，
用"按事件顺序扫 trace 行 + 字符串匹配 + provenance 分类"实现。

改造后，候选 flag 的判定变成：

```python
claim = graph.flag_claim(body)        # 返回该候选的全部 EVIDENCED_BY 边
first = claim.first_evidence          # 按 (session, trace order) 排序
verdict = {
    "grounded_remote": first.origin is OBSERVED and first.source_kind in
                       {"current_target", "current_target_response"},
    "authored":        first.source_kind == "authored",
    "derived_local":   first.origin is DERIVED,
    "ungrounded":      first is None,
}
```

**关键改进（直击 19/29 召回损失）**：
- `authored` / `ungrounded` → 幻觉族，维持现状（拒绝、不计数、不干预）；
- `derived_local`（**有真来源、只是本地算出来的**）→ 不再丢进未验证账本，
  而是生成一个 **`hypothesis` 节点 + 一条待建立的边**：
  `hypothesis("该候选需在当前靶标上复现")`，并把"需要哪种证据"写成 `BLOCKED_BY`
  （例如 `BLOCKED_BY(endpoint=X, need=remote_response)`）。这张图于是能告诉主 Agent
  **"你算出了一个候选，但还没在活靶标上复现它"**——这正是现有系统缺的那句话。
  提交仍走平台判对，但**候选不再被静默掐掉**。

这与 `hallucination.py` 的现有结论完全一致（推导族永不计数、只记不裁决），
只是把"记"从计数器升级成"图上的待办边"。

---

## 6. 消费面：四个消费者，一张预算视图

### 6.1 查询原语（纯函数，全部有单测）

```python
graph.frontier()          -> list[Node]   # OBSERVED 但其 kind 应有的出边还缺的节点（"边界"）
graph.open_goals()        -> list[Goal]   # SATISFIES 入边为空的 goal
graph.open_hypotheses()   -> list[Node]   # INFERRED hypothesis，附 EVIDENCED_BY
graph.dead_techniques()   -> list[Node]   # technique.state == "dead"（附依据）
graph.contradictions()    -> list[Edge]   # CONTRADICTS（只并置）
graph.attack_paths(goal, k=3) -> list[Path]  # Dijkstra + Yen K 短路，边权 = f(origin, blocked, cost)
graph.uncharred(host)     -> list[Node]   # 有 AUTH_ACCESS 但未对该 host 做过访问的 credential
graph.delta(session)      -> DeltaStats   # 本场新增的 OBSERVED 节点/边数（止损用）
graph.render_for_prompt(budget) -> str    # 预算化文本视图
```

### 6.2 消费者一：主 Agent prompt（替代/增强 `actionable_assets` + Heimdall）

`taskprompt.build_task_prompt()` 现在注入顺序是：黑板 `actionable_assets()` → Heimdall map → MEMORY。
改为**一张有预算的图视图**，段落与优先级：

```
<evidence-graph session=N budget=1800>
目标（未满足）: goal:foothold
边界（已观测、可继续）: 10.1.1.5:8080 [endpoint,obs] → 尚未枚举
待验证假设: h3f2a "凭据 admin:*** 可能可登 /admin"（依据: <ev:ab12>）
已证死(可推翻): t9c1d "对 /login 的 XX 爆破"（依据: 3×统一 404）
矛盾(由你裁决): "…" ⟷ "…"（sha1 …）
证据来源分布: observed 42 / derived 7 / inferred 5 / authored 3
</evidence-graph>
```

预算规则沿用 Heimdall：`_MAP_MAX=1800`、每类 `_KEEP_PER_KIND`、超预算砍尾部（保 goal/边界/矛盾）。
**红线 1 保持**：视图里没有祈使句，每条附依据，主 Agent 可推翻。

### 6.3 消费者二：止损（把"干场"从命令相似度升级为图增量）

`stoploss.py` 的 `dry_sessions` 现在依赖 `last_facts`（黑板新增数）。
改为读 `graph.delta(session)`：

- **有进展** = 本场新增 `OBSERVED` 节点或边（不含 `INFERRED`/`AUTHORED`）；
- **假设重复** = 本场 `technique` 节点被再次 upsert 但**没有新增 `EVIDENCED_BY`**
  （比命令字符串相似度准得多）；
- **目标不可达** = 对同一 `endpoint` 连续 N 场无任何新出边。

这一改动**不需要动 stoploss 的状态机骨架**，只换喂给它的信号，风险低、收益明确。

### 6.4 消费者三：规划（对齐 PentestGPT 的 plan 语言）

图是 planner 的天然输入：

```
open_goals + frontier + dead_techniques + attack_paths → LLM planner
  → TaskProposal(kind, target, objective, done_when, basis_ids=图节点 id, depends_on=…)
```

`basis_ids` 直接就是图节点 id——这正好落进 PentestGPT `plan.py` 的 `TaskProposal` 字段，
使"每条决策可追溯到图上证据"从口号变成类型约束。

### 6.5 消费者四：观测（只加计数，不加明文）

`observability.py` 的 `_PASSTHROUGH_KEYS`（手工维护清单）增加：

```
_graph_nodes / _graph_edges / _graph_frontier / _graph_dead / _graph_open_goals
```

前缀 `_` = 带外键，**只为运维可见，不入 18 键 LiveSnapshot、不落库**（与现有约定一致）。
必须同时更新 `test_status_bridge.PassthroughKeyDriftTests`。
Snapshots 里**绝不出现**任何节点内容（可能含凭据/明文）。

---

## 7. 红线（不变量，改图相关代码前先读）

1. **失败隔离**：图的任何写/读异常都被吞掉，绝不阻断解题。与 `heimdall` 红线 4、
   `hallucination` 第三安全性质同款。
2. **持久文件无明文**：`.graph.jsonl` / `.graph.json` 不写 flag 明文、不写完整命令输出；
   凭据值例外但过质量门且随题清除。渲染时凭 `evidence_id` 回读 transcript。
3. **跨实例不继承 lineage**：epoch 变更时 `OBSERVED` 子图 `expired`；
   `evidence_id` 绑定 trace scope，天然不可跨实例。
4. **出身不可混淆**：`INFERRED`/`AUTHORED`/`DERIVED` 永不能作为 flag 证据，永不能重置止损，
   永不能满足 goal。这条是 19/29 教训与现有 `verify`/`hallucination` 的合并断言。
5. **观察者不裁决**：`CONTRADICTS` 只并置，不自动废任何一侧。
6. **只读消费者**：prompt 渲染、止损、观测都只读图，不回写。唯一 writer 是 driver 进程内的三处。
7. **预算优先**：任何面向 prompt 的输出必须有字符上限与截断策略；图不能抢占主注意力。
8. **可重放**：`.graph.json` 必须能由 `.graph.jsonl` 逐字节重建（验收测试之一）。
9. **依赖红线**：图的模型/IO 全在 `redpilot/worker/adapter/`（策略层）；
   若把计数写进快照，键与词表按现有规则进 `contracts`；**`contracts` 不得反向依赖 worker**。

---

## 8. 模块布局与最小改动面

```
redpilot/worker/adapter/
├── graph.py            ← 新增：model（Node/Edge/Origin/EvidenceRef）+ 纯函数（merge/replay/
│                         query/render/attack_paths）。零 IO、零第三方，全可单测。
├── graph_store.py      ← 新增：IO（.graph.jsonl append + .graph.json 原子写）、容量淘汰、
│                         生命周期、purge/redact 钩子、epoch expire。
├── blackboard.py       ← 改造：Blackboard.observe 变为 graph.extract 的一个调用者；
│                         保留类名与 query/actionable_assets 作兼容外观（减少调用点改动）。
├── heimdall.py         ← 改造：merge() 之后追加"发 delta 到图"；.heimdall.json 变派生。
└── stoploss.py         ← 改造：dry/hypothesis 判据改读 graph.delta()（信号替换，骨架不动）

orchestrator.py         ← 接线：_observe_qualified_tool_facts / epoch / 提交 / purge 调 graph_store
taskprompt.py           ← 接线：render_for_prompt 替换 actionable_assets + heimdall 两段
observability.py        ← 接线：_PASSTHROUGH_KEYS 加计数
compliance.py           ← 不改（复用为唯一打码口径）
```

**为什么 `graph.py` 与 `graph_store.py` 分开**：前者是纯逻辑（可像 `heimdall.merge`/`render`
那样离线单测），后者是副作用（文件/原子写/淘汰）。这条边界一旦模糊，测试就会退化成集成测试。

---

## 9. 分阶段落地（每阶段独立可发布、可回滚）

| 阶段 | 做什么 | 开关 | 验收 |
|---|---|---|---|
| **P0 影子** | 新增 `graph.py`+`graph_store.py`；driver 只**写**不读；`_blackboard.json` 与 `.heimdall.json` 照旧 | `ADAPTER_GRAPH=shadow` | 线上跑一轮，图能重放、无明文、不阻断解题；比对图事实 ⊇ 黑板事实 |
| **P1 接证据** | `EVIDENCED_BY` 边落地；flag 判定改走图查询（结果与旧逻辑**逐条对齐**比对） | `ADAPTER_GRAPH=verify` | 对历史 29 条拒收做离线回放，确认 `derived_local` 被正确分到"待验证"而非"幻觉" |
| **P2 接消费** | prompt 渲染 + 止损信号切换 | `ADAPTER_GRAPH=consume` | 干场判定与旧逻辑的差异全部可解释；prompt 预算不超 |
| **P3 接规划** | planner 读 `open_goals+frontier` 产出 `TaskProposal` DAG | `ADAPTER_GRAPH=plan` | 跨场续接块由图生成；多段题阶段提示不再停在 recon |

回滚保证：每阶段都有 env 开关，P0–P3 任一层关掉后退回当前行为（`.heimdall.json` 与
`_blackboard.json` 在 P2 之前**始终仍是权威读数**，图只是影子/校验）。

---

## 10. 测试与验收

**纯函数单测**（无 IO，对标现有 `test_heimdall` / `test_stoploss` 风格）：

1. `merge` 幂等：同一 delta 重放两次，视图逐字节相等；
2. origin 单调：`INFERRED → DERIVED → OBSERVED → CONFIRMED` 允许，反向被拒；
3. 内容 id 确定性：不同大小写/空白同一 id；不同内容不同 id；
4. 生命周期：`active → stale` 的 `_STALE_SESSIONS` 边界；`retracted → reaffirmed`；
5. `render_for_prompt` 预算：超预算时保 goal/边界/矛盾、砍尾部；空图返回空串；
6. `attack_paths`：给定图的最短路可手算验证；含 `BLOCKED_BY` 的边被排除；
7. **权限矩阵**：`INFERRED`/`AUTHORED` 不能满足 goal、不能重置止损、不能作 flag 证据（逐条断言）。

**回归测试（最重要）**：

8. **19/29 召回回归**：把 `hallucination.py` 注释里那批历史拒收样本做成 fixture，
   断言 `derived_local` 类**全部**转化为 `hypothesis + need`，而不是被拒；
9. **跨实例不泄漏**：构造两个 epoch，断言旧 `OBSERVED` 节点在 `graph.queries` 里不可见；
10. **无明文**：对含 flag/凭据的 transcript 跑一轮，断言 `.graph.jsonl` 里搜不到明文
    （用 `compliance.flag_plaintext_rx` 反查）；
11. **失败隔离**：注入损坏的 `.graph.json` / 只读文件系统，断言解题路径零影响；
12. **重放**：删掉 `.graph.json`，仅用 `.graph.jsonl` 重建，与删除前逐字节相等。

**观测回归**：`test_status_bridge.PassthroughKeyDriftTests` 增加新计数键。

---

## 11. 反模式（明确不做）

| 反模式 | 为什么不做 |
|---|---|
| 引入 Neo4j / 图数据库 | per-workdir 单进程、节点量 2000 量级，JSONL 足够；引入运维与依赖成本 |
| 让主 Agent 自己维护图 | 占注意力、易幻觉；机械/语义/驱动三 writer 都在旁路，主 Agent 只读 |
| 把 `confidence` 当概率用 | 现状 `0.6` 是拍脑袋常量；改成 `origin` 枚举这类**可判定**的量，别用假数字 |
| 建 `VULNERABLE_TO`/`EXPLOITED_VIA` 的机械正则边 | 语义误判会污染路径搜索；留给语义层 |
| 图节点存命令输出明文 | 合规红线；只存 `evidence_id`，渲染时回读 |
| 在 LiveSnapshot 18 键里塞图内容 | 快照是 wire 契约且有前端镜像；只走 `_` 带外计数 |
| 自动裁决 `CONTRADICTS` | Heimdall 红线 3：观察者只举镜子，主 Agent 裁决 |

---

## 12. 一句话总结

把 `blackboard` 的**节点**、`heimdall` 的**生命周期**、`_ProgressEvidenceGate`/`verify` 的
**出身分级**，用 `EVIDENCED_BY` 与类型化边连成一张 per-challenge 的证据图；
真相是 append-only 的 `.graph.jsonl`，图是可重放视图；三条不变量（出身不可混淆、
跨实例不继承、持久文件无明文）把现有四处零散的安全性质收敛成一处。
**它补的不是"存储"，而是"凭什么说这是真的"——以及"读者有没有资格用它"。**

---

*本文件为设计产物，未改动任何代码。落地请按第 8 节模块布局与第 9 节阶段推进。*
