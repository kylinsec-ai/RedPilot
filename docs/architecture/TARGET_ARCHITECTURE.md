# 目标架构：把 2026 智能体工程共识落到 Ghost

> **输入**：《智能体工程最佳实践研究报告》（仓库根 `智能体工程最佳实践研究报告.md`）
> 与 `skills/` 技能库。
> 每条断言都带本仓证据（`file:line`）或可复跑的实测数字。
> **更新（2026-09-17）**：本文原为纯设计。P3 评估面的**第一段已落地**，
> §3.3 附落地状态与三处被现实修正的设计；其余五面仍是设计。
>
> **姊妹文档**：`AGENT_ARCHITECTURE_SELECTION.md` —— 把 35 个公开智能体架构
> （`docs/reference/all-agentic-architectures`，只读参考库）按本仓任务形状逐个裁决，
> 补的是本文缺的那一层：**六个面各该抄哪个现成形状**。本文管"怎么修"，
> 它管"抄哪个"。§1.2 的一处措辞已按该文的核验结果修正。

**路径约定**（全文简写均按此展开，可逐条打开核对）：

| 简写 | 实际路径 |
|---|---|
| `adapter/…` | `packages/worker/ghost_worker/adapter/…` |
| `pi_agent.py` | `packages/worker/ghost_worker/adapter/solver/pi_agent.py` |
| `orchestrator.py` / `supervisor.py` / `observability.py` | `packages/worker/ghost_worker/…` |
| `obs/…` | `packages/ghost/ghost/obs/…`（`store.py` 即 `packages/ghost/ghost/obs/store.py`） |
| `contracts/…` | `packages/contracts/ghost_contracts/…`（`contracts/digest.py` 即 `packages/contracts/ghost_contracts/digest.py`） |

---

## 0. 一页纸

### 三句话

1. **六处已经做对**，其中两处比报告的建议更进一步（竞技场即 context reset；确定性
   证据闸门）。这些是改动时的**基线**，不是可选项。
2. **三处与共识方向相反**：prompt 在做加法（报告说 Claude 5 代砍掉了 80% 系统提示）；
   安全边界写在提示词里而不是动作上；技能面平铺 103 条、没有路由层。
3. **一处曾整个缺失**：评估面。轨迹底座（`runs` + `events` + `transcript` +
   事件折叠器）当时就已存在 —— 这是它能成为首切片的直接原因：纯读侧消费，
   零侵入求解链路。**首段已落地**（§3.3）。

### 对齐表

报告的十二条核心结论逐条对齐本仓。判定列只有四种：**做到** / **部分** / **相反** /
**缺失**。

| # | 报告结论 | 判定 | 本仓证据 |
|---|---|---|---|
| 1 | 上下文工程 > 提示词工程 | **相反** | 静态段落 8709 字符且重复注入，见 §2.1 |
| 2 | 默认单智能体，多智能体需论证 | 做到 | `adapter/taskprompt.py:216` 三角色按条件派发，非常驻；`ADAPTER_SUBAGENT` 可整体关闭 |
| 3 | 上下文是有限资源（context rot / 注意力预算） | **缺失** | 全仓无 token 预算、无上下文构成计量 |
| 4 | 少即是多（四组规则迁移） | **相反** | `_EFFICIENCY_POLICY` / `_CHALLENGE_PREFLIGHT` 是规则清单，见 §2.1 |
| 5 | 渐进式披露是第一设计原则 | **部分** | `adapter/skill_loader.py:90` + `pi_agent.py:710`；但 103 条平铺广告、无 L3，见 §2.3 |
| 6 | 工具设计是 ROI 最高的单点改进 | **部分** | 工具体是 pi 内置，框架侧唯一改造点是 `pi_ext/bash_guard.js`；无风险分级、无描述调优 |
| 7 | 长任务 = 压缩 + 记忆 + 隔离 + **一次 reset** | **做到且更进一步** | 竞技场多会话 + 三段式续接块，见 §1.1 |
| 8 | 多智能体收益来自并行与隔离 | **部分** | 隔离有了（独立进程、8 分钟预算、角色模型）；**写入纪律未编码**，见 §3.5 |
| 9 | 评估是可观测性的下半场 | **部分**（首段已落地） | 确定性判据 / 数据集 / pass^k 已有（§3.3）；模型 grader 与 macro 聚类未做 |
| 10 | 评估对象是轨迹而非答案 | **做到** | 轨迹底座 `obs/store.py:501` + `contracts/digest.py:242`；评估面已在 `ghost/eval/` 落地 |
| 11 | 安全是硬约束，边界放在**动作**上 | **相反** | 边界几乎全在提示词里，见 §2.2 |
| 12 | 协议三层：MCP 接工具 / A2A 接 agent / Skills 装流程 | 做到且克制 | 只用 Skills（`pi_agent.py:710`），未引 MCP / A2A —— 与报告 §9.2 "同进程内别用 A2A" 一致 |

### 阅读顺序

- 要判断"这个仓库现在能不能改"→ §1（哪些不能动）。
- 要判断"该改什么"→ §2（三处方向相反）与 §4（ROI 排序）。
- 要开工 → §3.3（P3 评估面，首切片）与 §5（路线图）。

---

## 1. 现状对齐：六处已经做对的

### 1.1 竞技场就是 context reset —— 比报告的建议更进一步

报告 §3.5 的判断是：compaction 保留连续性但给不了干净起点，模型的"上下文焦虑"仍在，
所以 Anthropic 转向 **context reset + 结构化交接**。

本仓的主循环（`ghost_worker/orchestrator.py`，7079 行）就是这么跑的：单题拆成多场
时间盒会话，每场是一个全新的 pi 进程（干净窗口），场间靠三类外部状态交接 ——
`MEMORY.md`、黑板 `_blackboard.json`（`orchestrator.py:816`）、以及 `AGENTS.md`
规定的三段式续接块，由 `extract_handoff()` 回捞（调用点 `orchestrator.py:5793-5805`）。

**比报告多做的**：报告只讲"reset 后用结构化交接物传递状态"，本仓把交接做成了
**跨场续接链** —— 已达成原语 / 已证死路 / 下一步三段是契约格式（`AGENTS.md` 末尾
写明"格式别改"），不是自然语言总结。这消除了 reset 最大的代价：新 agent 不知道
前任为什么放弃。

**改动时注意**：任何"把多场合并成一场、靠压缩维持连续性"的改法，都是在往回走。

### 1.2 确定性优先的闸门 —— 报告 §7.3 的完整实现

