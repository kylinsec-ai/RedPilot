# 智能体架构选型：把 35 个公开架构裁决到 Ghost

> **输入**：`docs/reference/all-agentic-architectures` —— FareedKhan-dev/all-agentic-architectures
> 的浅克隆，MIT，快照 `cf9d620` / 2026-05-28，35 个 agentic 架构（36 个导出类）的公开实现。
> **该目录被 `.gitignore:33-38` 排除**，故本文所有引用的数字与原文均**就地引全**，
> 不要求读者装得上那份资料。
>
> **姊妹文档**：`docs/architecture/TARGET_ARCHITECTURE.md`（下称 TARGET）管
> 「六个面怎么修」，输入是《智能体工程最佳实践研究报告》即**共识层**。
> 本文管「35 条里哪些该要」，输入是**实现层与实测层**。两文互引，判定口径一致。
>
> **本文的性质**：裁决书，不是综述。每条判定都带本仓 `file:line` 或资料侧可复跑的实测数字。
> 结论若无法被 P3 度量，一律推迟 —— 这是本仓的既有教条（TARGET §3.0：P3 是其余五项的前置）。

---

## 0. 一页纸

### 四条论断

1. **Ghost 的任务形状排除掉这份资料里最流行的那一族。** 搜索型（LATS / ToT）需要
   **动作级的价值函数**，本仓的奖励**粒度停在 flag 不在 action**；投票型
   （Self-Consistency / Ensemble / Debate）在 flag 上不可投票，**且同题双解被显式禁止**；
   上下文分页型（MemGPT）被竞技场的一次 reset 取代。资料自己的 leaderboard 记录了同款
   **pattern-fit failure** —— **用错形状比用错模型更致命**。
2. **「已具备」的清单要重排，真身不是名字对得上的那几个。** 本仓最强的动作层边界是
   `pi_ext/bash_guard.js`（不是提示词）；最强的复核是 `pi_agents/checker.md`
   的**对抗式证伪**（不是泛化的 critique）；而 `compliance.py` **是打码器不是判据检查器**、
   `heimdall` **没有边**。凡「已具备」必须指得出代码对应物，指不出的降级为「部分」。
3. **最该抄的不是任何「架构」，是一条纪律 + 一个形状 + 一次重建。**
   纪律 = `deterministic-picker`（不问 LLM 要数字分）；形状 = Adaptive RAG 的**检索前路由**
   + Self-RAG 的**逐条质控**（= P2 的现成答案）；重建 = 把 `eval_bridge.py` 曾编译好的
   红线判据**在动作面重建并接到执行前检查**（= P4 的答案；原实现已随评估面于 2026-09 拆除）。
4. **唯一真正的新架构是「成功侧记忆」，而它与一条架构红线冲突。**
   本仓有完整的失败侧记忆，**无成功侧**（平台确认后只落 `candidate_sha256`，
   `orchestrator.py:1712-1765`）。仓库里有一具**退役残骸** `adapter/progress.py` ——
   说明这是**试过并主动放弃**，不是没想到。跨题复用又被
   `_remove_retired_owner_state:363` 明令禁止，故 Voyager / AWM 只能以
   **「方法提升进静态技能层」**的受限形态落地。

### 判定表（35 条，详见 §3）

| 判定 | 条数 | 是哪些 |
|---|---|---|
| **采纳** | 8 | Constitutional AI、Self-RAG、Adaptive RAG、Voyager△、AWM△、PEV、SWE-Agent、Dry-Run |
| **已具备** | 7 | Reflection、Agentic RAG、Tool Use、ReAct、Multi-Agent、Blackboard、Reflexive Metacognitive |
| **部分** | 6 | Reflexion、Chain-of-Verification、Episodic+Semantic、Planning、Meta-Controller、RLHF Self-Improvement |
| **拒绝** | 13 | Self-Discover；搜索族 5（Self-Consistency、ToT、LATS、Mental Loop、Ensemble）；Corrective RAG、GraphRAG；Graph Memory、MemGPT；Debate、STORM；Cellular Automata |
| **待度量** | 1 | BrowserAgent / Computer Use |
| | **35** | 合计（附录 A.1 给出计数自洽的复跑命令） |
| **横切纪律** | 1 | `deterministic-picker` —— **不属 35 条之内**，但优先级最高 |

△ = 受限形态，见 §4.4。**计数口径**：资料自述 35 个架构（= 35 个 notebook），
但注册表导出 36 个类（`browser_agent.py` 与 `computer_use.py` 共享 notebook 34）。
本表取 35 为准，把共享 notebook 的两类合并为一行。

### 阅读顺序

- 想知道**为什么大多数架构在这里是错的形状** → §1。
- 想知道**这份资料可信到什么程度** → §2。
- 要**查某一条的判定** → §3。
- 要**开工** → §4（六条落点）与 §5（实验设计）。

---

## 1. 判据：Ghost 的任务形状

架构选型的唯一合法判据是**任务形状**，不是架构的流行度。本节先立形状，再推排除项。

### 1.1 五条特征

初稿对形状的判断有两处**与代码不符**，此处据实修正，并保留修正痕迹 ——
因为「想当然的形状」正是选型出错的主要来源。

| # | 特征 | 核验结论 | 证据 |
|---|---|---|---|
| 1 | 动作真实且不可逆 | **部分成立**：环境**可重置**（额度 6 次 + 终身 6 次 + 600s 冷却）；不可逆只对**提交与计分**成立 | `orchestrator.py:4811-4813`、`:4816-4890` `_restart_target_for_fault` |
| 2 | 奖励稀疏二元 | **✗ 推翻**：`SubmitResult` 带 `awarded` / `cumulative_score` / `correct_flag_count` / `total_flag_count` / `matched_flag_index`；另有**扣分 hint**；N-of-M 进度驱动止损 | `adapter/platform/tsecbench_sdk.py:140-144`、`stoploss.record_platform_progress:600` |
| 3 | 长时程 | **✓**：4000s/题、8 场会话/题、21300s/轮、轮次时间盒递增 | `adapter/config.py:275-288` |
| 4 | 题目内部无先验 | **✓**（降级表述）：解题方对题目内部无先验，由未知类别 fail-closed + 跨题隔离红线支撑 | `verify.flag_evidence_policy`（未分类题目收紧）、`taskprompt.py:192 _ISOLATION_CONSTRAINT` |
| 5 | 可跨题复用 | **✗ 推翻**：**运行时产物被架构禁止跨题** | `orchestrator.py:363` docstring 原文 *"The architecture no longer permits cross-challenge answer reads"* |

