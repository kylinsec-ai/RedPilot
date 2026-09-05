# TSecBench(极简 MVP)

安全基准测试的最小闭环:**平台(core)出题判分 + 一个求解 worker 自动刷题 + 本地观测平台(obs)归档展示 agent 日志**。

```
┌──────────────┐  HTTP(BENCHMARK_TOKEN)   ┌───────────────────────────────┐
│ 远端跑分平台   │◀─────────────────────────│ worker 容器(VPN + Pi Agent)    │
│ /openapi/v1  │                           │ 常驻串行: 开题→解→直提→关题     │
└──────┬───────┘                           │ 求解中实时推送 → 观测平台        │
       │(本地 core 复刻同协议, 宿主 :8000)      └──────────────┬────────────────┘
       └──────────────────────────────────────────────┐      │ POST /api/internal/*
                                                       │      ▼ (agent 日志/事件/live)
                                               ┌───────▼───────────────┐
                                               │ 本地观测平台 obs       │  浏览器 ──▶ :8090
                                               │ FastAPI + SQLite WAL  │  态势台 SPA + Runs 历史
                                               │ :8090 同源托管 web/    │  (数据全部读平台库)
                                               └───────────────────────┘
```

- **平台 core**(`tsecbench/` + `main.py`):FastAPI 服务,管理跑分任务生命周期 —— 列题、起停题目实例(static/docker 供给)、可选 hint(扣分)、flag 提交判分(SHA-256,明文不落库)。状态持久化 SQLite。**无任何网页/控制台**。生产跑分走远端官方平台(`BENCHMARK_BASE_URL`),core 为协议兼容的本地方案。
- **求解 worker**(`adapter/` + `drivers/benchmark_driver.py` + compose):单个带 VPN 的容器,一次开一道题、单会话 Pi Agent 求解、候选 flag 直接提交(平台判分/幂等即唯一闸门),刷完轮询待命。无多 worker 分片、无记忆/黑板/止损/skeptic 验证等重机制。
- **本地观测平台 obs**(`obs/` + `Dockerfile.platform`):agent 日志入库并展示 —— worker 内置最小中继(`drivers/obs_relay.py`,订阅 LiveBus + transcript 字节续读)实时推送 run 生命周期/事件行/live 快照/roster 到平台 SQLite;FastAPI 同源托管 SPA 与只读 API(与旧 worker :8080 态势台字节兼容),新增 Runs 历史页。
- worker 平台接入直调官方 SDK `tsec-benchmark`(异步 `TSecBenchmarkAsync`,入口自带 VPN 预检;用法见 [`SDK_API.md`](SDK_API.md));平台服务端契约见 [`CHALLENGES_API.md`](CHALLENGES_API.md)。

## 运行平台 core

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 任务目录(JSON,含 token 与 challenges;每题 flag 明文写在此文件)
export TSECBENCH_CONFIG=/path/to/tasks.json
# 可选: TSECBENCH_DB_PATH(默认 ./data/tsecbench.sqlite3)、PORT(8000)、
#        TSECBENCH_MAX_ACTIVE_CHALLENGES(3)、TSECBENCH_PROVISIONER(static|docker)
.venv/bin/python main.py
```

任务目录格式:`{"tasks":[{token, challenges:[{unique_code, description, difficulty,
level, total_score, flags, hint?, hint_cost_radio?, container_addr?, image?,
container_port?, docker_network?}]}]}`,或直接传对象/数组;`TSECBENCH_TASKS_JSON`
可传内联 JSON。鉴权:`BENCHMARK_TOKEN` 请求头(缺失/无效返回 404 task_not_found)。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TSECBENCH_CONFIG` / `TSECBENCH_TASKS_JSON` | unset | 任务目录(文件路径 / 内联 JSON) |
| `BENCHMARK_TOKEN` | unset | 请求鉴权 token(也注入任务定义) |
| `TSECBENCH_DB_PATH` | `./data/tsecbench.sqlite3` | SQLite 路径 |
| `TSECBENCH_MAX_ACTIVE_CHALLENGES` | `3` | 同时活跃题目实例上限 |
| `TSECBENCH_PROVISIONER` | `static` | 题目供给: static(认 container_addr)/ docker |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | `python main.py` 监听地址 |

## 运行求解 worker

