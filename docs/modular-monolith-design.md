# RedPilot 模块化单体架构设计（非多包）

> 命题：把 RedPilot 从「三发行版 Python monorepo」收敛为「非多包、模块化单体应用」。
> 范围：源码组织与打包形态的架构收敛；**不改变**部署拓扑（server 容器 + worker 舰队）、
> 产品契约（HTTP relay/ingest、退出码、状态落点）与 387 条行为测试的语义（迁移后
> +16 条架构测试 = 403，见 §11 实施记录）。
> 关联：[`README.md`](../README.md)、[`docs/worker.md`](worker.md)（worker 约定）、
> [`tests/contracts/test_purity.py`](../tests/contracts/test_purity.py)（现有边界执行点）。

---

## 0. 结论（TL;DR）

1. **一个发行版**：`redpilot-contracts` / `redpilot` / `redpilot-worker` 三个 wheel
   合并为 `redpilot` 一个；根 `pyproject.toml` 唯一，`__version__` 唯一。
2. **四个一等模块**：`contracts`（共享内核）/ `control`（控制面）/ `obs`（观测平台）/
   `worker`（求解竞技场）。`app.py` 与 `worker/driver.py` 是两个**装配根**。
3. **边界改由 import 图执行**：pip 依赖图只能证明「装了什么」，不能证明「谁 import 谁」。
   本仓库需要的后者，用 stdlib AST 架构测试承担（零新依赖），顺带能覆盖包拆分覆盖不到的
   模块内部方向（control ↮ obs、adapter ↛ 编排、配置收编）。
4. **斩断唯一反向边**：`worker → redpilot.obs.localserver`（[`driver.py:28`](../packages/worker/redpilot_worker/driver.py)、
   [`roster.py:25`](../packages/worker/redpilot_worker/roster.py)）改为 worker 自己的
   `worker/dashboard.py`；协议常量仍归 `contracts`。合并后 worker 的 import 闭包 =
   自有依赖 + `contracts`，不再触碰 platform。
5. **不做兼容 shim**：旧包名的全部 import 点都在本仓（`packages/` 113 个 `.py` + 根 `tests/`，
   没有仓外消费者），shim 只会把旧名字再养一轮。迁移是机械替换，387 条测试 28s 内可验。

一句话：**包拆分买到的是安装时依赖闭包，代价是三个版本号、两处重复领域对象、一条反向边、
三处打包耦合；同一笔预算换成一组架构测试，边界保证更强、更全，且运行成本为零。**

---

## 1. 迁移前现状（快照）：三包 monorepo 到底在保证什么

> 本节与 §2 的路径是**迁移前**的 `packages/*` 快照，作为设计依据保留；当前布局见 §11。

| 发行版 | 位置 | 边界作用 | 机制 |
|---|---|---|---|
| `redpilot-contracts` | `packages/contracts/redpilot_contracts` | 零依赖共享内核（词汇/路径/脱敏/摘要/快照） | `dependencies = []` + `test_purity.py` AST 扫描 |
| `redpilot` | `packages/redpilot/redpilot` | 控制面 + 观测平台；FastAPI 归 `[platform]` extra | 基线依赖只有 contracts；`control/__init__.py` 延迟导入 FastAPI |
| `redpilot-worker` | `packages/worker/redpilot_worker` | 求解 worker（竞技场 + Pi 引擎 + relay） | 依赖 `redpilot`（基线）；`[project.scripts] redpilot-worker` |

### 1.1 真正被强制的只有「安装时依赖闭包」

- `pip install redpilot-contracts` 永远拉不到 fastapi —— 因为 `dependencies = []`。
- `pip install redpilot` 基线不含 fastapi —— 因为它们在 `[platform]` extra（Kali 镜像瘦身）。
- `pip install redpilot-worker` 会拉 `redpilot` 基线 —— 这正是 worker 能 import
  `obs.localserver` 的机制，也是反向边得以存在的通道。

这些都是**构建系统属性**，不是领域边界。它们随 `pip install` 成立，随 `pythonpath` 失效 ——
`pytest.ini` 就是同时把四个目录塞进 `pythonpath` 的，测试环境里包边界本来就是平的。

### 1.2 模块之间的方向只由注释与 review 保证

- 「obs 不得 import control」：写在 [`redpilot/__init__.py`](../packages/redpilot/redpilot/__init__.py)
  的 docstring 里，没有执行点。
- 「worker 只允许 import `redpilot.obs.localserver`」：同样只是 docstring。
- `test_purity.py` 只覆盖 contracts 自己（stdlib-only / 心跳路径 / 词汇一致性）。
- 结论：**包拆分没有在模块方向上提供任何自动化保证**。`obs → control` 今天写下一行
  `from redpilot.control...`，387 条测试一条都不会红。

---

## 2. 代价清单（每条有仓库内证据）

### 2.1 反向边：worker → platform，为一个 stdlib 仪表板

```
packages/worker/redpilot_worker/driver.py:28   from redpilot.obs.localserver import serve_forever_in_thread
packages/worker/redpilot_worker/roster.py:25   from redpilot.obs.localserver import challenge_detail, ...
```

