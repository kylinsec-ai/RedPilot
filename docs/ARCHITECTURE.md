# Ghost：自主红队 Agent Benchmark Harness

> 版本：2025-06-26（基于仓库当前 HEAD 的冻结诊断）  
> 状态：架构 RFC，只新增文档，不改动源码。  
> 定位：**Ghost 是评估控制面与观测平台，不是 Agent 本身**。Agent（pi backend）只是被测实现之一，可替换。

---

## 1. 设计原则

- **评估平台优先**：控制面（core）提供可复现的靶场生命周期与计分；观测面（obs）提供可审计的证据链；Agent（worker）只是调用方之一。
- **拒绝过度微服务化**：保留四个可部署单元（contracts / core / worker / obs），以进程为单位隔离，不拆到函数级服务。
- **单进程 SQLite**：core 与 obs 各持一个 SQLite WAL 文件，单写者（`workers=1`）是硬约束；暂不引入 Postgres/Kafka/gRPC。
- **权威数据源唯一**：canonical attempt 事件（`attempt.started` / `attempt.completed`）是终态唯一权威；telemetry（live/events/run_close）仅用于观测，不用于计分。
- **信任边界显式**：flag/token/transcript 的plaintext 只出现在受控边界内；obs 读端无鉴权是设计选择，但需明确其暴露范围。

---

## 2. 现状诊断（引用实际路径/符号）

### 2.1 core：API + Store + Provisioner 职责混杂

| 文件 | 符号 | 问题 |
|------|------|------|
| `packages/core/ghost/api.py` | `create_app()` | 同一个 FastAPI 实例同时挂载 **Challenges API**（`/openapi/v1/challenges/*`，面向 participant/agent）和 **Control Plane API**（`/api/v1/evaluations/*`、`/api/v1/workers/*`、`/api/v1/attempts/*`，面向 worker）。两者鉴权体系不同（`BENCHMARK_TOKEN` vs `X-Worker-Token` vs `GHOST_ADMIN_TOKEN`），却共享同一个 `Store` 与 `ChallengeService`。 |
| `packages/core/ghost/store.py` | `Store` | 一个类同时管理：① challenge/submission 业务状态（`challenges`、`submissions` 表）；② evaluation/job/attempt 控制面状态（`evaluations`、`jobs`、`attempts` 表）；③ 平台事件与 outbox（`platform_events`、`outbox_events` 表）。事务边界复杂，任何一层的 schema 变更都可能影响其他层。 |
| `packages/core/ghost/service.py` | `ChallengeService` | 业务规则（start/hint/submit/close）与 provisioner 调用（`ContainerProvisioner.start/stop`）耦合在同一个粗粒度锁内。 |
| `packages/core/ghost/provisioner.py` | `DockerProvisioner` / `StaticProvisioner` | 启动逻辑直接执行 `subprocess.run(["docker", ...])`，属于 side-effect 密集型操作，与 service 的单元测试难以隔离。 |
| `packages/core/ghost/api.py` | `_dispatch_outbox()` | core  lifespan 内嵌了向 obs 异步投递 canonical events 的循环，使 core 进程隐含依赖于 obs 的可用性；半配置（url/token 只配一个）时日志告警但无熔断。 |

### 2.2 worker：Orchestration / Relay / Pi 边界模糊

| 文件 | 符号 | 问题 |
|------|------|------|
| `packages/worker/ghost_worker/orchestration.py` | `solve_one()` | 直接驱动 pi 会话（`asyncio.to_thread(solver_backend.solve, ...)`），同时负责：① 平台 API 调用（start/hint/submit/close）；② LiveReporter 状态更新；③ relay flush；④ transcript 压缩。一个函数横跨业务编排、LLM 后端、观测上报三层。 |
| `packages/worker/ghost_worker/relay.py` | `ObsRelay._on_frame()` / `_close_run()` | relay 在 legacy 模式下自行推断 run 终态（solved/done/failed）并发送 `run_close`；在 assignment 模式下却要判断 `attempt_id` 存在时跳过 `run_close`，因为 canonical 事件拥有生命周期权威。这种“有时发终态、有时不发”的双轨逻辑增加了认知负担。 |
| `packages/worker/ghost_worker/driver.py` | `_solve_assignment()` / `amain()` | driver 同时支持 **legacy 模式**（直接拿 `BENCHMARK_TOKEN` 调用 Challenges API）和 **assignment 模式**（向 core 控制面 claim job）。两个模式的循环、心跳、错误处理路径不同，长期共存增加了维护成本。 |
| `packages/worker/ghost_worker/live/state.py` | `LiveState` | worker 本地写 `.live/<worker>.json` 与向 obs relay 推 live 帧是两条独立链路，没有统一的原子提交点。 |

### 2.3 obs：Ingest / Read / Proxy 混杂

| 文件 | 符号 | 问题 |
|------|------|------|
| `packages/obs/obs/read.py` | `control_proxy()` | obs 读端同时承担 **控制面反向代理**（`/api/v1/{path:path}`），把浏览器的 `X-Platform-Admin-Token` 透传给 core。这模糊了 obs（观测）与 core（控制）的边界。 |
| `packages/obs/obs/ingest.py` | `post_canonical_events()` / `post_run_close()` | 同一个 `ObsStore` 同时接收来自 **worker relay** 的 telemetry（`events`、`live`、`run_close`）和来自 **core outbox** 的 canonical events（`attempt.started` / `attempt.completed`）。虽然 `runs` 表已有 `attempt_id` 关联，但摄取端点未在 schema/API 层面严格区分“权威源”与“非权威源”。 |
| `packages/obs/obs/store.py` | `close_run()` | 对 relay 的 `run_close` 和 core 的 `attempt.completed` 使用同一套 `CLOSABLE_STATUSES` 检查，但两者的业务语义不同：前者是 worker 侧的乐观关闭，后者是控制面的权威终态。 |

