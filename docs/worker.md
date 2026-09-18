# RedPilot worker 模块（`redpilot.worker`）

安全基准测试的**求解 worker**：一个常驻容器，连上靶场 VPN，领题目、驱动 Pi Agent
解题、把 flag 提交回平台，同时把全过程实时推到观测平台。

本包的主循环是**竞技场**（`redpilot.worker.orchestrator`，约 7,000 行）：多轮时间盒、
多会话重访、多维止损、eager 即时提交、能力分片、跨场续接、舰队监督与协作式热重载。
求解引擎是**朋友的 Pi Agent 实现**（`redpilot.worker/adapter/solver/pi_agent.py`）：
四个看门狗（stall / 会话 deadline / stop_check / 子 Agent 静默死锁）、令牌式进程树
回收（驱动崩溃后脱组的 `nohup`/`setsid` 子孙也收得回）、subagent 与 skills 安装器、
逐题 `.pi-home` 隔离、provider 配置落地。

## 跑起来

```bash
cp .env.example .env      # 填 BENCHMARK_TOKEN / BENCHMARK_BASE_URL / DEEPSEEK_API_KEY
docker compose up -d      # server + 三容器 worker 舰队
```

裸 `up -d` 起的就是舰队（worker-1 持 VPN，worker-2/3 复用它的 netns）。
**单体形态**（一个容器自己持 VPN 自己解题）点它的名字起：

```bash
docker compose up -d worker
```

`worker` 带一个 profile 只是为了让裸 `up -d` 不带它；**别用 `--profile monolith up -d`**
（会把舰队一起拉起来）。compose 对显式点名的服务不看 profile 门槛。

| 服务 | 角色 | VPN |
|---|---|---|
| `worker-1` | 只维持 VPN + 状态汇总 + 他管，**不做题** | 持有 tun（`cap_add NET_ADMIN` + `/dev/net/tun`） |
| `worker-2` / `worker-3` | 解题 | `network_mode: service:worker-1`，复用 worker-1 的隧道 |
| `worker` | 单体形态：自己持 VPN 自己解题 | 持有 tun（与 worker-1 同配置） |

一个靶场 VPN 只应有一条隧道；三个容器各起一条会互相抢路由。代价是 worker-1 必须
常驻 —— 它一停另两个就断网（由 netns 看门狗兜底：worker-2/3 检测到被孤立会退出重启）。

## provider 与模型（都可配）

求解用的 provider 与模型**都不写死在代码里**，由两个环境变量决定：

| 变量 | 作用 | 缺省 |
|---|---|---|
| `ADAPTER_PROVIDER`（别名 `SOLVER_PROVIDER`） | 网关路由标签：决定 `--model <provider>/<id>` 前缀、逐题 `models.json` 的 `providers` 键、凭据 env 名 | `deepseek` |
| `SOLVER_MODEL` | 模型。写裸名则补上面的 provider；写 `<provider>/<id>` 则**前缀优先** | `mimo-v2.5` → `deepseek/mimo-v2.5` |

**provider → 凭据 env 对照**（`pi_agent.provider_profile()`，未登记的名字自动归为
`<PROVIDER>_API_KEY` + OpenAI 兼容，所以换网关不用改仓库代码）：

| provider | 凭据 env | api |
|---|---|---|
| `deepseek` | `DEEPSEEK_API_KEY` | `openai-completions`（含 DeepSeek reasoning 兼容块） |
| `glm` | `GLM_API_KEY` | `openai-completions` |
| `openai` | `OPENAI_API_KEY` | `openai-completions` |
| `anthropic` | `ANTHROPIC_API_KEY` | `anthropic-messages` |
| `openrouter` | `OPENROUTER_API_KEY` | `openai-completions` |
| 其它 | `<大写_下划线>_API_KEY` | `openai-completions` |

换 provider 的最短路径：`ADAPTER_PROVIDER=<name>` + `SOLVER_MODEL=<id>` +（容器里）
给 `docker-compose.yaml` 转发同名 `<NAME>_API_KEY`。注意凭据回退也走这张表 ——
不再回退到写死的 `DEEPSEEK_API_KEY`。

> 验证/观察者（verifier / Heimdall）走**独立**的 `_VERIFIER_PRESETS`（默认
> `deepseek-v4-flash`），不受上面的求解模型变更影响；要改用它自己的 `LLM_MODEL`。

求解引擎传输层（`--mode rpc` 常驻 / `--mode json --print` 一次性）见
`docs/pi-rpc-migration-research.md`，开关是 `ADAPTER_PI_TRANSPORT`。

