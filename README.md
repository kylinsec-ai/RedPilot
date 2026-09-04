# TSecBench(极简 MVP)

安全基准测试的最小闭环:**平台(core)出题判分 + 一个求解 worker 自动刷题**。

```
┌──────────────┐  HTTP(BENCHMARK_TOKEN)   ┌───────────────────────────────┐
│ core (宿主机) │◀─────────────────────────│ worker 容器(VPN + Pi Agent)    │
│ :8000        │  /openapi/v1/*           │ 常驻串行: 开题→解→直提→关题     │
│ 纯 REST 无 UI │                          └───────────────────────────────┘
└──────────────┘
```

- **平台 core** (`tsecbench/` + `main.py`):FastAPI 服务,管理跑分任务生命周期 —— 列题、起停题目实例(static/docker 供给)、可选 hint(扣分)、flag 提交判分(SHA-256,明文不落库)。状态持久化 SQLite。**无任何网页/控制台,worker 是唯一客户端**。
- **求解 worker** (`adapter/` + `drivers/benchmark_driver.py` + compose):单个带 VPN 的容器,一次开一道题、单会话 Pi Agent 求解、候选 flag 直接提交(平台判分/幂等即唯一闸门),刷完轮询待命。无多 worker 分片、无记忆/黑板/止损/skeptic 验证等重机制。
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
(镜像 `COPY web` 与宿主机 bind-mount 都只认 `web/`,后端零改动):

```bash
cd frontend && npm ci && npm run build   # svelte-check + vite build → ../web/
```

- 依赖全部在 `devDependencies`,运行时无任何 JS 依赖;产物已在浏览器端就绪。
- 本地开发:`npm run dev`(Vite :5173)会把 `/api` 代理到 `127.0.0.1:8080`(跑着的
  worker 容器),含 SSE 流式直通。
- 版本注意:typescript 必须锁 6.x(`svelte-check` 不支持 7.x Go 版);package-lock.json
  已锁定,装依赖用 `npm ci`。

## 目录

- `tsecbench/` — 平台 core(纯 REST)
- `adapter/` — worker 侧组件库:config/task/taskprompt + `solver/`(Pi Agent)
- `drivers/benchmark_driver.py` — 串行求解主循环(唯一编排者,异步直调官方 SDK)
- `drivers/status_server.py` + `drivers/roster.py` — 人读态势台(:8080)的只读数据层
  (stdlib-only HTTP:页面/资产/API/SSE 同源)
- `frontend/` — 态势台前端源码(Svelte 5 + TypeScript + Tailwind v4 + Vite)
- `web/` — 前端**构建产物**(提交入库):`web/index.html` + `web/assets/*`,被镜像
  COPY 与 bind-mount,status_server 按 mtime 热更
- `entrypoint.sh` / `Dockerfile` / `docker-compose.yaml` — worker 镜像与编排
- `CHALLENGES_API.md` / `SDK_API.md` — 平台服务端接口契约 / 官方 SDK 接入文档