### 1.2 修正后的排除逻辑反而更锋利

第 2 条被推翻**不是**利空，而是把排除项说得更准：

> **奖励不是没有，是粒度停在 flag，不在 action。**
> `matched_flag_index` 是 flag 级索引，不是动作级的价值估计。

搜索型架构要的从来不是「有没有奖励」，而是**「中间状态能不能被打分」**。
LATS 的 UCB1 选择、ToT 的 beam 排序，都要求对**部分解**给出价值 ——
本仓结构上给不出：一个 flag 级信号无法回传成动作树上的 reward backup。
**于是 LATS / ToT 的排除理由从「不可回滚」升级为「无动作级价值函数」，
后者更硬，因为前者可以用沙箱缓解，后者不能。**

第 5 条推翻则直接改写 §4.4 的可行性边界：

> **能跨题复用的只有两样：静态技能库 `skills/`（只读）与 harness 代码本身。**
> 任何运行时产物（账本、黑板、记忆、转录）都按 `work/<code>/` 隔离。

这条与 A8 的舰队分片是同一枚硬币 —— **分片 + 隔离 + 禁止同题双解是同一套设计**，
见下条。

### 1.3 由此推出的排除项

| 被排除的族 | 排除理由（可核对） |
|---|---|
| 搜索型（LATS / ToT / Mental Loop） | 无动作级价值函数；且 Mental Loop 依赖 LLM 打分数值，正是 flat-band 高危区（§2.3） |
| 投票型（Self-Consistency / Ensemble / Debate） | flag 是不可投票的字符串；**同题双解被显式禁止** —— `effective_best_of = 1`（`orchestrator.py:6223`），配 warning `:6229`。`ADAPTER_BEST_OF` 配置项存在但设了就只打一条日志 |
| 分页型（MemGPT） | 竞技场每场是**全新进程 + 全新上下文**（`create_solver` 在会话循环内调用 `:5159`，`subprocess.Popen` 在 `pi_agent.py:946`），一次 reset 强于 paging |
| 通用推理模块型（Self-Discover） | 本仓的知识面是 103 条领域技能，不是 16 个可组合的推理模块 |
| 需联网检索型（Corrective RAG） | 与 `_OFFLINE_CONSTRAINT`（`taskprompt.py:178`）冲突 |
| 图结构型（Graph Memory / GraphRAG） | 本仓的观察台账**零条边**（`heimdall.py:240`），要先有图才谈得上图查询 |

**一个必须写明的推论**：资料自己的 leaderboard 记录了同款 pattern-fit failure ——
`LATS` 在 `math_word` 上失败（搜索型用在没有价值函数的地方）、
`Debate` + `Ensemble` 在 `trick_logic`（Sally 三兄弟逻辑题）上失败（群体思维）、
`Reflexion` + `AWM` 在 `stateful_recall` 上失败（存的是反思与策略，不是事实）。**它证明的不是这些架构不好，
而是它们的形状与那类任务不匹配。** 本节的六条排除项是同一种推理，只是换成 Ghost 的形状。

---

## 2. 资料层：它能证明什么，不能证明什么

### 2.1 它是什么

一个 `pip install` 得上的 Python 包 + 35 个 Jupyter notebook + 一套 mkdocs 站点。
全部架构基于 **LangGraph 状态机**，共用同一份 `Architecture` 抽象基类
（`build()` 返回 `CompiledStateGraph`，`run(task)` 返回 `ArchitectureResult`），
换类即换模式。支持 9 家 provider，默认 Nebius。

**本仓不抄它的代码，只抄形状** —— 本仓的编排是 `orchestrator.py`（7079 行），
引入 LangGraph 是换地基不是抄形状。见 §6.1。

### 2.2 哪些是真跑的

这是这份资料最有价值的部分。`docs/benchmarks.md` 的表头原文是：

> Generated by `benchmarks/run_benchmark.py` over **36 architectures** × **17 tasks** = **42 attempts**.

（42 而非 612，是因为每个架构只跑它适用的子集，不适用的格子记 `—`。）
运行条件与总成绩在 `README.md`：

> A 17-task suite runs every architecture and scores results. Most recent run,
> **real Nebius Llama-3.3-70B, ~25 min, ~$1.50** in tokens — **33 / 42 correct (78%)**.

**它的可信度来自它没有粉饰失败格**：

- `CellularAutomata` 硬报错 `❌`：`[ValueError: Initial grid has 5 rows, expected 3]`；
- `LATS` 在 `math_word` 列失败（**0/1**，11.8s）—— 搜索型架构用在了没有价值函数的地方；
- `Debate`（10.7s）与 `Ensemble`（17.9s）双双在 `trick_logic` 列失败
  —— 该题是 Sally 三兄弟逻辑题，**群体思维的反面教材**；
- `Reflexion`（32.5s）与 `AgentWorkflowMemory`（28.4s）双双在 `stateful_recall` 上失败
  —— 该题是「What is my favourite colour?」，主 prompt 前先跑 2 个 setup prompt，
  **考的是跨轮记住事实**；而 Reflexion 存的是对失败的反思、AWM 存的是**策略配方**，
  两者都不是事实 —— **记忆形状不对**。对照：同一题 `MemGPT` ✓（13.9s）、
  `EpisodicSemanticAgent` ✓（8.9s），即**存事实的那两个架构通过了**；
- **延迟差两个数量级**：`Blackboard` 302.3s / `PEV` 140.1s / `MultiAgent` 90.6s，
  而 `Voyager` 5.0s / `ReflexiveMetacognitive` 1.4s。

