# Go 模块化单体重构计划

## 概述

将 Ghost 安全评估平台从 Python 四包架构重构为 Go 模块化单体（Modular Monolith），保持现有功能与 API 契约，采用 Go 最佳实践，提升性能、类型安全与部署简便性。

## 当前架构分析

### Python 代码规模
- **总计**: ~146 个 Python 文件，核心包约 8066 行代码
- **四包结构**:
  - `contracts`: 零依赖共享契约（词汇表、事件、路径、摘要、编辑）
  - `core`: 控制面（FastAPI + SQLite，任务调度、作业分配、计分）
  - `worker`: 工作编排（Pi Agent 求解器、任务领取、观测中继）
  - `obs`: 可观测平台（事件摄取、SQLite 存储、只读 API、SPA 托管）

### 关键技术栈
- **Web 框架**: FastAPI + Uvicorn
- **数据库**: SQLite WAL 模式，单写者进程（`workers=1` 硬约束）
- **鉴权**: HMAC constant-time 令牌比对
- **部署**: Docker Compose，三个独立容器
- **前端**: Svelte 5 + TypeScript（不变）

### 依赖关系
```
contracts (stdlib only)
    ↑
    ├── core (→ obs via HTTP)
    ├── worker (→ core, obs via HTTP; import obs.localserver)
    └── obs (← core HTTP outbox)
```

### 核心流程
1. **控制面**: 创建 evaluation → 生成 jobs → worker 领取 → 生成 attempt
2. **Worker**: 领取作业 → Pi Agent 求解 → 提交 flag → 中继遥测
3. **观测**: 接收遥测（events/live/run_close）+ 规范事件（attempt.started/completed）
4. **数据权威**: core 的 `attempt.completed` 是终态唯一真相源

## 目标架构：Go 模块化单体

### 设计原则

1. **单一二进制，多个可部署单元**
   - 一个 `ghost` 二进制，支持子命令：`ghost control`, `ghost worker`, `ghost platform`
   - 每个子命令启动一个独立进程，保持 SQLite 单写者约束
   - 测试环境可单进程运行多个 HTTP 服务器（不同端口）

2. **清晰的模块边界**
   - 包结构反映业务域，非技术层
   - 内部包（`internal/`）禁止外部引用
   - 依赖方向严格单向，禁止循环依赖

3. **领域驱动设计（DDD Lite）**
   - 每个子域有独立的 `domain`, `service`, `repository`, `handler` 层
   - 聚合根明确，事务边界清晰
   - 事件驱动通信（内部事件总线 + HTTP outbox）

4. **Go 最佳实践**
   - 标准项目布局（`cmd/`, `internal/`, `pkg/`）
   - 接口优先设计（依赖抽象）
   - Context 传递（超时、取消、trace）
   - 结构化日志（`slog`）
   - 错误包装（`fmt.Errorf` + `errors.Is/As`）

### 目录结构