## 结构

```
redpilot/worker/
├── driver.py         装配层：配置校验 + 观测面接线 + 注入观测桥（不起线程）
├── orchestrator.py   主循环（竞技场）：list → 派发 → 多会话 → 提交 → 收尾
├── supervisor.py     他管层：卡死判定 → 只允许「请求对方热重载」
├── observability.py  观测桥：编排状态 → LiveState/LiveBus（唯一的新逻辑，有单测）
├── relay.py          观测中继：run 生命周期/事件行/live/roster → 平台 /api/internal/*
├── roster.py         题目总览轮询（60s，落 work/.live/roster.json）
├── dashboard.py      本地态势台（:8080，stdlib，原 obs/localserver）
├── live/             实时状态（原子 JSON）与事件总线（SSE）
├── settings.py       进程配置的单一 getenv 收编点
├── solver/           引擎契约（SolveResult / touch_heartbeat）
└── adapter/          策略层：证据闸门、止损、黑板、heimdall、Pi 引擎、平台适配
```

引擎在 `adapter/solver/`，策略在 `adapter/`，编排在 `orchestrator.py`。**没有第二套
编排**：框架自己那套（`orchestration.solve_one` / `solver/friend.py` 桥接 /
assignment claim 链路）已随竞技场上位退位删除。

## 关键约定

- **状态有两个落点，都在 `/work`**：
  - `status/worker-<N>.json` —— 编排层的进度（`solving_active` / `sessions` /
    `flags_submitted` …）。`supervisor.py` 与只读控制台读它。
  - `.live/<worker_id>.json` —— 观测面快照（`phase` / `current_tool` / `turns` …）。
    relay 推到平台，本地 :8080 态势台也读它。
  两者由 `observability.StatusBridge` 单向同步；两套字段名不同是刻意的（各有各的
  消费方），映射只在一个地方。
- **退出码**（本表是唯一权威，别处只许指向它）：
  | 码 | 含义 | 后果 |
  |---|---|---|
  | `0` | 任务终态 / **配置错误** | 明示后停止，**不重启** |
  | `86` | 协作式热重载（`touch /work/.reload.wid<N>`） | 会话边界收尾后自行退出 → 换新码拉起 |
  | `4` | VPN 层瞬断（entrypoint 的 openvpn / tun0 预检失败） | 拉起重试 |
  | `3` | worker-2/3 被孤立（共享 netns 的提供者没了） | 退出重建 netns |
  | 其余非零 | 意外崩溃 | `on-failure` 拉起 |

  与 compose 的 `restart: on-failure` 配套。⚠️ 配置错误**绝不能非零退出** ——
  `on-failure` 会重启一切非零退出，用 exit 1 表达"环境变量没填" = 无限重启闷循环。
  这也是 `driver.py` 与 `entrypoint.sh` 在配置校验失败时都走 exit 0 的原因。
  （挂死探测已删除：它原本在装配层，而那里看不到编排层的心跳 —— 详见 driver.py 顶部。）
- **进程回收靠令牌不靠登记表**：逐次访问生成随机 token 写进 `<workdir>/_instance.json`，
  pi 及其全部子孙继承该环境变量，收尾时扫 `/proc/*/environ` 回收。它不依赖本进程的
  登记表，所以**驱动崩溃后仍然有效**，且按构造无法误伤 driver / VPN provider / 另一个
  worker 的进程。
- **flag 提交有两道闸门**：eager 线程在会话进行中就盯着 FLAG 文件即时投递（不必等
  会话结束），投递前过一道确定性 grounding 门（候选必须逐字出现在本场真实工具输出
  里；本地静态产物能否算证据由题目分类决定）。平台响应（correct / duplicate）是唯一
  的最终判据。
- **时间盒与单题终身预算是联动的**：只抬 `ADAPTER_TIMEBOX_HARD` 不抬
  `ADAPTER_PER_CHALLENGE_SECONDS`，困难题会在第 66 分钟被止损掐掉。

## 测试

```bash
pytest -q                    # 全仓
pytest -q tests/worker    # 本包
```

`tests/worker/` 覆盖观测桥映射、观测中继、provider 失败护栏、态势台绑定。
仓库根的 `tests/` 里有 100+ 条竞技场行为的回归用例（止损、task epoch、eager 提交、
交付账本、多段题推进、supervisor 判据），它们 `import redpilot.worker.orchestrator`。
