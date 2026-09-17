# 自主红队 / 进攻性安全智能体架构全景，与本仓库（RedPilot / RedPilot）架构对比

> 调研日期：2026-09（本文件为一次性调研产物，外部数据来自公开仓库与 arXiv，可能随时间失效）。
> 目的：把本仓库的 worker/竞技场架构放回**自主进攻性安全 Agent 的架构谱系**里，看清它站在哪、
> 强在哪、缺在哪，以及在哪些方向上有明确的可借鉴项。
>
> 数据来源：GitHub Search API、arXiv API、`Yeti-791/Awesome-Offensive-AI-Agentic-Landscape`
> （47 个开源 Agent + 116 篇论文的手工汇总）、各项目 README。外部条目的一手数据未逐条复核，
> 以「社区汇总 + 官方 README」为准，标注为 **ⓑ 外部未复核**。

---

## 0. 一句话结论

本仓库是一台**面向闭卷靶场基准（TSecBench）的、以治理与运维见长的常驻求解系统**，
而不是一台面向真实企业渗透的「全自主黑客」。它在**反幻觉、止损治理、舰队运维、观测面**四个
维度上做得比绝大多数开源框架更深；但在领域前沿正快速收敛的**显式规划、图记忆 / 攻击路径搜索、
领域模型后训练（RLVR）** 三个方向上，本仓库基本是空白或只有最朴素的形态。

---

## 1. 领域全景：自主进攻性安全 Agent 的架构谱系

### 1.1 四阶段演进（arXiv:2607.02605 综述，81 篇文献）ⓑ

| 阶段 | 时期 | 代表 | 驱动瓶颈 |
|---|---|---|---|
| ① 纯文本推理 | 2023 | PentestGPT（USENIX Sec'24）、hackingBuddyGPT | 没有执行反馈 |
| ② 工具增强单 Agent | 2024 | PentestAgent、RapidPen、EnIGMA、AutoAttacker | 单循环探索不足、长程规划差 |
| ③ 多 Agent 协作 | 2024–2025 | D-CIPHER、VulnBot、HPTSA、ARTEMIS、Incalmo、MAPTA、xOffense | 角色分工与长程规划 |
| ④ 可验证奖励后训练（RLVR） | 2025–2026 | Pentest-R1、xOffense、Cyber-Zero、Foundation-Sec | 推理稳定性与成本 |

> 结论：领域正从「谁编排得好」转向「谁把**规划**与**领域推理**做进了模型 / 系统架构里」。

### 1.2 架构家族分类（本次调研归纳，非论文原表）

| # | 家族 | 控制拓扑 | 代表项目 | 核心机制 |
|---|---|---|---|---|
| A | **单 Agent ReAct** | 单循环 | PentestGPT, hackingBuddyGPT, RapidPen | 推理/生成/解析三段，模型自己决定下一步 |
| B | **Planner-Executor（P/E）** | 两角色 | D-CIPHER, PentestAgent, ARTEMIS, Incalmo | 显式 Planner 分解任务 → Executor 执行 |
| C | **多 Agent 角色协作** | 中枢调度 | VulnBot（PTG 任务图）, MAPTA, xOffense, Red-MIRROR | 侦察/利用/报告等专职 Agent + 共享任务图 |
| D | **蜂群 / 信息素黑板** | 去中心化 | Pentest-Swarm-AI | 共享黑板 + 信息素权重衰减，触发器唤醒，无中心 Planner |
| E | **Planner-Executor-Observer + 因果图记忆** | 三角色 + 图 | LuaN1aoAgent | 推理图/操作图双图，Observer 异步投影，Plan-on-Graph |
| F | **持久交战状态 + 实体图 + 路径搜索** | 单/多 Agent + 图算法 | PentestCode | 实体关系图（EXPLOITED_VIA…）+ Dijkstra/Yen K 短路 |
| G | **经典规划外置脑** | Planner(PDDL)+LLM | CHECKMATE, Aurora, ChainReactor | LLM 负责语义，规划器负责长程结构 |
| H | **MCP 工具总线** | 工具层 | HexStrike(150+), mcp-kali-server, BloodHound-MCP | 把真实工具标准化暴露给任意宿主 LLM |
| I | **知识图谱 / RAG 增强** | 检索层 | VulnBot, PenForge, PTFusion | 漏洞知识图谱 + 历史决策检索 |