### 2.4 跨包依赖

- `contracts` 目前保持零依赖（`tests/test_purity.py` 验证），是唯一干净的层。
- `worker → obs.localserver` 的导入（`obs.localserver.serve_forever_in_thread`）是允许的跨界点，但 `worker` 不应再导入 `obs` 的其他模块。
- `core` 通过 HTTP outbox 推送事件到 `obs`，没有代码级导入，符合边界。

---

## 3. 目标最小架构：四个可部署单元

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              外部参与者                                       │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────────────────────────┐  │
│  │ 管理员    │  │ 浏览器/仪表板  │  │ Worker 容器 (Kali + Pi + VPN)        │  │
│  │(Admin)   │  │ (Observatory)│  │ ┌────────────┐  ┌────────────────┐  │  │
│  │          │  │              │  │ │ Agent Core │  │ Relay + Local  │  │  │
│  │ GHOST│  │              │  │ │ (pi backend)│  │ Server         │  │  │
│  │ _ADMIN_  │  │              │  │ └─────┬──────┘  └────────┬───────┘  │  │
│  │ TOKEN    │  │              │  │       │                 │           │  │
│  └────┬─────┘  └──────┬───────┘  └───────┼─────────────────┼───────────┘  │
│       │               │                    │                 │              │
│       │  HTTPS        │  HTTPS             │  HTTPS          │  HTTPS       │
│       │  ( privileged)│  (read-only)       │  (assignment)   │  (telemetry) │
│       ▼               ▼                    ▼                 ▼              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │                         Ghost 平台                                │  │
│  │  ┌─────────────────────┐        ┌─────────────────────────────────┐  │  │
│  │  │   core (控制面)      │        │   obs (观测平台)                 │  │  │
│  │  │  FastAPI + SQLite    │───────▶│  FastAPI + SQLite + SPA          │  │  │
│  │  │  - Challenges API    │ outbox │  - ingest (internal)             │  │  │
│  │  │  - Control Plane API │        │  - read (public)                 │  │  │
│  │  │  - Provisioner       │        │  - SSE / runs / timeline         │  │  │
│  │  └─────────────────────┘        └─────────────────────────────────┘  │  │
│  │           ▲                                                          │  │
│  │           │                                                          │  │
│  │  ┌────────┴────────┐                                                 │  │
│  │  │   contracts      │  (零依赖共享包: vocabulary, EventEnvelope,     │  │
│  │  │   (stdlib only)  │   redact, fsio, digest, paths, assets)        │  │
│  │  └─────────────────┘                                                 │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 进程边界与依赖方向

| 单元 | 进程模型 | 权威数据 | 依赖谁（代码 import） | 依赖谁（网络） |
|------|----------|----------|----------------------|----------------|
| **contracts** | 库（无进程） | 词汇/契约 | 无（stdlib only） | 无 |
| **core** | 单进程 FastAPI (`workers=1`) | `data/ghost.sqlite3` | contracts | obs（outbox HTTP POST） |
| **worker** | 单容器，单事件循环 | `/work` 目录、本地 `.live` | contracts、**obs.localserver**（注入式） | core（assignment API + Challenges API via SDK）、obs（relay HTTP POST） |
| **obs** | 单进程 FastAPI (`workers=1`) | `data/obs.sqlite3` | contracts | core（read.py 中的 control proxy，可选） |

**依赖方向约束**：
- `contracts` ← 所有人
- `core` → `obs`（HTTP outbox，无代码 import）
- `worker` → `core`（HTTP，SDK / AssignmentClient）
- `worker` → `obs`（HTTP relay，无代码 import）
- `worker` → `obs.localserver`（允许的代码 import，仅限驱动与 roster）
- **禁止** `obs` → `core` 的代码 import；`read.py` 的 control proxy 只是网络转发代理。

---

## 4. 端到端链路

```
Benchmark API (Challenges API)
    │  participant/agent 或 worker SDK 调用
    ▼
Run Orchestrator (core Control Plane)
    │  创建 evaluation → 生成 jobs → worker claim → 生成 attempt
    ▼
Target Adapter (Provisioner)
    │  StaticProvisioner / DockerProvisioner.start()
    ▼
Agent Adapter (worker orchestration.solve_one)
    │  调用 Pi backend (solver_backend.solve)
    ▼
Policy / Sandbox (Pi 内置工具沙箱 + VPN 网络隔离)
    │  工具执行、网络扫描、flag 提取
    ▼
Evidence / Telemetry (worker relay + core outbox)
    │  ① worker relay → obs (events/live/ping/roster)
    │  ② core outbox → obs (canonical attempt events)
    ▼
Evaluator (core store + ChallengeService.submit)
    │  SHA-256 哈希比对、扣分规则、累计得分
    ▼
Immutable Result (core platform_events + outbox)
    │  attempt.completed 事件写入 core DB，并通过 outbox 至少一次投递到 obs
    ▼
Observatory / UI (obs read API + SPA + SSE)
    │  /api/runs, /api/timeline, /api/status, /api/events (SSE)
    ▼
人可读的可视化与审计
```