- 后果：worker 的「允许 import platform 的名单」长期挂着一个文件；任何人顺手把
  `obs.store` / `obs.read` 接进来，没有测试能拦。
- 事实：`localserver` 的线程、端口、数据源（`LiveState`/`LiveBus`）、生命周期全在 worker
  进程里；它不是平台数据链路，是 worker 的本地诊断视图。放在 `obs/` 是**按协议归属**
  而不是按进程归属，方向反了。

### 2.2 重复领域对象：同一个概念两份定义

| 概念 | 旧（root） | 新（adapter） | 状态 |
|---|---|---|---|
| `AgentTask` | `redpilot_worker/task.py`（50 行） | `redpilot_worker/adapter/task.py`（62 行） | 字段不同：`files` 语义不同、`summary()`/`hint_fn` 只有一个有 |
| `SolveResult` | `redpilot_worker/solver/base.py` | `redpilot_worker/adapter/solver/base.py` | worker `__init__` 导出旧、orchestrator 用新 |
| flag 提取 | `redpilot_worker/flags.py` | `adapter/solver/base.py` + `verify.py` | 实现已归拢到 `flags.py`，但 adapter 里仍有 |
| 任务 prompt | `redpilot_worker/taskprompt.py`（141 行） | `adapter/taskprompt.py`（717 行） | 两套 `build_task_prompt`/`write_context_md` |

- worker `__init__.py` 导出旧的一份，orchestrator 用新的一份 —— 这正是「我是谁有两个答案」
  在领域对象层的重演（worker 序号推导刚在 `52c7d21` 修过同型 bug）。
- 后果：类型、序列化、prompt 组装的真实版本取决于 import 路径；改 A 忘 B 是必然。

### 2.3 打包耦合与手工路径维护

| 位置 | 内容 | 目录一变就要改 |
|---|---|---|
| `Dockerfile:82-88` | `COPY packages/{contracts,redpilot,worker}` + 三次 `pip install -e` | ✅ |
| `docker-compose.yaml:45-47,234-235` | 三个 editable 热补丁卷 | ✅ |
| `pytest.ini:2-3` | `testpaths = packages tests`；`pythonpath` 四个条目 | ✅ |
| `entrypoint.sh` / `Dockerfile.redpilot` | 模块入口 `redpilot_worker.driver` / `redpilot.app:app` | ✅ |

`Dockerfile` 里 skills 双 COPY 的注释记录过同类事故：路径漏改是**静默空目录**，
不是报错。三包让这种「多点同改」的成本乘了三倍。

### 2.4 概念成本

对外是两个进程角色（server / worker），仓库内部却先解释三个包。读者要先建立
「包 → 角色」的映射，才能读懂 README 的架构图。

---

## 3. 目标架构

### 3.1 目录（目标态）

```
redpilot/                          # 唯一发行版（top-level package）
├── __init__.py                    # 唯一 __version__；不 import 任何子模块（零副作用）
├── contracts/                     # ① 共享内核（原 redpilot_contracts）
│   ├── vocabulary.py  paths.py  redact.py  text.py
│   ├── snapshot.py    fsio.py   digest.py  assets.py  platform.py
│   └── __init__.py
├── control/                       # ② 控制面：challenges / 调度 / VPN / 评测
├── obs/                           # ③ 观测平台：ingest / read / SSE / SPA
├── app.py                         # 平台装配根（唯一同时 import control 与 obs 的地方）
└── worker/                        # ④ 求解：竞技场 + Pi 引擎 + relay + 本地态势台
    ├── __init__.py                # façade：只导出跨模块可用名
    ├── driver.py                  # worker 装配根
    ├── orchestrator.py            # 竞技场主循环
    ├── supervisor.py  relay.py  roster.py  observability.py
    ├── settings.py                # 进程配置唯一收编点
    ├── dashboard.py               # 原 obs/localserver.py（stdlib，零 fastapi）
    ├── live/                      # LiveState / LiveBus
    └── adapter/                   # 策略层 + Pi 引擎 + 平台适配
pyproject.toml                     # 唯一构建清单
tsecbench/                         # 本地靶场（dev harness，不进 wheel/镜像）
tests/
├── architecture/                  # 边界执行点（新增）
├── contracts/  control/  obs/  worker/   # 由 packages/*/tests 迁入
└── *.py                           # 竞技场端到端回归（保留原 175 条）
```

### 3.2 模块职责与数据所有权

| 模块 | 拥有 | 数据所有权 | 读者 |
|---|---|---|---|
| `contracts` | 词汇表、路径、快照 schema、脱敏/截断、摘要折叠、资产表 | 无状态 | 全部模块 |
| `control` | 题目定义、调度、容器供给、VPN、判分、outbox | `data/redpilot.sqlite3`（tasks/challenges/submissions/…/outbox_events） | `app.py`、控制面 API |
| `obs` | 摄取、读端、SSE、SPA、控制代理 | `data/redpilot.sqlite3`（观测库，独立于控制库） | `app.py`、态势台 |
| `worker` | 竞技场、止损、证据闸门、Pi 引擎、平台适配、relay、本地态势台 | `work/`（status/、.live/、逐题目录） | 平台（HTTP）、本地 :8080 |