```
ghost/
├── cmd/
│   └── ghost/
│       ├── main.go                    # 主入口
│       ├── control.go                 # control 子命令
│       ├── worker.go                  # worker 子命令
│       ├── platform.go                # platform 子命令
│       └── version.go                 # 版本信息
├── internal/
│   ├── contracts/                     # 共享契约（对应 Python contracts）
│   │   ├── vocabulary/                # 状态、阶段常量
│   │   ├── events/                    # EventEnvelope, 事件类型
│   │   ├── digest/                    # SHA-256 摘要
│   │   ├── redact/                    # 敏感信息编辑
│   │   ├── paths/                     # 文件路径辅助
│   │   └── snapshot/                  # 快照结构
│   │
│   ├── control/                       # 控制面（对应 Python core）
│   │   ├── domain/                    # 领域模型
│   │   │   ├── challenge.go          # Challenge 聚合根
│   │   │   ├── evaluation.go         # Evaluation 聚合根
│   │   │   ├── job.go                # Job 实体
│   │   │   ├── attempt.go            # Attempt 实体
│   │   │   └── errors.go             # 领域错误
│   │   ├── service/                   # 应用服务
│   │   │   ├── challenge_service.go  # 挑战生命周期
│   │   │   ├── control_service.go    # 调度与分配
│   │   │   └── provisioner.go        # 容器供给（接口）
│   │   ├── repository/                # 数据访问
│   │   │   ├── store.go              # Store 接口
│   │   │   ├── sqlite_store.go       # SQLite 实现
│   │   │   └── migrations.go         # Schema 迁移
│   │   ├── handler/                   # HTTP 处理器
│   │   │   ├── challenges_api.go     # Challenges API
│   │   │   ├── control_api.go        # Control Plane API
│   │   │   ├── vpn_api.go            # VPN 管理
│   │   │   └── middleware.go         # 鉴权、日志
│   │   ├── outbox/                    # 事件发件箱
│   │   │   └── dispatcher.go         # 异步投递到 obs
│   │   └── app.go                     # 应用装配
│   │
│   ├── worker/                        # 工作器（对应 Python worker）
│   │   ├── domain/
│   │   │   ├── assignment.go         # 作业分配
│   │   │   └── live_state.go         # 实时状态
│   │   ├── service/
│   │   │   ├── orchestration.go      # 求解编排
│   │   │   ├── solver.go             # 求解器接口
│   │   │   ├── pi_solver.go          # Pi Agent 适配器
│   │   │   └── relay.go              # 观测中继
│   │   ├── client/
│   │   │   ├── assignment_client.go  # 领取作业 HTTP 客户端
│   │   │   └── sdk_client.go         # Challenges API 客户端
│   │   ├── handler/
│   │   │   └── status_api.go         # 本地状态 API (localserver)
│   │   └── app.go
│   │
│   ├── platform/                      # 观测平台（对应 Python obs）
│   │   ├── domain/
│   │   │   ├── run.go                # Run 聚合根
│   │   │   ├── event.go              # Event 实体
│   │   │   └── timeline.go           # Timeline 视图
│   │   ├── service/
│   │   │   ├── ingest_service.go     # 摄取服务
│   │   │   ├── canonical_ingest.go   # 规范事件摄取
│   │   │   └── telemetry_ingest.go   # 遥测摄取
│   │   ├── repository/
│   │   │   ├── obs_store.go          # ObsStore 接口
│   │   │   ├── sqlite_obs_store.go   # SQLite 实现
│   │   │   └── migrations.go
│   │   ├── handler/
│   │   │   ├── ingest_api.go         # 摄取 API (internal)
│   │   │   ├── read_api.go           # 只读 API (public)
│   │   │   ├── sse.go                # Server-Sent Events
│   │   │   └── spa.go                # SPA 静态文件托管
│   │   └── app.go
│   │
│   ├── provisioner/                   # 容器供给实现
│   │   ├── docker.go                 # DockerProvisioner
│   │   └── static.go                 # StaticProvisioner
│   │
│   ├── shared/                        # 共享基础设施
│   │   ├── config/                   # 配置加载（环境变量）
│   │   ├── httputil/                 # HTTP 辅助（鉴权、错误）
│   │   ├── sqlite/                   # SQLite 辅助（WAL、迁移）
│   │   ├── logger/                   # 结构化日志
│   │   └── clock/                    # 时间抽象（便于测试）
│   │
│   └── testutil/                      # 测试辅助
│       ├── fixtures/                 # 测试夹具
│       ├── mock/                     # Mock 实现
│       └── integration/              # 集成测试辅助
│
├── pkg/                               # 可导出的公共包
│   └── sdk/                          # Ghost SDK (对应 Python tsec-benchmark)
│       ├── client.go                 # SDK 客户端
│       ├── types.go                  # 公共类型
│       └── errors.go
│
├── web/                               # 前端构建产物（不变）
├── frontend/                          # 前端源码（不变）
├── skills/                            # Pi Agent 技能（不变）
├── tools/                             # 辅助脚本
├── deployments/
│   ├── docker/
│   │   ├── Dockerfile.control        # 控制面镜像
│   │   ├── Dockerfile.worker         # Worker 镜像
│   │   ├── Dockerfile.platform       # 平台镜像
│   │   └── docker-compose.yaml
│   └── k8s/                          # Kubernetes manifests（可选）
├── go.mod
├── go.sum
├── Makefile
└── README.md
```

## 实施阶段

### Phase 1: 项目脚手架与共享契约（2-3 天）

**目标**: 搭建 Go 项目结构，实现零依赖的 `internal/contracts` 包。

**任务**:
1. 初始化 Go 模块：`go mod init github.com/yourusername/ghost`
2. 创建目录结构（`cmd/`, `internal/`, `pkg/`, `deployments/`）
3. 实现 `internal/contracts`:
   - `vocabulary`: 状态常量、阶段枚举、正则表达式
   - `events`: `EventEnvelope` 结构体、`NewEventEnvelope()` 工厂
   - `digest`: SHA-256 摘要函数
   - `redact`: 敏感信息编辑（flag、token）
   - `paths`: 文件路径辅助函数
   - `snapshot`: 快照序列化/反序列化
4. 实现 `internal/shared`:
   - `config`: 环境变量加载（使用 `github.com/caarlos0/env/v10`）
   - `logger`: `slog` 封装，支持 JSON/Text 格式
   - `httputil`: 常量时间令牌比对、标准错误响应
   - `sqlite`: WAL 模式初始化、连接池配置
   - `clock`: 时间接口（`time.Now()` 的可测试封装）
5. 编写单元测试，确保 `internal/contracts` 零外部依赖（通过 `go list -m` 检查）

**验收标准**:
- `go test ./internal/contracts/...` 全部通过
- `go mod graph | grep 'internal/contracts'` 无第三方依赖
- CI/CD 配置就绪（GitHub Actions 或 GitLab CI）

**依赖包建议**:
- 配置: `github.com/caarlos0/env/v10`
- 数据库: `modernc.org/sqlite` (纯 Go SQLite，无 cgo)
- HTTP 路由: `github.com/go-chi/chi/v5`
- JSON: 标准库 `encoding/json`
- 日志: 标准库 `log/slog`