---

## 5. 每层责任、输入/输出、权威数据源、状态机

### 5.1 Benchmark API（Challenges API）

- **责任**：为 participant 提供题目生命周期与 flag 提交接口。
- **输入**：`BENCHMARK_TOKEN` Header, `unique_code`, flag plaintext。
- **输出**：`ChallengeResponse`, `SubmitResponse`（correct/awarded/cumulative_score）。
- **权威数据源**：`core/store.py` 的 `challenges` + `submissions` 表。
- **状态机**：
  - challenge 级别：`stopped → pending → available → stop_pending → stopped`
  - 提交级别：按 `(task_token, unique_code, flag_index)` 唯一，幂等由 `DuplicateSubmission` 保护。

### 5.2 Run Orchestrator（Control Plane API）

- **责任**：把一次 evaluation（对一个 task 的完整评估）拆分为 jobs（每题一个），再调度给 workers（claim/lease/heartbeat/complete）。
- **输入**：`GHOST_ADMIN_TOKEN`（创建 evaluation）、`X-Worker-Token`（worker 操作）。
- **输出**：`AssignmentRow`（含 `task_token` / `lease_id` / `attempt_id`）。
- **权威数据源**：`core/store.py` 的 `evaluations` / `jobs` / `attempts` / `platform_events` 表。
- **状态机**：

```
evaluation:  queued ──▶ running ──▶ completed
                      │           │
                      ▼           ▼
                    canceled    expired

job:        pending ──▶ running ──▶ completed
                        │         │
                        ▼         ▼
                      interrupted(回pending)  failed

attempt:    starting ──▶ solving ──▶ submitting ──▶ closing
                                              │
                                              ▼
                          ┌───────────────────┼───────────────────┐
                          ▼                   ▼                   ▼
                        solved               done               failed
                          │
                          └─────────────────────────────────────────┘
                                          ▲
                                          │
                                    interrupted (lease lost / evaluation canceled)
```

- **取消/超时/重试**：
  - **取消**：`POST /api/v1/evaluations/{id}/cancel` 把 evaluation 及下属 pending/running jobs 全部置为 canceled；running attempts 置为 interrupted。
  - **超时**：job lease 过期（`lease_expires_at < now`）由下一次 `claim_job` 的事务内回收，attempt 置 interrupted，job 回 pending。
  - **重试**：attempt 失败（`failed/interrupted`）后，job 回 pending，由下一轮 claim 重新分配；`attempt_no` 递增。

### 5.3 Target Adapter（Provisioner）

- **责任**：把 challenge definition 转换为可访问的网络端点。
- **输入**：`ChallengeDefinition`（image/container_addr/container_port/docker_network）。
- **输出**：`ProvisionedContainer(addresses, container_id)`。
- **权威数据源**：`core/store.py` 的 `challenges.container_addr_json` + `container_status`。
- **实现**：`StaticProvisioner`（用配置地址）或 `DockerProvisioner`（docker run）。

### 5.4 Agent Adapter（Worker Orchestration）

- **责任**：领取 assignment 后，驱动一次 LLM 会话完成单题求解。
- **输入**：`AssignmentRow` / `Challenge`（SDK 模型）、`SolverConfig`、pi backend。
- **输出**：`attempt.complete`（status=solved/done/failed/interrupted）、accepted flags 列表、transcript 文件。
- **权威数据源**：无本地权威；所有计分以 core 的 `submissions` 表为准。
- **状态机（worker 侧）**：
  - `idle → starting → solving → submitting → closing → idle`
  - 异常分支：`error`（通用异常）、`interrupted`（lease lost / VPN 失败）。
- **取消/超时/重试**：
  - **取消**：lease lost 时 `asyncio.Event` 触发，`solve_task.cancel()`，上报 `interrupted`。
  - **超时**：pi 会话由 `SOLVER_SESSION_SECONDS` 硬截断（pi 层实现）。
  - **重试**：`START_MAX_RETRIES=8`（容器启动）、`PROVIDER_FAILURE_RETRIES=2`（LLM provider 失败）、`CLOSE_RETRIES=3`（容器关闭）。

### 5.5 Policy / Sandbox

- **责任**：限制 Agent 可执行的操作范围；目前由 pi 内置工具策略 + VPN 网络隔离实现。
- **信任边界**：Agent 运行在 worker 容器内，具有 root 权限（Kali 镜像）；靶场容器与 worker 容器通过网络隔离（VPN / docker network）。
- **输入**：pi 工具调用（bash/python 等）。
- **输出**：工具 stdout/stderr，受 `ghost_contracts.redact` 摘要规则约束（relay 侧 summarize_args）。

### 5.6 Evidence / Telemetry

- **责任**：收集求解过程的证据与实时遥测。
- **分两条链路**：
  1. **Telemetry（非权威）**：worker `relay.py` → obs `/api/internal/{events,live,run_close,roster,ping}`。包含 transcript 事件行、live 快照、roster 快照。
  2. **Canonical Events（权威）**：core `api.py:_dispatch_outbox()` → obs `/api/internal/canonical-events`。只包含 `attempt.started` 和 `attempt.completed`。