规则：**一个库/目录只有一个写模块**。跨模块读走 API 或事件（worker relay → obs ingest
的 HTTP 契约是产品契约，不因合并发行版而变成函数调用）。

### 3.3 依赖红线（R1–R8）

```
                 contracts
                 ↑       ↑
        control  ↑       ↑  worker
            ↑    ↑       ↑    ↑
            └── app.py   └ driver.py（两个装配根）
```

| # | 规则 | 理由 |
|---|---|---|
| R1 | `contracts` 只允许 stdlib + 自身 | 现有 `test_purity.py` 承诺；两套 Python 环境（Kali apt / python:3.13）的安全交集 |
| R2 | `worker` 不得 import `control` / `obs` / `app`；import 闭包内不得出现 fastapi/pydantic/uvicorn | Kali 镜像瘦身从「装什么」升级为「import 什么都不行」 |
| R3 | `control` ↮ `obs`（双向禁止） | 现状只有 docstring；两者只通过 `app.py` 装配与 HTTP 代理相见 |
| R4 | `app.py` 不承载领域逻辑 | 装配根只做构造、路由、lifespan；领域规则下沉到模块 |
| R5 | 跨模块只准 façade：`from redpilot.<mod> import <名字>`，且 `<名字> ∈ 对方 __all__` | 消灭深 import 形成的事实公共 API |
| R6 | 模块内依赖单向：装配 → 编排 → 端口/适配器 → 内核；适配器不得反向 import 编排 | `transcripts` 曾绕 ABC 被 driver 直连（已修），这类渗漏要可执行地防住 |
| R7 | env 只在 `config.py` / `settings.py` 解析；领域与编排代码不收 env | worker `settings.py` 已是收编点，但 `orchestrator.py` 还有 10 处 getenv 直读（L77/188/204/…）——规则要能拦新债 |
| R8 | 数据所有权：一个 SQLite/文件只有一个写模块；跨模块读走 API/事件 | 现状正确，写进测试防回退 |

**例外（唯一）**：`app.py` 与 `worker/driver.py` 是装配根，允许深 import 自己进程内的模块
（组合根模式）；测试代码不受 R5 约束。`contracts` 的公开面就是它的子模块
（`redpilot.contracts.paths` 等），因为内核模块本身即稳定接口，不再往下包一层
`__all__` 重导出。

### 3.4 模块公共 API 契约

```python
# redpilot/obs/__init__.py
from .ingest import router as ingest_router
from .read import router as read_router
__all__ = ["ingest_router", "read_router"]

# redpilot/worker/__init__.py —— 只保留跨角色真正被用的名字
from .task import AgentTask
from .flags import extract_flags, is_valid_flag
__all__ = ["AgentTask", "extract_flags", "is_valid_flag", ...]
```

- 私有模块/文件统一 `_` 前缀（如 `worker/_sdk.py`），禁止跨模块 import `_` 名字。
- 没有 `__all__` 的模块视为「全部私有」，跨模块 import 即违例（强制作者表态）。
- 单模块内部随便深 import，规则只在模块边界生效。

### 3.5 决策记录：control/obs 是否收进 `platform/`

- **采用 A（平铺）**：`redpilot.control` / `redpilot.obs`。迁移路径最短（这两个 import
  路径在平台侧完全不变），红线由 R2/R3 测试显式表达，不依赖目录层级。
- 备选 B：`redpilot.platform.{control,obs,app}`。优点：R2 可写成「worker 不得 import
  `redpilot.platform` 前缀」一条规则，角色一目了然；缺点：多一次全量 rename，收益主要是命名学。
- 判定标准：若平台侧出现第三个模块（如 admin/internal API），升级到 B；当前选 A。

### 3.6 `localserver` 归属裁决

| | 现状 | 目标 |
|---|---|---|
| 位置 | `redpilot/obs/localserver.py` | `redpilot/worker/dashboard.py` |
| 依赖 | contracts | contracts |
| 被谁 import | worker（driver/roster） | worker 自己 |
| 与 obs 的关系 | worker 反向依赖 platform | 协议常量（路由/帧/snapshot kind）在 `contracts`，两个实现共同依赖 |

- 收益：R2 从「有一个例外」变成「零例外」；obs 可独立重构/删除而不影响 worker。
- 风险控制：迁移前先补一条**协议对比测试**（同一输入下 dashboard 与 `obs.read` 的
  JSON 键集/SSE 帧形状一致）—— 这条测试本来就该存在（`localserver` docstring 声称
  「字节兼容由测试强制」），现在是补票的时机。

### 3.7 `tsecbench/` 与 `fastapi-console/`：不是产品模块

- `tsecbench/` 模拟的是**外部平台**，是测试替身，归测试支撑（`tests/support/` 或保持根目录），
  不进 `redpilot` 包、不进 wheel、不进镜像。