补充共识：`Towards Optimal Agentic Architectures`（arXiv:2604.18718）用 600 次对照实验给出
**「拓扑不是越多越好」** 的实证——独立多 Agent（MAS-Indep）验证检出率最高（64.2%），
单 Agent（SAS）单位成本最低（$0.058/validated finding）。这直接否定了「无脑堆 Agent」。

### 1.3 本仓库在谱系中的位置

**不属于 A–I 中任何单一家族**，最接近 **C + 自研治理层**，但骨架是「单引擎 + 竞技场编排」：

- 求解内核 = **单个 Pi Agent 进程**（`pi --mode json --print`），有 subagent（scout/worker/checker），
  但没有独立 Planner 角色、没有 Executor 池；
- 编排 = **竞技场主循环**（多轮时间盒 / 多会话重访 / 跨场续接），这层在别的框架里通常不存在；
- 与前沿差距最大的是 **E/F 的图记忆** 与 **G 的经典规划**。

---

## 2. 本仓库架构剖析

### 2.1 单发行版四模块（依赖方向是红线）

```
redpilot/contracts   共享内核：零依赖契约（snapshot/vocabulary/redact/fsio/paths）
redpilot/control     控制面（challenges/调度/VPN）+ redpilot/obs 观测平台（摄取/读端/SSE/SPA）
redpilot/worker      求解 worker：竞技场编排 + Pi Agent 引擎 + 观测中继 + 本地态势台
redpilot/app.py      平台进程装配根（唯一同时 import control 与 obs）
```

依赖红线由 `tests/architecture/` 的 AST 测试执行（worker ↛ control/obs，control ↮ obs）。控制面/观测面两张表见下。

| 层 | 关键文件 | 行数 | 职责 |
|---|---|---|---|
| 契约 | `redpilot.contracts/{snapshot,vocabulary,redact,paths}.py` | — | 18 键 LiveSnapshot、phase/run 状态词表、打码口径、心跳路径单源 |
| 控制面 | `redpilot/control/*` | — | 题目/调度/VPN/评测启停，SQLite |
| 观测面 | `redpilot/obs/*` | — | 摄取、读端、SSE、runs/transcript 存储，独立 SQLite |
| **编排** | `redpilot.worker/orchestrator.py` | **7079** | 竞技场主循环，146 个顶层符号 |
| 监督 | `redpilot.worker/supervisor.py` | 450 | worker-1「他管」层，能力面被限死为一个动作 |
| 观测桥 | `redpilot.worker/observability.py` / `relay.py` | 265/494 | status → LiveState → relay → 平台 |
| **验证** | `adapter/verify.py` | **3592** | 三重门 + 置信度分级 + 证据接地 |
| 引擎 | `adapter/solver/pi_agent.py` | 1395 | 四看门狗 + 令牌式进程树回收 |
| 止损 | `adapter/stoploss.py` | 791 | 多维止损治理 |
| 观察者 | `adapter/heimdall.py` | 515 | 只读复盘 Agent，生成 `<heimdall-map>` |
| 黑板 | `adapter/blackboard.py` | 323 | 正则机械抽取事实（recon/credential/vuln/foothold…） |
| 幻觉哨兵 | `adapter/hallucination.py` | 333 | 把「凭空编」与「真推导但没资格」分开记账 |
| 提示组装 | `adapter/taskprompt.py` | 717 | Skill 渐进式披露 + 续接块 + 已知事实 |
| 合规 | `adapter/compliance.py` | 125 | flag 打码唯一实现处 |