---

### Phase 2: 控制面核心（5-7 天）

**目标**: 实现 `internal/control`，支持 Challenges API 与 Control Plane API。

**任务**:
1. **领域模型** (`domain/`):
   - `Challenge`: 唯一码、描述、难度、flag 列表、容器配置
   - `Evaluation`: ID、任务列表、创建时间、状态
   - `Job`: 评估 ID、挑战、状态（pending/running/solved/failed）
   - `Attempt`: 作业 ID、Worker ID、租约、开始/结束时间
   - 领域错误: `ErrChallengeNotFound`, `ErrDuplicateSubmission`, `ErrLeaseExpired`

2. **Repository 层** (`repository/`):
   - `Store` 接口:
     ```go
     type Store interface {
         // Challenges API
         StartChallenge(ctx context.Context, token string) (*ChallengeRow, error)
         GetChallenge(ctx context.Context, token string) (*ChallengeRow, error)
         SubmitFlag(ctx context.Context, token, candidate string) (*SubmissionResult, error)
         CloseChallenge(ctx context.Context, token string) error
         
         // Control Plane API
         CreateEvaluation(ctx context.Context, tasks []TaskDef) (*Evaluation, error)
         ClaimJob(ctx context.Context, workerID string, leaseSec int) (*AssignmentRow, error)
         RenewLease(ctx context.Context, attemptID, leaseID string, leaseSec int) error
         CompleteAttempt(ctx context.Context, attemptID, leaseID string, status string) error
         
         // Outbox
         AppendEvents(ctx context.Context, events []EventEnvelope) error
         PollOutbox(ctx context.Context, limit int) ([]OutboxRow, error)
         MarkDispatched(ctx context.Context, eventIDs []string) error
     }
     ```
   - `SQLiteStore` 实现:
     - 表结构迁移（`tasks`, `challenges`, `submissions`, `evaluations`, `jobs`, `attempts`, `platform_events`, `outbox_events`）
     - 使用 `database/sql` + `modernc.org/sqlite`
     - 事务管理：`BeginTx`, `Commit`, `Rollback`
     - 连接池配置：`SetMaxOpenConns(1)` （单写者）
   - 迁移脚本: 版本化 SQL 文件，使用 `PRAGMA user_version`

3. **Service 层** (`service/`):
   - `ChallengeService`:
     - `Start()`: 创建挑战、调用 provisioner 启动容器
     - `Submit()`: 验证 SHA-256 哈希、计算得分、记录提交
     - `Close()`: 停止容器、标记挑战完成
   - `ControlService`:
     - `CreateEvaluation()`: 解析任务定义、生成作业
     - `ClaimJob()`: 事务内分配作业、生成租约、写入 `attempt.started` 事件
     - `CompleteAttempt()`: 验证租约、更新作业状态、写入 `attempt.completed` 事件
   - `Provisioner` 接口:
     ```go
     type Provisioner interface {
         Start(ctx context.Context, cfg ContainerConfig) ([]string, error) // 返回地址列表
         Stop(ctx context.Context, containerID string) error
     }
     ```

4. **Handler 层** (`handler/`):
   - `ChallengesAPI`:
     - `POST /openapi/v1/challenges/start` (鉴权: `BENCHMARK_TOKEN`)
     - `POST /openapi/v1/challenges/submit`
     - `POST /openapi/v1/challenges/hint`
     - `POST /openapi/v1/challenges/close`
   - `ControlAPI`:
     - `POST /api/v1/evaluations` (鉴权: `GHOST_ADMIN_TOKEN`)
     - `POST /api/v1/workers/claim` (鉴权: `X-Worker-Token`)
     - `POST /api/v1/workers/heartbeat`
     - `POST /api/v1/attempts/:id/complete`
   - `VpnAPI`:
     - `POST /openapi/v1/vpn/upload` (鉴权: `GHOST_ADMIN_TOKEN`)
     - `POST /openapi/v1/vpn/start`
     - `POST /openapi/v1/vpn/stop`
   - 中间件:
     - `AuthMiddleware`: 常量时间令牌比对，未配置令牌返回 503
     - `LoggingMiddleware`: 请求日志（`slog`）
     - `RecoveryMiddleware`: panic 恢复
     - `CORSMiddleware`: 跨域配置（如需要）

5. **Outbox 调度器** (`outbox/`):
   - `Dispatcher`: 后台 goroutine，每 N 秒轮询 `outbox_events`
   - HTTP POST 到 `obs` 的 `/api/internal/canonical-events`
   - 成功后调用 `MarkDispatched()`
   - 错误处理: 指数退避，最多重试 M 次后记录错误日志

6. **应用装配** (`app.go`):
   - `NewControlApp()`: 依赖注入，组装 Store → Service → Handler → Router
   - `Run()`: 启动 HTTP 服务器、Outbox 调度器，优雅关闭

**验收标准**:
- `go test ./internal/control/...` 全部通过
- 集成测试: 启动控制面，调用 Challenges API，验证 SQLite 状态
- 压测: 1000 并发请求，响应时间 < 100ms (P99)
- Outbox 投递: 模拟 obs 宕机，验证重试与最终一致性