- **权威数据源**：core 的 `platform_events` 表 + `outbox_events` 表。
- **幂等保证**：obs `events` 表有 `UNIQUE(run_id, seq)`；core `platform_events` 表有 `UNIQUE(attempt_id, seq)`。

### 5.7 Evaluator

- **责任**：判定 flag 正确性并计分。
- **输入**：flag plaintext（来自 participant 或 worker SDK）。
- **输出**：`SubmitResponse`（correct/awarded/matched_flag_index）。
- **权威数据源**：`core/store.py` 的 `challenges.flags_json`（SHA-256 哈希）+ `submissions` 表（已提交索引）。
- **规则**：hint 被查看后，本题后续所有正确 flag 按 `hint_cost_radio` 比例扣分（`discounted_score()`）。

### 5.8 Immutable Result

- **责任**：保存不可变的评估结果与审计事件。
- **权威数据源**：
  - core：`attempts` 表（终态）+ `platform_events` 表（事件序列）。
  - obs：`runs` 表（投影）+ `events` 表（telemetry 投影）。
- **一致性模型**：core 对 obs 是 **至少一次投递**（outbox 模式）；obs 以 `attempt_id` 为主键对 canonical 事件做幂等写入。

### 5.9 Observatory / UI

- **责任**：人机可读的可视化、实时 SSE、历史查询。
- **输入**：obs `ObsStore` 的查询结果。
- **输出**：`/api/status`（最新 live 快照）、`/api/events`（SSE）、`/api/runs`（历史）、`/api/timeline`（折叠时间线）。
- **信任边界**：读端 **无鉴权**，暴露 plaintext flags 与完整 transcript；默认绑定回环（`STATUS_BIND=127.0.0.1`），compose 内显式 `0.0.0.0` 由 docker-proxy 转发。

---

## 6. 最小版本化事件 Envelope 与 REST API

### 6.1 事件 Envelope（已存在，contracts 层）

当前实现：

```python
# packages/contracts/ghost_contracts/platform.py
@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    event_type: str
    evaluation_id: str | None
    job_id: str | None
    attempt_id: str | None
    worker_id: str | None
    seq: int
    occurred_at: float
    payload: dict[str, Any]
    schema_version: int = 1
```

- **schema_version = 1** 是当前唯一合法版本。
- **seq**：attempt 内单调递增，由 `Store._next_event_seq_locked()` 分配。
- **event_id**：`uuid4().hex`，全局唯一。
- **约束**：`event_type` 非空、`seq >= 0`、`occurred_at` 有限。

### 6.2 权威终态来源：Canonical Attempt Event

core 通过 outbox 向 obs 投递以下两类事件，**obs 将其视为权威**：

| event_type | 发送方 | 语义 | 幂等键 |
|------------|--------|------|--------|
| `attempt.started` | core | attempt 创建，lease 生效 | `(attempt_id, seq=0)` |
| `attempt.completed` | core | attempt 终态（solved/done/failed/interrupted） | `(attempt_id, seq=N)` |

- **obs 处理**：`ingest.py:post_canonical_events()` 把 `attempt.started` 映射为 `runs` 表 `running` 行（`run_id=attempt_id`）；把 `attempt.completed` 映射为 `close_run(canonical=True)`。
- **优先级**：canonical 是唯一权威终态，relay `run_close` 永不覆盖 canonical：
  `close_run(canonical=False)` 在目标行 `canonical=1` 时拒写；若同一 `attempt_id` 任一行已 `canonical=1`（即使 `run_id` 不同、relay 未带 `attempt_id` 但行上有），跨行一律拒写。canonical `attempt.completed` 始终可写，可覆盖 relay 乐观终态（含 `running`/`interrupted`/relay 终态），重复投递幂等。
- **单行归一**：同一 `attempt_id` 只留一行。`append_events()` 在 `attempt_id` 非空时先按 `attempt_id`（`canonical DESC, started_at DESC, rowid DESC`）找已有关联行，命中则复用其 `run_id` 落事件，不另起行；复用时仅回填 `evaluation_id`/`job_id` 与占位值（`unknown`/空 `worker`/`model` → 真实值），绝不用 `unknown` 覆盖真实值、不碰 `status`。
- **乱序不丢**：`attempt.completed` 先到且无行可关时建占位终态行（`run_id=attempt_id`，`challenge_code="unknown"`，`started_at=ended_at`，`canonical=1`）保留终态；后补 `attempt.started` 复用该行（`INSERT OR IGNORE` 不洗终态，仅回填 `challenge_code`/`evaluation`/`job`）。

### 6.3 Telemetry（非权威）

worker relay 向 obs 投递的数据：

| 端点 | 内容 | 用途 |
|------|------|------|
| `POST /api/internal/events` | transcript 事件行（seq, type, payload） | 重构求解过程 |
| `POST /api/internal/live` | LiveState 快照（phase/code/turns/error...） | 实时仪表板 |
| `POST /api/internal/run_close` | run 终态（status/error/flags_accepted） | legacy 模式关闭 run |
| `POST /api/internal/roster` | 题目总览快照 | 挑战列表 |
| `POST /api/internal/ping` | worker 心跳 | 存活判定 |

- **非权威声明**：telemetry 可被 replay、可被 worker 伪造（理论上）、可被网络重排；obs 只用它做观测与调试，**不参与计分**。
- **run_id vs attempt_id**：telemetry 使用 relay 自生成的 `run_id`（uuid4().hex）；canonical 使用 core 分配的 `attempt_id`。obs `runs` 表同时保留两者，通过 `attempt_id` 关联权威事件。