### 2.2 五个标志性机制

1. **竞技场（Arena）多轮编排**
   按难度给单场时间盒（20/30/90 分钟），未解题挂起，**后续轮次以递增时间盒重访**；
   跨场通过 `extract_handoff` 续接（已达成原语 / 已证死路 / 下一步）。整体挂钟上限
   `ADAPTER_TOTAL_SECONDS`，单题终身预算 `ADAPTER_PER_CHALLENGE_SECONDS`（与时间盒联动）。

2. **三重门 + 接地（Grounding）验证**
   flag 候选必须**逐字出现在本场真实工具输出**里；再走否定式质疑、追问式复核；
   分 HIGH/MEDIUM/LOW 置信度。本地静态产物能否算证据由题目分类决定。

3. **Eager 即时提交 + 双闸门**
   会话进行中 eager 线程盯 `FLAG` 文件即时投递，不等会话结束；投递前过确定性 grounding 门；
   平台响应（correct/duplicate）是唯一终审。

4. **多维止损治理器**
   单题活动时间 / 连续无新事实会话数 / 目标连续不可达访问数 / 假设空间重复度 /
   单题终身会话上限 / 复活冷却 / 零 flag 切换。这是**一整套状态机**，非单阈值。

5. **运维级舰队**
   单 VPN + netns 复用（worker-1 提供，worker-2/3 共享）；supervisor 只能「请求对方热重载」
   （`touch .reload.widN` → 被监督者会话边界 exit 86），**物理上没有重启容器能力**；
   退出码契约 0/86/4/3 与 `restart: on-failure` 配套；**配置错误必须 exit 0**（防无限重启）。
   进程回收靠 `TSECBENCH_PI_INSTANCE_TOKEN` 扫 `/proc/*/environ`，驱动崩溃后仍有效。

### 2.3 两套状态、两个落点

| 落点 | 内容 | 消费方 |
|---|---|---|
| `work/status/worker-<N>.json` | 编排层进度（solving_active/sessions/flags_submitted…） | supervisor、只读控制台 |
| `work/.live/<worker_id>.json` | 观测面快照（phase/current_tool/turns…，18 键） | relay → 平台；:8080 态势台 |

`observability.StatusBridge` 单向同步，字段名不同是刻意的。观测面**只读、绝不抛、坏掉不拖垮解题**。

---

## 3. 对比矩阵

图例：● 领先 / ◐ 具备 / ○ 薄弱或缺失 / – 不适用。外部列标注 **ⓑ**（未一手复核）。