---

### Phase 3: 可观测平台（4-5 天）

**目标**: 实现 `internal/platform`，支持遥测摄取、规范事件摄取、只读 API、SSE、SPA 托管。

**任务**:
1. **领域模型** (`domain/`):
   - `Run`: run_id, worker_id, challenge_code, 状态, 指标（turns, sessions, flags_found）
   - `Event`: run_id, seq, type, payload (JSON), occurred_at
   - `LiveSnapshot`: worker_id, kind, snapshot (JSON)
   - `Timeline`: 聚合视图（按时间排序的 runs 和 events）

2. **Repository 层** (`repository/`):
   - `ObsStore` 接口:
     ```go
     type ObsStore interface {
         // 摄取
         AppendEvents(ctx context.Context, req EventsRequest) error
         UpdateLive(ctx context.Context, req LiveRequest) error
         CloseRun(ctx context.Context, req RunCloseRequest) error
         IngestCanonicalEvents(ctx context.Context, events []EventEnvelope) error
         
         // 查询
         GetRun(ctx context.Context, runID string) (*Run, error)
         ListRuns(ctx context.Context, filters RunFilters) ([]Run, error)
         GetEvents(ctx context.Context, runID string) ([]Event, error)
         GetTimeline(ctx context.Context, filters TimelineFilters) (*Timeline, error)
         GetLiveSnapshot(ctx context.Context, workerID string) (*LiveSnapshot, error)
     }
     ```
   - `SQLiteObsStore` 实现:
     - 表结构: `runs`, `events`, `live`, `roster`
     - `runs.canonical` 标志位（`1` = 来自 core，优先级高）
     - `AppendEvents()`: 幂等性（run_id + seq 唯一约束）
     - `CloseRun()`: 检查 `CLOSABLE_STATUSES`，不覆盖 canonical run
     - `IngestCanonicalEvents()`: 标记 `canonical=1`，覆盖遥测数据

3. **Service 层** (`service/`):
   - `IngestService`:
     - `HandleEvents()`: 验证 run_id 格式、限制批次大小（500）
     - `HandleLive()`: 更新实时快照
     - `HandleRunClose()`: 调用 `ObsStore.CloseRun()`，处理 canonical 冲突
   - `CanonicalIngestService`:
     - `HandleCanonicalEvents()`: 解析 `attempt.started` / `attempt.completed`
     - 映射 `attempt_id → run_id`（如果遥测已创建 run，复用行）
     - 更新 runs 表，标记 `canonical=1`
   - `ReadService`:
     - `GetRun()`, `ListRuns()`: 直接委托给 ObsStore
     - `GetTimeline()`: 聚合多个数据源
     - `StreamEvents()`: SSE 流，订阅 `EventBus`

4. **Handler 层** (`handler/`):
   - `IngestAPI` (需要 `X-Observability-Token`):
     - `POST /api/internal/events`
     - `POST /api/internal/live`
     - `POST /api/internal/run-close`
     - `POST /api/internal/ping`
     - `POST /api/internal/roster`
     - `POST /api/internal/canonical-events` (来自 core outbox)
   - `ReadAPI` (无鉴权):
     - `GET /api/runs`
     - `GET /api/runs/:id`
     - `GET /api/runs/:id/events`
     - `GET /api/timeline`
     - `GET /api/status`
     - `GET /api/events` (SSE)
   - `SPAAPI`:
     - `GET /`, `GET /runs/:id` 等: 托管 `web/` 目录下的静态文件
     - SPA 路由回退到 `index.html`
   - SSE 实现:
     - 使用 `http.Flusher` 接口
     - 订阅内部 `EventBus`，推送新事件
     - 客户端断开时清理订阅

5. **事件总线** (`bus/`):
   - 内存中 pub/sub（仅限单进程 SSE，不跨实例）
   - `Subscribe(topic string, ch chan<- Event)`, `Publish(topic string, event Event)`
   - 用于 SSE 实时推送

6. **应用装配** (`app.go`):
   - `NewPlatformApp()`: 依赖注入
   - `Run()`: 启动 HTTP 服务器，绑定到 `127.0.0.1:8090`（默认）
   - 环境变量 `OBS_HOST_IP=0.0.0.0` 允许 LAN 访问

**验收标准**:
- `go test ./internal/platform/...` 全部通过
- 集成测试: 
  - 发送遥测 → 验证 SQLite 状态
  - 发送规范事件 → 验证覆盖遥测数据
  - SSE 订阅 → 发送事件 → 验证客户端收到
- 前端集成: 启动 platform，打开 `http://localhost:8090`，验证 SPA 渲染

---

### Phase 4: Worker 编排（5-7 天）

**目标**: 实现 `internal/worker`，支持作业领取、Pi Agent 集成、观测中继。

**任务**:
1. **领域模型** (`domain/`):
   - `Assignment`: evaluation_id, job_id, attempt_id, lease_id, lease_expires_at, benchmark_token, challenge
   - `LiveState`: 18 个字段（phase, status, turns, sessions, flags_found 等）