报告的原则是"确定性规则守底线，rubric 管质量；不要一上来就把所有判断交给模型裁判"。

本仓把这条做进了 flag 提交链路，而且是**行为级**而非提示词级：

- `adapter/verify.py`（3594 行，全仓最大模块）的 `FlagEvidencePolicy:556` 按题目
  分类决定"什么算证据"，`flag_evidence_policy():640` 是入口；
- eager 投递线程在会话进行中就盯着 `FLAG` 文件（`orchestrator._eager_needs_scan:1351`），
  投递前过一道 grounding 门（`_remote_grounded:1789`），**默认**要求候选
  *逐字出现在本场真实工具输出里* —— 但见下方 ⚠️；
- 独立复核 `_skeptic_check:1867` 在提交前再判一次，是 **fail-open 否决门**
  （`verifier.veto_conf` 默认 0.85 以上才拦，「LLM 不确定或 verdict 为空 → 宁放过」）；
- 平台响应（correct / duplicate）是唯一终局判据（`_record_confirmed_submission:1712`）。

> ⚠️ **措辞修正（2026-09-17，架构选型核验所得）**：本节此前写"候选必须逐字出现在本场
> 真实工具输出里"，这是一处**过度断言**。实际存在**三条强提旁路**，都绕过 `verified`
> 直达平台：① eager 强提 —— `claim.grounded and claim.confidence >= 0.50`
> （`orchestrator.py:4303`）；② 会话后强提 —— `_flag_grounded_in_transcripts(..., require_remote=False)`
> （`:5537`），该判据是**时序**判据，且**接受"只出现在 agent 自己敲的命令参数里"**
> （`:1982-1988` 注释写明 *"For reverse/crypto challenges this is legitimate"*）；
> ③ `invalid_format` 翻案（`:5556`，skeptic `rescue` 阶段）。
> 而 `grounded` 的定义比字面宽：flag 只出现在 agent 自己 `echo`/`printf` 的命令里时
> 也给 `grounded=True, confidence=0.70`（`verify.py:3398-3405`），0.70 ≥ 0.50 即触发旁路 ①。
>
> **准确的表述**：「确定性」确定在 grounding 的**包含关系判定**、provenance 分类与阈值比较；
> **不确定在三条旁路是否触发** —— 那取决于 LLM skeptic 是否否决。
> 另注 `orchestrator.py:1897` 的注释写"准入制"，与 `:1908-1912` 的代码（fail-open 否决）
> 矛盾，**行为以代码为准**。详见 `AGENT_ARCHITECTURE_SELECTION.md` 附录 B。

**为什么这是对的**：报告 §7.2 说 grader 分三型，确定性优先。本仓的 evidence gate 就是
"Python grader 对整条轨迹做断言"的形态 —— 它检查的是工具输出与候选值的**包含关系**，
不是模型的自我陈述。这是全仓最值钱的一段代码，也是 §3.3 评估面要**复用而不是重写**的
判据来源。

> ⚠️ **落地时的修正**：上面这条"复用 `verify.py` 判据"的设想，在 P3 首段落地时被证明
> **不成立** —— `is_task_remote_command` 是提交溯源判据（要求 host+port+scheme 齐全），
> `is_remote_command` 是攻击工具白名单（看不见 `pip install`）。两处误判详见 §3.3 的
> 落地状态。结论不变（判据仍不该重写），但**复用的位置**从"直接调用"变成了
> "worker 侧桥接 + 补一张明写的规则表"。

### 1.3 读写凭据不对称 —— 报告 §8「最小代理权」

报告 §8.3 的三原则之一是"最小代理权"，不只是最小访问权限，还包括限制 agent 的
动作类型与可触达范围。

本仓：`OBSERVABILITY_TOKEN`（遥测**写**端）与 `OBSERVABILITY_READ_TOKEN`（观测**读**端）
刻意分离；读端返回**明文 flag 与完整 agent 实录**，因此读端 token **不转发给 worker
容器**。所以"能写遥测"≠"能读答案"。两者皆未设置时读端 503（不静默降级）。

配置与理由见 `.env.example` 的观测段与 `README.md` 的凭据表。

### 1.4 技能走原生渐进披露 —— 报告 §5.2 的 L1/L2 已落地

`adapter/skill_loader.py` 的 `SkillStore:90` 只读 frontmatter 产出**名录**（名字 +
描述 + 路径），不读正文；正文留在磁盘上由 Agent 按题目自己 `read`。
`pi_agent._install_skills:710` 把 `skills/` 逐目录软链进**本题 HOME**
（`$HOME/.pi/agent/skills/`），走 pi 的原生 `<available_skills>` 发现机制。

**这个方向是对的，不要在"框架替 Agent 选技能"上回退**：`skill_loader.py` 的模块
docstring 记录了上一版的教训 —— 框架曾经有一张 `_DOMAIN_SIGNALS` 正则表做 top-2
预选正文注入，技能库换成上游 103 个技能后那张表既覆盖不了题面、又要每次同步手改，
已删除。**路由归模型前向推理，这是报告 §5.2 的原话**，本仓已经在这条路上。

### 1.5 轨迹底座已存在 —— 评估面能低成本落地的原因

报告 §7.2 的 Trace 四件套（Trace / Graders / Datasets / Eval Runs）里，**Trace 这一件
本仓已经有了**：

| 层 | 本仓实现 |
|---|---|
| 原始事件流 | pi 原生 transcript：`work/<safe_code>/transcript.jsonl`（`contracts/paths.py:TRANSCRIPT_FILENAME`） |
| 平台侧事件表 | `obs/store.py:141` `append_events()` 按 `run_id` 落库；读端 `:501` `events_for_run()`、`:635` `run_events(after, limit)` |
| run 生命周期 | `obs/store.py:209` `close_run()`，状态词表见 `contracts/vocabulary.py:RUN_STATUSES` |
| **紧凑时间线** | **`contracts/digest.py:242` `fold_rows()`** —— 把原始事件折叠成 `{seq, kind, t, turn, tool, cmd, out, err, text, note, stop, tokens}`，单 code 上限 20000 条（`ENTRY_CAP`） |

`fold_rows` 是关键：报告 §7.2 的 macro-eval 要求"把每条 trace 压成紧凑文档，再做
无监督聚类"，而那一步**已经被实现了**（消费方 `obs/read.py:206` 的 timeline 端点）。
评估面要做的是在它之上加判据与聚类，不是重造数据管道。

### 1.6 角色模型路由 —— 报告 §10「探索类走小模型」