**但不能当成本仓的预测。** 它的 17 个任务是通用形状（math / logic / rag / writing /
research / memory / retrieval），**不含任何安全攻防任务，不含任何不可逆动作，
不含任何终局确定性判据**。它对本仓的价值是**排除项的证据**与**形状的参考**，
不是通过率的参考。

### 2.3 它最重要的技术结论：deterministic-picker

`docs/tutorials/deterministic-picker.md` 记了一条**实测病态**，值得整段引：

> 让 Llama-3.3-70B 在 1–5 或 1–10 上打质量分，无论 rubric 多严，
> 输出都塌缩到同一档：`sample 1: 4/5`、`sample 2: 4/5`、`sample 3: 4/5`、`sample 4: 4/5`。
> **依赖分数做选择的架构（beam search / MCTS / 排序检索 / 接受-拒绝循环）因此变成任意的。**

解法是**不问数字，问分类特征**，再由 Python 合成决策信号：

```python
class _EditorCritique(BaseModel):
    is_on_brief: bool          # LLM 承诺一个 bool，不是一个数字
    word_count: int
    has_concrete_imagery: bool
    avoids_cliches: bool
    is_engaging: bool

def _composite_score(features, wc_range) -> int:
    score  = 4 * features["is_on_brief"]
    score += 2 if wc_range[0] <= features["word_count"] <= wc_range[1] else 0
    score += 2 * features["has_concrete_imagery"]
    score += 1 * features["avoids_cliches"]
    score += 1 * features["is_engaging"]
    return score  # 0-10，有真实离散度
```

> 5 个独立布尔值无法像 1 个数字那样被压平。

**这条与 Ghost 的关系是本份研究中最直接的一处**：本仓的
`FlagEvidencePolicy` + `flag_confidence()` 就是同一条哲学的一个实例
（LLM 不产出决策值，Python 产出），但**只用在了 flag 一个维度上**。
资料侧统计 13 个架构应用了这条纪律，另有 9 个「架构上免疫」（本就没有 LLM-as-Scorer 步骤）。

**它必须先于 P3 第二段的模型 grader 落地** —— rubric 打分正是 flat-band 的高危区。
见 §4.1、§5 的 E4。

---

## 3. 全量裁决：8 族 35 条

判定四档沿用 TARGET 的口径（做到 / 部分 / 相反 / 缺失），但用词对齐本文：
**已具备**（找到代码对应物）/ **采纳**（该抄且已定落点）/
**部分**（有雏形但缺关键一步）/ **拒绝**（形状不匹配）/ **待度量**（信息不足，须 P3 说话）。

### 族 1 · Reasoning & Reflection

| # | 架构 | 判定 | 理由与证据 |
|---|---|---|---|
| 01 | Reflection (generate→critique→refine) | **已具备** | `adapter/pi_agents/checker.md:8` 原文「你的任务是**证伪它**，而不是确认它」，判定 CONFIRMED / NOT_CONFIRMED / PARTIAL，且**硬性要求自己重跑并附命令原文**（`:38-41`）。这是**生成者/评估者分离**，强于资料里同上下文自省的 critique |
| 18 | Reflexion（失败反思入 episodic memory） | **部分** | 失败侧四件套齐备：`.continuation.json`（`_CONTINUATION_FILENAME`，只收计数与枚举标签，明确无答案）、`tried_commands.md`（`_persist_tried_commands`，命令原文去重表**无结果列**）、`.rejected_flags`（只给 sha1 指纹，回灌见 `taskprompt.py:520-544`）、`.unverified_flags`。**成功侧缺失** → §4.4 |
| 20 | Chain-of-Verification | **部分** | 证据闸门是其确定性版本，但 CoVe 的关键一步 ——**「不看 baseline 独立作答」**——未被显式编码。本仓的复核者拿到的是「已被声称的发现」，隔离的是**上下文**而非**结论** |
| 19 | Self-Discover | **拒绝** | 16 个通用推理模块 vs 本仓 103 条领域技能。知识形状不同 |
| 32 | Constitutional AI | **采纳** | 判据曾写好过一份（`eval_bridge.py:73 EGRESS_RULES`，7 条正则把 `_OFFLINE_CONSTRAINT` 点名禁止的 `apt/pip/npm/git clone/docker pull`（外加自选的 `gem/cargo`） 编译成谓词），且**只报告、不拦截**（其自陈是「真实的债务，不是设计选择」）。该实现已随评估面于 2026-09 拆除，需在原地重建 → §4.5 |

### 族 2 · Sampling & Search

| # | 架构 | 判定 | 理由与证据 |
|---|---|---|---|
| 21 | Self-Consistency | **拒绝** | 需「同题多采样 + 聚合投票」两个要素，本仓**两个都没有**：采样是不同题（分片，见下），聚合由平台单次判正。且**同题双解被显式禁止**：`effective_best_of = 1`（`orchestrator.py:6223`）+ warning `:6229` |
| 09 | Tree of Thoughts | **拒绝** | 要求分支可回滚 + 对部分解打分。动作不可回滚（§1.1 第 1 条），无动作级价值函数（§1.2） |
| 22 | LATS | **拒绝** | MCTS 的 reward backup 需要中间状态的价值估计；本仓奖励粒度停在 flag。**资料自己的 leaderboard 记录了 LATS 在算术题上的 pattern-fit failure** |
| 10 | Mental Loop | **拒绝** | 核心是「对 K 个候选动作心内模拟再打分」—— 打分环节正是 §2.3 的 flat-band 高危区。除非改用分类特征，否则整条不成立 |
| 13 | Ensemble | **拒绝** | 同上不可投票；**且资料自身 leaderboard 记录它因群体思维在逻辑题上失败** |

> **核验补充（舰队不是 best-of-N）**：worker-2/3 是**分片**不是冗余 ——
> 分片键 `crc32(unique_code) % (count-1)`（`orchestrator.py:2254-2289`，退回轮转 `:2286-2288`），
> 由 per-challenge `flock` 租约（`:252-274`）与跨 worker 活跃守卫（`:568`）**禁止双解**。
> 该守卫的注释写明理由：防两个 worker 并发写同一 workdir / `.pi-home` 互相污染并重复烧 token。
> **因此不能说「舰队相当于 Self-Consistency 的并行采样」。** 真正的 N 是
> **跨轮次的时间序重试**（`schedule_rounds` 多轮 + `stoploss.revive:755`，冷却默认 3600s），不是并行。