2. **Client 层** (`client/`):
   - `AssignmentClient`:
     - `Claim(ctx, workerID, leaseSec)`: POST `/api/v1/workers/claim`
     - `Heartbeat(ctx, attemptID, leaseID)`: POST `/api/v1/workers/heartbeat`
     - `Complete(ctx, attemptID, leaseID, status)`: POST `/api/v1/attempts/:id/complete`
   - `SDKClient`:
     - 封装 Challenges API（start, submit, hint, close）
     - 使用 `benchmark_token` 鉴权
   - HTTP 重试逻辑: 指数退避，最多 3 次

3. **Service 层** (`service/`):
   - `Solver` 接口:
     ```go
     type Solver interface {
         Solve(ctx context.Context, challenge Challenge) (*SolveResult, error)
     }
     ```
   - `PiSolver` 实现:
     - 调用 Pi Agent CLI（`pi --model <model> --prompt <prompt>`）
     - 使用 `os/exec.CommandContext`，设置超时
     - 解析输出，提取 flag 候选
     - 错误处理: Pi 进程退出码、stderr 日志
   - `OrchestrationService`:
     - `SolveOne(ctx, assignment)`:
       1. 调用 SDK `Start()`
       2. 调用 SDK `Hint()`（如果配置）
       3. 调用 `Solver.Solve()`
       4. 提取 flag 候选，去重
       5. 逐个调用 SDK `Submit()`，直到全部接受或超时
       6. 调用 SDK `Close()`
       7. 更新 `LiveState`
     - 租约续约: 后台 goroutine，每 N 秒调用 `AssignmentClient.Heartbeat()`
     - 租约丢失: 设置 `context.Context` 取消，提前终止求解
   - `RelayService`:
     - `SendEvents(ctx, runID, events)`: POST 到 obs `/api/internal/events`
     - `SendLive(ctx, workerID, snapshot)`: POST 到 obs `/api/internal/live`
     - `SendRunClose(ctx, runID, status, metrics)`: POST 到 obs `/api/internal/run-close`
     - 批量发送: 累积 N 个事件或 T 秒后 flush
     - 错误处理: 重试 3 次，失败后丢弃（遥测非关键）

4. **Handler 层** (`handler/`):
   - `StatusAPI`: 本地状态服务器（对应 Python `obs.localserver`）
     - `GET /status`: 返回当前 LiveState
     - `GET /roster`: 返回挑战列表
     - 绑定到 `127.0.0.1:8080`（仅容器内访问）

5. **应用装配** (`app.go`):
   - `NewWorkerApp()`: 依赖注入
   - `Run()`:
     1. 启动本地状态服务器（后台 goroutine）
     2. 进入主循环:
        - Assignment 模式: 调用 `AssignmentClient.Claim()`，获取作业
        - Legacy 模式: 从 SDK 列举未完成挑战
        - 按难度排序，逐个调用 `OrchestrationService.SolveOne()`
        - 全部完成后 sleep 30s，重复
     3. 租约监视: 独立 goroutine，检测租约过期，设置 `context.Context` 取消
     4. 优雅关闭: 收到 SIGTERM 时，完成当前挑战后退出

**验收标准**:
- `go test ./internal/worker/...` 全部通过
- Mock 测试: Mock `Solver`, 验证编排逻辑
- 集成测试:
  - 启动 control + platform + worker
  - 创建 evaluation
  - Worker 领取作业、求解、提交 flag
  - 验证 obs 接收遥测、control 记录 attempt
- Pi Agent 集成: 实际运行一个简单挑战，验证端到端流程

---

### Phase 5: Provisioner 与 VPN（3-4 天）

**目标**: 实现容器供给与 VPN 管理。

**任务**:
1. **Provisioner 实现** (`internal/provisioner/`):
   - `DockerProvisioner`:
     - `Start()`: 调用 `docker run` 启动容器
     - 使用 `os/exec.CommandContext`
     - 解析容器 ID、端口映射
     - 健康检查: 重试连接容器端口，最多 N 次
     - `Stop()`: 调用 `docker stop` + `docker rm`
   - `StaticProvisioner`:
     - `Start()`: 返回预配置的 `container_addr`（无副作用）
     - `Stop()`: no-op

2. **VPN 管理** (`internal/control/handler/vpn_api.go`):
   - `POST /openapi/v1/vpn/upload`:
     - 读取 `.ovpn` 配置文件
     - 验证指令白名单（拒绝 `script-security`, `up`, `down`, `plugin` 等）
     - 写入 `/etc/openvpn/client.conf`
   - `POST /openapi/v1/vpn/start`:
     - 调用 `openvpn --config /etc/openvpn/client.conf --script-security 1`
     - 后台运行，保存 PID
   - `POST /openapi/v1/vpn/stop`:
     - 发送 SIGTERM 到 openvpn 进程
   - 安全: 鉴权 `GHOST_ADMIN_TOKEN`，日志记录所有操作