### 6.4 REST API 分层

**Core（控制面 + Challenges API）**：

```
# Participant / Agent
GET  /openapi/v1/challenges               (BENCHMARK_TOKEN)
POST /openapi/v1/challenges/start         (BENCHMARK_TOKEN)
GET  /openapi/v1/challenges/hint          (BENCHMARK_TOKEN)
POST /openapi/v1/challenges/submit        (BENCHMARK_TOKEN)
POST /openapi/v1/challenges/close         (BENCHMARK_TOKEN)

# Worker
POST /api/v1/workers/{id}/register        (X-Worker-Token)
POST /api/v1/workers/{id}/heartbeat       (X-Worker-Token)
POST /api/v1/workers/{id}/claim           (X-Worker-Token)
POST /api/v1/attempts/{id}/heartbeat      (X-Worker-Token)
POST /api/v1/attempts/{id}/events         (X-Worker-Token)
POST /api/v1/attempts/{id}/complete       (X-Worker-Token)

# Admin
POST /api/v1/evaluations                  (GHOST_ADMIN_TOKEN)
GET  /api/v1/evaluations
GET  /api/v1/evaluations/{id}
POST /api/v1/evaluations/{id}/cancel
GET  /api/v1/workers
GET  /api/v1/attempts/{id}/events
GET  /openapi/v1/vpn/status               (GHOST_ADMIN_TOKEN)
POST /openapi/v1/vpn/config
POST /openapi/v1/vpn/start
POST /openapi/v1/vpn/stop
```

**Obs（观测平台）**：

```
# Ingest (internal, X-Observability-Token)
POST /api/internal/live
POST /api/internal/events
POST /api/internal/run_close
POST /api/internal/roster
POST /api/internal/ping
POST /api/internal/canonical-events      (from core outbox)

# Read (public, unauthenticated)
GET  /api/health
GET  /api/status
GET  /api/events                         (SSE)
GET  /api/roster
GET  /api/challenge?code=
GET  /api/transcript?code=
GET  /api/timeline?code=
GET  /api/runs
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/events
GET  /api/runs/{run_id}/timeline
GET  /api/v1/{path:path}                 (control proxy, optional)
```

---

## 7. 信任边界

### 7.1 Flag

- **存储**：core `challenges.flags_json` 存储 SHA-256 哈希，**不存 plaintext**。
- **传输**：worker 通过 SDK `submit_flag(code, flag)` 传输 plaintext；HTTPS 传输。
- **落盘**：worker `/work/<code>/FLAG` 文件可能包含 plaintext 候选（含错误尝试）；obs `runs.flags_accepted` 存储 plaintext（来自 relay 或 canonical 事件）。
- **暴露面**：obs 读端无鉴权，`/api/runs` 与 `/api/transcript` 可返回 plaintext flags；默认绑定回环，需显式配置才能外网访问。

### 7.2 Token

| Token | 持有者 | 权限 | 失效方式 |
|-------|--------|------|----------|
| `BENCHMARK_TOKEN` | participant / worker | 题目操作（start/submit/close） | task 过期/停止 |
| `X-Worker-Token` | worker | claim job / heartbeat / append events / complete | core 侧 `GHOST_WORKER_TOKEN` 变更 |
| `GHOST_ADMIN_TOKEN` | 平台管理员 | evaluation 生命周期 / VPN 操作 | 环境变量变更 |
| `X-Observability-Token` | core / worker | 向 obs 写入数据 | obs 侧 `OBSERVABILITY_TOKEN` 变更 |

- **常量时间比较**：所有 token 比较均使用 `hmac.compare_digest()`（`api.py`）。
- **fail closed**：`GHOST_ADMIN_TOKEN` / `GHOST_WORKER_TOKEN` / `OBSERVABILITY_TOKEN` 未配置时，对应端点返回 503。

### 7.3 Transcript

- **本地**：worker `transcript.jsonl`（后压缩为 `transcript.jsonl.gz`）包含完整 LLM 交互记录。
- **上传**：relay 过滤掉 `message_update` 类型后，将事件行上传至 obs。
- **暴露面**：obs `/api/transcript` 与 `/api/timeline` 可返回完整交互记录；与 flag 同信任域。

### 7.4 Target / Sandbox

- **靶场容器**：由 `DockerProvisioner` 启动，与 worker 容器可能共享 docker socket（特权模式）。
- **网络隔离**：通过 OpenVPN（`core/vpn.py`）或 docker network 隔离。
- **Admin VPN 端点**：`/openapi/v1/vpn/config` 拒绝包含 `script-security`/`up`/`down`/`plugin`/`route-up`/`tls-verify` 等执行指令的配置，防止代码执行。

### 7.5 Agent（Pi Backend）

- **不可信假设**：Agent 可能产生任意工具调用；platform 侧只信任 `submissions` 表的哈希比对结果，不信任 agent 的任何声明。
- **worker 容器内**：Agent 以 root 运行，可访问 `/work`、docker socket（若挂载）、VPN 接口。

---

## 8. 推荐目录树

> `已有` = 仓库当前已存在且功能稳定的文件。  
> `迁移` = 需要移动、拆分或改名的文件（不改变行为，只调整边界）。  
> `新增` = 当前不存在、目标架构需要引入的文件。