### 族 3 · Retrieval / RAG

| # | 架构 | 判定 | 理由与证据 |
|---|---|---|---|
| 23 | Agentic RAG | **已具备** | `ADAPTER_SKILL_AGENT` 默认开，pi 自主决定 read 哪个技能；`skill_loader.py:6` 明写「框架不再自己挑技能（2026-09-16）」 |
| 24 | Corrective RAG | **拒绝** | fallback 到 web 与 `_OFFLINE_CONSTRAINT` 直接冲突 |
| 25 | Self-RAG | **采纳** | 逐条质控形状（per-doc 分类标签 → Python keep/drop）正对技能重复簇 → §4.2 |
| 26 | Adaptive RAG | **采纳** | **检索前**按复杂度三分类路由 = P2 的现成答案 → §4.2 |
| 27 | GraphRAG | **拒绝** | 需先有图。本仓的观察台账是**扁平节点表**（`heimdall.py:240`：`{version, nodes, tensions, session}`，node 有 `id/kind/text/why/status/first_seen/last_seen/miss`），**没有边**。「图」这个字要收回 |

### 族 4 · Memory

| # | 架构 | 判定 | 理由与证据 |
|---|---|---|---|
| 08 | Episodic + Semantic | **部分** | 有分层雏形（MEMORY / 黑板 / 四本账本 / 续接检查点分离），但**无向量检索层、无语义层**。资料侧的记忆选型决策树（`docs/tutorials/memory.md`）对本仓的映射见 §3 末 |
| 12 | Graph Memory | **拒绝** | 同 GraphRAG：无图，且它是默认关的旁路（`ADAPTER_HEIMDALL` 默认 `"0"`，`orchestrator.py:3618`；`_heimdall_init:3621` 拿不到 LLM 就直接不启用）。唯一消费方是只读控制台（曾为 `fastapi-console/.../services.py:432`，该目录已于 2026-09 删除，**现无任何消费方**），评估面不消费 |
| 31 | MemGPT | **拒绝** | 竞技场的一次 reset 强于 paging；报告侧也承认 compaction 消除不了 context anxiety |
| 29 | Voyager | **采纳（受限）** | 可复用技能库 + 课程。**硬约束：产物不跨题** → §4.4 |
| 35 | Agent Workflow Memory | **采纳（受限）** | 存「策略配方」而非事实或代码；与 Voyager 合流 → §4.4 |

> **核验补充（失败侧的确切边界）**：`_multiflag_hint_*`（`orchestrator.py:742/:756/:776`）
> **不是文件**，是函数，且**刻意不持久化 hint 正文**（`:773`）。它不是成功侧记忆。
> `MEMORY.md` 是**混合容器**：`_merge_memory`（`:3862-3889`）在
> `<!-- driver-memory -->` 固定段内**幂等替换**，agent 自写笔记原样保留，注入时截断 4000 字符
> （`taskprompt.py:340`）。里面装的是黑板摘要 + 状态，**没有「上次这样打成功了」的条目**。

### 族 5 · Tools & Actions

| # | 架构 | 判定 | 理由与证据 |
|---|---|---|---|
| 02 | Tool Use | **已具备** | 单工具 + runaway 上限，由 pi 引擎承担，框架侧不重复实现 |
| 03 | ReAct | **已具备** | pi 的 Thought→Action→Observation 就是它（`adapter/solver/pi_agent.py`） |
| 04 | Planning | **部分** | 有任务卡与策略层，**无显式 replan 契约** —— 重新规划靠换一场会话，不是循环内的显式节点 |
| 06 | PEV (Plan-Execute-Verify) | **采纳** | 每步执行后独立验证 + 失败重试，正对「动作可能静默失败」→ §4.3 |
| 33 | SWE-Agent (ACI) | **采纳** | 工具面收窄（`Literal[list, read, write, run_check, answer]`）+ **路径逃逸拒绝** → §4.3 |
| 34 | BrowserAgent / Computer Use | **待度量** | 需先知道 web 题型占比；引入 Playwright 是对求解链路的**大改**，不能凭直觉上 |

### 族 6 · Multi-Agent

| # | 架构 | 判定 | 理由与证据 |
|---|---|---|---|
| 05 | Multi-Agent (Supervisor) | **已具备** | 三角色 scout / worker / checker（`pi_agent._install_subagents`），且有**强制派发规则**：未解场次 ≥2 且上一场零新 flag → 第一条回复必须是 subagent 调用（`orchestrator.py:5046-5068`）。角色模型路由 `pi_agent._inject_role_model` |
| 07 | Blackboard | **已具备** | `adapter/blackboard.py`：机械**正则**抽取（`_IP_PORT_RX:92` / `_SERVICE_RX:93` / `_CRED_RX:101`），由 driver 在会话**返回后**跑（`_observe_qualified_tool_facts`，由 driver 在会话返回后跑），主 Agent 会话期间不占注意力；`actionable_assets():301` 以插入序取前 5 条回灌（`taskprompt.py:492`）。**不是 HEARSAY-II 式投标制**：`confidence` 是插入时按类别写死的常量（network 0.8 / service 0.7 / credential 0.6，`:241/:248/:257`），全仓**无一处按 confidence 排序或仲裁** |
| 28 | Debate | **拒绝** | flag 判定是确定性的（平台说了算），辩论无增益；资料自身 leaderboard 记录其群体思维失败 |
| 30 | STORM | **拒绝** | 多视角检索 + 写文章，与本仓形状无关 |
| 11 | Meta-Controller | **部分** | 分类已有（`orchestrator.py:917 _infer_category`），但正确的路由目标是**策略**不是架构 → 并入 §4.2 |