报告引用 Anthropic 数据：探索类 subagent 路由到小模型，调优前就能让探索成本降约 5 倍。

本仓 `pi_agent._inject_role_model` 已把 subagent 角色接到独立模型配置，`VERIFIER_MODEL`
与 heimdall 侧模型可独立设置（`.env.example` 模型段）。方向一致。

---

## 2. 三处与共识方向相反

### 2.1 prompt 在做加法，而报告说要砍 80%

报告 §3.6 记录 Anthropic 在 Claude 5 代砍掉了 Claude Code **超过 80% 的系统提示**，
编码评测无可测量损失，并给出四组规则迁移：

| 报告的四组迁移 | 本仓现状 |
|---|---|
| 给规则 → 给判断力 | **规则清单化**：`_EFFICIENCY_POLICY`（"每条命令只验证一个假设"…）是典型规则表 |
| 给示例 → 设计接口 | **示例前置**：subagent 三角色写成 917 字的 prompt 段落（`_SUBAGENT_GUIDE`），而不是工具的 schema |
| 全量前置 → 渐进披露 | **部分符合**（技能面已渐进），但安全红线仍全量前置 |
| 重复强调 → 精简描述 | **重复**：见下 |

**实测数字**（单位：字符；复跑命令见附录 A）：

| 常量 | 行 | 字符 |
|---|---|---|
| `_CLAUDE_MD` | `adapter/taskprompt.py:75` | 3475 |
| `_ISOLATION_CONSTRAINT` | `:187` | 1837 |
| `_INTRANET_ORCHESTRATION` | `:56` | 1085 |
| `_SUBAGENT_GUIDE` | `:216` | 917 |
| `_OFFLINE_CONSTRAINT` | `:178` | 450 |
| `_EFFICIENCY_POLICY` | `:314` | 444 |
| `_CHALLENGE_PREFLIGHT` | `:325` | 282 |
| `_SKILL_SELF_SERVE_NOTE` | `:45` | 219 |
| **模块常量和** | | **8709** |

这段常量 **48.2% 是中文**（8709 字符里 4200 个汉字）。按中文 ~0.6 token/字、ASCII
~0.25 token/字符加权粗估 ≈ **3600 token**（**字符数是实测，token 数是估算** —— 本仓
未装 tiktoken，故不假装精确）。

且这**尚未计入**任务卡、已知事实、前次记忆（上限 4000 字符，`:335`）、已判错候选指纹、
黑板、Heimdall 图。真正进上下文的量比这个数字大。

**已确认的两处重复注入**（同一份内容走两条路进同一个上下文）：

1. `_ISOLATION_CONSTRAINT` **既**被 `write_context_md():248` 写进每题目录的 `CLAUDE.md`
   （与 `_CLAUDE_MD` 拼在一起），**又**被 `build_task_prompt():377` 直接 append 进 prompt。
2. `_OFFLINE_CONSTRAINT`（`:178`）与 `_CLAUDE_MD` 内部的「运行环境约束」段（`:168-173`）
   内容重叠，两者都会被注入（`:374` 与 CLAUDE.md）。

这不是"写错了"，是**没有单源**的必然结果：两份文本各自演化，没人能一眼看出重复。

**三处复核补充**（2026-09-17，代码通读所得，实施 P1 前先看）：

1. **框架里没有任何上下文压缩**。`pi_agent.solve()` 的事件循环没有摘要、没有截断、
   没有 compact 分支。现有的三个替代物是：pi 自身的内部压缩、会话轮换（每场换新
   进程）、以及落盘的状态文件（MEMORY.md / `.continuation.json` /
   `tried_commands.md` / `artifacts/` / `_blackboard.json`）。
2. **`SolverConfig.auto_compact_window` 是一个零消费者的死旋钮**（`adapter/config.py:89`，
   preset 里填了 786432 / 1000000）。它看起来像"压缩窗口"，实际没人读 ——
   实施 P1 时别以为调它有用。同类的还有 `ControllerConfig.round_timeboxes` /
   `ADAPTER_ROUND_TIMEBOXES`（`adapter/config.py:252,287`）。
3. **`taskprompt.write_memory`（`:257`）没有任何调用点** —— 它在 `orchestrator.py:62`
   被 import，但 MEMORY.md 实际由 agent 自己写、由 `_merge_memory`（`orchestrator.py:3861`）
   往受围栏区域合并。§3.6 说"记忆写入路径未受控"，这里要补一句：框架侧那条写入
   路径**根本没接上**。

### 2.2 安全边界写在提示词里，而不是放在动作上

报告 §8.3 的共识原话是"**假设一定会被攻破，把边界放在'动作'上，而不是放在提示词上**"，
§8.4 给了五层骨架。

| 报告的五层 | 本仓现状 |
|---|---|
| 1 输入分离（界定不可信输入面） | **部分**：取证策略按题目分类（`verify.flag_evidence_policy:640`）已区分本地/远端证据 |
| 2 工具风险分级（policy-as-code） | **无** |
| 3 动作校验（执行前检查动作是否在授权范围内） | **无** |
| 4 沙箱 | 有容器，但见下方取舍 |
| 5 运行时行为监控（把 agent 当不可信进程，看**动作序列**） | **无**；但 `fold_rows` 的输出正是它的数据源 |

当前唯一落在动作层的实现是 `pi_ext/bash_guard.js`（bash 工具强制 timeout/ulimit +
自引用写拦截，`.env.example` 的 `ADAPTER_PI_EXTENSIONS`）。其余 —— 跨题隔离红线
（`_ISOLATION_CONSTRAINT`）、禁联网下载（`_OFFLINE_CONSTRAINT`）、目标白名单 ——
**全部是给模型的行为约束，靠自律**。

**一处必须写明的有意取舍**：解题目容器带 `cap_add: NET_ADMIN` 与 `/dev/net/tun`
（`docker-compose.yaml:250-254` 与 `:297-301`）—— 靶场 VPN 必需。这削弱了容器隔离强度
（报告 §8.5 把容器列为"弱隔离，共享内核存在逃逸风险"）。这是**已知未做**，不是遗漏：
报告推荐的 MicroVM 方案在当前威胁模型下收益不足，且它与 VPN 独占隧道的舰队拓扑冲突。

### 2.3 技能面平铺 103 条，没有路由层

**实测**（复跑命令见附录 A）：