- `fastapi-console/` 是只读运维工具，当前 import `redpilot_worker.adapter.*` 与 `tsecbench`。
  迁移时只改 import 前缀；不要顺手并进 `app.py`。若长期维护，再单独立项为
  `redpilot.tools.console`（可选 extra）。
- 判据：`redpilot` 包 = 产品运行时；其他顶层目录 = 开发/运维外围。

---

## 4. 边界执行：用测试替代 pip 依赖图

### 4.1 规则 → 测试矩阵

| 规则 | 测试 | 断言方式 |
|---|---|---|
| R1 contracts stdlib-only | `tests/architecture/test_layers.py` | AST；白名单 `sys.stdlib_module_names` |
| R2 worker ↛ platform | 同上 | AST；禁边 `redpilot.worker* → redpilot.{control,obs,app}` |
| R3 control ↮ obs | 同上 | AST；两条禁边 |
| R4 app.py 无领域逻辑 | 同上 | AST；`app.py` 只允许 import 名单（fastapi/contextlib/asyncio/logging/两模块 façade） |
| R5 公共 API | `tests/architecture/test_public_api.py` | 跨模块 `ImportFrom.name ∈ 目标 __all__`；`_` 前缀模块/名字拒绝 |
| R6 模块内单向 | `tests/architecture/test_layers.py` | AST；适配器禁 import `orchestrator/driver/supervisor/relay` |
| R7 配置收编 | 同上 | AST；`os.getenv`/`os.environ` 只允许出现在 `settings.py`/`config.py`（存量债入 allowlist，只许减） |
| R8 数据所有权 | `tests/architecture/test_data_ownership.py` | AST；`worker` 不得 import `sqlite3`；`obs`/`control` 各自 SQL 只在自己模块内（现有 store 单写者模式可测） |
| R2 运行时足迹（强验证） | `tests/architecture/test_runtime_footprint.py` | 子进程 import `redpilot.worker.orchestrator`，断言 `sys.modules` 无 fastapi/pydantic/uvicorn |
| 单一来源（Phase 5） | `tests/architecture/test_single_source.py` | AgentTask / build_task_prompt / extract_flags 全仓唯一定义；`__version__` 只在 `redpilot/__init__.py` |

### 4.2 实现要点

- **纯 stdlib AST**，复用 `test_purity.py` 的范式（导入扫描、白名单、逐文件断言）。
- 相对 import 必须解析：先按包的 `__init__` 链还原绝对模块名，再建图（附录 B 给骨架）。
- `TYPE_CHECKING` 块内的 import 也算静态依赖（它表达的仍是作者意图的方向）。
- allowlist 只给 R7 存量债，并配一条「allowlist 只许缩短」的自测（防止新债顺手进白名单）。
- 架构测试放 `tests/architecture/`，不依赖 fastapi/SDK，两套运行环境都能跑。

### 4.3 为什么不用 import-linter

1. 新增 dev 依赖 vs 仓库已有且正在用的 AST 测试范式（`test_purity.py`）。
2. 需要在 Kali 的 apt-python 与 platform 的 python:3.13 两套环境都可用。
3. 私有模块、`__all__` 白名单、配置收编、运行时足迹都超出 import-linter 的层次图能力，
   最终仍要写测试；一个机制好过两个。

### 4.4 与现有测试的关系

- `test_purity.py` 的 contracts 部分**泛化**进 `test_layers.py` 的 R1，行为测试
  （心跳路径对齐 compose、snapshot key、redact）原样保留。
- 所有新规则都是静态可判定的，不引入 flaky；运行成本在毫秒级。

---

## 5. 打包与运行时工程细节（迁移最容易断的地方）

### 5.1 根 `pyproject.toml`

```toml
[project]
name = "redpilot"
dynamic = ["version"]                       # 单源：redpilot/__init__.py
requires-python = ">=3.10"
dependencies = ["httpx>=0.27,<1"]           # 两角色都用（control outbox / worker relay）

[project.optional-dependencies]
worker   = ["tsec-benchmark", "requests>=2.31,<3", "openai>=1.30,<2"]
platform = ["fastapi", "uvicorn", "pydantic", "python-dotenv"]
dev      = ["pytest>=8,<10"]

[project.scripts]
redpilot-worker = "redpilot.worker.driver:main"

[tool.setuptools.dynamic]
version = {attr = "redpilot.__version__"}
```

两处修正顺带落进去：
- `requests` / `openai` 是 worker 代码**直接 import** 的（`adapter/platform/tsecbench_http.py:22`、
  `adapter/llm.py:44`），必须在 extra 里直接声明，不能赌 `tsec-benchmark` 的传递依赖。
- `tsecbench/` 用 platform extra 的 fastapi 即可，不需要独立发行版。

### 5.2 镜像

```dockerfile
# platform（Dockerfile.redpilot）
COPY pyproject.toml /opt/redpilot/pyproject.toml
COPY redpilot /opt/redpilot/redpilot
RUN pip install --no-cache-dir "/opt/redpilot[platform]"
CMD ["sh", "-c", "exec uvicorn redpilot.app:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}"]

# worker（Dockerfile，Kali）
COPY pyproject.toml /opt/redpilot/pyproject.toml
COPY redpilot /opt/redpilot/redpilot
RUN pip3 install --break-system-packages --no-build-isolation --no-cache-dir \
        -e "/opt/redpilot[worker]"
```