> **黑板的两个必须写明的边界**（核验所得，比「机械抓取」更准）：
> ① **截断窗口**：IP / 服务只扫 `output[:2000]`（`:236/:245`），凭证只扫 `output[:3000]`（`:252`）
> —— 大输出尾部的凭据**系统性丢失**，这是真实的能力上限；
> ② **flag 被刻意排除在黑板外**（`:260-264`），注释写明理由：未确认的 flag 留在黑板里
> 会让「重复本地读取」看起来像新事实，把停滞题无限续命。

### 族 7 · Safety & Routing

| # | 架构 | 判定 | 理由与证据 |
|---|---|---|---|
| 14 | Dry-Run | **采纳** | 「propose → 模拟影响 → Python 不可逆性硬上限 → 审批门」。**去掉审批门**（无人值守），保留前两步 → §4.3 |
| 17 | Reflexive Metacognitive | **已具备** | `_adaptive_session_limits:662-712`：按 session_idx 与难度分档，首场强制 5 分钟 / 20 turns 快速探索，上一场有新事实才放宽一档，无事实则短场 + `pivot-after-no-progress` 标记。**比资料里的四分类更细**。但**无 escalate 语义** → §4.6 |

### 族 8 · Specialty

| # | 架构 | 判定 | 理由与证据 |
|---|---|---|---|
| 15 | RLHF Self-Improvement | **部分** | 名不副实（非真 RLHF，是确定性多维打分 + 优质输出归档）。其形状就是 P3 grader 的产物；确定性打分这一半是本仓强项 |
| 16 | Cellular Automata | **拒绝** | 林火 / 舆情模拟，与本仓形状无关。资料自己的 leaderboard 里它是唯一硬报错的一条 |

### 横切 · 纪律

| # | 纪律 | 判定 | 理由 |
|---|---|---|---|
| — | **deterministic-picker** | **采纳（最高优先）** | §2.3。与 `FlagEvidencePolicy` 同源但只用在一处；**是 P3 第二段模型 grader 的前置** → §4.1 |

---

## 4. 重点深挖的六条

### 4.1 deterministic-picker：纪律层，先于模型 grader

| | |
|---|---|
| **落在哪个面** | P3（评估）+ P4（动作），横切 |
| **落点文件** | `adapter/verify.py`、`adapter/stoploss.py`。（原另举 `eval/graders/deterministic.py:33` 的 `Predicates` 注入协议为「已经是它的事例」—— 该文件已随评估面于 2026-09 拆除，若重做需重建该协议） |
| **要抄的形状** | 凡有「picker」（排序 / 打分 / 选择）的地方：① 识别分类特征；② 用严格类型 Pydantic schema 化（`bool` / bounded `int` / `Literal[...]`）；③ **Python 合成决策信号**；④ LLM 的数值输出（若有）只进 trace，**绝不作决策值** |
| **为什么最优先** | P3 第二段要做「模型 grader + rubric」。rubric 打分**正是** §2.3 记录的病态场景。纪律先落地，模型 grader 才不会建在沙子上 |
| **验收** | 见 §5 的 E4：**分数的离散度**。若 rubric 分数塌缩到同一档，纪律没生效 |

**本仓已经有的两个好事例**（可直接当模板）：`FlagEvidencePolicy` 把证据判定拆成
「来源边界 + 取证判定」两层由 Python 合成；`_adaptive_session_limits` 把「该给多少预算」
拆成分档规则而非让 LLM 报一个数字。

### 4.2 技能面的形状：预路由 + 逐条质控 → P2

| | |
|---|---|
| **落在哪个面** | P2 技能面 |
| **现状（TARGET §2.3 已测）** | 103 个技能全扁平、**零个有子目录**；L1 首句广告合计 4427 字符；SKILL.md 总量 1,141,974 字符（≈28.5 万 token）；**存在真实重复簇**（`business-logic-vuln` 1.2KB × `business-logic-vulnerabilities` 31.6KB，体量差 26 倍）；**没有负样本** |
| **要抄的形状** | **Adaptive RAG 的检索前路由**（按复杂度三分类：no / single / multi → 决定检索与否）+ **Self-RAG 的逐条质控**（per-doc 分类标签 → Python keep/drop） |
| **具体落点** | `skills-overlay/{INDEX.md, overlay.yaml}` + `tools/gen_skill_index.py`（皆 TARGET §3.2 已规划）；与 `orchestrator.py:917 _infer_category` 合流 |
| **必须同时改两处** | 技能面有**两条**上料路径：软链进本题 HOME（`pi_agent._install_skills:710`）与 `pi_agent._build_cmd:833` 的 `--skill <dir>`。**只改一处会漏**（TARGET §2.3 复核已记） |

**核验新增的三条实测**（本份文档首次记录）：

1. 「首句」的实现是 **`partition(". ")` 朴素切分**（`skill_loader.py:145`），不是语言学首句。
   实测 103/103 条描述都含 `. `，故切分确实生效 —— 但 `hack` 的描述被从中间切断。
2. **负样本不是「完全没有」，是「唯一一条恰好被切掉」**：全库仅 **1/103** 条描述含负向约束
   （`skills/attack-surface-mapping/SKILL.md` 的 *"Do not open with directory brute or payload spray."*），
   而它位于第一个 `. ` **之后**，被 `partition(". ")` 丢掉。
   **所以负样本缺失的责任一半在数据源、一半在截断实现** —— 修 P2 时两处都要动。
3. 名录侧只在**兜底路径**成立。默认路径（`ADAPTER_SKILL_AGENT=1`）下框架**什么都不注入**，
   由 pi 的原生 `<available_skills>` 承担。`skill_summary_xml():134` 是
   `ADAPTER_SKILL_AGENT=0` 时的退路。

**验收**：E1（§5）—— 触发率 / 误触率 / 静态 prompt token。P3 曾有
`datasets/skill_routing.json`（20 张卡，explicit / implicit / noisy / negative 各 5），
**恰好就是为这件事准备的** —— 已随首切片于 2026-09 拆除，重做 E1 时需一并重建。

### 4.3 动作面的形状：Dry-Run + PEV + ACI 合成 → P4

