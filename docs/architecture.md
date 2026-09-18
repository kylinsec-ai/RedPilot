# RedPilot 架构设计（总体）

> **本文定位**：RedPilot 的**系统级架构设计** —— 回答"系统由哪些部件构成、边界为什么这样切、
> 数据怎么流、哪些性质不可破坏、哪些路径已经证死"。
> **不重复**：[`README.md`](../README.md)（上手指南）、
> [`docs/modular-monolith-design.md`](modular-monolith-design.md)（打包收敛与 R1–R8 的完整论证）、
> [`docs/worker.md`](worker.md)（worker 运维约定）、
> [`docs/pi-rpc-migration-research.md`](pi-rpc-migration-research.md)（RPC 迁移实测）、
> [`docs/graph-engineering-design.md`](graph-engineering-design.md) 与
> [`docs/deterministic-recon-design.md`](deterministic-recon-design.md)（未实施提案）。
> **出处纪律**：本文每条"已成立"的声明都能在代码或 `tests/architecture/` 找到执行点；
> 只有提案没有执行点的，一律集中在 [§13 演进路线](#13-演进路线) 并显式标注"未实施"。

---

## 0. 结论（TL;DR）

1. **一个发行版、四个模块、两个进程角色**。`contracts / control / obs / worker` 同版本发布，
   `app.py` 与 `worker/driver.py` 是仅有的两个装配根；边界由 AST 架构测试执行，不靠 pip 依赖图。
2. **一个编排、一个求解引擎**。竞技场（`worker/orchestrator.py`）是唯一主循环；
   Pi Agent（`worker/adapter/solver/`）是唯一求解引擎。框架里不存在第二套编排或第二个 solver。
3. **单隧道原则**。一个靶场 VPN 只应有一条隧道：worker-1（`ADAPTER_ROLE=monitor`）持 tun
   并共享 netns，worker-2/3 以 `network_mode: service:worker-1` 复用；隧道没了由 netns
   看门狗 `os._exit(3)` 自愈。
4. **两套存储、两个权威**。control SQLite（`data/redpilot.sqlite3`）是题目/调度/判分的权威；
   obs SQLite（`data/obs.sqlite3`）是观测投影。`attempt.*` canonical 事件是终态唯一权威，
   relay telemetry 永远不能覆盖它。
5. **flag 两道闸门**。确定性 grounding/证据门（候选必须逐字出现在本场真实输出里）→
   平台 response（correct / duplicate）才是最终判据；eager 提交不必等会话结束。
6. **一个 flag 语义**：候选先分"幻觉族 / 推导族"，再走置信度分级；幻觉族拒收，推导族进复核。
7. **状态全部外置**。进度在 `work/status/`、观测快照在 `work/.live/`、逐题记忆在题目目录
   （`MEMORY.md` / `_blackboard.json` / `.continuation.json` / `transcript.jsonl`），
   跨场续接只带元数据，不带答案文本。
8. **观测面是可拆件**。relay / 本地态势台 / Heimdall /判别 Agent 任何一个坏掉，求解路径
   都不受影响；只有平台提交与 grounding 门是硬路径。
9. **权限不对称是刻意的**。遥测写端 token ≠ 观测读端 token；worker 只持写端；
   控制面 admin token 与 worker token 分离，未配置即 503 fail closed。
10. **被架构根除的危险能力**：worker-1 没有 docker socket，所以"他管"的全部能力面
    只有一个动作 —— 写协作热重载标记（`touch /work/.reload.wid<N>`），
    由被监督者自己在会话边界退出 86。

---

## 1. 设计目标与不变量

### 1.1 目标

| # | 目标 | 判据 |
|---|---|---|
| G1 | **自主解题**：连上靶场 VPN，自主侦察、利用、提交 flag | 平台判分正确，`flags_submitted > 0` |
| G2 | **全过程可观测**：运行中实时、结束后可回溯 | 观测 SPA 可看图/看 transcript/看 run 历史；断连不丢权威终态 |
| G3 | **长时程稳定**：数小时到数天的持续运行 | 会话级看门狗、舰队监督、热重载、netns/VPN 自愈 |
| G4 | **不为幻觉付钱**：提交的每个 flag 都有证据、有出身 | `verify.py` 三重门 + 平台判据；幻觉族与推导族分账 |
| G5 | **不重复踩坑**：跨会话、跨轮次、跨 flag 不重复失败 | 被拒/未证 ledger、`.continuation.json`、黑板、Heimdall |
| G6 | **可替换**：平台、模型、传输、技能面都不写死 | `adapter/platform/*`、provider 路由、`ADAPTER_PI_TRANSPORT`、`skills/` |

### 1.2 不变量（改架构前先读）

| # | 不变量 | 执行点 |
|---|---|---|
| I1 | 单一发行版、单一 `__version__` | `tests/architecture/test_single_source.py` |
| I2 | 唯一编排：竞技场是唯一主循环 | 仓库无第二套 `solve_one` 派发链路 |
| I3 | 唯一求解引擎：Pi Agent | `adapter/solver/factory.py` 只生产 Pi 后端 |
| I4 | canonical > telemetry：relay 终态永不能覆盖 canonical | `obs/store.py` 的 `close_run`/`append_events` 守卫 + `test_canonical_run_close.py` |
| I5 | flag 必须过 grounding 门才能提交 | `adapter/verify.py` + `test_progress_evidence_gate.py` |
| I6 | 读端凭据 ≠ 写端凭据；读 token 不转发给 worker | `obs/ingest_common.py`、`.env.example` |
| I7 | 观测面失败隔离：坏掉不影响求解 | relay/Heimdall/verifier 全部 `try/except` 降级 |
| I8 | **配置错误 exit 0**（绝不触发 `restart: on-failure` 闷循环） | `driver.py`、`entrypoint.sh` |
| I9 | 单靶场单隧道；worker-1 是唯一 netns 提供者 | `docker-compose.yaml`、`_start_netns_watchdog` |
| I10 | 一个库/目录只有一个写模块 | `tests/architecture/test_data_ownership.py` |

### 1.3 非目标（显式不做）

- **不做多租户 SaaS 控制面**：evaluation/job/attempt 状态机是单平台语义。
- **不做通用编排 DSL**：循环逻辑全部留在 `orchestrator.py` 一个文件里，可读优先于抽象。
- **不做 worker 自动更新**：镜像替换由部署方负责（热重载只换"已就位的新码"）。
- **不引图数据库**：图工程提案（`docs/graph-engineering-design.md`）以 SQLite/文件为存储。
- **不做求解 Agent 的文件系统沙箱**：现状是"进程已加载 + 部署纪律"（见 §13 缺口 D3）；
  DAC 降权 + 控制面状态搬家**已实施**，容器内真降权待 §3 探针，见
  [`docs/solver-isolation-design.md`](solver-isolation-design.md)。

---

## 2. 设计方法：六个上下文工程技能对 RedPilot 的裁决

本次设计显式调用了六个技能，逐一对现有架构做"应然 vs 实然"的裁决。结论先列，
各域的展开见对应章节。

| 技能 | 作用的架构层 | 裁决结论 | 执行点 / 差距 |
|---|---|---|---|
| **harness-engineering** | 控制面（§6.1 竞技场、§10.1 面分类） | RedPilot 本质是"围绕 Pi Agent 的控制系统"；四类面已存在，但"锁定面"靠进程模型而非权限模型 | grounding 规则 + 平台判据 + `tests/architecture/` 是锁定面；缺口 D3 |
| **multi-agent-patterns** | 拓扑（§4 舰队、§6.4 观察者） | 舰队层 = **文件协调的 swarm**（无中心调度器）；题目内 = **单求解者 + 隔离上下文的观察者/对抗校验者**；不搞角色扮演式分工 | `/work` 共享卷 + 平台 lease；Heimdall 只读、skeptic 独立会话 |
| **filesystem-context** | 数据（§8.2 `/work`、§6.2 技能装载） | `/work` 是标准答案式的**上下文溢出层**：大输出落文件、计划持久化、子 Agent 文件通信、技能动态装载、日志可 grep | `transcript.jsonl`、`MEMORY.md`、`_blackboard.json`、skill_loader |
| **memory-systems** | 记忆（§6.4） | 分层正确（工作/短期/长期），**时间有效性只做到一半**：ledger 与 epoch 有，黑板的撤销/陈旧没有 | `.continuation.json` 按 task epoch 失效；黑板只增不改（缺口 D2） |
| **advanced-evaluation** | 判分（§6.3） | 判分器设计符合"证据先于分数 + 确定性预检 + 面板投票 + 置信校准"；幻觉/推导分族是防自我增强偏差的关键 | `verify.py` 三重门、`skeptic_votes`、rescue/veto 阈值 |
| **long-horizon-prompting** | 任务简报（§6.1 会话、§10.5） | `taskprompt._CLAUDE_MD` 就是每题的长时程简报：成功谓词、非计数结果、止损、反停滞、返回条件都在；**努力地板与污染护栏部分还是 prompt 级而非 harness 级** | `taskprompt.py`、`stoploss.py`；缺口 D4 |

### 2.1 harness-engineering 的五类面（RedPilot 映射）

| 面 | RedPilot 里的对应物 | 规则 |
|---|---|---|
| **Locked（锁定）** | grounding 证据规则、flag 置信度阈值、`tests/architecture/` 红线、平台判据 | 求解 Agent 不能改评分规则；改这些必须走人类提交 |
| **Editable（可写）** | 逐题目录（脚本、笔记、靶标产物）、`skills/` 的增补（部署前） | Agent 在题目工作区内自由写 |
| **Append-only（追加）** | `transcript.jsonl`、`_events.jsonl`、rejected/unverified ledger、obs events 表 | 只追加不重写；平台与读端靠它审计 |
| **Human-controlled（人控）** | 部署、镜像、凭据、`.env`、合并 | 不进入自动循环 |

反馈回路的节拍：**平台 response（分钟级）＞ 观测 relay（秒级）＞ 本地态势台（1s 节流）**。
主动作（提交 flag）的反馈最短、最不可伪造，这是"紧反馈回路"成立的原因。

### 2.2 拓扑裁决：为什么不是"主管-工人"层级

`multi-agent-patterns` 的判据是**上下文隔离收益**，不是组织隐喻。RedPilot 据此做了三个选择：

1. **舰队不设中央主管**：三个 worker 各自从平台拉题、按能力分片认领，靠共享 `/work`
   的 advisory lock 与平台 lease 排他。文件系统是协调机制，避免了主管摘要造成的
   "电话游戏"（telephone game）信息衰减 —— 每个 worker 直接持有自己的完整上下文。
2. **观察者不重述、校验者不共谋**：Heimdall 只举镜子（只标 DEAD/LOCK/TENSION，可撤销），
   skeptic 是独立上下文、独立会话的对抗验证者，不复用求解者的自我辩护。
3. **危险能力从架构根除**：worker-1 没有 docker socket，能力面被限死为"写一个重载标记"。
   能从架构上删掉的能力，不靠纪律约束。

---

## 3. 系统上下文

```
                    ┌─────────────────────────────────────────────┐
   操作者浏览器 ───▶ │  RedPilot server :8000                       │
   (SPA / API)      │  控制面：challenges/调度/VPN/评测            │
                    │  观测平台：摄取/读端/SSE/SPA                  │
                    └───────▲───────────────────────┬─────────────┘
                            │ telemetry(写 token)    │ 读 token（含明文 flag）
                            │ canonical(平台 token)  │
                    ┌───────┴───────────────────────▼─────────────┐
   宿主 Docker ───▶ │  worker 舰队（共享 /work 卷）                │
   (启动/重启)      │  worker-1 monitor：VPN + netns + 他管        │
                    │  worker-2/3：竞技场主循环 → Pi Agent         │
                    └───────────────┬─────────────────────────────┘
                                    │ 平台 REST / LLM API
              ┌─────────────────────┼───────────────────────────┐
              ▼                     ▼                           ▼
      靶场平台（题目/容器/    LLM 网关               靶标主机（仅经 VPN tun）
      提交/VPN 清单）        （OpenAI 兼容等）
```

| 外部系统 | 协议 | 凭据 | 谁能到达 |
|---|---|---|---|
| 靶场平台 | REST（list/start/hint/submit/VPN check） | `BENCHMARK_TOKEN` | monitor + 所有 solver |
| LLM 网关 | Pi 官方 provider 协议 | `<PROVIDER>_API_KEY` / `~/.pi/agent/auth.json` | solver（Pi 子进程） |
| 观测平台 | HTTP relay/ingest + SSE | `OBSERVABILITY_TOKEN`（写）/ `OBSERVABILITY_READ_TOKEN`（读） | worker 持写；浏览器持读 |
| 靶标主机 | 任意 TCP/UDP | 无（网络隔离即权限） | 仅 VPN 内 worker |
| 宿主 Docker | `docker compose` | 宿主用户 | 操作者；**不暴露给容器** |

> 术语提醒：`redpilot.control` 是 RedPilot **自己的**控制面（平台角色）；
> worker 的 `adapter/platform/*` 是**靶场平台**客户端。两者中文都叫"平台"，
> 读代码时以包名为准。

---

## 4. 运行时拓扑

```
┌──────────────────────────── server 容器（python:3.13-slim）──────────────────────┐
│  redpilot.app:app                                                                  │
│  control 路由 /openapi/v1/* /api/v1/*     obs 路由 /api/*（SSE /api/events）       │
│  SPA 静态文件 /                          control store + obs store（两库）         │
│  healthcheck: GET /healthz             outbox loop → canonical-events              │
└────────────────────────────────────────────────────────────────────────────────────┘
            ▲ telemetry / canonical                                    ▲ 明文 flag 读
            │                                                          │
┌───────────┴──────────────── worker 容器（Kali 基底 + pi CLI）──────────────────────┐
│  ┌─ worker-1（ADAPTER_ROLE=monitor，cap_add NET_ADMIN，/dev/net/tun）─┐            │
│  │  openvpn 常驻 → tun0；_monitor_loop 汇总；_start_vpn_watchdog     │            │
│  │  唯一能力：touch /work/.reload.wid<N>（"请求重启"，不能"替人重启"）│            │
│  └──────────────────────────── netns 提供者 ────────────────────────┘            │
│         ▲ network_mode: service:worker-1（复用隧道，不再各起 VPN）                  │
│  ┌──────┴────────┐   ┌────────────────┐                                           │
│  │ worker-2      │   │ worker-3       │  竞技场：认领→会话→止损→eager 提交         │
│  │ 能力分片      │   │ 能力分片        │  Pi 引擎 → FLAG → grounding 门 → 平台       │
│  └───────────────┘   └────────────────┘                                           │
│  共享卷 /work：status/ .live/ .stoploss-locks/ <code>/（逐题目录）                   │
└───────────────────────────────────────────────────────────────────────────────────┘
```

| 容器 | 角色 | 网络 | 特权 | 退出语义 |
|---|---|---|---|---|
| `server` | 控制面 + 观测平台 | 桥接；宿主发布 `:8000` | 无 | 常驻 |
| `worker-1` | monitor：VPN + netns + 他管 | 自己起 tun0 | `NET_ADMIN` + `/dev/net/tun` | 常驻（它一停 2/3 断网） |
| `worker-2/3` | solver | 复用 worker-1 netns | 无 | 0 终态 / 86 热重载 / 3 被孤立 |
| `worker`（profile `monolith`） | 自发 VPN 自解题 | 自己起 tun0 | `NET_ADMIN` + `/dev/net/tun` | 同 solver |

**为什么单隧道**：多条隧道会互相抢路由，且平台把来源 IP 与 VPN 会话绑定。
代价是 worker-1 成为单点 —— 由两个看门狗兜底：
netns 提供者重建时，消费者连续 3 次检测不到默认路由即 `os._exit(3)`，
靠 `restart: on-failure` 重建并重新 join。**看门狗的重启对象是自己，不是别人**，
这个方向不能反（worker-1 没有 docker socket，也不该有）。

---

## 5. 代码架构

### 5.1 布局

```
redpilot/                     # 唯一发行版，唯一 __version__
├── contracts/                # ① 共享内核：stdlib-only，零第三方依赖
├── control/                  # ② 控制面：challenges/调度/VPN/评测 + outbox
├── obs/                      # ③ 观测平台：摄取（canonical/telemetry）/读端/SSE/SPA
├── worker/                   # ④ 求解：竞技场 + Pi 引擎 + relay + 本地态势台
│   ├── driver.py             #    worker 装配根
│   ├── orchestrator.py       #    竞技场主循环（唯一编排）
│   ├── supervisor.py         #    worker-1 他管层（能力面一个动作）
│   └── adapter/              #    策略层：solver/platform/verify/stoploss/blackboard/heimdall
├── app.py                    # 平台装配根（唯一同时 import control 与 obs 的地方）
└── __init__.py               # 唯一 __version__，不 import 任何子模块
```

### 5.2 模块职责与数据所有权

| 模块 | 拥有 | 数据所有权 | 读者 |
|---|---|---|---|
| `contracts` | 词汇表、路径、快照 schema、脱敏/截断、摘要折叠、共享平台契约 | 无状态 | 全部模块 |
| `control` | 题目定义、调度、容器供给、VPN、判分、outbox | `data/redpilot.sqlite3` | `app.py`、控制面 API |
| `obs` | 摄取、读端、SSE、SPA、控制代理（可选） | `data/obs.sqlite3` | `app.py`、态势台 |
| `worker` | 竞技场、止损、证据闸门、Pi 引擎、平台适配、relay、本地态势台 | `/work/`（status/.live/逐题目录） | 平台、本地 `:8080` |

规则：**一个库/目录只有一个写模块**（I10）。跨模块读走 API 或事件
（worker relay → obs ingest 的 HTTP 契约是产品契约，不因合并发行版而变成函数调用）。

### 5.3 依赖红线（R1–R8，摘要）

完整论证见 [`docs/modular-monolith-design.md`](modular-monolith-design.md) §3.3。
执行点全部在 `tests/architecture/`：

```
                 contracts
                 ↑       ↑
        control  ↑       ↑  worker
            ↑    ↑       ↑    ↑
            └── app.py   └ driver.py（两个装配根）
```

| # | 规则 | 测试 |
|---|---|---|
| R1 | `contracts` 只允许 stdlib + 自身 | `test_layers.py` AST + `sys.stdlib_module_names` |
| R2 | `worker` ↛ `control/obs/app`；import 闭包无 fastapi/pydantic/uvicorn | AST 禁边 + `test_runtime_footprint.py` 子进程足迹 |
| R3 | `control` ↮ `obs`（双向禁止） | AST 禁边 |
| R4 | `app.py` 不承载领域逻辑 | 顶层只有 `create_app`，import 白名单 |
| R5 | 跨模块只准 façade（`__all__` 名字，禁 `_` 前缀） | `test_public_api.py` |
| R6 | 模块内单向：装配 → 编排 → 端口/适配器 → 内核 | `test_layers.py` 适配器禁 import 编排 |
| R7 | env 只在 config/settings 解析 | AST 扫描 + 只减不增的 `ENV_DEBT` allowlist |
| R8 | 数据所有权：一个库/文件一个写模块 | `test_data_ownership.py` |

### 5.4 装配根

- **`redpilot/app.py`**：构造 control 子应用但**不启动它的 lifespan**（只取路由），
  统一 lifespan 显式承担 obs store + housekeeper + **canonical outbox**。
  `app.state.store` 恒为 obs store（既有读端契约），control store 挂
  `app.state.control_store`；命名冲突是刻意用两个名字避免的。
  控制面路由直接并入主 app（mount 会与观测 `/api/*` 抢前缀）。
- **`redpilot/worker/driver.py`**：只做三件事 —— 校验配置、起观测面
  （`LiveState`/`LiveBus`/relay/roster/`:8080`）、把 `StatusBridge` 注入编排。
  **不新增任何"要不要解这道题"的逻辑**，否则又会出现两套口径。

---

## 6. 求解域（worker）

### 6.1 竞技场主循环

```
list_challenges → 能力分片过滤 → 优先级排序 → [舰队锁 + 平台 lease] 认领
   → 建 task epoch / 写题目上下文 → 多会话时间盒（Pi 求解 + eager 提交 + 止损）
   → 未解挂起，后续轮次递增时间盒重访 → 平台终态 → 下一题
```

| 机制 | 语义 | 代码/落点 |
|---|---|---|
| **task epoch** | 区分"同一平台任务的新一轮"与"进程重启"，只存元数据 | `_activate_task_epoch`、`work/*.epoch` |
| **challenge lease** | 舰队内排他：advisory 锁 + 平台 claim，双保险 | `_try_acquire_challenge_lease`、`work/.stoploss-locks/` |
| **能力分片** | `ADAPTER_CAPABILITIES` 主职 + `ADAPTER_EXTRA_CAPABILITIES` 兜底；unknown 用 `crc32(code)` 均匀分流 | `_capability_filter`、`_unknown_bucket` |
| **优先级** | 难度与分值排序，先易后难拿分 | `_prioritize` |
| **时间盒** | 单场会话上限随轮次递增；与单题终身预算联动 | `_adaptive_session_limits`、`ControllerConfig` |
| **多会话重访** | 同题换新上下文，MEMORY 只带结论，Heimdall 带"为什么放弃" | `_shared_board_for`、`_heimdall_init` |
| **eager 提交** | 会话进行中盯 FLAG 文件，过 grounding 门即投递 | 交付账本一族函数 |
| **跨场续接** | `.continuation.json` 只记录"哪条路已证死"，same-epoch 才生效 | `_CONTINUATION_VERSION` |
| **舰队监督** | worker-1 只写重载标记；被监督者会话边界 `os._exit(86)` | `supervisor.py`、`_reload_watch` |
| **协作热重载** | 新码就位后由被监督者自行退出，零孤儿、零网络中断 | 同上 |

> **技能视角（long-horizon-prompting）**：竞技场就是"长时程自主运行"的 harness。
> 它已经具备：努力地板（时间盒 + 单题终身预算）、返回条件（flag 过闸门 + 平台终态）、
> 非计数结果登记（unverified/rejected ledger）、受阻路线登记（continuation/黑板）、
> 对抗审计（skeptic）。不足之处见缺口 D4（努力地板与"5 分钟爆破上限"仍是 prompt 级）。

### 6.2 求解引擎：Pi Agent

```
factory.create_solver(SolverConfig)
  └─ PiAgentSolver
       └─ transport = rpc（默认） | print（回退/老版 pi）
            - rpc：常驻 JSONL，prompt 走 stdin，agent_settled 收尾，可 steer/abort
            - print：一次性，prompt 在 argv，进程结束即会话结束
```

- **传输差异被挡在传输层**（`pi_transport.py`）：事件处理循环对两种传输完全同接口，
  协议帧过滤、扩展弹窗自动应答、常驻进程收尾都在这一层吸收。
- **RPC 专属护栏**：prompt 被接受却迟迟没有 `agent_start`（模型/凭据解析失败时
  pi 静默）→ 超过 `ADAPTER_RPC_STARTUP_GRACE`（默认 90s）判 `rpc_no_agent_start`。
- **provider/模型可配**：`ADAPTER_PROVIDER` 决定 `--model` 前缀与凭据 env 名；
  未登记 provider 自动归入 `<PROVIDER>_API_KEY` + OpenAI 兼容。
- **技能面渐进式披露**：系统提示只放 75 个技能的名字 + 描述，Agent 按需 `read`；
  框架用 top-2 关键词预选当先验。
- **进程回收靠令牌**：逐次访问生成随机 token 写 `_instance.json`，pi 及全部子孙
  继承该环境变量，收尾时扫 `/proc/*/environ` 回收 —— 驱动崩溃后仍然有效，
  且按构造无法误伤 driver / VPN provider / 另一个 worker。

> **技能视角（filesystem-context）**：技能装载是 Pattern 4（动态技能加载）的教科书实现 ——
> 把 O(n) 的静态 token 成本压成 O(1)/题；transcript 是 Pattern 5（终端/日志持久化 + grep 检索）。

### 6.3 flag 判分与提交（两道闸门）

```
候选提取 extract_flags
  → 置信度分级 flag_confidence
       ├─ 幻觉族（≤0.3）：placeholder / low_entropy / agent_authored / not_grounded → 拒
       └─ 推导族（0.4~0.6）：local_computed_only / local_derived / no_source_cmd → 复核
  → 确定性 grounding 门：候选必须逐字出现在本场真实工具输出（按离线/远程证据策略）
  → eager 提交（交付账本去重、拒绝账本防重试）
  → 平台 response 为唯一最终判据（correct / duplicate / …）
```

| 设计点 | 实现 | 对应的判分器纪律 |
|---|---|---|
| 证据先于分数 | 候选必须逐字命中真实输出 | `advanced-evaluation`：先证据后评分 |
| 确定性预检 | grounding/evidence policy 在 LLM 之前 | 先 schema/来源预检，再进语义判断 |
| 面板投票 | `skeptic_votes` 多票 | Panel of LLMs，抵抗单模型偏差 |
| 置信校准 | rescue ≥0.75 / veto ≥0.85 阈值 | 置信度映射到动作，而非直接采信 |
| 防自我增强 | `agent_authored` 直接拒；远程/本地证据分策略 | 生成者不能给自己打分 |
| 分族记账 | 幻觉族 vs 推导族 | 攻击面不同：一个删、一个复核 |

**提交之后**：平台 correct 写 FLAG 账本；duplicate / rejected 进拒绝账本并
从候选文件里摘除，防止同一候选无限重投。多段题（multiflag）按题面推进，
hint 后的 dry-session 截止阈值单独配置。

### 6.4 记忆与证据层

| 层 | 载体 | 生命周期 | 内容 |
|---|---|---|---|
| 工作记忆 | Pi 会话上下文 | 单场会话 | 思考、工具输出、当下计划 |
| 短期 | `work/.live/<worker>.json`、`work/status/worker-N.json` | 进程/会话 | 观测快照 18 键、编排进度 |
| 长期（逐题） | 题目目录 `MEMORY.md`、`_blackboard.json`、`_instance.json`、`transcript.jsonl` | 题目存活期 | 事实图谱、结论、原始实录 |
| 长期（跨场） | `.continuation.json` | 同 task epoch | 仅"哪些路线已证死"等元数据 |
| 语义观察 | `<heimdall-map>`（只读观察者） | 会话间 | DEAD/LOCK（可撤销）、TENSION（只并置） |
| 负记忆 | rejected / unverified ledger | 题目存活期 | 被拒候选、未证候选及原因 |

> **技能视角（memory-systems）**：分层与"失效但不丢弃"（invalidate but don't discard）是对的；
> 但黑板只有增、没有撤/陈旧标记，时间有效性只做到 epoch 级 —— 这正是
> `docs/graph-engineering-design.md` 要补的（缺口 D2）。**不要为此引入图数据库**：
> 先补 `origin/evidence/lifecycle` 三个字段，仍存 SQLite/文件。

**失败隔离红线**（I7）：Heimdall、verifier LLM、relay、态势台全部允许缺席；
缺席时的降级必须是"少一层判断"，不能是"停止解题"。
`hallucination.py` 的存在理由就是把"判断器犯错"与"解题器犯错"记账分开。

### 6.5 worker 观测面

```
orchestrator._update_status ──▶ StatusBridge ──▶ LiveState(原子写 .live/<w>.json)
                                              └▶ LiveBus ──┬─▶ relay ──▶ obs ingest（HTTP）
                                                           └─▶ dashboard :8080（本地 SSE）
roster.py ──▶ /work/.live/roster.json ──▶ relay + dashboard（单实例轮询，避免双写）
```

- `LiveState` 快照是 **18 键 wire 契约**（`contracts/snapshot.py`），
  与编排层字段名不同是刻意的；映射只允许在 `StatusBridge` 一处。
- 高频 progress 只走内存 + SSE，边界事件才落盘（1s 节流）。
- 快照带 `_` 前缀的带外键（如 closing 帧的明文 flags）**只走 relay → 平台**，
  绝不向浏览器广播、不落快照。
- 本地 `:8080` 默认只绑回环；compose 内显式 `0.0.0.0`（docker-proxy 需要），
  宿主侧再收成回环发布。

---

## 7. 平台域（server）

### 7.1 控制面

```
challenges.py（业务外观：评分规则/靶场预留）
scheduling.py（调度外观：evaluation/job/attempt 状态机）
        └── ControlPlaneService ── Store（SQLite，事务）
                  │                    └── outbox_events（同事务写入）
                  ▼
        provisioner.py（容器供给适配器）   vpn.py（openvpn 生命周期 + 指令黑名单）
```

- **状态机**（`contracts/platform.py`）：evaluation（queued/running/completed/canceled/expired）、
  job（pending/running/completed/failed/canceled）、attempt（starting/solving/submitting/
  closing/solved/done/failed/interrupted）。attempt 的 `submitting/closing` 也在
  "活跃"集合里，崩溃守卫按它判定。
- **凭据分离**：管理端点（VPN 生命周期）要 `REDPILOT_ADMIN_TOKEN`；
  worker 端要 `X-Worker-Token`。未配置 → 503 **fail closed**，不是放行。
- **VPN 即代码**：上传的 openvpn 配置有指令黑名单（script-security≥2、up/down、
  tls-verify、log/status、chroot 等），命中即 400 拒收，不静默剥离。
  以 `--script-security 1` 兜底。
- **outbox 投递语义**：2xx → 删除；429/5xx → 指数退避重试（更行保留）；
  其他 4xx → dead-letter（重试永不成功，防无界堆积）；传输异常 → 下轮重投。
  半配置（url/token 只配其一）只告警，不装配。

### 7.2 观测平台

```
                    ┌── canonical ingest（权威）── attempt.started/completed
worker relay ───────┤     来源：control outbox；runs.canonical=1
（写 token）        └── telemetry ingest（非权威）── live/events/run_close/roster/ping/accepted_flags
                         守卫：canonical 终态不可覆盖（同行拒写 + 跨行 attempt_id 守卫）

读端（读 token）：SSE /api/events；/api/status|roster|challenge|transcript|timeline|runs*
SPA：/（外壳公开）、/assets/*（公开）；其余 /api/* 全部需要凭据
```

- **两个写路径、一个权威**：终态只信 canonical；`run_close` 只能关闭
  `running/interrupted`，且不能改已 canonical 的行。
- **幂等地基**：events 表 `UNIQUE (run_id, seq)`，重复投递天然去重。
- **乱序容忍**：跨 run_id 合并规则在 `ObsStore.append_events/close_run`
  （`test_canonical_run_close.py` 是 P0 不变量）。
- **读端暴露明文 flag 与完整实录**，所以 token 必须与写端分离，
  且 `OBSERVABILITY_READ_TOKEN` 不转发给 worker 容器。
- **housekeeping**：events 是唯一无界增长路径；按保留天数 + 单批行数上限
  分批删除，避免长写锁阻塞 ingest。

### 7.3 统一装配（`app.py`）

合并后的服务面：

| 前缀 | 归属 | 示例 |
|---|---|---|
| `/openapi/v1/*` | control | challenges、vpn |
| `/api/v1/*` | control | evaluations、workers、attempts |
| `/api/internal/*` | obs | live/events/run_close/roster/ping/accepted_flags、canonical-events |
| `/api/*` | obs 读端 | status/roster/challenge/transcript/timeline/runs、SSE |
| `/` `/assets/*` | obs SPA | 前端外壳 |
| `/healthz` | control | 存活探测 |

`obs/control_proxy.py` 只在控制面是**外部**服务时启用（把 `/api/v1/*` 转出去）；
统一部署下默认关闭。

---

## 8. 数据架构

### 8.1 两库

| 库 | 默认路径 | 写者 | 主要表 | 权威性 |
|---|---|---|---|---|
| control | `data/redpilot.sqlite3` | `control/store.py`（单连接 + RLock） | tasks/challenges/submissions/evaluations/workers/jobs/attempts/platform_events/outbox_events | 题目与终态权威 |
| obs | `data/obs.sqlite3` | `obs/store.py`（进程单写者，WAL） | runs/events/live_state/roster_snapshot | 观测投影（canonical 事件承载权威终态） |

### 8.2 `/work` 布局（共享卷）

```
/work/
├── status/worker-<N>.json      # 编排进度（supervisor/只读控制台读）
├── .live/
│   ├── <worker_id>.json        # 观测快照（relay/态势台读）
│   ├── roster.json             # 题目总览（单实例轮询写）
│   └── digests/<code>.json     # transcript 折叠摘要
├── .stoploss-locks/            # 舰队级止损/认领 advisory 锁
├── *.epoch                     # task epoch 标记（元数据）
├── .reload.wid<N>              # 协作热重载标记
└── <safe_code>/                # 逐题工作目录（Pi 的 cwd）
    ├── MEMORY.md               # 长期记忆（结论/handoff）
    ├── _blackboard.json        # 事实图谱
    ├── .continuation.json      # 跨场续接（元数据，same-epoch）
    ├── _instance.json          # 进程回收令牌
    ├── transcript.jsonl        # pi 原始实录（append-only）
    ├── FLAG / flag.txt         # 候选 flag（提交入口）
    ├── 靶标产物、脚本、笔记
    └── CLAUDE.md               # 发给解题 Agent 的指令（taskprompt.write_context_md 写）
```

> **技能视角（filesystem-context）**：这是 Pattern 1/2/3/5 的组合 ——
> 大输出落 `transcript.jsonl`、计划落 `MEMORY.md`、子 Agent 经文件通信
> （黑板 + Heimdall map）、终端输出可 grep。清理策略在 obs 侧（events 保留天数），
> `work/` 由题目生命周期与部署方清理（缺口 D5：scratch 清理策略未在 worker 侧显式执行）。

### 8.3 线格式（wire contracts）

| 契约 | 定义处 | 改动纪律 |
|---|---|---|
| live 快照 18 键 | `contracts/snapshot.py` | 改名须同步 `LiveState` 与前端 `types.ts` |
| phase / run 状态 / 事件 kind | `contracts/vocabulary.py` | 前端 `types.ts` 是手工镜像，须同步 |
| 平台状态机与 canonical 词汇 | `contracts/platform.py` | SQL CHECK 与读端过滤共用 |
| 路径/文件名/心跳 | `contracts/paths.py` | compose healthcheck 读同一字面量 |
| relay/ingest HTTP | obs ingest 路由 + relay | 产品契约，跨模块只经 HTTP |

---

## 9. 关键时序

### 9.1 一题的生命周期（求解 → 提交）

```
worker                    Pi Agent                    平台            obs
  │ 认领（lease/claim）      │                          │               │
  │ 建 epoch/写上下文 ──────▶│                          │               │
  │ 启动会话（timebox）      │──list/start─────────────▶│               │
  │                         │◀─题目/容器───────────────│               │
  │                         │ 工具循环（侦察/利用）      │               │
  │ 观测桥 ─────────────────┼──────────────────────────┼──────────────▶│ live/events
  │ eager 扫描 FLAG ◀───────│ 写候选 flag              │               │
  │ grounding 门（证据）      │                          │               │
  │ submit ─────────────────┼─────────────────────────▶│               │
  │◀─ correct/duplicate ────┼──────────────────────────│               │
  │ 账本更新/会话收尾         │                          │               │
  │ attempt.completed ──────┼──────────────────────────┼──outbox─────▶│ canonical
```

### 9.2 权威终态路径（不与 telemetry 混）

```
control Store 事务：写 attempts 终态 + outbox_events（同一事务）
   → outbox loop：POST /api/internal/canonical-events（写 token）
   → obs canonical_ingest：runs.canonical=1
   → telemetry 的 run_close 只能在 running/interrupted 上收尾，不能覆盖
```

---

## 10. 横切关注点

### 10.1 配置与 env 边界（R7）

env 只在六处解析：`contracts/paths.py`（内核例外）、`control/config.py`、
`obs/config.py`、`worker/settings.py`、`worker/config.py`、`worker/adapter/config.py`。
其余文件的 env 直读是**存量债**（12 个文件，allowlist 只减不增，有 stale 测试兜底）。
全部可调项见 [`.env.example`](../.env.example) 与 [`docs/worker.md`](worker.md)。

### 10.2 安全模型

| 边界 | 机制 |
|---|---|
| 观测读/写 | 写 token 只给 worker；读端返回明文 flag，读 token 不转发 |
| 控制面 admin/worker | `REDPILOT_ADMIN_TOKEN` / `X-Worker-Token` 分离，缺失 503 fail closed |
| VPN 配置 | 指令黑名单 + `--script-security 1`，命中即拒 |
| 容器能力 | 只有 worker-1/monolith 有 `NET_ADMIN` + tun；无 docker socket |
| 数据脱敏 | `contracts/redact.py` 统一脱敏/截断；快照带外键不广播 |
| 进程隔离 | 回收令牌 + `/proc/*/environ` 扫描，按构造不误伤 |
| 网络 | 靶标仅在 VPN 内可达；server 不直接触达靶标 |

### 10.3 失败模式与降级矩阵

| 故障 | 行为 | 是否影响解题 |
|---|---|---|
| `OBSERVABILITY_URL` 未设 | relay 整体禁用 | 否 |
| obs 宕机 | outbox 退避重试；relay 重试；canonical 留在本地 | 否，恢复后补投 |
| 观测 token 未配 | ingest 响亮 503（不静默丢） | 否 |
| verifier LLM 不可用 | 降级为 grounding-only | 否（判断层变薄） |
| `ADAPTER_SKEPTIC=0` | skeptic 空操作，纯确定性规则 | 否 |
| Heimdall 拿不到 llm | 不启用观察者 | 否 |
| 本地态势台起不来 | 记异常继续 | 否 |
| 心跳停更 | compose healthcheck 判死重启（挂死探针已删除，见 driver 注释） | 是，靠重启恢复 |
| netns 被孤立 | 连续 3 次无默认路由 → `os._exit(3)` | 是，靠重启恢复 |
| VPN 层瞬断 | entrypoint 预检 → exit 4 | 是，靠重启恢复 |
| 配置缺失/坏值 | exit 0 明示停止（I8） | 是，人工介入 |
| 平台不可达 | `_await_task` 复查等待，不进死亡螺旋 | 否（等待恢复） |

### 10.4 进程生命周期与退出码

| 码 | 含义 | 后果 |
|---|---|---|
| `0` | 任务终态 / **配置错误** | 明示后停止，不重启 |
| `86` | 协作热重载 | 会话边界退出 → 换新码拉起 |
| `4` | VPN 层瞬断 | 拉起重试 |
| `3` | worker-2/3 被孤立 | 退出重建 netns |
| 其余非零 | 意外崩溃 | `on-failure` 拉起 |

权威表在 [`docs/worker.md`](worker.md)，此处只指向。

### 10.5 发给解题 Agent 的指令

`redpilot/worker/adapter/taskprompt.py` 的 `write_context_md()` 把指令写进每题工作目录的
`CLAUDE.md`（内容 = `_CLAUDE_MD` + `_ISOLATION_CONSTRAINT`）。它包含：
先侦察后利用、最小代价优先、不编造 flag、立即记录/提交、**反停滞**（同向 3 次失败换思路、
爆破 5 分钟无果换攻击面）、`INFRA_BLOCKED` 标记、会话结束的三段式续接块。
竞技场侧的续接回捞（`extract_handoff`）与它配套。

> 📌 仓库根曾有一份 `AGENTS.md`，文档一度称它「写进每道题的工作目录」——
> **那不是真的**：没有任何代码读它，逐题 `CLAUDE.md` 一直来自上面这个模块。
> 它已在 2026-09 清理中删除。

> **技能视角（long-horizon-prompting）**：这份指令是"成功谓词 + 非计数结果 +
> 返回条件 + 反停滞"的轻量版。缺口 D4：其中的努力地板（5 分钟）与部分护栏
> 仍是 prompt 级建议 —— 按该技能的判据，**必须被优化压力考验的约束要放进 harness**。

---

## 11. 边界执行与验证

| 层 | 命令 | 内容 |
|---|---|---|
| 架构红线 | `pytest -q tests/architecture` | 17 条：R1–R8 + 运行时 import 足迹 + 单一来源 |
| 模块单测 | `pytest -q tests/{contracts,control,obs,worker,app}` | 各模块行为 |
| 竞技场回归 | `pytest -q tests/` | 175 条：止损、task epoch、eager 提交、交付账本、多段题、supervisor 判据 |
| 回归全量 | `pytest -q` | 404 passed（README 记录） |

新增边界的工作流：先在 `test_layers.py` 的 `FORBIDDEN`/白名单里写规则（红），
再改代码让规则变绿。**allowlist 只许缩短**（`test_env_reads_are_collected` 的
stale 兜底防止"顺手把新债加进白名单"）。

---

## 12. 部署

- **镜像**：worker 用 Kali 基底（`Dockerfile.base → redpilot/kali → Dockerfile`，
  预置安全工具 + pi CLI）；server 用 `python:3.13-slim`（`Dockerfile.redpilot`）。
  `[platform]` extra 不进 Kali 镜像。
- **compose 形态**：裸 `docker compose up -d` 起 server + 舰队三容器；
  单体点名单个 `worker`（profile `monolith`），不要 `--profile monolith up -d`
  （会连带拉起舰队）。
- **卷**：`/work` 共享卷（状态协作）、`data/`（两库）、`skills/`（技能面）。
- **凭据**：`BENCHMARK_TOKEN` / `BENCHMARK_BASE_URL` / `<PROVIDER>_API_KEY` /
  `OBSERVABILITY_TOKEN` / `OBSERVABILITY_READ_TOKEN`；详见 `.env.example`。

---

## 13. 演进路线

> 以下均为**未实施**项，按"先修执行点、再上能力"排序。

| # | 事项 | 依据 | 建议动作 |
|---|---|---|---|
| D1 | R7 env 收编存量债 12 文件 | `tests/architecture/test_layers.py` `ENV_DEBT` | 每迁一个文件从集合删除；优先 `orchestrator.py`（10 处直读） |
| D2 | 黑板缺 `origin/evidence/lifecycle` | `docs/graph-engineering-design.md` | 加三字段与撤销/陈旧语义，仍存 SQLite/文件；先补"协议对比测试" |
| D3 | 求解 Agent 无文件系统沙箱 | 本设计 §1.3 | **已实施首批**：Pi 降权（uid 10001）+ 控制面状态搬家 + ACL/chown 交接，见 `docs/solver-isolation-design.md` §4–§5、§14；容器内真降权待探针 |
| D4 | 努力地板/爆破上限仍是 prompt 级 | `taskprompt._CLAUDE_MD` vs `stoploss.py` | **已实施首批**：SurfaceLedger（软提示 → `steer` → `abort`+`follow_up`），见 `docs/solver-isolation-design.md` §6、§14；`bash_guard` 错误翻译未做 |
| D5 | `work/` scratch 清理策略 | `filesystem-context` Gotcha 1 | **已实施首批**：终态 `.closed` + 保留天数、默认 dry-run、digest 前置，见 `docs/solver-isolation-design.md` §7、§14 |
| D6 | 确定性侦察 sidecar | `docs/deterministic-recon-design.md` | T0 秒级探针先出、LLM 立即开工；结构化工具输出（nmap -oX 等）编译成事实 |
| D7 | ~~`fastapi-console` 与 `web/` 前端归并~~ | — | **已消解**：`fastapi-console/` 于 2026-09 删除（与 `redpilot/obs` 功能重复且走另一条数据链） |
| D8 | 事件密集路径的 obs 写入放大 | obs housekeeping 设计 | 已有保留天数 + 分批删除；观察后再决定是否采样 |

---

## 14. 决策记录（ADR 摘要）

| 决策 | 选择 | 主要理由 |
|---|---|---|
| 打包形态 | 单发行版模块化单体 | 包拆分只买到安装闭包；边界改用 AST 测试执行（`docs/modular-monolith-design.md`） |
| 控制面与观测合并 | 同一 FastAPI `:8000`，两库 | 部署简单；权威性靠 canonical/telemetry 分离而非进程分离 |
| 舰队拓扑 | worker-1 持 VPN，2/3 复用 netns | 单隧道原则；消费者 self-exit 自愈 |
| 危险能力 | worker-1 无 docker socket，只能请求重启 | 从架构根除而非纪律约束 |
| 求解引擎 | Pi Agent 唯一 | 避免多引擎行为漂移 |
| 传输 | rpc 默认、print 回退 | 靠 settled 收尾/steer/token 遥测；老版 pi 自动降级 |
| 提交闸门 | grounding 确定性门 + 平台判据 | 证据先于分数；平台是唯一不可伪造判据 |
| 幻觉/推导分族 | 两族分开记账 | 拒收与复核是两种动作，混在一起会误杀真推导 |
| 记忆载体 | 文件 + SQLite，无图数据库 | 先补 origin/evidence/lifecycle 三字段，够用再加 |
| 观测面 | 可选件、失败隔离 | 坏一层判断 ≠ 停止解题 |
| 配置错误 | exit 0 | `restart: on-failure` 会重启一切非零退出 |