| 维度 | **RedPilot** | A 单 Agentⓑ | B P/Eⓑ | C 多 Agent 角色ⓑ | D 蜂群黑板ⓑ | E 认知图ⓑ | F 状态+图搜索ⓑ | G 经典规划ⓑ | H MCP 总线ⓑ |
|---|---|---|---|---|---|---|---|---|---|
| 控制拓扑 | 单引擎 + 竞技场编排 | 单循环 | Planner+Executor | 中枢调度 | 去中心化触发器 | P/E/Observer | 单/多 + 图算法 | Planner(PDDL)+LLM | 无（工具层） |
| 显式规划 | ○（LLM + Skill 预选） | ○ | ● 任务分解 | ● 任务图(PTG) | ◐ 涌现 | ● Plan-on-Graph | ◐ 路径建议 | ● 形式化 | – |
| 记忆 / 状态 | ◐ 扁平黑板 + MEMORY.md | ○ | ○ | ◐ | ● 信息素黑板 | ● 双因果图 | ● 实体关系图 | ○ | ○ 无状态 |
| 攻击路径搜索 | ○ | ○ | ○ | ○ | ◐ 权重偏置 | ◐ 图查询 | ● Dijkstra/Yen | ● | ○ |
| 工具执行 | ● Bash + Skills + 子 Agent | ◐ | ● | ● | ● | ● 沙箱 | ● | ◐ | ● 150+ |
| 反幻觉 / 接地 | ● 三重门 + 哨兵 + 观察者 | ○ | ○ | ○ | ◐ 证明式利用 | ● 证据图 | ◐ | ○ | ○ |
| 预算 / 止损 | ● 多维止损全套 | ○ | ○ | ○ 课程调度 | ◐ 预算耗尽 | ◐ task budget | ○ | ○ | ○ |
| 观测 / 可追溯 | ● 18 键快照 + SSE + 读/写双 token | ○ | ◐ | ◐ | ◐ | ● 事件/产物 | ◐ | ○ | ○ |
| 期望运营 | ○（真实企业） | 研究/CTF |
| 目标场景 | **闭卷靶场基准（flag 提交）** | 真实 Web/企业渗透、CTF |
| 领域模型 | ○ 通用 DeepSeek/GLM | ◐ xOffense/Pentest-R1 微调 | ● | ● | ○ | ◐ | ○ | ◐ | – |
| 多主机 / 内网 | ◐ 多段内网题（同会话保活） | ○ | ● Incalmo/ARTEMIS | ◐ | ○ | ◐ | ● | ● | ○ |
| 失败恢复 / 运维 | ● 舰队监督 + 热重载 + 进程回收 | ○ | ○ | ○ | ○ | ◐ durable events | ◐ | ○ | ○ |
| 防御对抗/闭环 | ○ 静态靶标 | ○ | ○ | → ZERO-APT 闭环ⓑ |
| 人在回路 | ○ 全自动 | ◐ 部分 | ◐ | ● 部分 | ○ | ● 控制台 | ○ | ○ | ● 宿主 |

---

## 4. 本项目的差异化优势（相对开源主流）

1. **反幻觉是一等公民，而不是 prompt 里的一句叮嘱。**
   领域内公认的失败模式是「独白式幻觉」（EnIGMA, ICML'25）ⓑ。本仓库用
   `verify.py`（3592 行，三重门 + 置信度）、`hallucination.py`（区分幻觉族/推导族，
   且**推导族永不计数**）、`heimdall.py`（只读复盘、DEAD/LOCK 可撤销、TENSION 只并置）
   组成三层。多数开源框架在这一维度上是 0。

2. **预算治理粒度远超同类。**
   `stoploss.py` 是多维状态机（时间/干会话/不可达/假设重复/终身上限/复活冷却），
   而主流仅「tool call 上限」或「预算耗尽」。`Towards Optimal Agentic Architectures` 关注
   单位成本，但没有把**止损**作为架构组件——本仓库有。

3. **为长时间无人值守运行设计的运维面。**
   舰队 netns 复用、能力面被架构限死（supervisor 不能重启容器）、退出码契约、
   令牌进程回收、协作式热重载、配置错误 exit 0。这些是研究框架普遍缺失的「生产化」细节。

4. **观测面是独立契约，不是日志。**
   零依赖 contracts 包把 18 键快照/词表/打码口径钉成单源；读端/写端双 token 不对称，
   「能写遥测」≠「能读答案」。PentAGI 用 Langfuse+Grafana 拼，本仓库是自洽闭环。

5. **跨场续接（handoff）与竞技场重访。**
   `extract_handoff`（已达成/已证死/下一步）在主流框架里基本没有对应物——
   多数 Agent 是一次性 run，不跨会话继承「为什么放弃」。

---

## 5. 结构性缺口（对标前沿）