| | |
|---|---|
| **落在哪个面** | P4 动作与安全面 |
| **落点文件** | `redpilot/worker/adapter/pi_ext/bash_guard.js`（**框架侧唯一的动作层改造点**） |
| **要抄的形状** | 三条合流：**Dry-Run**（动作按不可逆性分级 → Python 硬上限）、**PEV**（每步执行后独立验证）、**SWE-Agent ACI**（工具面收窄 + 路径逃逸拒绝） |
| **必须去掉的** | 资料里 Dry-Run 的**人工审批门**。无人值守基准测试没有人在环 —— 这条不适用，不是遗漏 |

**本仓已有的底座比想象的厚**（核验所得）：`bash_guard.js` 是**全仓唯一**把边界放在动作
而非提示词上的东西，且已经有三道闸：

- 自引用重定向拦截（`PI-SAFETY-INTERCEPTED`，`:160`）—— 起因是 2026-09-04 磁盘爆到 307GB 的事故；
- 强制 `timeout -k 15 -s KILL` + `ulimit -f` 单文件上限（`:169-170`）—— 兜底长命令与写爆文件；
- **单场重复命令熔断**：同命令 ≥ `REPEAT_LIMIT`（默认 3，`:32`）次则短路返回
  `[PI-SAFETY-REPEAT]`（`:137-144`），**跨会话计数持久化**到 `.bash_guard_state.json`（`:41`），
  首见命令实时 append 到 `tried_commands.md`。

**P4 要加的不是「加一道闸」，是给这道闸补上「不可逆性分级」这一维** —— 现有三道闸管的都是
**资源与重复**，没有一维管**动作的破坏性**。

### 4.4 成功侧记忆：受限形态 → P6

| | |
|---|---|
| **落在哪个面** | P6 记忆与状态面（+ P2 的 `skills-overlay/` 作为写入目标） |
| **要抄的形状** | **Voyager**（可复用技能库）+ **Agent Workflow Memory**（策略配方）合流：从成功的 run 里提炼「方法」，而不是记「答案」 |
| **硬约束** | **产物不跨题** —— `orchestrator.py:363` docstring 原文 *"The architecture no longer permits cross-challenge answer reads"*。因此唯一合规的形态是：**把验证过的方法提升进静态技能层**（`skills-overlay/`），运行时隔离红线保持不动 |
| **数据结构** | 复用 P3 已落地的 `runs` + `transcript` + `fold_rows`；与 `ghost_contracts/paths.py` 的状态文件契约（TARGET §3.6 已规划）合流 |

**这为什么不是「照抄 Voyager」**：Voyager 的技能是**可执行 Python**，靠
subprocess 真执行 + self-verification 来确认有效。本仓的**结构性优势**在于
**平台回 `correct` 是确定性的** —— 学到的配方天生**被验证过**，
不需要自我验证这一环。这是本仓相对资料库的独有能力，也是
`_record_confirmed_submission:1712-1765` 已经在收的数据（它现在只落了
`candidate_sha256` 与计数，**不记手法** —— 缺的就是「手法」这一列）。

**必须正面处理的历史教训**：`adapter/progress.py` 是一具**退役的成功侧残骸**：

- `ChallengeProgress.save_attempt(tools_used, findings, dead_ends, next_steps):24` 会写 `_progress.json`；
- `build_resume_prompt():67` 会生成「### 已发现信息 ✓ ... ### 建议的下一步」——**这就是成功侧**；
- `_update_memory_md():97` 用 `open(..., 'w')` **整体覆写** MEMORY.md。

**它是死代码**：`orchestrator.py:38` import 了 `ChallengeProgress` 与
`extract_progress_from_result`，全仓**零调用点**。而它的覆写行为与
`_merge_memory` 的不变量**直接冲突** —— `:3864` 的注释记录的正是这次实测污染
（「driver 若整体覆写会把结构化进展冲掉」，长会话记忆被污染成黑板噪音）。

> **所以这一条不是「本仓没想过成功侧记忆」，是「试过、主动放弃、且放弃的理由今天仍然成立」。**
> 新方案必须解释清楚它凭什么不会重蹈覆辙：答案是**写入目标不同** ——
> 残骸写的是**运行时产物**（`_progress.json` 与 MEMORY.md，逐题、被隔离红线管），
> 新方案写的是**静态技能层**（跨题、只读、经 P2 的路由层分发）。

**验收**：E3（§5）—— `pass^k` 跨题迁移。**风险**：配方过度特化
（资料侧 `build_29..35` 的 Failure 表把「工作流过度特化」列为成熟失败模式）。

### 4.5 判据重建：Constitutional AI 的真身 → P4

| | |
|---|---|
| **落在哪个面** | P4 动作与安全面 |
| **要抄的形状** | Constitutional AI 逐条规则 pass/fail → Python `all()`。**本仓要做的不是发明判据，是把判据接进动作面**（判据本身需先重建 —— 见下沿革） |
| **判据曾存在** | `adapter/eval_bridge.py:73 EGRESS_RULES` —— 7 条正则，把 `_OFFLINE_CONSTRAINT`（`taskprompt.py:178`）点名禁止的 `apt/pip/npm/git clone/docker pull`（外加自选的 `gem/cargo`） **编译成可执行谓词**；`TaskPredicates.is_offline_violation():184` 是两段式判据（规则表 + 授权主机范围）；消费方曾是 `graders/deterministic.py:230` 的 `_offline` 判据。**该文件已随评估面于 2026-09 拆除**（最后存在于 `a3ea29c`） |
| **缺的一步** | **它只报告、不拦截。** 它曾自陈第 ② 段规则表是「真实的债务，不是设计选择」—— 这是拆除前的原话，重做时同样成立 |
| **要做的动作** | 重建同一份判据，并接进 `bash_guard.js` 的执行前检查 —— 让它成为动作面的判据 |
| **先例** | `_ProgressEvidenceGate`（`orchestrator.py:3057`）已经演示了「同一套 provenance 规则、第二个消费点」的模式。**这是本仓内部的成功范式，不是外来概念** |