```
Ghost/
├── docs/
│   └── ARCHITECTURE.md                 (新增)
├── packages/
│   ├── contracts/
│   │   └── ghost_contracts/
│   │       ├── __init__.py             (已有)
│   │       ├── platform.py             (已有: EventEnvelope, 状态集合)
│   │       ├── vocabulary.py           (已有: phase/status/kind/常量)
│   │       ├── redact.py               (已有)
│   │       ├── text.py                 (已有)
│   │       ├── fsio.py                 (已有)
│   │       ├── snapshot.py             (已有)
│   │       ├── digest.py               (已有)
│   │       ├── paths.py                (已有)
│   │       └── assets.py               (已有)
│   ├── core/
│   │   └── ghost/
│   │       ├── __init__.py             (已有)
│   │       ├── main.py                 (已有: uvicorn 入口)
│   │       ├── api.py                  (已有, 需迁移: 拆为 challenges.py + control.py)
│   │       ├── config.py               (已有)
│   │       ├── errors.py               (已有)
│   │       ├── models.py               (已有: TaskDefinition, ChallengeDefinition, FlagDefinition)
│   │       ├── vpn.py                  (已有)
│   │       ├── service.py              (已有: ChallengeService, 需迁移: 解耦 provisioner 锁)
│   │       ├── provisioner.py          (已有)
│   │       ├── control.py              (已有: ControlPlaneService)
│   │       ├── store.py                (已有, 需迁移: 拆为 challenge_store + control_store)
│   │       └── outbox.py               (新增: 独立 outbox 调度器, 替代 api.py 内嵌的 _dispatch_outbox)
│   ├── worker/
│   │   └── ghost_worker/
│   │       ├── __init__.py             (已有)
│   │       ├── driver.py               (已有, 需迁移: legacy 模式标记为 deprecated)
│   │       ├── settings.py             (已有)
│   │       ├── config.py               (已有: SolverConfig)
│   │       ├── assignment.py           (已有: AssignmentClient)
│   │       ├── orchestration.py        (已有, 需迁移: 抽离 AgentAdapter 抽象)
│   │       ├── relay.py                (已有)
│   │       ├── roster.py               (已有)
│   │       ├── flags.py                (已有)
│   │       ├── task.py                 (已有)
│   │       ├── taskprompt.py           (已有)
│   │       ├── transcripts.py          (已有)
│   │       ├── live/
│   │       │   ├── __init__.py         (已有)
│   │       │   ├── state.py            (已有: LiveState)
│   │       │   └── bus.py              (已有: LiveBus)
│   │       └── solver/
│   │           ├── __init__.py         (已有)
│   │           ├── base.py             (已有)
│   │           ├── factory.py          (已有)
│   │           └── pi_agent.py         (已有)
│   └── obs/
│       └── obs/
│           ├── __init__.py             (已有)
│           ├── app.py                  (已有)
│           ├── config.py               (已有)
│           ├── bus.py                  (已有: asyncio LiveBus)
│           ├── db.py                   (已有)
│           ├── schema.py               (已有)
│           ├── store.py                (已有: ObsStore)
│           ├── ingest.py               (已有, 需迁移: 拆为 telemetry_ingest + canonical_ingest)
│           ├── read.py                 (已有, 需迁移: 拆出 control_proxy.py)
│           ├── localserver.py          (已有: stdlib dashboard, worker 直接引用)
│           └── ingest_canonical.py     (新增: 专门处理 core outbox 的权威事件)
├── main.py                             (已有: core 启动脚本)
├── entrypoint.sh                       (已有)
├── docker-compose.yaml                 (已有)
├── Dockerfile                          (已有)
├── Dockerfile.base                     (已有)
├── Dockerfile.platform                 (已有)
├── Dockerfile.core                     (已有)
├── frontend/                           (已有: Svelte5 SPA 源码)
└── web/                                (已有: 构建产物, 提交到仓库)
```

**关键迁移说明**：

1. `core/api.py` → 拆分为 `challenges_api.py` 与 `control_api.py`（或保留一个文件但内部路由分组更清晰）。当前已经用 `tags=["challenges"]` / `tags=["control-plane"]` / `tags=["worker"]` 做了标签分组，但 store/service 实例仍是共享的。
2. `core/store.py` → 逻辑上拆分为 `ChallengeStore` 与 `ControlStore`，但物理上可保留一个文件，通过类拆分降低耦合。当前 `Store` 已超 500 行，是核心债务。
3. `worker/orchestration.py` → 引入 `AgentAdapter` 抽象接口（类似 `ContainerProvisioner`），使 `solve_one` 只负责编排，不直接依赖 pi backend。
4. `obs/read.py` → `control_proxy()` 可独立为 `control_proxy.py`，明确其“可选插件”地位。

---

## 9. P0 / P1 / P2 迁移步骤

### P0：最小垂直切片（assignment 模式端到端可审计）

**目标**：让 `worker_mode=assignment` 的完整链路（claim → solve → complete → canonical event → obs run 关闭）在单测与集成测试中稳定跑通，legacy 模式标记为 deprecated 但不删除。

**任务清单**：

1. **core**：确保 `attempt.completed` 事件在 `complete_attempt()` 事务内原子写入 `platform_events` + `outbox_events`，且 payload 包含 `flags_found` / `error` / `status`。
   - 相关代码：`packages/core/ghost/store.py:complete_attempt()`（已有，需确认字段完备）。