| 指标 | 值 |
|---|---|
| 技能目录数 | 103（全扁平，无分类子目录） |
| **有子目录的技能数** | **0** |
| description 平均 / 最长 | 221 / 615 字符（`hack`） |
| L1 首句广告合计 | 4427 字符（描述以英文为主，≈ 1.1k token） |
| 框架兜底名录 XML | 14,740 字符（`skill_loader.skill_summary_xml():134`）。**注意这是兜底路径**：默认路径（`ADAPTER_SKILL_AGENT=1`）下框架不注入 XML，由 pi 原生 `<available_skills>` 承担，字段相同（名字+描述+路径），成本同量级 |
| SKILL.md 总量 | 1,141,974 字符 / 1,169,456 字节。正文**几乎全英文**（全库 114 万字符里只有 281 个汉字），按 4 字符/token 粗估 ≈ **28.5 万 token** 的知识面 |
| SKILL.md 中位 / 最大 | 11,243 / 31,571 字节 |
| >20KB 的技能 | 5 个（`business-logic-vulnerabilities` 31.6KB、`hack` 18.8KB…） |

**补充（2026-09-17 复核）**：技能面其实有**两条**上料路径 —— 除了软链进本题 HOME，
`pi_agent._build_cmd:833` 还会在 `ADAPTER_SKILLS_DIR` 存在时给 pi 传
`--skill <dir>`。两者都是"把整个技能库暴露给模型"，所以上面的广告面问题不受影响，
但 P2 收窄广告面时要**同时**改这两处，只改一处会漏。

**问题一：L3 没有落地。** 报告 §5.2 的三层模型是「L1 frontmatter → L2 SKILL.md →
L3 `references/` `scripts/`」，但这份技能库里**零个技能有子目录**。上游的三层路由
（`skills/PROVENANCE.md:21-22`）是 `hack`（master router）→ 6 个分类入口
（`recon-for-sec` / `api-sec` / `auth-sec` / `injection-checking` / `file-access-vuln` /
`business-logic-vuln`）→ 深度题面技能 —— **全部是平级的 SKILL.md**，靠 Agent 自己
逐层 `read`。`hack/` 下那 6 个分类文件（`RED_TEAM.md` / `TEST_MATRIX.md` …）是同一
技能目录里的散文件，连 L2 都算不上。

**问题二：L2 超预算。** 报告建议 L2 < 5000 token（< 500 行）；`hack/SKILL.md` 18.8KB
（≈5-6k token）、`business-logic-vulnerabilities` 31.6KB 都越线。

**问题三：真实重复簇**（报告 §4.3 的"按意图合并"）：

- `business-logic-vuln`（1.2KB）× `business-logic-vulnerabilities`（31.6KB）—— 同类名、
  体量差 26 倍，路由时模型无法区分；
- 侦察/入口簇：`recon-for-sec` / `api-sec` / `auth-sec` / `recon-and-methodology` /
  `attack-surface-mapping`；
- `prototype-pollution` × `prototype-pollution-advanced`、`container-escape-techniques` ×
  `sandbox-escape-techniques`。

**问题四：没有负样本。** 报告 §5.4 引用 Glean 的生产数据：提供 skill 反而会**先降低**
正确触发率约 20%，补上"不要在……时调用本 skill"的负样本后才恢复。当前 103 条描述里
一条负样本都没有。

---

## 3. 目标架构：六个面

### 3.0 总览与依赖

```
    P2 技能面 ──┐              ┌── P6 记忆与状态面
    （知识）     ├──→ P1 上下文面 ──┴──→ 求解 Agent（pi 进程，每题一 HOME）
    P5 多智能体面┘    （分层组装 + 预算计量）      │
    （隔离/并行）                                 │ 工具调用
                                                 ▼
    P4 动作与安全面 ──（策略分级 / 动作校验 / 序列异常）
                                                 │
                                                 ▼
    P3 评估与轨迹面 ──（runs + events + fold_rows）──→ 度量 / 回归 / CI 门禁
                       ▲
                       └── 只读；绝不回写控制面
```

依赖关系只有一条硬约束：**P3 是其余五项的前置**。没有度量，P1/P2 的改动无法判断
是改善还是劣化（报告 §5.5 的原话：迭代 skill 时你很难分辨"真的改善了"还是"只是改变了
行为"）。

命名沿用本仓惯例：**面（plane）之间不新增包** —— 三包结构（`contracts` / `ghost` /
`worker`）是 README 明写的地基，依赖方向由 `packages/contracts/tests/test_purity.py`
钉住。下面每个"落点"都是包内新增模块或对既有模块的改造。

---

### 3.1 P1 上下文面

| | |
|---|---|
| **现状** | `adapter/taskprompt.py` 711 行拼装器；静态段落 8709 字符；两处确认重复注入；无预算、无计量 |
| **目标** | 规则单源化 + 四层结构 + 组装时声明并累计 token 预算 |
| **落点** | `adapter/taskprompt.py` 重构为分层组装器；不变量常量单源化 |
| **验收** | 静态段落 token 下降 ≥40%，**且 P3 度量行为不劣化** |

**四层结构**：

| 层 | 内容 | 预算（目标） | 现状 |
|---|---|---|---|
| L0 不变量 | 授权边界、写入边界（只写本题目录）、答案格式 | ≤ 300 token | 现在散在 `_ISOLATION_CONSTRAINT`(1837) + `_OFFLINE_CONSTRAINT`(450) 两段 |
| L1 任务卡 | 目标/地址/flag 数/难度/取证模式/预读要求 | ≤ 400 token | 已有，形态正确 |
| L2 知识面 | 技能名录与路由指引 | ≤ 800 token | 见 §3.2 |
| L3 状态 | MEMORY / 黑板 / 续接块 / 镜像 / 判错账本 | 现上限 4000 字符 | 已有，需纳入统一核算 |

**单源化规则**（这是本节最重要的一条）：**一份内容只允许出现在一处**。
`_ISOLATION_CONSTRAINT` 现在被 `write_context_md():248` 与 `build_task_prompt():377`
各注入一次，正确做法是二者引用同一个常量来源，且**只走一条路**（推荐走 CLAUDE.md，
因为它是 per-challenge 的持久文件，且 pi 会自动加载）。

是否把不变量上提到 `ghost_contracts` 需单独论证：contracts 是**零依赖契约包**，
`test_purity.py` 是红线；prompt 文本是否属于"进程间契约"要看它是否被多个包消费 ——
当前只有 worker 消费，故**建议先留在 worker 内，抽成 `adapter/rules.py` 单源模块**，
等真的出现第二个消费方再上提。