前置:core 已起;`.env` 填好平台地址/凭据与模型 key;VPN 配置文件放在 `./vpn/client.ovpn`。

首次先构建基础镜像(自包含 kali-linux-headless + pi 环境;`tsecbench/kali:latest` 已存在则跳过):

```bash
docker build -f Dockerfile.base -t tsecbench/kali:latest .
```

```bash
cp .env.example .env        # 填 BENCHMARK_TOKEN/BASE_URL/DEEPSEEK_API_KEY
docker compose up -d --build
docker compose logs -f      # 观察刷题进度
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `BENCHMARK_TOKEN` / `BENCHMARK_BASE_URL` | — | 平台凭据(必填) |
| `DEEPSEEK_API_KEY` | — | LLM 凭据(默认 provider deepseek 时必填;pi 官方 env 名,仓库零读写) |
| `SOLVER_MODEL` | `deepseek/deepseek-v4-flash` | 完整 `[provider/]model`,逐字透传(缺省值见 `adapter/config.py`) |
| `SOLVER_SESSION_SECONDS` | `1500` | 单会话时长上限 |
| `PI_STALL_TIMEOUT` | `480` | pi 无输出判定卡死的秒数 |
| `ADAPTER_WORKDIR` | `/work` | 解题工作目录根(compose 已设) |
| `ADAPTER_FLAG_FORMAT` | `flag{...}` | 提示词中的 flag 格式说明 |
| `ADAPTER_VPN_CONFIG` | `/vpn/client.ovpn` | VPN 配置(entrypoint 读取,无则跳过) |
| `WATCHDOG_MAX_IDLE_SECONDS` | `300` | 心跳容忍(compose healthcheck) |
| `OBSERVABILITY_URL` | `http://platform:8090` | obs 摄取地址;compose 内自动指向 service;宿主直跑 driver 设 `http://127.0.0.1:8090`;**未设则中继整体禁用**(worker 行为零变化) |
| `OBSERVABILITY_TOKEN` | — | 与平台同值;只配 URL 忘配 token 时平台 ingest 全灰(503,响亮而非静默丢日志) |

## 运行本地观测平台(obs)

worker 求解过程中的 agent 日志(transcript 事件行、live 快照、run 生命周期、roster)由
`drivers/obs_relay.py` 实时推送到平台入库,浏览器访问平台即看到与旧态势台同构的 UI +
Runs 历史页。数据全部读平台库(SQLite WAL,`./data/obs.sqlite3`);worker 侧 :8080
status_server 保留、仅作容器本地调试。