**为什么这才是 P4 的正解**：TARGET §2.2 的判定是「安全边界写在提示词里，而不是放在动作上」，
并列出报告五层骨架里本仓有三层是「无」。但**第 3 层（动作校验）的判据曾写好过一份**，
只是装在了评估面。

> **沿革（2026-09-18）**：本节原写「P4 的最低成本路径不是新建判据体系，是**搬迁**」，
> 前提是判据还在 —— 它已经不在了（见上表「判据曾存在」），故路径变回「新建」。
> 值得带走的只有一条教训：**把动作面的判据寄存在读侧，读侧一被清理，判据就一起没了。**
> 判据该住在动作面，评估面只是它的**第二个**消费点。

`_ISOLATION_CONSTRAINT`（`taskprompt.py:192`）同理 —— 它现在有两条注入路径
（写进每题 `CLAUDE.md` 与直接 append 进 prompt，TARGET §2.1 已点名重复），
判据接进动作面后，提示词里只留一句「这条被强制了，不是请求」。

**验收**：E2（§5）—— 违禁动作**执行前**拦截率。

### 4.6 止损的 escalate 语义 → P5 / P6

| | |
|---|---|
| **落在哪个面** | 调度层（P5 多智能体）+ P6 |
| **现状（核验修正）** | `stoploss.should_stop:630-705` 是**单题、五维、纯谓词**的止损器（时间预算 / 会话数 / 无新事实干旱窗口 / 连续 0 flag / 连续不可达），只产出 `(stop, reason)`，**本身不含任何跨题资源重分配** |
| **但「只有停」不准确** | 资源重分配**存在，在调度层**：stop → `outcome: "dropped"`（`:4526`/`:4619`/`:4947`）→ `dropped.add(code)` → 轮次循环取下一题；派发顺序由 `_prioritize:995-1014` 决定（单 flag 题优先、组内低分优先、同分 easy→hard）；周期复活 `stoploss.revive:755`（冷却 `ADAPTER_REVIVE_COOLDOWN` 默认 3600s） |
| **要补的形状** | 把「停」的收益**显式归给别的题** —— 即资料里 Reflexive Metacognitive 的 `escalate` 语义，与舰队优先级队列合流 |
| **另一个必须写准的事实** | 止损状态是**跨 worker 共享**的（`workdir/<code>/.stoploss.json`，锁在 `workdir/.stoploss-locks/`，`stoploss.py:143-154`；`should_stop` 末尾 `:703-705` 明说「把别的 worker 最新计数作为快照保存」）。**它是舰队级共享预算账本，不只是单进程自省** —— 这一点决定了 escalate 应该在舰队层面而非进程内实现 |

---

## 5. 可比实验设计（P3 重建后）

**前提约束**：对齐 TARGET §6 未解问题 1 ——「多智能体的收益无法在等 token 预算下比较」。
**任何架构对比必须先固定 token 预算**，否则赢面分不清是架构带来的还是 token 更多带来的
（报告侧数字：Anthropic 内部评测多智能体高 90.2%，但 token 约 15 倍；
BrowseComp 上 token 量单独解释 80% 的性能方差）。

| 实验 | 对照 | 主指标 | 数据源 | 是否需实跑 |
|---|---|---|---|---|
| **E1 技能路由** | 平铺 103 条 vs 预路由 + 逐条质控 | 触发率 / 误触率 / 静态 prompt token | `replay.py` + `datasets/skill_routing.json`（四类各 5，含负样本）——**两者已随首切片于 2026-09 拆除，需重建** | 否（回放即可） |
| **E2 判据重建** | 仅报告 vs 执行前拦截 | 违禁动作**执行前**拦截率 | 事件行 + `bash_guard` 状态 | 否（历史轨迹可判） |
| **E3 成功侧记忆** | 无配方库 vs 有 | `pass^k`（原 `DEFAULT_K=3`，**已随首切片拆除，需重建**） | `runs` + 平台判 correct 的 run | 是 |
| **E4 grader 纪律** | 数值打分 vs 分类特征合成 | 分数**离散度**（flat-band 是否发生） | P3 第二段 rubric 输出 | **否（只需 rubric 历史输出）** |

**方法（评估面重建后）**：全部走其 `report` 的确定性聚合，**零 LLM 零网络** ——
该链路（回放 → 判据 → 报告）曾由 `tests/test_eval_end_to_end.py` 验证过；
**该测试与整条链路已于 2026-09 一并拆除**（最后存在于 `a3ea29c`）。
**E4 须待 P3 第二段落成后才能做**（它的数据源是 rubric 输出，目前不存在），
届时它会是四条里成本最低的一条。

**产出**：一张基线表进本文附录，附**复跑命令**（沿用 TARGET 附录 A 的体例 ——
本文的每一个数字都应该能被读者重跑出来）。

---

## 6. 不做的事（有意取舍，不是遗漏）

1. **不引入 LangGraph，不移植资料库的代码。** 资料是 LangGraph 状态机，
   本仓编排是 7079 行的 `orchestrator.py`。**抄形状，不抄依赖** ——
   引入 LangGraph 是换地基。
2. **不做架构对比的实跑。** 本文只给实验设计（§5）；E3 需要靶场与模型凭据，
   本文不假装跑过。
3. **不推翻竞技场与证据闸门。** 二者在 TARGET §1「六处已做对」之列，
   且在资料库里找不到对手 —— 它们是基线不是选项。§4.5 只**搬判据**，不改**判决**。
4. **不给无关项留篇幅。** STORM / Cellular Automata / Self-Discover 只占一行判定。
5. **不改 `ADAPTER_BEST_OF` 的语义。** 同题双解被禁是既定决策；
   本文只是把它作为「Self-Consistency 不适用」的**证据**引用。
6. **不声称 35 条已经穷尽了智能体架构。** 这份资料本身缺 swarm / 层级多 agent /
   市场机制（其 README 提到的 Hierarchical 只在对比表里出现，未实现）。
   本文裁决的是**资料里有的 35 条**，不是「所有架构」。

---

## 附录 A：复现本文数字的命令

**A.1 判定表计数自洽检查**（§0 与 §3 —— 四档相加必须等于 35）：