把「瘦身是注释」变成**构建期断言**（selfcheck 段追加）：

```sh
python3 -c "import sys, redpilot.worker.orchestrator; \
            assert 'fastapi' not in sys.modules, 'fastapi leaked into worker image'"
```

### 5.3 compose 热补丁卷

```yaml
# 原三行 packages/* 挂载收敛为：
- ./redpilot:/opt/redpilot/redpilot:ro
- ./pyproject.toml:/opt/redpilot/pyproject.toml:ro   # 元数据变更也能热更
```

（editable 安装锚在 `/opt/redpilot`，挂载源目录即热补丁，语义与现状一致。）

### 5.4 entrypoint / main.py / pytest.ini

- `entrypoint.sh:142` → `exec python3 -m redpilot.worker.driver`
- `entrypoint.sh` 里对 `pi_ext`/`pi_agents` 的路径引用 → `redpilot/worker/adapter/...`
- `main.py` → `from redpilot.app import app`（路径不变，`app` 仍在 `redpilot` 顶层）
- `pytest.ini` → `testpaths = tests`；`pythonpath = .`

### 5.5 测试树

- `packages/contracts/tests` → `tests/contracts/`；
  `packages/redpilot/tests` → `tests/{control,obs,app}/`；
  `packages/worker/tests` → `tests/worker/`。
- 根 `tests/` 的 175 条竞技场回归原地保留（它们已 import `redpilot_worker`，只改前缀）。
- `conftest.py` 的相对导入改为按包路径的绝对导入；`from conftest import ...` 在
  同目录下继续可用。

---

## 6. 迁移计划

**纪律**：搬移与行为变更分开提交；每步的闸门是同一条命令 —— `python3 -m pytest -q`
全绿（当前基线 387 passed + 8 subtests，28s，成本允许每步全量跑）。
分支 `refactor/modular-monolith`；当前工作区有未提交改动（skills 删除等），先隔离再动。

### Phase 0：基线冻结

```bash
git status --short          # 确认并隔离既有未提交改动
python3 -m pytest -q        # 387 passed
git tag modular-monolith-base
```

### Phase 1：contracts 归位（纯机械）

```bash
mkdir redpilot
git mv packages/contracts/redpilot_contracts redpilot/contracts
grep -rl 'redpilot_contracts' --include='*.py' . | xargs sed -i 's/redpilot_contracts/redpilot.contracts/g'
# pytest.ini: pythonpath 加 .、删 packages/contracts
python3 -m pytest -q
```

陷阱：`from redpilot_contracts import X`（无子模块）与 `redpilot_contracts.paths` 两种写法
都要覆盖；`sed` 后 `grep -rn 'redpilot_contracts'` 必须归零。

### Phase 2：worker 归位 + localserver 搬移

```bash
git mv packages/worker/redpilot_worker redpilot/worker
# 先补协议对比测试（dashboard vs obs.read），再搬：
git mv packages/redpilot/redpilot/obs/localserver.py redpilot/worker/dashboard.py
grep -rl 'redpilot_worker' --include='*.py' . | xargs sed -i 's/redpilot_worker/redpilot.worker/g'
grep -rl 'redpilot\.obs\.localserver' --include='*.py' . | xargs sed -i 's/redpilot\.obs\.localserver/redpilot.worker.dashboard/g'
python3 -m pytest -q
```

验收加一条：`grep -rn 'redpilot\.obs' redpilot/worker` 为空（R2 零例外）。

### Phase 3：platform 归位

```bash
git mv packages/redpilot/redpilot/control redpilot/control
git mv packages/redpilot/redpilot/obs redpilot/obs
git mv packages/redpilot/redpilot/app.py redpilot/app.py
# 合并两个 __init__.py：版本单源 + 边界 docstring
python3 -m pytest -q
```

`redpilot.control.*` / `redpilot.obs.*` 路径未变，平台内部零 sed；只有包 `__init__` 与
root `tests/` 需要检查。

### Phase 4：单 pyproject + 构建/部署切换

```bash
git rm packages/contracts/pyproject.toml packages/redpilot/pyproject.toml packages/worker/pyproject.toml
# 新建根 pyproject.toml（§5.1）；更新 Dockerfile / Dockerfile.redpilot /
# docker-compose.yaml / entrypoint.sh / pytest.ini（§5.2–5.4）
python3 -m pytest -q
python3 -c "import redpilot.worker.orchestrator, sys; assert 'fastapi' not in sys.modules"
```

> 打包切换必须与 Phase 1–3 在**同一分支收口**：中间态的 pyproject 会指向已移动的目录。
> Docker 构建在本机（Termux）无法验证，需在目标主机或 CI 跑 `docker compose build` +
> `docker compose config` 作为出门条。

### Phase 5：去重（行为变更，独立提交）

- 以 `adapter/task.py` / `adapter/taskprompt.py` 为准，删除 root 重复件
  （`worker/task.py`、`worker/taskprompt.py`、`worker/flags.py`）；