```bash
# 容器方式(与 worker 同栈起):
docker compose up -d --build platform     # 或整体 docker compose up -d --build
# 浏览器: http://localhost:8090(首页即 SPA;#/runs 为历史页)

# 宿主裸跑(开发,复用 .venv):
OBSERVABILITY_TOKEN=your-token \
OBSERVABILITY_WEB=$PWD/web \
OBSERVABILITY_DB=$PWD/data/obs.sqlite3 \
.venv/bin/uvicorn obs.app:app --host 0.0.0.0 --port 8090
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `OBSERVABILITY_TOKEN` | — | ingest 鉴权(`X-Observability-Token` 常量时间比较);未配 → ingest 响亮 503 |
| `OBSERVABILITY_DB` | `./data/obs.sqlite3` | SQLite 路径(父目录自动建) |
| `OBSERVABILITY_WEB` | —(未配则 / 返回 404 说明) | SPA 产物目录;容器内 `/app/web` |
| `OBSERVABILITY_HOST` / `OBSERVABILITY_PORT` | `0.0.0.0` / `8090` | uvicorn 监听(通常由 CMD 传入) |

摄取端点(`/api/internal/*`)仅接受带 token 的 POST;读端(`/api/status|roster|challenge|
timeline|transcript|runs...`、SSE `/api/events`、静态)只读开放。语义注:

- 事件行为原文 JSONL(与 transcript 同信任域);`message_update` 流式增量在 worker 侧
  丢弃,故 raw 面板/事件页不含增量(实时感由 SSE 快照 + 时间线轮询维持)。
- `runs.status` 由中继按关闭帧推导(`solved`=全解出 / `done`=结束未全解 / `failed`=出错 /
  `interrupted`=心跳超时或 worker 换题/重启残留)——仅为展示口径,判分以跑分平台为准。
- 平台进程要求单实例(FastAPI `workers=1`):进程内唯一 SQLite 写者 + SSE bus 的硬前提。

### 更换 provider / 模型(pi 官方凭据机制)

LLM 凭据遵循 pi 官方机制(<https://pi.dev/docs/latest/providers>):key 以 provider
官方 env 名直通容器,由 pi 自行解析鉴权(解析顺序:`auth.json` > env);仓库代码不做
任何映射/别名。默认 provider 为 deepseek(env 名 `DEEPSEEK_API_KEY`)。换内置
provider 只需三处:

1. `docker-compose.yaml`:加一行同名 env 转发(如 `OPENCODE_API_KEY: ${OPENCODE_API_KEY:-}`);
2. 宿主 `.env`:提供同键名的官方 env 值;
3. 设 `SOLVER_MODEL=provider/model`(完整 id,推荐写全)。

| provider | pi 官方 env 名 |
|---|---|
| deepseek(默认) | `DEEPSEEK_API_KEY` |
| opencode-go | `OPENCODE_API_KEY` |
| xiaomi | `XIAOMI_API_KEY` |
| 其余 | <https://pi.dev/docs/latest/providers> |

其他说明:

- `SOLVER_MODEL` 为完整 `[provider/]model`(如 `opencode-go/gpt-5.1`),逐字透传;裸 id(如 `deepseek-v4-flash`)不再自动补前缀,解析归 pi。
- 目录外模型/自定义 provider:`./pi/` 下放 `models.json`(官方格式)并启用 compose 中注释的卷挂载;`auth.json`(0600,优先于 env)同理;不要烤进镜像。
- 宿主直跑 driver(不走 compose)需自行 `export DEEPSEEK_API_KEY=...`(worker 侧 Python 不加载 `.env`)。

## 构建/开发态势台前端

`web/` 是构建产物、`frontend/` 是源码——改 UI 后需重新构建并提交产物
(平台镜像/服务与 worker 容器 bind-mount 都只认 `web/`,后端零改动):

```bash
cd frontend && npm ci && npm run build   # svelte-check + vite build → ../web/
```

- 依赖全部在 `devDependencies`,运行时无任何 JS 依赖;产物已在浏览器端就绪。
- **数据源已是观测平台**:平台服务(obs :8090)同源托管 `web/` 并提供全部 `/api/*`
  (含 SSE `/api/events` 流式);worker 容器 :8080 的 status_server 保留,仅容器本地调试。
- 本地开发:`npm run dev`(Vite :5173)会把 `/api` 代理到 `127.0.0.1:8090`(宿主裸跑的
  观测平台),含 SSE 流式直通。
- 版本注意:typescript 必须锁 6.x(`svelte-check` 不支持 7.x Go 版);package-lock.json
  已锁定,装依赖用 `npm ci`。

## 目录

- `tsecbench/` — 平台 core(纯 REST,远端跑分平台的本地协议复刻)
- `obs/` — **本地观测平台**:FastAPI 装配 + SQLite(WAL,版本化迁移)存储/摄取/折叠/读端
  (`app/db/store/schema/ingest/read/digest/redact/bus` + `tests/`)
- `adapter/` — worker 侧组件库:config/task/taskprompt + `solver/`(Pi Agent)+ `live/`(快照/SSE bus)
- `drivers/benchmark_driver.py` — 串行求解主循环(唯一编排者,异步直调官方 SDK)
- `drivers/obs_relay.py` — worker 最小观测中继(订阅 LiveBus + transcript 字节续读 → POST obs)
- `drivers/status_server.py` + `drivers/roster.py` — 容器本地调试态势台(:8080,已非数据主源)
- `frontend/` — 态势台前端源码(Svelte 5 + TypeScript + Tailwind v4 + Vite;总览/题目/Runs 历史)
- `web/` — 前端**构建产物**(提交入库),被平台与 worker 容器 bind-mount 共享
- `entrypoint.sh` / `Dockerfile` / `Dockerfile.platform` / `requirements*.txt` /
  `docker-compose.yaml` — worker 与观测平台镜像/编排
- `CHALLENGES_API.md` / `SDK_API.md` — 跑分平台服务端契约 / 官方 SDK 接入文档