**验收标准**:
- `go test ./internal/provisioner/...` 全部通过
- Mock Docker: 验证 `DockerProvisioner` 调用正确的 `docker` 命令
- VPN 集成: 上传合法配置 → 启动 → 验证网络连接 → 停止

---

### Phase 6: SDK 与 CLI（2-3 天）

**目标**: 实现公共 SDK 与 `ghost` CLI。

**任务**:
1. **SDK** (`pkg/sdk/`):
   - `Client` 结构体:
     ```go
     type Client struct {
         BaseURL string
         Token   string
         HTTPClient *http.Client
     }
     func NewClient(baseURL, token string) *Client
     func (c *Client) StartChallenge(ctx context.Context) (*Challenge, error)
     func (c *Client) SubmitFlag(ctx context.Context, token, candidate string) (*SubmitResult, error)
     func (c *Client) CloseChallenge(ctx context.Context, token string) error
     ```
   - 类型定义: `Challenge`, `Flag`, `SubmitResult`
   - 错误处理: 自定义错误类型（`ErrInvalidState`, `ErrVpnCheckFailed`）

2. **CLI** (`cmd/ghost/`):
   - 子命令:
     - `ghost control --config <path>`: 启动控制面
     - `ghost worker --config <path>`: 启动 worker
     - `ghost platform --config <path>`: 启动观测平台
     - `ghost version`: 显示版本信息
   - 使用 `github.com/spf13/cobra` 或 `github.com/urfave/cli/v2`
   - 配置加载: 环境变量 > 配置文件 > 默认值
   - 优雅关闭: 监听 SIGTERM/SIGINT，调用 `app.Shutdown(ctx)`

3. **版本管理**:
   - 使用 `ldflags` 注入版本号: `go build -ldflags "-X main.Version=$(git describe --tags)"`
   - `ghost version` 输出: 版本号、Git commit、构建时间

**验收标准**:
- `go test ./pkg/sdk/...` 全部通过
- CLI 功能: 所有子命令可正常启动与关闭
- 文档: `README.md` 包含安装、配置、运行示例

---

### Phase 7: Docker 化与部署（3-4 天）

**目标**: 创建 Docker 镜像与 Compose 配置。

**任务**:
1. **Dockerfile.control**:
   ```dockerfile
   FROM golang:1.24-alpine AS builder
   WORKDIR /build
   COPY go.mod go.sum ./
   RUN go mod download
   COPY . .
   RUN CGO_ENABLED=0 go build -ldflags="-w -s" -o ghost ./cmd/ghost
   
   FROM alpine:latest
   RUN apk --no-cache add ca-certificates openvpn
   COPY --from=builder /build/ghost /usr/local/bin/ghost
   EXPOSE 8000
   CMD ["ghost", "control"]
   ```

2. **Dockerfile.worker**:
   - 基于 Kali Linux（保留 Pi Agent 环境）
   - 安装 Go 1.24
   - 编译 `ghost` 二进制
   - 安装 Node.js + Pi Agent（`npm install -g @earendil-works/pi-coding-agent`）
   - 复制 `skills/` 目录
   - Entrypoint: VPN 设置 + `ghost worker`

3. **Dockerfile.platform**:
   - 类似 `Dockerfile.control`，但 `CMD ["ghost", "platform"]`
   - 复制 `web/` 目录到 `/app/web`

4. **docker-compose.yaml**:
   ```yaml
   services:
     control:
       build:
         context: .
         dockerfile: deployments/docker/Dockerfile.control
       ports:
         - "8000:8000"
       environment:
         - GHOST_TASKS_JSON=${GHOST_TASKS_JSON}
         - GHOST_ADMIN_TOKEN=${GHOST_ADMIN_TOKEN}
         - GHOST_WORKER_TOKEN=${GHOST_WORKER_TOKEN}
         - OBSERVABILITY_URL=http://platform:8090
       volumes:
         - ./data/control:/data
     
     platform:
       build:
         context: .
         dockerfile: deployments/docker/Dockerfile.platform
       ports:
         - "127.0.0.1:8090:8090"
       environment:
         - OBSERVABILITY_TOKEN=${OBSERVABILITY_TOKEN}
       volumes:
         - ./data/obs:/data
     
     worker:
       build:
         context: .
         dockerfile: deployments/docker/Dockerfile.worker
       depends_on:
         - control
         - platform
       environment:
         - GHOST_CONTROL_URL=http://control:8000
         - GHOST_WORKER_TOKEN=${GHOST_WORKER_TOKEN}
         - OBSERVABILITY_URL=http://platform:8090
         - OBSERVABILITY_TOKEN=${OBSERVABILITY_TOKEN}
         - SOLVER_MODEL=${SOLVER_MODEL}
       cap_add:
         - NET_ADMIN
       devices:
         - /dev/net/tun
       volumes:
         - ./skills:/root/.pi/agent/skills:ro
   ```