2. **core**：outbox 投递失败时，4xx 错误直接 dead-letter（已有），5xx/429 退避重试（已有）。
   - 相关代码：`packages/core/ghost/api.py:_dispatch_outbox()`。
3. **worker**：assignment 模式下，`relay.py` 对绑定 `attempt_id` 的 run **跳过** `run_close`，仅排干 events。
   - 相关代码：`packages/worker/ghost_worker/relay.py:_close_run()`（已有逻辑，需加固单测）。
4. **obs**：`post_canonical_events()` 正确处理 `attempt.started`（建 running 行）与 `attempt.completed`（关行）。
   - 相关代码：`packages/obs/obs/ingest.py:post_canonical_events()`。
5. **obs**：当 canonical 事件到达时，若 relay 已提前建了一条 `running` 行（通过 `events` 或 `live`），`close_run` 应能正确关联并关闭。
   - 相关代码：`packages/obs/obs/store.py:append_events()`（已建 running 行）+ `close_run()`（终态幂等）。
6. **集成测试**：新增一条从 core `create_evaluation` → worker `claim` → `solve_one` → `complete` → obs `runs` 表终态可查的端到端测试。
   - 可基于现有 `packages/obs/tests/` 的 TestClient 测试扩展。

**验收测试**：

```bash
# 1. 单元测试全绿
pytest -q

# 2. assignment 模式集成测试
pytest packages/obs/tests/test_assignment_e2e.py -v
# 期望: evaluation 创建 → job claim → attempt complete → obs runs 表中
#       run.status == "solved|done|failed" 且 run.attempt_id == attempt_id

# 3. legacy 模式回归
pytest packages/worker/tests/ -v
# 期望: legacy list 模式测试仍通过
```

### P1：Store 拆分与边界清晰化

**目标**：将 core 的 `Store` 拆分为 `ChallengeStore`（业务数据）与 `ControlStore`（调度数据），降低 schema 变更的交叉影响。

**任务清单**：

1. **core**：新建 `challenge_store.py`，迁移 `challenges` / `submissions` / `tasks` 相关操作。
2. **core**：新建 `control_store.py`，迁移 `evaluations` / `jobs` / `attempts` / `platform_events` / `outbox_events` / `workers` 相关操作。
3. **core**：`ChallengeService` 只依赖 `ChallengeStore`；`ControlPlaneService` 只依赖 `ControlStore`。
4. **core**：`api.py` 中两个路由组分别注入各自的 store/service。
5. **worker**：新建 `agent_adapter.py`，定义 `AgentAdapter` Protocol（`solve(task, cfg) -> SolveResult`）；`pi_agent.py` 实现该协议。
6. **worker**：`orchestration.py` 只依赖 `AgentAdapter`，不直接 import `solver_backend`。

**验收测试**：

```bash
# 1. 拆分后单元测试全绿
pytest packages/core/tests/ -q

# 2. ChallengeStore 可独立实例化（不创建 evaluations/jobs 表）
pytest packages/core/tests/test_challenge_store.py -v

# 3. ControlStore 可独立实例化（不创建 challenges/submissions 表）
pytest packages/core/tests/test_control_store.py -v
```

### P2：Obs 摄取分层与 Control Proxy 剥离

**目标**：obs 的 telemetry 与 canonical 摄取在代码层完全分离；control proxy 可选且可禁用。

**任务清单**：

1. **obs**：新建 `telemetry_ingest.py`，迁移 `post_live` / `post_events` / `post_run_close` / `post_roster` / `post_ping`。
2. **obs**：新建 `canonical_ingest.py`，迁移 `post_canonical_events`。
3. **obs**：`ingest.py` 变为 router 组装文件（或删除）。
4. **obs**：新建 `control_proxy.py`，迁移 `control_proxy()`；`read.py` 不再包含代理逻辑。
5. **obs**：`Settings.control_url` 未配置时，`control_proxy.py` 的 router 不被挂载。
6. **obs**：`ObsStore` 中增加 `source` 标记（`telemetry` vs `canonical`），便于审计查询。

**验收测试**：

```bash
# 1. 拆分后单元测试全绿
pytest packages/obs/tests/ -q

# 2. telemetry 端点与 canonical 端点可分别禁用
#    - 不配置 OBSERVABILITY_TOKEN → 所有 ingest 503
#    - 配置 token 但不配置 OBS_CONTROL_URL → /api/v1/* proxy 404

# 3. 端到端: core outbox → obs canonical-ingest → runs 表可查询
pytest packages/obs/tests/test_canonical_ingest.py -v
```

---

## 10. 明确暂不做

以下技术在本文档版本内明确排除，避免架构膨胀：

- **gRPC**：所有进程间通信保持 HTTP/REST，便于调试与容器穿透。
- **Kafka / RabbitMQ**：事件流使用 SQLite outbox + HTTP 投递，单写者足够。
- **Postgres**：core 与 obs 继续使用 SQLite WAL；若未来 QPS 超过单进程上限，再评估迁移。
- **微服务拆分**：不将 evaluation 调度器、job 队列、attempt 管理拆分为独立服务；它们同属 core 的 Control Plane。
- **多进程 core / obs**：`workers=1` 是硬约束（SQLite 单写者 + SSE bus）。
- **Worker 侧状态持久化**：worker 仍是无状态容器（除 `/work` 目录），attempt 中断后由 core 重新调度，worker 不做断点续接。