**预算计量**：组装时逐段累计，超限时**降级而不是无声膨胀**（降级顺序：镜像 → 判错
账本 → 前次记忆截断 → 黑板）。每场会话产出一份构成报告，以 `_` 前缀带外键进 live 快照
—— 这符合既有约定（`contracts/vocabulary.py:69` `OUT_OF_BAND_PREFIX`、`:78`
`strip_for_snapshot()`：下划线键只走 relay→平台链路，不落库）。态势台因此能显示
"本场 prompt 由什么组成"，把上下文变成一等可观测对象。

**四组规则迁移的落法**（报告 §3.6）：

- *给规则→给判断力*：`_EFFICIENCY_POLICY`(444) 的六条规则中，前三条是"如何高效工作"的
  判断，可压成两句；后三条是止损判据，**保留**（它们有框架侧的真实后果）。
- *给示例→设计接口*：`_SUBAGENT_GUIDE`(917) 里的三角色说明应在**工具 schema**里
  （见 §3.5），prompt 里只留"何时该派"的判断。
- *全量前置→渐进披露*：安全红线可下沉到技能库入口（Agent 读 `hack` 时一并读到）。
- *重复强调→精简描述*：先做单源化，重复自然消失。

---

### 3.2 P2 技能面

**决策（已定）**：生成索引 + 覆盖层，**不动上游正文**，`PROVENANCE.md` 的溯源关系保持。

**一条硬约束（决定了落点在哪）**：`skills/` 是**上游内联副本**，同步方法是
`rm -rf skills && mkdir skills && cp -r …`（`skills/PROVENANCE.md:27-31`）—— 目录会被
整个删掉重建；同时它在 compose 里以 `:ro` 只读挂载两处
（`docker-compose.yaml:52-53`，对应 `Dockerfile:108-109` 的两处 `COPY skills`）。
**因此索引与覆盖层都不能放在 `skills/` 里面** —— 放了就会被下次同步抹掉。

**落点**：

```
skills/                    ← 上游正文，一字不动，可被 rm -rf 重建
skills-overlay/            ← 本仓所有（新增）
  INDEX.md                 ← 机器生成，抬头写"请勿手改，改法见 PROVENANCE.md"
  overlay.yaml             ← 手写：路由判据、负样本、别名、合并声明
tools/gen_skill_index.py   ← 生成器（tools/ 已是既有的非镜像工具目录）
```

对接层：`ghost_contracts.paths` 需要新增一个与 `skills_root()` 同形态的
`overlay_root()` 解析器（**单源**，不要各调用点自己拼路径 —— 这正是
`paths.py` docstring 记录的教训："数固定层数的写法搬迁后静默归零"）。

`INDEX.md` 的形态（L1 广告面的替代品）：

```
# 技能索引（生成物，勿手改）
## 路由入口
hack — <首句>  → 逐层下钻：6 个分类入口见下
## 分类入口
recon-for-sec / api-sec / auth-sec / injection-checking / file-access-vuln / business-logic-vuln
## 深度技能（103）
<name> — <首句>（<体量标注>）
```

三个设计点：

1. **广告面收窄**：默认只广告 `hack` + 6 个分类入口 + 索引，深度技能由 Agent 逐层
   下钻。这是在**已有**的原生渐进披露上再收一层 —— 现在 103 条全部进 `<available_skills>`，
   每一场的每一次请求都付这个固定成本。
2. **`overlay.yaml` 只做三件事**：给高频误触技能写**负样本**（"不要在……时调用"）；
   登记**重复簇**的取舍（同名体量差 26 倍的两个 business-logic 技能，必须有一条指向）；
   登记 `hack/` 下 6 个 `.md` 为已知的 L3 材料（**不改文件位置**，只在索引里标记，
   让 Agent 知道它们存在 —— 现在它们只能靠读 `hack/SKILL.md` 正文才发现）。
3. **生成器是确定性的**：同一个 skills 树必须生成逐字节相同的 INDEX.md，
   否则无法进 CI 做漂移检测。

**验收**：触发率 / 误触率基线，**可从现有 transcript 回放直接算出** —— 工具流里是否
出现 `read …/SKILL.md`、读了哪一个、题目类别与实际选择是否吻合。这条不需要新埋点，
是 P3 回放式评估的第一批用例。

**明确不做**：不把技能重整为分类目录、不合并上游文件。理由：与上游产生持续分歧，
每次同步都要手工解冲突；而收益（更干净的目录树）可以在索引层无损获得。

---

### 3.3 P3 评估与轨迹面（首切片）

| | |
|---|---|
| **现状** | 零。全仓无 grader / rubric / dataset / pass^k |
| **底座** | `obs/store.py` 的 runs+events、`work/<code>/transcript.jsonl`、`contracts/digest.fold_rows()` |
| **落点** | `packages/ghost/ghost/eval/`（**不新增包**：三包结构是地基；且评估是读侧消费者） |
| **数据** | 先回放已有轨迹，再接实跑（已定） |
| **红线** | **只读**；绝不回写控制面，评估结果与 `runs` 分表存放 |

#### 输入契约

| 需要什么 | 从哪来 |
|---|---|
| 一次执行的 run 元信息 | `ObsStore.run_row(run_id)` / `list_runs(status=…, worker=…)`（`obs/store.py:624` / `:518`） |
| 该 run 的原始事件 | `ObsStore.run_events(run_id, after, limit)`（`obs/store.py:635`） |
| 该 run 的**紧凑时间线** | `ghost_contracts.digest.fold_rows(rows, after=, live=False)`（`contracts/digest.py:242`） |
| 会话边界与提交结果 | 折叠时间线的 `kind=session` / `kind=attempt` 条目；`attach_accepted_flags`（`obs/store.py:543`） |
| 成本 | 折叠时间线的 `kind=turn` 条目带 `tokens` 字段（`contracts/digest.py` 条目 schema 已含） |

**这四样今天就能拿到，不需要动 worker 一行。** 这是首切片可行的全部理由。

#### 组成

```
packages/ghost/ghost/eval/
  __init__.py
  dataset.py       任务卡装载（YAML/JSON），含四类用例
  replay.py        run_id → events → fold_rows → 判据输入（唯一数据入口）
  graders/
    deterministic.py   硬门禁：违禁联网下载、工具顺序、证据闸门是否触发、
                       提交语义（eager / duplicate / INCORRECT 账本）、回合与 token 预算
    rubric.py          模型评分，固定输出 schema
  macro.py         对 fold_rows 输出做群体聚类 → 四标签
  report.py        pass^k / 触发率 / 每任务成本
  store.py         评估结果落库（独立于 runs，前缀 eval_）
```