```bash
cd /home/kali/RedPilot
python3 - <<'PY'
import re
from collections import Counter
t = open('docs/architecture/AGENT_ARCHITECTURE_SELECTION.md', encoding='utf-8').read()
rows = re.findall(r'^\| ?\d\d ?\| .+? \| \*\*(已具备|采纳|部分|拒绝|待度量)[^*]*\*\*', t, re.M)
c = Counter(rows)
for k in ("已具备", "采纳", "部分", "拒绝", "待度量"):
    print(f"  {k}: {c[k]}")
print("总计 =", sum(c.values()), "(应为 35)")
PY
```

**A.1b 「已具备」必须指得出代码对应物**（指不出的降级为「部分」）：

```bash
cd /home/kali/RedPilot
for p in "redpilot/worker/adapter/pi_agents/checker.md" \
         "redpilot/worker/adapter/blackboard.py" \
         "redpilot/worker/adapter/pi_ext/bash_guard.js" \
         "redpilot/worker/adapter/stoploss.py" \
         "redpilot/worker/adapter/skill_loader.py"; do
  # 注：本清单原有 `eval/graders/deterministic.py` 与 `eval/replay.py` 两条，
  # 2026-09 随评估面拆除后移除（最后存在于 a3ea29c）。
  [ -e "$p" ] && echo "OK   $p" || echo "MISS $p"
done
```

**A.2 三条强提旁路**（§0 论断 2、§4.5 引用的「证据闸门并非无旁路」）：

```bash
cd redpilot/worker/adapter
grep -n "require_remote=False" orchestrator.py          # → :5538（会话后强提）
sed -n '4303p'   orchestrator.py                        # → eager 强提 conf>=0.50
sed -n '5556p'   orchestrator.py                        # → invalid_format 翻案
```

**A.3 同题双解被禁**（§1.3、§3 族 2）：

```bash
cd redpilot/worker/adapter
sed -n '6223,6230p' orchestrator.py
# → effective_best_of = 1 ；ADAPTER_BEST_OF=%d ignored
```

**A.4 舰队是分片不是冗余**（§3 族 2 核验补充）：

```bash
cd redpilot/worker/adapter
grep -n "crc32\|_solver_shard" orchestrator.py | head
grep -n "_other_solver_active_on" orchestrator.py | head -3
```

**A.5 成功侧残骸是死代码**（§4.4）：

```bash
cd /home/kali/RedPilot
grep -rn "ChallengeProgress\|extract_progress_from_result" --include="*.py" . | grep -v '\.venv'
# → 只有 adapter/progress.py 的定义 + orchestrator.py:38 的 import，零调用点
```

**A.6 技能面负样本统计**（§4.2）：

```bash
cd /home/kali/RedPilot/skills
# 含负向约束的 description 条数
grep -lEi "^description:.*(do not|never|not for|avoid)" */SKILL.md 2>/dev/null | wc -l
# → 1
```

---

## 附录 B：与 TARGET 的交叉引用与一处校正

**分工**：TARGET 管「六个面怎么修」（输入 = 共识层报告）；本文管「35 条里哪些该要」
（输入 = 实现层资料库）。两文互引，判定口径一致。

**一处必要校正 —— 已落地（2026-09-17）**：TARGET §1.2 原写
「候选必须**逐字出现在本场真实工具输出里**」，这是一处**过度断言**，
本文核验时坐实并已就地修正（见该节新增的 ⚠️ 块）。三点更正：

1. 存在**三条强提旁路**绕过 `verified` 直达平台：
   `orchestrator.py:4303`（eager 强提，`confidence >= 0.50`）、
   `:5537`（会话后强提，`require_remote=False`）、`:5556`（`invalid_format` 翻案）；
2. `grounded` 的定义比字面宽 —— flag 只出现在 agent 自己 `echo`/`printf` 的命令里时
   也给 `grounded=True, confidence=0.70`（`verify.py:3398-3405`），
   而 0.70 ≥ 0.50 即触发旁路 1；
3. LLM skeptic 是 **fail-open 否决门**（`verifier.veto_conf` 默认 0.85 以上才拦，
   「LLM 不确定或 verdict 为空 → 宁放过」，`orchestrator.py:1908-1912`）
   而**非准入门** —— 注意 `:1897` 的注释写的是「准入制」，与代码矛盾，**行为以代码为准**。

> **给后续编辑者的提醒**：本文与 TARGET 都在用 `file:line` 体例，但
> **指向彼此的引用应当用节号**（如「TARGET §1.2」）而非行号 ——
> 本次修订在 TARGET 头部与 §1.2 各插入一段，就使全部指向它的行号引用失效。
> 行号只用于指向**代码**。

**TARGET 落地后的表述**（该节 ⚠️ 块的依据，此处留档）：

> 确定性判定分两层，都在 `adapter/verify.py`：**来源边界层**
> `FlagEvidencePolicy:556` + `flag_evidence_policy():640`；**取证判定层**
> `flag_confidence():3139` 与 `Verifier.verify():3558` —— 后者按
> **来源类别**分别记账（`_tainted_inputs` / `_authored_paths` /
> `_downloaded_artifacts` / `_derived_artifacts` / `_response_artifacts` … 各成 frozenset，
> `verify.py:3318-3330`），**不是**一个「非自造」布尔值。
> LLM skeptic（`verify.py:3465`，开关 `orchestrator.py:6802`，**注意关法是 `!= "1"`**，默认开）
> 构成**否决门**与 `invalid_format` 通道上的**翻案门**。
> 三条强提旁路绕过 `verified` 但仍受 skeptic 否决约束。
> **「确定性」确定在 grounding 的包含关系、provenance 分类与阈值比较；
> 不确定在三条旁路是否触发。**

**另附三条开关语义备忘**（写文档时容易踩，三处写法各不相同）：
`ADAPTER_SKEPTIC` 关法是 `!= "1"`（`orchestrator.py:6802`）、
`_heimdall_on()` 是 `== "1"`（`:3618`）、
`_multiflag_hint_review_enabled` 是 `== "1"`（`:753`）。
