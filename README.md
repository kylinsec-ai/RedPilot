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

- **平台 core**(`packages/core/tsecbench/` + `main.py`):FastAPI 服务,管理跑分任务生命周期 —— 列题、起停题目实例(static/docker 供给)、可选 hint(扣分)、flag 提交判分(SHA-256,明文不落库)。状态持久化 SQLite。**无任何网页/控制台**。生产跑分走远端官方平台(`BENCHMARK_BASE_URL`),core 为协议兼容的本地方案。
- **求解 worker**(`packages/worker/`,pip 包 `tsecbench-worker` + compose):单个带 VPN 的容器,一次开一道题、单会话 Pi Agent 求解、候选 flag 直接提交(平台判分/幂等即唯一闸门),刷完轮询待命。无多 worker 分片、无记忆/黑板/止损/skeptic 验证等重机制。
- **本地观测平台 obs**(`packages/obs/`,pip 包 `tsecbench-obs` + `Dockerfile.platform`):agent 日志入库并展示 —— worker 内置最小中继(`tsecbench_worker.relay`,订阅 LiveBus + transcript 字节续读)实时推送 run 生命周期/事件行/live 快照/roster 到平台 SQLite;FastAPI 同源托管 SPA 与只读 API(与 worker :8080 本地态势台字节兼容),新增 Runs 历史页。
- worker 平台接入直调官方 SDK `tsec-benchmark`(异步 `TSecBenchmarkAsync`,入口自带 VPN 预检;用法见 [`SDK_API.md`](SDK_API.md));平台服务端契约见 [`CHALLENGES_API.md`](CHALLENGES_API.md)。

### 四包架构(monorepo)

```
packages/
├── contracts/   # tsecbench-contracts — 零依赖契约单源:词汇(phase/run状态/事件kind/信封)、
│                #   snapshot schema、redact/截断、原子IO、/work 路径约定、digest 折叠状态机、资产 mime 表
├── core/        # tsecbench-core → import tsecbench(平台 core,自包含)
├── worker/      # tsecbench-worker → import tsecbench_worker(编排/求解/中继/本地数据层)
└── obs/         # tsecbench-obs → import obs(观测平台;基线纯 stdlib,[platform] extra 才带 fastapi)
```

依赖方向(测试强制):`contracts ← core/worker/obs`;core 无人依赖;worker 与 obs 互不 import
—— 唯一例外是 worker 的 driver 导入 `obs.localserver`(注入式 stdlib 本地仪表板 :8080,
协作者全部构造注入)。跨包语义改动只改 contracts 一处;`packages/contracts/tests/`
含纯度门(零第三方依赖)与 fold-parity 测试(worker I/O 壳与 obs 无状态折叠同语义)。

## 运行平台 core