5. **Makefile**:
   ```makefile
   .PHONY: build test docker-build docker-up docker-down
   
   build:
       go build -o bin/ghost ./cmd/ghost
   
   test:
       go test -v -race ./...
   
   docker-build:
       docker compose -f deployments/docker/docker-compose.yaml build
   
   docker-up:
       docker compose -f deployments/docker/docker-compose.yaml up -d
   
   docker-down:
       docker compose -f deployments/docker/docker-compose.yaml down
   ```

**验收标准**:
- `make docker-build` 成功构建三个镜像
- `make docker-up` 启动所有服务
- 端到端测试: 创建 evaluation → worker 求解 → 查看 obs 仪表板
- 镜像大小: control/platform < 50MB, worker < 500MB（Kali 基础镜像较大）

---

### Phase 8: 测试与文档（3-4 天）

**目标**: 补充测试、性能优化、文档完善。

**任务**:
1. **测试覆盖率**:
   - 目标: 单元测试覆盖率 > 80%
   - 工具: `go test -coverprofile=coverage.out ./...`
   - 补充测试:
     - 边界情况: 租约过期、并发提交、SQLite 锁
     - 错误路径: 网络超时、Pi Agent 崩溃、obs 宕机
     - 幂等性: 重复摄取相同事件

2. **集成测试**:
   - 使用 `testcontainers-go` 启动真实 Docker 容器
   - 端到端流程:
     1. 启动 control + platform
     2. 创建 evaluation
     3. 模拟 worker 领取作业、提交 flag
     4. 验证 obs 数据一致性
   - CI/CD: GitHub Actions 运行集成测试

3. **性能测试**:
   - 压测工具: `hey` 或 `vegeta`
   - Challenges API: 1000 req/s，P99 < 100ms
   - Control API: 500 req/s，P99 < 200ms
   - 优化点:
     - SQLite 连接池（虽然单写者，但读可以并发）
     - HTTP 客户端连接复用
     - JSON 序列化缓存

4. **文档**:
   - `README.md`: 快速开始、安装、配置、API 示例
   - `ARCHITECTURE.md`: 架构图、模块职责、数据流
   - `DEPLOYMENT.md`: Docker 部署、Kubernetes 部署（可选）
   - `API.md`: OpenAPI 规范（使用 `swaggo/swag` 生成）
   - `CHANGELOG.md`: 版本历史

**验收标准**:
- `go test -race -cover ./...` 覆盖率 > 80%
- 集成测试在 CI 中通过
- 压测达到性能目标
- 文档完整，新用户可按文档独立部署

---

## 技术选型

### 核心依赖

| 功能 | Python 包 | Go 包 | 理由 |
|------|-----------|-------|------|
| Web 框架 | FastAPI | `github.com/go-chi/chi/v5` | 轻量、标准库兼容、性能优 |
| 数据库驱动 | `sqlite3` | `modernc.org/sqlite` | 纯 Go，无 cgo，易交叉编译 |
| 配置管理 | `python-dotenv` | `github.com/caarlos0/env/v10` | 类型安全、零样板代码 |
| HTTP 客户端 | `httpx` | 标准库 `net/http` | 内置连接池、超时控制 |
| 日志 | `logging` | 标准库 `log/slog` | 结构化日志、零依赖 |
| 数据验证 | `pydantic` | 手写验证 + `validator` 标签 | Go 类型系统更严格 |
| CLI | `argparse` | `github.com/urfave/cli/v2` | 子命令支持、帮助生成 |

### 可选依赖

- **OpenAPI 生成**: `github.com/swaggo/swag` (从注释生成规范)
- **测试容器**: `github.com/testcontainers/testcontainers-go`
- **Metrics**: `github.com/prometheus/client_golang` (Prometheus)
- **Tracing**: `go.opentelemetry.io/otel` (OpenTelemetry)

---

## 迁移策略

### 数据库兼容性

**保持 SQLite Schema 兼容性**:
- Go 版本使用相同的表结构与字段名
- 迁移脚本: 从 Python 版本导出 SQL，导入 Go 版本
- 验证: 使用 `sqlite3` CLI 工具比对两个数据库的 schema

**迁移路径**:
1. 导出 Python 版本数据: `sqlite3 data/ghost.sqlite3 .dump > backup.sql`
2. 启动 Go 版本（会自动创建 schema）
3. 导入数据: `sqlite3 data/ghost-go.sqlite3 < backup.sql`
4. 运行数据验证脚本，确保一致性

### API 兼容性

**保持 HTTP API 100% 兼容**:
- 所有端点路径、方法、请求/响应格式完全一致
- 错误码与错误信息保持一致（方便 SDK 复用）
- 鉴权机制不变（相同的令牌、相同的 HMAC 比对）

**验证方法**:
- 使用 Python SDK 连接 Go 版本的 control
- 运行原有的集成测试，确保通过
- API 契约测试: 使用 Pact 或自定义脚本比对请求/响应

### 前端无需变更

- Go 版本的 `platform` 服务托管相同的 `web/` 目录
- API 端点不变，前端代码零改动
- 验证: 启动 Go platform，打开浏览器，功能测试

### 渐进式迁移（可选）

如果需要逐步迁移，可以采用"绞杀者模式"：