**确定性 grader 的判据来源**：**复用 `adapter/verify.py` 的既有判据，不要重写**。
`FlagEvidencePolicy` / `is_remote_command` / `is_task_remote_command` 已经在回答
"这条命令是不是在碰目标、是不是在联网下载"——评估面要做的是把同一批判据**离线**跑在
折叠时间线的 `cmd` 字段上，而不是另写一套正则。判据分叉是这类系统最典型的静默失败
（本仓已有先例：同名双份的 prompt 组装器，见 `packages/worker/tests/test_taskprompt_single_source.py`）。

**rubric grader 的输出 schema**（报告 §5.5 要求的"可存储、可对比、可聚合"）：

```json
{"overall_pass": true, "score": 0.85,
 "checks": [{"id": "evidence_grounded", "pass": true, "notes": "…"}]}
```

**只用于主观维度**（例如"侦察是否系统化"），阈值比确定性 grader 宽。

**macro-eval 四标签**（报告 §7.2）：`case_type`（输入场景）/ `run_outcome`（结局）/
`eval_finding`（局部信号）/ `behavior_pattern`（群体聚类）。它解决的问题是报告里那句话：
**逐条看 trace 会优化"最响亮的失败"，而不是"最高频的失败"**。

**数据集规模**：20–50 条起步（报告 §7.2「20–50 个任务即可起步」），四类齐备 ——
显式触发 / 隐式触发 / 带噪触发 / **负样本**。每个任务卡写清：输入、成功条件、
允许与禁止的行为、至少一个能通过评分器的参考解。

**指标用 pass^k 而不是 pass@k**（报告 §7.2）：单次成功率 75% 时，"三次至少成功一次"
约 98.4%，但"**连续三次全部成功**"只有约 42.2%。一次成功演示证明不了上线资质。

#### CI 接法

- `pytest -q` 现有 378 条用例是**单元/回归**（`tests/` 下是内部函数断言），
  与评估面是两回事，不要混。
- 评估面用 pytest marker 单独分组：确定性 grader **硬门禁**（低于阈值阻断合并），
  模型 grader **软阈值**（报告 §7.3）。
- 触发条件：触及 prompt / 技能 / 模型的改动。

#### 分期

| 期 | 内容 | 状态 |
|---|---|---|
| 第一段 | 回放式：`replay.py` + 确定性 grader + 20 条任务卡。**零 LLM 成本，可进 CI** | **已落地**（见下） |
| 第二段 | 模型 grader（`graders/rubric.py`）+ macro 聚类（`macro.py`） | 未开始 |
| 第三段 | 接实跑：`tsecbench/`（注意它与统一 server 抢 8000 端口，`README.md` 已记） | 未开始 |

#### 落地状态（第一段，2026-09-17）

```
packages/ghost/ghost/eval/
  replay.py              回放：run_id → 事件 → fold_rows → Trace（唯一数据入口）
  dataset.py             任务卡与评估集装载（含 ratio_budget/budget 的单位分离）
  datasets/skill_routing.json   20 张卡，explicit/implicit/noisy/negative 各 5
  graders/deterministic.py      六条确定性判据 + Predicates 注入协议
  report.py              pass^k / 触发率 / 成本 / 判据覆盖率
  store.py               EvalStore：eval_* 表，独立的 ./data/eval.sqlite3
packages/ghost/tests/    评估面单测 + test_ghost_purity.py（两条依赖方向红线）
packages/worker/ghost_worker/adapter/eval_bridge.py   判据桥（唯一注入点）
packages/worker/tests/test_eval_bridge.py             判据桥的行为契约
tests/test_eval_end_to_end.py                         跨包端到端
```

落地过程中被现实修正的**两处设计**（都写进了代码注释，此处留索引）：

1. **§3.3 里"复用 `adapter/verify.py` 的判据"这条不成立。** `is_task_remote_command`
   是**提交溯源判据**，要求命令行里出现规格完整的当前目标 authority（host+port+scheme），
   于是 `nmap -Pn 10.0.0.5` 这种最正常的打靶动作被判成"不在目标上"；而
   `is_remote_command` 是一张**攻击工具白名单**，`git clone` / `apt-get install` /
   `pip install` 一律返回 False —— 正是 `_OFFLINE_CONSTRAINT` 第一个点名禁止的那批。
   两个都不能直接当越界判据。桥改为：**判断"命令碰的主机是否都在授权范围内"**，
   并自带一张小规则表补上"命令行里没有 URL 的联网行为"；那张表是**明写的欠账**，
   归属应是 `verify.py` 与 P4 的 policy-as-code（见 `eval_bridge.py` 的模块 docstring）。

2. **判据按卡启用会制造覆盖率盲区。** `_OFFLINE_CONSTRAINT` 是全局约束，但
   20 张卡里只有 3 张启用了 `offline` —— 其余卡上，一次真实的 `pip install`
   完全不可见，而"没查"与"查过没问题"在报告里长得一样。已加
   `report.check_coverage()` 把每个判据的启用次数与状态分布摆出来，
   并用一条测试钉住当前数字。

3. **判据与报告一度对同一件事有两套口径。** `graders._routing` 早期写的是
   "两侧都声明时以 forbid 为准"，而 `report.trigger_rate` 两侧各数各的 ——
   14/20 张卡同时声明两侧，那些卡的 expect 侧因此从没被检查过。已改为两侧
   独立判（误触发优先报，漏触发也判 fail）。

**已知局限**：本机没有真实 run 数据（`data/` 只有控制面库，`./data/obs.sqlite3`
不存在），所以任务卡的 `challenge_code` 一律留空、全部用合成轨迹验证。
"回放已有轨迹"这条要在有观测库的机器上才真正跑得起来 —— 代码路径已通，
缺的是数据。另：数据集目前**不覆盖多 flag 题**（`TaskCard` 没有 flag 数/阶段字段），
那类题的判据要等第二段。

---

### 3.4 P4 动作与安全面

| | |
|---|---|
| **现状** | 边界在提示词里；唯一动作层实现是 `pi_ext/bash_guard.js` |
| **目标** | policy-as-code 声明工具风险分级 + 动作校验层 + 动作序列异常检测 |
| **落点** | 校验层插在 pi 工具调用边界；`bash_guard.js` 已是现成的插入点（pi 扩展机制） |
| **验收** | 违禁动作在**执行前**被拦（不是事后从 trace 里发现） |