```bash
python3 -m venv .venv && .venv/bin/pip install -e packages/contracts -e packages/core -e packages/obs -e packages/worker
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

> 安全边界:`/openapi/v1/vpn/*`(上传/启停平台级 openvpn,进程以 root 运行)属管理
> 端点,仅接受 `TSECBENCH_ADMIN_TOKEN` 请求头,与参与方任务 token 严格隔离;
> 上传配置会拒绝脚本执行类指令(`script-security`/`up`/`down`/`plugin` 等)并以
> `--script-security 1` 启动。多参与方共享平台时,`TSECBENCH_ADMIN_TOKEN` 只由运营者持有。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TSECBENCH_CONFIG` / `TSECBENCH_TASKS_JSON` | unset | 任务目录(文件路径 / 内联 JSON) |
| `BENCHMARK_TOKEN` | unset | 任务请求鉴权 token(也注入任务定义);**仅限 `/openapi/v1/challenges/*`** |
| `TSECBENCH_ADMIN_TOKEN` | unset | 管理端点(`/openapi/v1/vpn/*` 平台级 openvpn 生命周期)专用凭据;未配置 → 管理端点 503 拒用(fail closed);参与方任务 token 一律不可触达 |
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

### 升级 pi(必须保持最新)

镜像内 `npm install -g @earendil-works/pi-coding-agent` 不钉版本——**重建镜像即取最新**。
pi 新版发布后的标准动作(2026-09-08 事故教训:pi 0.74.2 不发 Console Go 强制的
`x-opencode-session` 头,provider 400 被 pi 表象成 0-turn/err=none 会话,静默烧题库;
修复在 0.75.5,而 0.85.1 已要求 Node ≥22.19):

1. `npm view @earendil-works/pi-coding-agent version` + 读 CHANGELOG(重点:engines/Node 要求、compat/session 头、tools 协议变化)。
2. pi 的 `engines.node` 升了就同步改 `Dockerfile.base` / `Dockerfile` 的 `NODE_VERSION`(两处必须一致——两层都各自 tar 解压覆盖 `/usr/local`)。
3. `docker build -f Dockerfile.base -t tsecbench/kali:latest . && docker build -f Dockerfile -t tsecbench-adapter:latest .`
4. 回归:`docker run --rm --env-file .env tsecbench-adapter:latest pi --version`、
   `docker run --rm --env-file .env tsecbench-adapter:latest pi --print --model $SOLVER_MODEL "Reply with exactly: OK"`(应输出 OK)、
   起 worker 后首题 turns>0。
5. 提交本次版本钉(Dockerfile 的 NODE_VERSION),复盘记录距上次重建天数。

```bash
cp .env.example .env        # 填 BENCHMARK_TOKEN/BASE_URL/DEEPSEEK_API_KEY
docker compose up -d --build
docker compose logs -f      # 观察刷题进度
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `BENCHMARK_TOKEN` / `BENCHMARK_BASE_URL` | — | 平台凭据(必填) |
| `DEEPSEEK_API_KEY` | — | LLM 凭据(默认 provider deepseek 时必填;pi 官方 env 名,仓库零读写) |
| `SOLVER_MODEL` | `deepseek/deepseek-v4-flash` | 完整 `[provider/]model`,逐字透传(缺省值见 `packages/worker/tsecbench_worker/config.py`) |
| `SOLVER_SESSION_SECONDS` | `1500` | 单会话时长上限 |
| `PI_STALL_TIMEOUT` | `480` | pi 无输出判定卡死的秒数 |
| `ADAPTER_WORKDIR` | `/work` | 解题工作目录根(compose 已设) |
| `ADAPTER_FLAG_FORMAT` | `flag{...}` | 提示词中的 flag 格式说明 |
| `ADAPTER_VPN_CONFIG` | `/vpn/client.ovpn` | VPN 配置(entrypoint 读取,无则跳过) |
| `WATCHDOG_MAX_IDLE_SECONDS` | `300` | 心跳容忍(compose healthcheck) |
| `OBSERVABILITY_URL` | `http://platform:8090` | obs 摄取地址;compose 内自动指向 service;宿主直跑 driver 设 `http://127.0.0.1:8090`;**未设则中继整体禁用**(worker 行为零变化) |
| `OBSERVABILITY_TOKEN` | — | 与平台同值;只配 URL 忘配 token 时平台 ingest 全灰(503,响亮而非静默丢日志) |
| `STATUS_BIND` | `127.0.0.1` | 状态服务(容器内 :8080)监听地址;数据无鉴权 → 默认仅回环。**compose 内须显式 `0.0.0.0`**(docker-proxy 需全网卡转发),宿主侧暴露由 `STATUS_HOST_IP` 收口 |
| `STATUS_HOST_IP` | `127.0.0.1` | compose 宿主侧发布绑定(默认回环);LAN/远程查看才设 `0.0.0.0`(自担风险) |

## 运行本地观测平台(obs)

worker 求解过程中的 agent 日志(transcript 事件行、live 快照、run 生命周期、roster)由
`tsecbench_worker.relay` 实时推送到平台入库,浏览器访问平台即看到与旧态势台同构的 UI +
Runs 历史页。数据全部读平台库(SQLite WAL,`./data/obs.sqlite3`);worker 侧 :8080
obs.localserver 保留、仅作容器本地调试(注入式,协作者零 import)。

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
| `OBSERVABILITY_HOST` / `OBSERVABILITY_PORT` | `0.0.0.0` / `8090` | uvicorn 监听(通常由 CMD 传入);容器内须 0.0.0.0(worker 中继走 compose 网络) |
| `OBS_PORT` / `OBS_HOST_IP`(compose 宿主侧) | `8090` / `127.0.0.1` | 发布端口 / 绑定地址;默认回环 —— 读端无鉴权,见下 |

摄取端点(`/api/internal/*`)仅接受带 token 的 POST;读端(`/api/status|roster|challenge|
timeline|transcript|runs...`、SSE `/api/events`、静态)只读开放 —— 刻意无鉴权,但读端
返回**已接受 flag 明文与完整 agent 实录**(评测答案材料):compose 默认只把 8090 发布到
**宿主回环**(`host_ip: 127.0.0.1`;浏览器 `http://localhost:8090` 即可,远程查看请走
SSH 隧道;确需局域网放行时显式设 `OBS_HOST_IP=0.0.0.0`,答案泄露风险自担)。语义注:

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
  (含 SSE `/api/events` 流式);worker 容器 :8080 的 status_server 保留,仅容器本地调试
  (宿主侧默认仅回环发布,容器内监听由 `STATUS_BIND` 控制 —— 见 worker 环境变量表)。
- 本地开发:`npm run dev`(Vite :5173)会把 `/api` 代理到 `127.0.0.1:8090`(宿主裸跑的
  观测平台),含 SSE 流式直通。
- 版本注意:typescript 必须锁 6.x(`svelte-check` 不支持 7.x Go 版);package-lock.json
  已锁定,装依赖用 `npm ci`。

## 目录

- `packages/` — **四包 monorepo**(各含 pyproject.toml 与独立测试;依赖方向 contracts←其余)
  - `packages/contracts/` — 零依赖共享契约:词汇/快照 schema/脱敏与截断/原子 IO/路径约定/
    digest 折叠状态机/资产 mime 表(+ purity/fold-parity 测试)
  - `packages/core/tsecbench/` — 平台 core(纯 REST,远端跑分平台的本地协议复刻)
  - `packages/obs/obs/` — **本地观测平台**:FastAPI 装配 + SQLite(WAL,版本化迁移)存储/
    摄取/读端(`app/db/store/schema/ingest/read/bus` + `localserver.py` 本地实时源仪表板)
  - `packages/worker/tsecbench_worker/` — worker 全量:`settings/config/task/taskprompt/
    flags/transcripts/live/solver(Pi Agent)/orchestration/driver/roster/relay/
    platform(平台抽象 ABC)/queue(jobs API 惰性)`
- `skills/` — **pi skills 知识库**(按题型打法:web-recon-toolkit / known-cve-playbook /
  waf-bypass / sandbox-escape / cloud-security / network-pwn / reverse-engineering)。
  以 pi 原生 skill 格式(SKILL.md + name/description frontmatter)挂载到容器
  `/root/.pi/agent/skills`(自动发现,渐进披露:description 常驻、正文模型按需 read);
  每题 workdir 的 CLAUDE.md 只留硬规则(flag 协议 + skill 指针)。改内容免重建镜像,
  compose bind-mount 下一题即生效;镜像构建时 COPY 烘焙。
- `tools/` — 浏览器自动化助手脚本(pw_fetch.py / pw_example.py),挂载 `/opt/tools`
- 串行求解主循环 = `packages/worker/tsecbench_worker/driver.py`(进程装配 + 心跳看门狗)+
  `orchestration.py`(唯一编排者,异步直调官方 SDK)
- 观测中继 = `packages/worker/tsecbench_worker/relay.py`(订阅 LiveBus + transcript 字节续读 → POST obs)
- 本地态势台 = `packages/obs/obs/localserver.py`(:8080,已非数据主源;协作者注入)
- `frontend/` — 态势台前端源码(Svelte 5 + TypeScript + Tailwind v4 + Vite;总览/题目/Runs 历史)
- `web/` — 前端**构建产物**(提交入库),被平台与 worker 容器 bind-mount 共享
- `entrypoint.sh` / `Dockerfile` / `Dockerfile.platform` / `pytest.ini` /
  `docker-compose.yaml` — worker 与观测平台镜像/编排
- `CHALLENGES_API.md` / `SDK_API.md` — 跑分平台服务端契约 / 官方 SDK 接入文档