| 缺口 | 现状 | 前沿对照 | 影响 |
|---|---|---|---|
| **无显式规划器** | 靠 LLM 直接决策 + Skill 关键词预选 | D-CIPHER Planner、CHECKMATE PDDL、LuaN1ao Plan-on-Graph | 长程一致性差；复杂多阶段题易迷路 |
| **无图记忆 / 因果图** | 扁平 `blackboard`（正则）+ `MEMORY.md` | LuaN1ao 双图、VulnBot PTG、PentestCode 实体图 | 结论不可回溯到证据链；重复探索难根除 |
| **无攻击路径搜索** | 无 | PentestCode Dijkstra/Yen K 短路、攻击树（arXiv:2509.07939） | 多跳内网/横向移动缺乏全局最优性 |
| **无领域模型后训练** | 通用 DeepSeek/GLM | xOffense(Qwen3-32B 微调)、Pentest-R1(RL)、Foundation-Sec | 单位算力下的攻击推理密度低 |
| **无经典规划外置脑** | 无 | CHECKMATE（超 Claude Code 20%）ⓑ、Aurora/ChainReactor | 结构化长程任务吃亏 |
| **Skill 库小** | 23 个 SKILL.md | Anthropic-Cybersecurity-Skills 817、CyberStrike 7300+ ⓑ | 覆盖广度受限 |
| **无闭环防御对抗** | 静态靶标 | ZERO-APT（attacker-defender-judge）ⓑ | 缺乏「智能防御下行为」评估 |
| **静态靶标下的验证偏保守** | grounding 要求逐字命中真实输出 | 证明式利用（swarm「exploits what it finds, proves it」）ⓑ | 可能拒掉真答案（历史 29 拒收中 19 为真答案） |
| **多段题依赖同会话保活** | `ADAPTER_MULTIFLAG_MAX_TURNS` 放宽 | 独立子目标 + 结构化任务状态 | 上下文压力大 |

> 关键张力：本仓库的**证据接地门**在提升精确率的同时，历史数据显示会**拒掉约 2/3 的真答案**
> （`hallucination.py` 顶部影子审计：29 条被拒里 19 条实为正确答案）。这说明「严格接地」与
> 「召回」在 flag 型任务上尚未平衡——前沿做法（图记忆 + 证明式利用）正是为了在**保留召回**的
> 同时给证据以结构。

---

## 6. 可借鉴的演进项（按性价比排序）

1. **把 `blackboard` 升级为证据图（低成本、高收益）**
   在现有 `Fact(kind, content, source, confidence, iter)` 上加 `derived_from` 边，
   让 Heimdall 的 TENSION/DEAD 结论可挂到图上。参考 LuaN1ao 的「证据→假设→漏洞→利用」链。
   这直接缓解「真答案被接地门拒掉」的问题：有证据链 = 可解释为何不是幻觉。

2. **引入显式 Planner 角色（中成本）**
   不必推翻竞技场。可在每场会话前加一个轻量 planner 调用，产出**任务依赖图**（JSON），
   交给现有 Pi Agent 执行；竞技场负责跨场调度。参考 D-CIPHER 的 Planner-Executor 与
   CHECKMATE 的 PEP 范式。这与现有「多轮时间盒」天然契合。

3. **在 stoploss 里加「假设空间重复度」的图化实现（中成本）**
   现有热词是命令快照；用证据图上的节点访问频次可以更准地判定「换汤不换药」。

4. **攻击路径搜索外挂（中成本）**
   把 `blackboard` 事实建成实体图后，用 Dijkstra/Yen 生成候选攻击路径，作为 planner 输入。
   参考 PentestCode。对多段内网题收益最大。

5. **领域模型微调 / 路由（高成本）**
   对高频题型（Web 注入、反序列化、提权）做 SFT/LoRA；或引入 xOffense/Pentest-R1 类模型
   作为 solver，通用模型做规划。参考 RLVR 阶段。

6. **闭环防御对抗（高成本，评估口径升级）**
   引入 ZERO-APT 式 defender，把「静态靶标正确率」升级为「智能防御下成功率」。

7. **Skill 库扩容（低成本、可外包）**
   现有 23 个；可结构化接入 Anthropic-Cybersecurity-Skills / ctf-skills 的映射，
   保持渐进式披露机制不变。

---

## 7. 定位判断

- **不要**试图把本仓库改成「全能真实渗透平台」——它的价值锚点是**闭卷基准下的高精度、可观测、
  可运维求解**，这一细分与 PentestGPT/PentAGI/Strix 的目标（真实渗透报告）不同，直接比分数
  是错位的。