- `worker/__init__.py` 改为 façade：AgentTask / build_task_prompt / extract_flags
  各指向唯一实现；单测补一条「同名定义全仓唯一」的守卫；
- ⚠️ **两个 `SolveResult` 是刻意的，不合并**：`redpilot.worker.solver.base` 那份带
  `provider_failure`（0-turn+报错=引擎没跑起来），`adapter.solver.base` 那份是引擎返回形状；
  `solver/base.py` 的 docstring 记录了合并会丢哪一半语义（2026-09-08 静默烧库事故）。
- 闸门：全绿 + `tests/architecture/test_single_source.py`。

### Phase 6：测试树 + 文档收尾

- 按 §5.5 迁移 `packages/*/tests` → `tests/`，删除 `packages/` 空壳；
- `packages/worker/README.md` → `docs/worker.md`（或随包保留），根 README 架构段改述「四模块」，
  doc map 加本文件；
- 验收：`find . -name pyproject.toml` 只有一个；旧包名的 **import** 归零：
  `! grep -rnE '(from|import) +redpilot_(worker|contracts)' --include='*.py' .`
  （历史注释里保留旧名不算数）。

### 回滚

- 每 Phase 一个 commit；`git reset --hard pre-phase` / `git revert <sha>` 即可。
- 搬移用 `git mv`，`git log --follow` 保留历史；无 shim，回滚不需要清理残留名字。

---

## 7. 最佳实践（模块化单体自身）

1. **一个发行版、一个版本号、一个构建入口**。多 wheel 只在有独立发布节奏时才成立；
   本仓库三个包永远同版本发布，拆分是净成本。
2. **模块 = 边界上下文，不是代码分类**。不要 `utils/`、`common/`、`helpers/`
   垃圾桶；共享代码先放共享内核，放不下说明边界划错了。
3. **公共 API 必须显式**：`__all__` + façade；跨模块禁止 deep import。
   没有表态的模块默认全私有。
4. **依赖方向单向、无环**；依赖倒置只为真实存在的可替换点引入（平台适配器、求解引擎），
   不为「解环」发明抽象。
5. **装配是唯一允许认识具体实现的地方**（composition root）。领域对象不 new 基础设施，
   基础设施通过构造注入 —— 本仓库的 `localserver(live, bus, poller)` 与
   `create_control_app(tasks=, provisioner=)` 都是现成范式。
6. **数据所有权单一**：一张表一个写模块；跨模块读走 API/事件，不直查对方表。
7. **配置在边界解析一次**：`Settings.from_env()` 进，显式参数出；领域代码 `os.getenv` 归零。
8. **错误在边界翻译**：模块内部异常类型不外泄，边界翻成 API 错误/退出码。
9. **事件用于通知，调用用于查询**：不要把内部调用改成事件来伪装解耦 ——
   那样只会把编译期错误变成运行期静默。
10. **架构规则必须可执行**（测试/lint），否则等于注释。本仓库已经证明：写在 docstring
    里的「obs 不得 import control」三年都不会被遵守；写在测试里的当天就会被遵守。
11. **模块可整体删除**：判据是删掉一个模块时不需要改另一个模块的内部实现。
12. **包布局跟随进程角色，不跟随协议归属**：`localserver` 放 worker 而非 obs，
    就是第 12 条的实例。

---

## 8. 反模式清单（本仓库真实案例，迁移后由测试防复发）

| 反模式 | 案例 | 防线 |
|---|---|---|
| 第二套编排并存 | 框架自家 `orchestration.solve_one` 与竞技场长期并存（已删） | 单一装配根 + 入口测试 |
| 双状态写者 | `status/*.json` 与 `.live/*.json` 曾各自更新（已由 StatusBridge 单向同步） | R8 数据所有权测试 |
| 绕过抽象直连内部 | `driver` 曾直接 import `adapter/solver/pi_agent` 的 `compress_transcript`（已归拢） | R6 适配器禁反向 import |
| 同一领域对象两份定义 | root vs adapter 的 `AgentTask`/`taskprompt`/`flags`（Phase 5 已清偿；`SolveResult` 两份是刻意保留） | 同名定义唯一性测试 |
| 反向依赖 | worker → `obs.localserver` | R2 禁边 + 运行时足迹测试 |
| 配置来源分裂 | `settings.py` 收编后 `orchestrator.py` 仍有 10 处 getenv | R7 + 存量 allowlist 只减不增 |
| 注释当契约 | `redpilot/__init__.py` 的三条边界规则无执行点 | §4 全部规则进测试 |

---

## 9. 风险与取舍