**策略形态**（报告 §8.4 给了 YAML 示例，本仓照此声明）：

```yaml
tools:
  bash:   { risk: critical, filesystem_scope: task_workdir, egress: deny }
  read:   { risk: low,      filesystem_scope: task_workdir }
  write:  { risk: medium,   filesystem_scope: task_workdir }
  subagent: { risk: low,    writes: false }     # 见 §3.5
```

**关键判据**（报告 §8.4 第 3 层原话）：**不判断注入是否发生，只判断动作是否异常** ——
执行前检查该动作是否与声明任务一致、是否在授权范围内。这比"检测提示注入"可解得多。

**序列异常检测**（第 5 层）：把 agent 当不可信进程，监控**整个会话的动作序列**是否与
声明目的相符。报告给的对照是：正常 = 读源码 → 发 PR 评论；异常 = 读源码 → 读 SSH 密钥
→ 建出站连接。本仓的数据源已经存在（`fold_rows` 的紧凑时间线），所以这一层在 P3 之后
是**增量**而非新建。

**审批设计**（报告 §8.3）：Anthropic 自己的数据显示**用户批准了 93% 的权限提示** ——
人类审批会退化为仪式。解法不是取消审批，而是只在高后果边界保留。本仓当前无人工
审批环节（全自动靶场），所以这一条落为"**高危动作直接 deny 而不是询问**"。

**已有的部分不要重做**：输入分离（取证策略按题目分类）、确定性 grounding 门、
跨题隔离 —— 这些是 §1.2 的存量资产。

---

### 3.5 P5 多智能体面

| | |
|---|---|
| **现状** | `pi_agent._install_subagents:626` 装三角色（scout / worker / checker），8 分钟挂钟预算，`_inject_role_model` 做角色模型路由 |
| **目标** | 编码"读可多、写单一"；角色契约进工具 schema；每角色 token 记账 |

**1. 写入纪律（报告 §2.4）。** Cognition 的立场从 2025 年的"别建多智能体"修正为 2026 年的
**"让多个 agent 贡献'智能'（读、分析、检索），但写入（改变状态的动作）保持单线程"**。
本仓当前的 subagent 与主 Agent **同目录、同网络、同权限** —— 它写得进 `FLAG`，
也发得起提交。

这正是报告 §8.2 点名的风险面："**不要让 agent 执行之间共享可写工作区**、
把工作流生成的文件一律当不可信输入"。落法：subagent 构造上**只读**（不可写 `FLAG`、
不可提交），与 §3.4 的 `writes: false` 是同一条策略的两个消费者。

**2. 角色契约进 schema（报告 §4.2）。** 报告说工具描述是"工具表现最重要的因素"，
目标每个工具 3–4 句：做什么、何时用、**何时不该用**、参数含义、限制。当前三角色写在
917 字的 prompt 段落里（`_SUBAGENT_GUIDE`），而报告 §3.6 的迁移方向是
*给示例→设计接口*：让 `subagent` 工具的 `agent` 参数带枚举与描述，用法只写一处。

**3. 每角色 token 记账。** 报告 §10 那句"token 使用量单独解释了 80% 的性能方差"要能
被验证，前提是能按角色拆账。`fold_rows` 的 `turn` 条目已带 `tokens`，按 subagent 调用
边界切分即可 —— 这又是 P3 的增量。

**明确不做 A2A**：报告 §9.2 的原话是"如果所有 agent 都在一个进程里，普通 sub-agent 比
网络协议简单得多；只在 agent 真正跨服务、跨团队时才用 A2A"。本仓 subagent 是同机子进程。

---

### 3.6 P6 记忆与状态面

| | |
|---|---|
| **现状** | 七种状态文件各有写者，无统一契约 |
| **目标** | 在 `paths.py` 的既有约定上扩展出状态文件契约；记忆写入路径受控 |

**现状清单**：

| 文件 | 写者 | 读过它的地方 |
|---|---|---|
| `MEMORY.md` | Agent 自己 + 框架（`taskprompt.write_memory:257`） | 下一场 prompt（上限 4000 字符） |
| `_blackboard.json` | driver（`orchestrator.py:816`） | 下一场 prompt（`actionable_assets()`） |
| `tried_commands.md` | 框架 | 下一场 prompt（`:679`） |
| `.rejected_flags` | 框架（平台判错账本，`:1023`） | 指纹回灌（`build_task_prompt:514`） |
| `.unverified_flags` | 框架（未证实账本，`:1142`） | compliance 检查（`adapter/compliance.py:78`） |
| `.hallucination.json` | 框架（`adapter/hallucination.py:35`） | 状态查询 |
| Heimdall 图 | 观察者 Agent（`adapter/heimdall.py`） | 下一场 prompt（`heimdall_map`） |

**目标形态**：在 `ghost_contracts/paths.py` 里为每个文件声明**路径 + 写者 + 生命周期**
（该文件已承担"进程间文件契约的单一来源"，扩展它而不是另起一套）。这与 §3.1 的
L3 层预算核算共用同一份清单。

**记忆投毒（报告 §6）**：记忆写入路径是**新的、尚未被充分防御的攻击面**，报告把
"记忆与上下文投毒"列为 agent 系统主要威胁之一。本仓当前 `MEMORY.md` 由 Agent 自己写，
内容直接回灌下一场 prompt —— 这是一条**未受控的写入路径**。落法：会话边界由框架做
结构化提炼（已有 `extract_handoff()` 的形态可复用），Agent 的自由文本与框架提炼的
事实分层存放。

---

## 4. 差距清单（ROI 排序）

| 序 | 差距 | 面 | 为什么排在这 |
|---|---|---|---|
| 1 | ~~无评估面，一切改动无法量化~~（首段已落地，剩模型 grader / macro 聚类） | P3 | 其余五项的前置 |
| 2 | 静态 prompt 8709 字符且有重复注入 | P1 | 单点 ROI 最高；改动局限在一个模块 |
| 3 | 技能广告面平铺 103 条、无 L3、有重复簇 | P2 | 每一场每一次请求的固定成本 |
| 4 | 边界在提示词里 | P4 | 风险最高，但报告自己也把它排在 31–60 天 |
| 5 | subagent 共享可写工作区 | P5 | 结构性风险，改造成本中等 |
| 6 | 七种状态文件无契约 | P6 | 长期债，不阻塞 |

---

## 5. 路线图

对齐报告第十二章的三段式，但按本仓的实际依赖重排 —— **P3 必须先行**，否则 P1/P2 的
每一处改动都只能凭体感判断。