---

## 11. 第一条可实现的最小垂直切片

**切片名称**：`assignment-canonical-run-close`

**范围**：只改 obs 侧，不改动 core/worker 源码。

**目标**：让 core 投递的 `attempt.completed` 事件在 obs 侧正确关闭对应的 run，即使该 run 此前已由 relay 的 `events` 或 `live` 建立。

**具体改动**：

1. 在 `packages/obs/obs/store.py` 的 `append_events()` 中，当传入 `attempt_id` 时，使用 `attempt_id` 作为 `run_id` 建行（当前已使用 `run_id` 参数，但 canonical 事件到达时需保证 `run_id == attempt_id`）。
2. 在 `packages/obs/obs/ingest.py` 的 `post_canonical_events()` 中，`attempt.started` 调用 `append_events` 时，显式把 `run_id` 设为 `attempt_id`。
3. 在 `packages/obs/obs/store.py` 的 `close_run()` 中，增加按 `attempt_id` 查找 running 行的逻辑（当前只按 `run_id`）。
4. 新增单测 `packages/obs/tests/test_canonical_run_close.py`：模拟 relay 先建 running 行（`run_id=attempt_id`），然后 canonical `attempt.completed` 到达，断言最终 status 为 canonical 指定的终态。

**验收**：

```python
# 伪代码验收测试
def test_canonical_closes_relay_run(client):
    # relay 先建 running 行
    client.post("/api/internal/events", json={
        "run_id": "a1b2c3...",
        "attempt_id": "a1b2c3...",
        ...
    }, headers=token)
    # canonical 到达
    client.post("/api/internal/canonical-events", json={
        "events": [{
            "event_type": "attempt.completed",
            "attempt_id": "a1b2c3...",
            "payload": {"status": "solved", "solved": True}
        }]
    }, headers=token)
    # 断言
    run = store.run_row("a1b2c3...")
    assert run["status"] == "solved"
```

**实现状态**：已实现（见仓库当前 HEAD）并经严格审查加固。
- 实际改动与上述基本一致，额外增加 **migration v4** (`runs.canonical INTEGER DEFAULT 0`)：
  `close_run(canonical=True)` 写入时置 `canonical=1`，此后 relay `run_close` 对该行返回 `False`（不可覆盖）。
- `close_run` 的 `attempt_id` 回落已去掉 `attempt_id != run_id` 限制：即使规范侧 `run_id=attempt_id`，只要该 run_id 不存在，仍按 `attempt_id` 列查找 relay 预建行（`canonical DESC` 置顶，已分裂旧库优先命中权威行）。
- 加固后的不变量（`packages/obs/tests/test_canonical_run_close.py` 全覆盖）：
  1. 同一 `attempt_id` 只留一行 —— `append_events()` 按 `attempt_id` 复用已有关联行，canonical started/completed 与 relay telemetry（`run_id != attempt_id`）收敛到同一 run，不分裂两行；
  2. 乱序 completed 不丢失 —— 无行可关时 `close_run(canonical=True)` 建占位终态行（`challenge_code="unknown"`，`started_at=ended_at`），后补 started 复用该行且不洗终态（仅回填 code/evaluation/job）；
  3. relay 永不覆盖 canonical —— 同行 `canonical=1` 拒写，跨行（`run_id` 不同、重复投递、relay 带/不带 `attempt_id`）经 `attempt_id` 全局守卫一律拒写；
  4. canonical 重复投递幂等，legacy 无 `attempt_id` 链路不受归一影响，旧库迁移（v1→v4，含漂移 events 重建）保持兼容。

**为什么这是最小切片**：
- 不新增进程、只新增一列标记。
- 只改 obs 的摄取逻辑，不改 core 的 outbox 格式，也不改 worker 的 relay 行为。
- 修复了“canonical 事件无法关闭 relay 预建 run”的潜在一致性问题，为 P0 的端到端测试奠定基础。

---

## 附录：术语表

| 术语 | 含义 |
|------|------|
| **Task** | 一次评估的完整题目集合，由 `BENCHMARK_TOKEN` 鉴权。 |
| **Challenge** | 单道题目，含 flags、hint、容器配置。 |
| **Evaluation** | 对某个 task 的一次完整评估请求，生成一组 jobs。 |
| **Job** | 一个 evaluation 内对单道 challenge 的评估任务。 |
| **Attempt** | 一个 job 被某个 worker 领取后的一次具体求解尝试。 |
| **Run** | obs 侧的观测记录，对应一次 attempt 的 telemetry 投影。 |
| **Canonical Event** | 来自 core 控制面的权威生命周期事件（`attempt.started/completed`）。 |
| **Telemetry** | 来自 worker relay 的非权威观测数据（events/live/roster/ping）。 |
| **Outbox** | core 的 `outbox_events` 表，用于至少一次投递 canonical events 到 obs。 |
| **Lease** | worker 对 job/attempt 的租约，过期后控制面自动回收。 |
| **Provisioner** | 靶场容器启动器（static / docker）。 |
| **Relay** | worker 侧的观测数据中继，将本地数据推送到 obs。 |
| **Localserver** | worker 侧的 stdlib 状态服务器（`obs.localserver`），提供本地仪表板。 |