| 风险 | 影响 | 缓解 |
|---|---|---|
| editable 路径漂移 | 镜像内 import 到旧副本或空目录（静默） | 单一 `EDITABLE_ROOT=/opt/redpilot` 约定 + Dockerfile selfcheck 断言 |
| worker 镜像意外带 fastapi | 违背 Kali 瘦身既定决策 | extras 隔离 + 运行时足迹测试 + 构建期断言（双保险） |
| AST 测试误报/漏报 | 红线形同虚设 | 规则测试自身进 CI；相对 import 解析有单测；allowlist 只减不增 |
| 大 diff 难 review | 搬移掩盖行为变更 | 机械搬移与行为变更分 commit；`git mv` 保历史；每步 387 全绿 |
| Docker/CI 无法本机验证 | Phase 4 出门条缺口 | 目标主机/CI 跑 build + compose config；镜像 selfcheck 兜底 |
| 单一的 pyproject 膨胀 | extras 组合爆炸 | 只保留 `worker`/`platform`/`dev` 三个 extra，禁止细粒度拆分 |
| 合并后误把 HTTP 契约改成函数调用 | 破坏部署边界（worker 可独立扩缩、跨主机） | 在架构测试中禁 `worker → obs.ingest` 的 import：relay 必须走 HTTP |

**明确不做**：微服务化、消息中间件、命名空间包（namespace packages）、
把 `web/`/`skills/`/`tsecbench/` 打进 wheel、引入 import-linter（可换，非必须）。

---

## 10. 验收标准

```bash
# 1. 全量测试（迁移后为 387 行为回归 + 17 架构）
python3 -m pytest -q                          # 期望 404 passed + 8 subtests

# 2. 单发行版、单构建清单
find . -name pyproject.toml -not -path './.venv/*'   # 只剩根 pyproject.toml
python3 -c "import redpilot; print(redpilot.__version__)"

# 3. 边界执行点存在且全绿
python3 -m pytest -q tests/architecture

# 4. worker 瘦身（import 足迹）
python3 -c "import sys, redpilot.worker.orchestrator; assert 'fastapi' not in sys.modules"
python3 -c "import sys, redpilot.worker.driver"   # 需 worker extra（tsec-benchmark）

# 5. 旧包名的 import 归零（历史注释里的旧名不算）
! grep -rnE '(from|import) +redpilot_(worker|contracts)' --include='*.py' .

# 6. 部署面（目标主机/CI）
docker compose config
docker compose build
```

评审通过判据：新增一个模块时，**不需要**修改 Dockerfile / compose / pytest.ini；
删除一个模块时，**不需要**修改另一个模块的内部实现。

---

## 11. 实施记录（2026-09-17）

本设计已在 `main` 工作区实施完成，结果如下：

| 阶段 | 状态 | 结果 |
|---|---|---|
| P1 contracts 归位 | ✅ | `redpilot/contracts`；`redpilot_contracts` 全仓归零 |
| P2 worker 归位 + dashboard | ✅ | `redpilot/worker`；`localserver.py` → `worker/dashboard.py`（web 回退路径同步修正）；worker 不再 import platform |
| P3 platform 归位 | ✅ | `redpilot/{control,obs,app.py}`；`redpilot.control/obs` 导入路径未变 |
| P4 单 pyproject + 部署切换 | ✅ | 根 `pyproject.toml`（`worker`/`platform`/`dev` extras）；Dockerfile/Dockerfile.redpilot/compose/entrypoint/pytest.ini/.dockerignore/.gitignore 同步 |
| P5 去重 | ✅ | 删除 `worker/{task,taskprompt,flags}.py`；façade 指向 adapter 唯一实现；`SolveResult` 两份刻意保留 |
| P6 测试树 + 文档 | ✅ | `packages/*/tests` → `tests/{contracts,control,obs,worker,app}`；`packages/` 目录删除；`docs/worker.md`；README 改述四模块 |
| 边界执行点 | ✅ | `tests/architecture/` 17 条：R1–R8 + 运行时 import 足迹 + 单一来源 |

验证（本机实测）：

```
python3 -m pytest -q          → 404 passed (387 行为回归 + 17 架构)
python3 -m pytest -q tests/architecture → 17 passed
find . -name pyproject.toml   → 只剩根 pyproject.toml
```

遗留（有测试兜底的存量债）：

- R7 env 收编未完成：12 个文件仍在直接读 env（`tests/architecture/test_layers.py::ENV_DEBT`，
  只减不增；修好一个删一个，stale 条目会让测试变红）。
- Docker 构建在本机（Termux）无法验证，需在目标主机/CI 跑 `docker compose config/build`；
  镜像 selfcheck 已加入 worker import 足迹断言。
- `fastapi-console/` 的 import 已跟改 `redpilot.worker.*`，但它仍是外围工具，未并入产品包。

---

## 附录 A：红线矩阵（测试实现速查）

| 源 → 目标 | contracts | control | obs | app | worker | stdlib/三方 |
|---|---|---|---|---|---|---|
| contracts | ✅ | ❌ | ❌ | ❌ | ❌ | 仅 stdlib |
| control | ✅ | ✅ | ❌ | ❌ | ❌ | fastapi/pydantic/dotenv/httpx |
| obs | ✅ | ❌ | ✅ | ❌ | ❌ | fastapi/pydantic/dotenv |
| app | ✅ | ✅（装配） | ✅（装配） | — | ❌ | fastapi |
| worker | ✅ | ❌ | ❌ | ❌ | ✅ | httpx/tsec-benchmark/requests/openai |