- **要**把前沿的**规划 + 图记忆**接进来，因为这两项恰好补的是当前架构在「长程一致性」和
  「证据可回溯」上的短板，而且能与竞技场/止损/接地/观测四个既有优势**正交叠加**，不必推倒重来。
- 一句话：**本仓库的治理层是超配的，认知层是欠配的。** 下一阶段的重心应从「怎么不出错、
  怎么可观测、怎么不烧钱」转向「怎么想得更远、记得更结构化」。

---

## 附录 A：检索到的关键开源项目（Star 为 2026-09 社区汇总值 ⓑ）

| 项目 | 语言 | 架构要点 |
|---|---|---|
| usestrix/strix (60.1k) | Python | 动态执行 + 真实 PoC 验证 |
| KeygraphHQ/shannon (47.6k) | TS | 白盒 Web/API 自主渗透 |
| vxcontrol/pentagi (22.2k) | Go | 4 Agent + planner，微服务 + pgvector + Langfuse/Grafana |
| GreyDGL/PentestGPT (15.2k) | Python | 单 Agent ReAct 奠基，仅建议不执行 |
| 0x4m4/hexstrike-ai (11.5k) | Python | MCP 暴露 150+ 工具 |
| aliasrobotics/cai (9.8k) | Python | 首个网络安全自主等级分类（已归档） |
| elder-plinius/T3MP3ST (5.9k) | TS | 复用本机 AI 编码代理做零日猎手 |
| Armur-Ai/Pentest-Swarm-AI (2.4k) | Go | 信息素黑板 + 触发器，真蜂群 |
| SanMuzZzZz/LuaN1aoAgent (1.3k) | Python | Planner-Executor-Observer + 因果图记忆 |
| s0ld13rr/pentestcode (704) | — | 持久交战状态 + 实体图 + Dijkstra/Yen |
| m-sec-org/BreachWeave (525) | TS | Manager/Observer/Solver 三角色 |
| aielte-research/HackSynth (316) | Python | Planner + Summarizer 双模块 |
| KHenryAegis/VulnBot (191) | Python | 渗透任务图 PTG + RAG |
| andreashappe/cochise (134) | Python | AD 假定失陷自主渗透 |

## 附录 B：关键论文（架构向）

| 论文 | 关联 | 要点 |
|---|---|---|
| PentestGPT (USENIX Sec'24) | A | 奠基，推理/生成/解析 |
| D-CIPHER (arXiv:2502.10931) | B | Planner + 异构 Executor，动态反馈环 |
| HPTSA (arXiv:2406.01637) | B | 规划 Agent 启动子 Agent 打 0day，+4.3× |
| Incalmo (arXiv:2501.16466) | B | CMU 多主机红队，指出 SOTA 失败模式 |
| ARTEMIS (arXiv:2512.09882) | B | Stanford，8000 主机，胜 9/10 人类 |
| CAI (arXiv:2504.06017) | C | 首个网络安全自主等级分类 |
| CHECKMATE (arXiv:2512.11143) | G | Planner-Executor-Perceptor，超 Claude Code 20% |
| LuaN1aoAgent (READMEⓑ) | E | 双因果图 + Plan-on-Graph |
| Towards Optimal Agentic Architectures (arXiv:2604.18718) | 全域 | 600 次消融，MAS-Indep vs SAS |
| A Survey of LLM-Driven Pentesting (arXiv:2607.02605) | 全域 | 四阶段演进 + Agents4Pentest 分类 |
| ZERO-APT (arXiv:2606.05567) | 闭环 | attacker-defender-judge |
| Guided Reasoning via Attack Trees (arXiv:2509.07939) | 规划 | ATT&CK 攻击树约束推理 |

---

*本文件由一次调研会话产出，未改动任何代码。外部数据标注 ⓑ 者未一手复核。*