**第 0–30 天**
- P3 第一段：`replay.py` + 确定性 grader + 20 条任务卡（含负样本）；用它对现状跑出基线。
- 顺带产出两个免费指标：技能触发率（从 transcript 回放）、每任务 token 成本。

**第 31–60 天**
- P1 上下文分层与单源化，**用 P3 度量收益**（静态段落 token 下降 ≥40% 且行为不劣化）。
- P2 `tools/gen_skill_index.py` + `skills-overlay/` + 对接层路径解析。
- P4 policy-as-code + 动作校验层（`bash_guard.js` 扩展点）。

**第 61–90 天**
- P5 写入纪律（subagent 只读）+ 角色 schema 化 + 每角色 token 记账。
- P6 状态文件契约；记忆写入路径分层。
- P3 第二/三段：模型 grader、pass^k 报告、macro 聚类，接实跑。

---

## 6. 未解问题、有意取舍与不做的事

### 有意取舍（不是遗漏）

1. **worker 容器持 `NET_ADMIN` + `/dev/net/tun`**（`docker-compose.yaml:250-254`）——
   靶场 VPN 必需，削弱容器隔离强度。报告 §8.5 推荐的 MicroVM 在当前威胁模型下
   收益不足，且与"一个 VPN 只应有一条隧道"的舰队拓扑冲突。
2. **技能库不动上游正文** —— 换来的是 `rm -rf` 同步方法可以原样保留，
   代价是索引与覆盖层要放在 `skills/` 之外。
3. **不引 MCP / A2A** —— 与报告 §9.2 一致：同进程内无收益。

### 未解问题

1. **多智能体的收益无法在等 token 预算下比较**（报告 §13.7）。本仓有舰队与 subagent，
   但 P3 落地之前，没有能力回答"这些赢面是不是只是 token 更多"。
2. **委托缺口是信心问题而非能力问题**（报告 §13.1）：无法验证的东西无法委托。
   本仓的 evidence gate 就是在做"可验证性"，但只覆盖了 flag 这一个维度。
3. **记忆投毒无防御**（见 §3.6）；技能 manifest 属供应链面（`skills/` 是上游内联副本，
   有 `PROVENANCE.md` 管溯源，但脚本与 manifest 未审计）。
4. **协议碎片化**：本仓只有 Skills 一个协议面，不构成问题；但若将来接第二个平台适配器，
   报告 §9.3 的三层分工表应作为选型依据。

---

## 附录 A：复现本文数字的命令

**prompt 常量体量**（§2.1）：

```bash
python3 - <<'PY'
import ast, pathlib
t = ast.parse(pathlib.Path(
    "packages/worker/ghost_worker/adapter/taskprompt.py").read_text(encoding="utf-8"))
chr_ = cjk = 0
for n in t.body:
    if isinstance(n, ast.Assign):
        for tg in n.targets:
            if isinstance(tg, ast.Name) and isinstance(n.value, ast.Constant) \
               and isinstance(n.value.value, str) and tg.id.startswith("_"):
                s = n.value.value
                print(f"{tg.id:28s} {len(s):6d}")
                chr_ += len(s); cjk += sum(1 for c in s if "一" <= c <= "鿿")
print("chars total", chr_, "CJK", cjk, f"({cjk/chr_:.1%})")
# token 是估算：中文 ~0.6 token/字、ASCII ~0.25 token/字符
print("token est", round(cjk * 0.6 + (chr_ - cjk) * 0.25))
PY
```

**技能库统计**（§2.3）：

```bash
python3 - <<'PY'
import pathlib, re, statistics
root = pathlib.Path("skills")
sz, first, chars, cjk = [], [], 0, 0
for d in sorted(root.iterdir()):
    p = d / "SKILL.md"
    if not p.is_file(): continue
    sz.append(p.stat().st_size)              # 字节：文件真实体量
    t = p.read_text(encoding="utf-8"); chars += len(t)
    cjk += sum(1 for c in t if "一" <= c <= "鿿")
    fm = t[3:t.find("\n---", 3)]
    m = re.search(r"^description:\s*(.*)$", fm, re.M)
    v = m.group(1).strip()
    if v in (">-", ">", "|", "|-"):          # 上游一律是块标量折行
        blk = []
        for line in fm[m.end():].splitlines():
            if line.strip() == "" or line[:1].isspace(): blk.append(line.strip())
            else: break
        v = " ".join(x for x in blk if x)
    first.append(len(v.split(". ")[0]))      # 字符：L1 首句广告
print("skills", len(sz), "| chars", chars, "CJK", cjk,
      f"({cjk/chars:.2%}) -> token est ~{round(chars/4)} (正文几乎全英文, 按 4 字符/token)")
print("desc first-sentence chars", sum(first))
print("SKILL.md bytes total", sum(sz),
      "median", statistics.median(sz), "max", max(sz))
print("subdirs", sum(1 for d in root.iterdir()
                    if d.is_dir() and any(c.is_dir() for c in d.iterdir())))
PY
```

**兜底名录 XML 体量**（§2.3）：按 `skill_loader.skill_summary_xml()` 的拼装格式
（`name` + 路径 + 首句描述）对同一棵 skills 树复算，得 14,740 字符。注意
`available_skills` 的**默认**提供方是 pi 原生机制而非本函数（见 §2.3 表内注）。

**测试收集数**（§3.3）：

```bash
.venv/bin/python -m pytest -q --collect-only | tail -1
```

---

## 附录 B：本文引用的报告条目

| 本文位置 | 报告章节 |
|---|---|
| §0 对齐表 | 摘要十二条结论 |
| §1.1 | §3.5 context reset |
| §1.2 | §7.2 三家评估方法论 / §7.3 三层最小体系 |
| §1.3 | §8.3 三原则与三层成熟度 |
| §1.4 / §2.3 / §3.2 | §5.2 三层加载 / §5.4 写 skill 的实践要点 |
| §1.5 / §3.3 | §7.2 Trace / Graders / Datasets / Eval Runs；§7.2 macro-eval |
| §1.6 / §3.5 | §10 模型路由；§2.4 反方与修正 |
| §2.1 | §3.6 Claude 5 代的新上下文规则 |
| §2.2 / §3.4 | §8.2 harness 层漏洞；§8.4 分层防御骨架；§8.5 沙箱选型 |
| §3.6 | §6 记忆与状态 |
| §5 | 第十二章 落地路线图（90 天） |
| §6 | 第十三章 风险与未解问题 |