`worker` 额外禁止：`sqlite3`、`fastapi`、`pydantic`、`uvicorn`（import 闭包内）。

## 附录 B：AST 测试骨架

```python
# tests/architecture/test_layers.py
"""模块边界执行点：R1–R7。纯 stdlib AST，不 import 被测代码。"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "redpilot"
PKG = "redpilot"
STDLIB = set(sys.stdlib_module_names)

# 禁边：源模块前缀 -> 目标模块前缀（仓库内规范名，已去掉 redpilot. 前缀）
FORBIDDEN = (
    ("worker", "control"), ("worker", "obs"), ("worker", "app"),
    ("control", "obs"), ("obs", "control"),
    ("control", "worker"), ("obs", "worker"), ("contracts", "control"),
)
WORKER_LOOP = ("orchestrator", "driver", "supervisor", "relay")
ENV_ALLOWLIST = {"worker/settings.py", "control/config.py", "obs/config.py"}
ENV_DEBT = {f"worker/{m}" for m in (
    "orchestrator.py", "relay.py", "roster.py", "config.py", "supervisor.py",
)}  # 存量债：只许缩短


def _module_name(path: Path) -> tuple[str, bool]:
    """返回（仓库内规范模块名, 是否包 __init__）。"""
    rel = path.relative_to(ROOT)
    if rel.name == "__init__.py":
        return ".".join(rel.parent.parts), True
    return ".".join(rel.with_suffix("").parts), False


def _canon(dotted: str) -> str:
    """redpilot.a.b -> a.b；非 redpilot 前缀原样保留。"""
    return dotted[len(PKG) + 1:] if dotted.startswith(PKG + ".") else dotted


def _resolve_relative(parts: list[str], is_pkg: bool, node: ast.ImportFrom) -> str:
    """相对 import -> 仓库内规范模块名。level 在包 __init__ 与普通模块中语义不同。"""
    if not node.level:
        return _canon(node.module or "")
    drop = node.level - 1 if is_pkg else node.level
    base = parts[: len(parts) - drop] if drop else parts
    return ".".join(base + ([node.module] if node.module else []))


def _module_exists(dotted: str) -> bool:
    p = ROOT.joinpath(*dotted.split("."))
    return p.with_suffix(".py").is_file() or (p / "__init__.py").is_file()


def _iter_files():
    for p in ROOT.rglob("*.py"):
        if "__pycache__" not in p.parts:
            yield p


def _imports(path: Path):
    mod, is_pkg = _module_name(path)
    parts = mod.split(".")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield mod, _canon(a.name)
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_relative(parts, is_pkg, node)
            if not target:
                continue
            yield mod, target
            # `from pkg import submodule`：按文件系统补全为子模块名
            for a in node.names:
                sub = f"{target}.{a.name}"
                if _module_exists(sub):
                    yield mod, sub


def test_forbidden_edges_absent():
    violations = []
    for path in _iter_files():
        for mod, target in _imports(path):
            for src, dst in FORBIDDEN:
                if (mod == src or mod.startswith(src + ".")) and \
                   (target == dst or target.startswith(dst + ".")):
                    violations.append(f"{path.relative_to(ROOT.parent)}: {src} -> {dst} ({target})")
    assert not violations, "forbidden imports:\n" + "\n".join(violations)


def test_adapters_do_not_import_loop():
    violations = []
    for path in _iter_files():
        if "/adapter/" not in str(path).replace("\\", "/"):
            continue
        for mod, target in _imports(path):
            tail = target.rsplit(".", 1)[-1]
            if tail in WORKER_LOOP:
                violations.append(f"{path.relative_to(ROOT.parent)}: adapter imports {target}")
    assert not violations, "adapter -> orchestration imports:\n" + "\n".join(violations)


def test_contracts_is_stdlib_only():
    for path in (ROOT / "contracts").rglob("*.py"):
        for mod, target in _imports(path):
            root = target.split(".")[0]
            assert root in STDLIB or target.startswith("contracts"), \
                f"{path}: non-stdlib import {target!r}"


def test_env_reads_are_collected():
    for path in _iter_files():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel in ENV_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"getenv", "environ"}:
                assert rel in ENV_DEBT, f"{path}: env read outside settings (R7)"
```

```python
# tests/architecture/test_public_api.py（要点）
# 1) 跨模块 import：`from redpilot.<mod> import <name>`（非装配根）时，
#    <name> 必须出现在目标模块 __all__（或 __init__ 顶层定义）。
# 2) 目标模块文件/名字以 "_" 开头 -> 直接违例。
# 3) 装配根例外：redpilot/app.py、redpilot/worker/driver.py。
# 4) contracts 的子模块 import 豁免（内核模块即公共 API）。

# tests/architecture/test_runtime_footprint.py（要点）
# subprocess.run([sys.executable, "-c",
#   "import sys, redpilot.worker.orchestrator;"
#   "assert not {'fastapi','pydantic','uvicorn'} & set(sys.modules)"])
# 用 import 实际发生的足迹，补静态 AST 的漏报（动态 import）。
```