1. **Phase 1**: 先迁移 `platform`（最独立）
   - Go 版本 platform 与 Python core/worker 并存
   - 验证 obs 摄取与只读 API

2. **Phase 2**: 迁移 `control`
   - Go 版本 control 与 Python worker 并存
   - 验证 Challenges API 与 Control API

3. **Phase 3**: 迁移 `worker`
   - 完全切换到 Go 版本
   - 下线所有 Python 服务

4. **Phase 4**: 清理 Python 代码
   - 归档 `packages/` 目录
   - 更新 `README.md` 与文档

---

## 风险与缓解

### 风险 1: Pi Agent 集成复杂度

**风险**: Pi Agent 是 Node.js CLI，Go 调用需要 `os/exec`，可能出现进程通信问题。

**缓解**:
- 早期验证: Phase 4 开始前，先实现 `PiSolver` 原型，测试进程调用
- 备用方案: 如果 CLI 调用不稳定，考虑使用 HTTP API（如果 Pi 提供）
- 隔离层: 将 Pi 调用封装在 `Solver` 接口后，便于替换实现

### 风险 2: SQLite 并发性能

**风险**: Go 的 `database/sql` 默认连接池可能与 SQLite WAL 单写者冲突。

**缓解**:
- 配置: `db.SetMaxOpenConns(1)` 强制单写者
- 测试: 压测验证无 `SQLITE_BUSY` 错误
- 备用方案: 如果性能不足，考虑切换到 PostgreSQL（需要修改 Repository 层）

### 风险 3: 时间表延期

**风险**: 总工期 27-37 天，可能因技术难点延期。

**缓解**:
- MVP 优先: Phase 1-4 是核心，优先完成
- 并行开发: Phase 2 (control) 与 Phase 3 (platform) 可部分并行
- 持续集成: 每个 Phase 结束后立即集成测试，避免最后阶段的大量返工

### 风险 4: 团队 Go 经验不足

**风险**: 如果团队对 Go 不熟悉，可能产生低质量代码。

**缓解**:
- 培训: 提前学习 Go 最佳实践（effective Go, Go Proverbs）
- Code Review: 每个 PR 都需要 Review，确保遵循规范
- 工具链: 使用 `golangci-lint`、`gofmt`、`goimports` 自动化检查

---

## 成功标准

### 功能完整性
- [ ] 所有 Python 版本的 API 端点在 Go 版本中实现
- [ ] 前端无需修改，可与 Go 后端无缝对接
- [ ] Docker Compose 一键启动，端到端流程通过

### 性能提升
- [ ] Challenges API 响应时间: P99 < 100ms（Python 版本约 150ms）
- [ ] 内存占用: control < 50MB（Python 版本约 150MB）
- [ ] 启动时间: < 1s（Python 版本约 5s，包含 uvicorn 启动）

### 代码质量
- [ ] 测试覆盖率 > 80%
- [ ] `golangci-lint` 零警告
- [ ] 所有公共 API 有 GoDoc 注释

### 可维护性
- [ ] 模块边界清晰，依赖方向单向
- [ ] 每个 Package 有独立的 `README.md`
- [ ] 新增功能的开发时间减少 30%（得益于类型安全与工具链）

---

## 附录

### A. Go 项目布局参考

参考 Go 社区标准布局: https://github.com/golang-standards/project-layout

### B. SQLite 迁移最佳实践

- 使用版本化迁移（`PRAGMA user_version`）
- 迁移脚本放在 `internal/*/repository/migrations/` 目录
- 每个迁移一个 `.sql` 文件，按版本号命名（`001_initial.sql`, `002_add_canonical_flag.sql`）
- 启动时自动应用未执行的迁移

### C. 测试策略

- **单元测试**: 测试单个函数/方法，Mock 外部依赖
- **集成测试**: 测试模块间交互，使用真实 SQLite（`:memory:`）
- **端到端测试**: 启动所有服务，通过 HTTP 调用验证
- **契约测试**: 验证 Go 版本与 Python 版本的 API 一致性

### D. 持续集成配置示例

```yaml
# .github/workflows/test.yaml
name: Test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: '1.24'
      - run: go test -v -race -coverprofile=coverage.out ./...
      - run: go tool cover -func=coverage.out
      - uses: golangci/golangci-lint-action@v4
```

---

## 总结

本重构计划将 Ghost 从 Python 迁移到 Go，采用模块化单体架构，保持功能完整性与 API 兼容性，同时提升性能与可维护性。关键要点：

1. **单一二进制，多子命令**: `ghost control`, `ghost worker`, `ghost platform`
2. **清晰的模块边界**: `contracts` ← `control`/`worker`/`platform`
3. **保持 SQLite 单写者约束**: 每个进程独立 DB，WAL 模式
4. **API 100% 兼容**: 前端与 SDK 无需修改
5. **渐进式实施**: 8 个 Phase，27-37 天总工期

通过采用 Go 的类型安全、并发原语、标准库丰富性，预期性能提升 30-50%，代码可维护性显著改善，为 Ghost 的长期演进奠定坚实基础。
