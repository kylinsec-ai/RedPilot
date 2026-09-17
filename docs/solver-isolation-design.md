# 求解面隔离与预算执行设计（Solver Isolation & Budget Enforcement）

> **本文定位**：补 [`docs/architecture.md`](architecture.md) §13 的缺口 **D3（求解 Agent 无沙箱）
> / D4（努力地板仍是 prompt 级）/ D5（work/ 清理策略）**，把总体设计里"锁定面靠纪律"的三处
> 隐含假设，改成有执行点的权限模型与预算闸门。
> **不重复**：总体架构、红线 R1–R8、竞技场主循环、flag 双闸门（均在 `docs/architecture.md`）；
> 证据图（D2）走 `docs/graph-engineering-design.md`；侦察事实编译（D6）走
> `docs/deterministic-recon-design.md`；RPC 能力实测见 `docs/pi-rpc-migration-research.md`。
> **状态**：**已实施（M1–M4 首批）**，本机回归 `pytest -q` → 436 passed / 2 skipped
> （跳过的是需 root + `solver` 用户的真降权用例）。修复文件与验证边界见 §14 实施记录；
> **未验证**：容器内真降权（file caps / ACL / `Popen(user=)` 的 P0/P1/P2/P3 探针）
> 需在镜像构建后跑 §3 清单。

---

## 0. 结论（TL;DR）

1. **现状一句话**：求解 Agent 以 **容器 root** 运行，与 harness 同 uid；预算状态、舰队锁、
   状态快照、题目 scratch 全在同一个可写 `/work` 里。v1 架构把"锁定面"定义为规则
   （grounding / 平台判据 / `tests/architecture`），但 enforcement 只是"进程已加载 + 部署纪律"。
2. **设计一句话**：**把纪律变成执行点** ——
   - D3 → **DAC 权限模型**（M1 降权 + M2 控制面状态搬家），
   - D4 → **SurfaceLedger 会话内预算阶梯**（软提示 → `steer` → `abort`+`follow_up`），
   - D5 → **终态题目的保留策略**（先 dry-run 报告，后真删）。
3. **三处暴露面不是理论风险**：预算状态 `.stoploss.json` 就住在 Agent 的 cwd 里
   （`stoploss.py:143`），一次 `rm -rf .` / `git clean -fdx` 就能删掉；同 uid 可读其他题目的
   `FLAG`；`/app` 的判分规则可写。**求解者不是恶意对手，但它是有 shell 的随机策略** ——
   事故是必然而非偶然。
4. **零新依赖、零新沙箱框架**：用 Python 3.9+ 的 `Popen(user=...)`、POSIX ACL、
   已实测的 RPC `steer/abort`（`docs/pi-rpc-migration-research.md` §5）与既有架构测试范式；
   不需要 userns / bwrap / gVisor，也不动共享 netns 拓扑。
5. **上片顺序**：M0 实测（4 条探针，半天）→ M1+M2 同批（权限与状态搬家，是一体的）→
   M3（会话内预算，独立）→ M4（保留策略，独立）。每片有开关、可单独回退。

---

## 1. 现状（有出处）

### 1.1 今天求解进程能碰什么

容器没有 `USER`、compose 没有 `user:`（`Dockerfile`、`docker-compose.yaml` 实测均无），
所以 driver 与 Pi 都是 root。权限面清点：

| 对象 | 今天的状态 | 求解进程 | 出处 |
|---|---|---|---|
| `/app`（判分/证据代码、`pi_ext` 护栏、技能面、`web/`） | root 属主，`chmod +x` 无降权 | **可写** | `Dockerfile` COPY + 无 USER |
| `/work/.stoploss-locks/`（舰队锁） | /work 卷内 | **可写** | `stoploss.py:146-155` |
| `/work/<code>/.stoploss.json`（预算状态） | **在题目的 cwd 里** | **可写/可删** | `stoploss.py:143-144` |
| `/work/<code>/.rejected_flags`、`.unverified_flags`（负记忆） | /work 卷内 | 可写 | `orchestrator.py:1017-1142` |
| `/work/<other-code>/`（别人的实录、候选 flag） | /work 卷内 | **可读** | 共享卷 + 同 uid |
| `/work/.live/`、`status/`、`*.epoch`、`.reload.wid*` | /work 卷内 | 可写 | `contracts/paths.py`、orchestrator |
| `/root/.pi/agent/auth.json`、skills | root 属主 | 可读 | `Dockerfile` COPY |
| docker socket / `NET_ADMIN` | **没有**（已根除） | 不可用 | `docker-compose.yaml`、v1 I9 |

已经做对的部分不动：逐题 `HOME` 隔离（`pi_agent.py:1005` 把 `HOME` 指到
`<code>/.pi-home`）、令牌式进程回收、无 docker socket、观测读/写 token 分离。

### 1.2 三个具体事故面

**A. 控制面状态与 Agent scratch 同居。** `StopLoss._state_path()` 返回
`<workdir>/<code>/.stoploss.json` —— 这正是 Pi 的 cwd。解题过程里所有"清理现场"的动作
（`rm -rf .`、`find . -delete`、`git clean -fdx`、写错重定向）都会连带删掉预算状态：
止损窗口清零、`zero_flag` 计数归零、`flag_hashes` 去重键丢失。`_lock_path()` 的 docstring
已经认真讨论过"driver 清目录 unlink flock 文件"的 inode 竞态并因此把锁移出题目目录 ——
但**同一类事故的另一半（求解者清目录）没有被覆盖**，因为两者同 uid。

**B. 跨题可读。** 同 uid 下，A 题的 Agent 可以 `cat /work/<B题>/FLAG` 或读别人的
`transcript.jsonl`。这会污染"幻觉族 / 推导族"分族记账的独立性（v1 §6.3）：一个候选
如果来自另一题的实录，它的出身既不是本场工具输出、也不是本场推导。
这条红线**题面已经写了**（`taskprompt.py` 的 `_ISOLATION_CONSTRAINT`：禁止读其他挑战目录、
`_events.jsonl`、`/app` 源码、环境变量），但今天它没有任何执行点 —— 题面是请求，不是边界。

**C. 锁定面可写。** v1 §2.1 把 grounding 规则、flag 阈值、`tests/architecture` 划为
locked；但求解进程对 `/app` 有写权限，"锁定"只靠"进程已加载"这一条 —— 改了磁盘上的
规则不影响本次会话，却影响下一次重启后的会话，且没有任何检测点。

### 1.3 可复用的现成原语（这是本设计便宜的原因）

| 原语 | 出处 | 用途 |
|---|---|---|
| `Popen(user=..., group=...)`（Python 3.9+） | 本仓库 3.13 | 只给 Pi 子进程降权，driver 不动 |
| 逐题 `HOME` 已隔离 | `pi_agent.py:1005` | 降权后天然兼容，无需新路径 |
| `--no-session`、`--tools` allowlist、`--skill` | `pi_agent.py:_build_cmd` | 工具面无状态、可按需收窄 |
| RPC `steer` / `abort` / `follow_up` 已实测 | `docs/pi-rpc-migration-research.md` §5 | 会话内预算的执行通道 |
| `on_event(kind, payload)` / `on_fact(tool,args,out)` | `pi_agent.py:1266-1331`、`orchestrator.py:5130` | SurfaceLedger 的输入 |
| `blackboard.observe()` 返回新增事实数 | `blackboard.py:222` | "有没有新事实"的确定性信号 |
| `bash_guard.js` 的 **harness 级动作约束先例**：`REPEAT_LIMIT` 拦重复命令、`tried_commands.md` 跨场记账、命令级短路返回警示 | `pi_ext/bash_guard.js` | 执行通道已经存在；M3 只是把"同一命令"扩到"同一攻击面" |
| `tests/architecture` AST/足迹范式 + `x-worker-*` compose 锚点 | `tests/architecture/`、`docker-compose.yaml` | 新边界的执行点与部署开关的落点 |

---

## 2. 目标与不变量

| # | 目标 / 不变量 | 执行点（本设计的验收） |
|---|---|---|
| S1 | 求解进程**不可写**锁定面（`/app`）与控制面状态（`.harness/`、锁、`status/`、`.live/`、`epoch`、`reload`） | 身份哨兵探针 + `tests/worker/test_solver_identity.py` |
| S2 | 求解进程**只读写自己题目的目录**；其他题目不可读 | 同上（读哨兵） |
| S3 | 预算裁决归 harness：Agent 决定"怎么解"，harness 决定"解多久"（在额度内） | M3 状态机单测 + 假 RPC 对端集成测 |
| S4 | 隔离失败**响亮可回退**：默认降级告警继续解题，`ADAPTER_ISOLATION_STRICT=1` 时 fail closed | 启动自检 + `isolation.degraded` 事件 |
| S5 | 能力降级**显式可查**：非 root 后哪些工具退化，必须可探测、可上报，不静默 | M0 探针 + 运行期能力清单事件 |
| S6 | 观测面（relay / dashboard / Heimdall）不受权限变更影响 | 回归：404 条 + 端到端本地靶场 |
| S7 | 预算状态与题目目录**解耦**（M2 前置 M1） | 搬迁后 `stoploss.py` 状态路径单测 |

---

## 3. M0：四个探针（先做完再动代码）

| # | 探针 | 怎么测 | 判据 | 失败则 |
|---|---|---|---|---|
| P1 | Docker 默认 bounding set 是否含 `NET_RAW`；file capabilities 对非 root 是否生效 | 容器内 `capsh --print`（或读 `/proc/self/status CapBnd`）+ `setcap cap_net_raw+ep <copy-of-nmap>` 后用非 root 跑 `-sS` | `-sS` 成功 | 降级为 `-sT`，并写进能力清单 |
| P2 | 挂载卷（`./work` bind / Docker volume）是否支持 POSIX ACL | `setfacl -m u:solver:rwx d && getfacl d`，再验证跨进程 | ACL 生效 | 改用 chown + 写点 `os.chown` 显式交接（§3.4 备选） |
| P3 | Kali 的 `pip` 是否 PEP 668 externally-managed；`pip install --user --break-system-packages` 对非 root 是否可行 | 非 root 身份实测 | 能建 venv 或 `--user` 安装 | 技能安装器降级为"仅 venv"并在题面提示 |
| P4 | `Popen(user=...)` 在容器内可用（目标 uid 存在、driver 为 root） | 最小 Python 脚本 | 子进程 euid=10001 | 改用 `preexec_fn` + `setgid/setuid` 兜底 |

**为什么先测**：P1/P2 决定 M1 用不用改镜像与 ACL 方案，P4 决定实现路径。测完把结论写回本文件
（对照 `docs/pi-rpc-migration-research.md` 的做法：先实测、再评估、再实施）。

---

## 4. M1：身份模型（driver=root，Pi=solver）

### 4.1 身份与交接

```
镜像新增系统用户 solver（固定 uid 10001，组 solver）      ← Dockerfile
driver / orchestrator / relay / dashboard / 看门狗        ← 保持 root（进程回收扫 /proc、netns 自愈都依赖）
Pi 子进程（含它启动的 bash、子 Agent、工具）              ← Popen(user="solver", group="solver", extra_groups=[])
```

实现落点只有一个：`pi_transport.py` 的 `PrintTransport.__init__` 与 `RpcTransport.__init__`
（`pi_transport.py:49-55`、`99-105`）把身份参数传进 `subprocess.Popen`，参数从 solver 配置读取 ——
**传输层是唯一 spawn pi 的地方**，所以这是单点改动，不散落。

交接协议（每次会话）：

```
driver（root）认领题目
  → _prepare_challenge_dir(code)：
      a) 不是 solver 属主的文件/目录 → os.chown(递归, 10001)            （一次，成本可控）
      b) setfacl -d -m u:solver:rwx <dir>（默认 ACL，让 driver 之后新建的文件
         对 solver 仍可写 —— 单靠 chown 不行：driver 写的 MEMORY/黑板是 root:root 0644）
      c) .pi-home chown + ACL
  → 启动 Pi（solver 身份）
  → 会话结束：不动目录（下次认领时重跑 a/b，天然幂等）
```

**为什么用默认 ACL 而不是"逐写点 chown"**：题目目录里 driver 与 Pi 都是写者
（`transcript.jsonl` 由 driver 打开追加 `pi_agent.py:1080`，`MEMORY.md` 两边都写，
`FLAG/flag.txt` 由 Pi 写、driver 摘除）。默认 ACL 一次设置覆盖所有未来文件，
不必在每个写点加 `os.chown`，漏一个就会产生"下次会话 solver 改不动自己笔记"的隐性故障。

### 4.2 权限矩阵

| 路径 | 属主:组 | mode / ACL | solver | driver |
|---|---|---|---|---|
| `/app`、`/opt/tools`、`/usr` | root:root | 0755 | 只读（**DAC 天然生效，无需挂载改动**） | 读写 |
| `/work/.harness/`（新，M2 的落点） | root:root | 0700 | 无 | 读写 |
| `/work/.stoploss-locks/` | root:root | 0700 | 无 | 读写 |
| `/work/.live/`、`/work/status/`、`*.epoch`、`.reload.wid*` | root:root | 0700（文件 0600） | 无 | 读写 |
| `/work/<当前题>/` | root:root | 2770 + 默认 ACL `u:solver:rwx` + `g::rwx` + `o::---` | 读写 | 读写 |
| `/work/<当前题>/.pi-home/` | solver:solver | 0700 | 读写 | 读写（root 绕过 DAC） |
| `/work/<其他题>/` | root:root | 0710 | **不可读**（Traverse 都不给） | 读写 |
| `/tmp` | root:root | 1777 | 读写（sticky） | 读写 |
| `/root/.pi/agent/`（auth.json、skills） | root:root | 0700 | **不可读** | 读写 |

> 说明：Docker 默认给容器 root `CAP_DAC_OVERRIDE`，driver（root）永远能绕过文件模式 ——
> 这正是 driver 能写 solver 目录、而 solver 反向写不了的原因。容器**不降** driver 的 caps，
> 否则 netns 看门狗与 `/proc` 回收会先坏。

### 4.3 凭据路径的显式交接（容易踩的坑）

entrypoint 的凭据解析顺序是"官方 env 名 → `~/.pi/agent/auth.json`"（`entrypoint.sh` 注释）。
降权后 `HOME=<code>/.pi-home`，`/root/.pi/agent/auth.json` **不可读**，所以：

- **首选**：继续用 env / `--api-key`（`_build_cmd` 已经显式传 `--api-key`）——今天主路径就是它，
  降权不影响；
- **兜底**：若部署只靠 `/root/.pi/agent/auth.json`，driver 在会话前把它复制到
  `<code>/.pi-home/.pi/agent/auth.json`（mode 0600，属主 solver），**并上报一次
  `isolation.credentials_bridged` 事件**；不复制则启动自检必须报错（凭据缺失是静默失败家族，
  见 `entrypoint.sh` 的告警史）。

### 4.4 能力降级清单（P1/P3 的结论决定实际内容，这里先立表）

| 能力 | 今天（root） | 降权后 | 处置 |
|---|---|---|---|
| `nmap -sS/-O`（raw socket） | 默认 bounding set 含 `NET_RAW`（待 P1 证实） | 需 file caps | `setcap cap_net_raw+ep /usr/bin/nmap`；不行则 `-sT` 并写进能力清单 |
| `ping`、`tcpdump` 等 | 同 uid 0 | 可能失效 | 同 P1 处置；能力清单里逐项标注 |
| `apt-get` / `dpkg` | 可用 | **不可用** | 策略上**不给 sudo**：系统包只走镜像预装，或运维显式 `ADAPTER_ISOLATION=0`（见 §6） |
| `pip install` | 受 PEP 668 限制（待 P3） | venv 内可用 | 技能安装器优先 venv；失败降级为 `/app/skills` 既有命中 |
| 端口绑定 <1024 | 可用 | 不可用 | 靶场场景不需要；`cap_net_bind_service` 不预置 |
| Chromium / 浏览器 | 需 `--no-sandbox` 才能稳 | **非 root 反而更稳** | 无动作（净收益） |
| 写 `/etc`、`/var` | 可用 | 不可用 | 走 `$HOME`（题目目录）或显式运维例外 |

### 4.4.1 降权后的错误面（tool-design）

非 root 会引入一类**新失败模式**：`apt-get`/`pip`/写 `/etc` 拿到 `EACCES`，而模型很可能
把它读成"目标问题"然后原样重试。按 tool-design 的纪律，错误必须可行动：

1. **能力清单进题面**：`taskprompt.build_task_prompt` 增加一节"身份与能力"（与
   `_OFFLINE_CONSTRAINT` 同位置、同风格）：可写=当前题目目录与 `$HOME`；不可写=`/app`、
   控制目录；不可用=`apt/dpkg`、`sudo`；`pip` 走 venv。让模型**动手前**就知道边界。
2. **常见 EACCES 的命令级翻译**：`pi_ext/bash_guard.js` 已是命令级注入点（已在拦自引用
   重定向与重复命令，可短路返回 `[PI-SAFETY-REPEAT]`）。在同一个 spawnHook 加一张
   "命令 → 可行动提示"小表（`apt-get install` → "本环境禁止安装系统包，用 venv/已装
   工具替代"），命中则不执行、直接把提示交给模型。
3. **不改 pi 本体**：边界信息经题面与 bash 护栏进入模型视野，工具契约不动。

### 4.5 失败模式与回退

| 情况 | 行为 |
|---|---|
| `solver` 用户不存在（旧镜像） | `isolation.degraded` + 醒日志；照常以 root 解题（S4：默认可用性优先） |
| `ADAPTER_ISOLATION_STRICT=1` 且任一前置失败 | 走 I8 语义 **exit 0** 明示配置错误（不闷循环） |
| ACL 工具缺失 / 文件系统不支持（P2 失败） | 回退 §3.4 备选：chown 目录 + 在所有 driver 写点后 `os.chown`（漏点由"下次会话 solver 不可写"的哨兵探针在启动时发现并告警） |
| 降权后 Pi 启动失败（权限导致） | 看门狗照旧；`isolation.degraded` 事件带 errno；**不自动回 root**（避免“悄悄失去隔离”这类最难查的事故） |

### 4.6 执行点

- `tests/worker/test_solver_identity.py`：Linux + root 时用临时目录树 + `Popen(user=solver)`
  跑三条哨兵（写 `/app` 模拟目录、写 `.harness`、读别人的题）——三条都必须 **EPERM/EACCES**，
  写自己的题必须成功；不满足条件（Termux / 非 root）`pytest.skip`。
- `tests/architecture/test_solver_spawn.py`（AST）：全仓 `Popen` 起 pi 的调用点只在
  `pi_transport.py`；且身份参数从配置读取（防止后续有人加第二条 spawn 路径绕过降权）。
- 运行期：driver 启动 + 每次会话前，以 solver 身份探测哨兵路径，结果进 obs（`isolation.probe`）。

---

## 5. M2：控制面状态搬家（M1 的前提）

### 5.1 搬家清单

| 状态 | 今天 | 搬到 | 兼容期 |
|---|---|---|---|
| StopLoss 预算状态 `.stoploss.json` | `<code>/.stoploss.json` | `/work/.harness/stoploss/<code>.json` | 启动时单向迁移：新路径缺失且旧路径存在 → 读取旧值写新路径，旧文件留作证据不删 |
| SurfaceLedger（M3 新增） | — | `/work/.harness/surface/<code>.json` | — |
| 舰队锁 `.stoploss-locks/` | /work 根 | 原地（只收 mode 0700） | — |

**不搬的**：题目目录里的 `MEMORY.md`、`_blackboard.json`、`transcript.jsonl`、`FLAG`、
`.rejected_flags`、`.unverified_flags` —— 它们是**题目记忆与提交入口**，本来就是 Agent 的
可写面（v1 §2.1 Editable/Append-only）。`_blackboard.json` 由 driver 写、Agent 读，
按 §4.2 的默认 ACL 两边都可写；这是刻意的（Agent 有理由纠正自己的笔记）。
要收紧的只有**裁决状态**：预算、锁、调度进度、快照。

### 5.2 迁移纪律

- 兼容读旧路径只保留一个发布周期；`tests/worker/test_stoploss_migration.py` 覆盖
  "旧路径存在 / 新路径存在 / 两者都在（新者胜）"三态。
- 搬迁后 `stoploss._state_path()` 返回 `.harness` 路径；`.harness` 的创建与 mode 收敛放在
  driver 启动阶段（`driver.py:80` 现在只 `makedirs(workdir)`，在此扩成一个
  `_prepare_control_dirs()`）。
- **顺序不能反**：M1 降权在前、状态还在题目目录 → solver 连自己目录都写不了 StopLoss 状态？
  不会 —— solver 写不了状态是"好事"，但 driver 仍在自己进程里写（root），所以功能不坏；
  真正会坏的是"一个周期后旧状态读不到"的兼容性。因此 M1 与 M2 必须同批发布，这也是 §0 结论 5。

### 5.3 执行点

- `tests/worker/test_control_state_paths.py`：状态/锁/快照路径全部落在 `.harness|.stoploss-locks|.live|status`，
  且**没有**落在任一题目目录内（防止回归）。
- `tests/architecture/test_data_ownership.py` 增一条：`.harness` 只由 StopLoss/AgentRuntime 写，
  编排层不直接写文件（延续 I10 的"一个目录一个写模块"）。

---

## 6. M3：会话内预算（SurfaceLedger）

### 6.1 为什么现有止损不够

`StopLoss` 的粒度全部是 **题目 × 轮次**：`dry_cutoff`（连续 3 场无新事实）、
`zero_flag_cutoff`（连续 3 场 0 flag）、`per_challenge_seconds`（终身时间预算）、
单场 `deadline`。而 `AGENTS.md` 里真正影响胜负的两条是 **动作级**的：

```
- 同一方向连续失败 3 次 → 立即换思路
- 爆破超过 5 分钟无结果 → 停止，换攻击面
```

harness 看不见"方向"和"攻击面"，所以这两条只能靠模型自觉 —— 这正是 v1 缺口 D4 的原文
（"必须被优化压力考验的约束要放进 harness"）。后果可量化：一场 480–2000s 的会话，
Agent 可以把 80% 时间烧在一个死方向上，harness 只在**场末**才知道"这场没新事实"。

### 6.2 数据模型（确定性、无 LLM）

```python
# adapter/surface.py（新模块；环境解析按 R7 放 adapter/config.py）
SurfaceKey = "sha1(target)[:12]:tactic"

def classify(tool: str, args: dict) -> tuple[str, str]:
    """(target, tactic)。纯规则表，表驱动单测。"""
# 规则示例（确定性，不调用模型）：
#   bash + nmap  →  target=host[/CIDR],        tactic=scan
#   bash + ffuf/hydra/sqlmap/nuclei → target=host:port 或 URL origin,  tactic=brute|exploit
#   bash + aws/s3cmd → target=bucket/endpoint, tactic=cloud_enum
#   bash + strings/gdb/ropper → target=文件路径, tactic=analysis
#   read/edit/write → target=cwd 内相对路径,    tactic=analysis
#   复合命令（&& ; |）→ 取第一个命中的已知工具（规则固定，可单测）
#   未知 → target=argv[0]，tactic=other（仍给预算，但额度乘子更高）

@dataclass
class SurfaceEntry:
    epoch: int             # 所属 task epoch（跨场续接只认同 epoch，与 .continuation.json 同纪律）
    first_wall: float
    last_fact_wall: float
    attempts: int          # 工具调用次数
    facts: int             # 该面产生的新事实数（blackboard.observe 返回值）
    steers: int            # 已发硬信号次数
    aborts: int            # 已强制中止次数
    rescued: bool = False  # 关闭后又被新事实救活（只用于测量误杀，不自动重开）
    closed_reason: str = ""  # "" | stall | absolute | ignored_steer
    verdict: str = ""      # "" | "closed"
```

**防换皮（指标博弈抵抗）**：`surface_key` 由命令**内容**推导（target + tactic），不是 argv
原样哈希。同一目标、同一战术意图下换个工具名或写法（`nmap -sS` → `masscan`、`curl` →
`python requests`）**不产生新 surface**：target 归一化（host:port），tactic 由工具类别表
映射；"同 target 且无新事实"的调用一律记到旧面。分类键变化时发 `surface.rekey` 事件留痕，
供复盘区分"真换面"与"换皮"。

状态上限：每 code 保留最近 64 个 surface（LRU），原子写、与 StopLoss 同一把题级锁。
"新事实"信号唯一来源是 `blackboard.observe()` 的新增计数（`blackboard.py:222`，
`orchestrator.py:5130` 已有 `_on_fact` 回填），不由 Agent 自述决定。

### 6.3 判定与动作阶梯（全部有出路，不制造新死锁）

| 级 | 条件（默认值，全部可配） | 动作 | 通道 |
|---|---|---|---|
| L0 软 | 某 surface `now - last_fact_wall ≥ 180s` 且 `attempts ≥ 5` | 在下一次 prompt 注入一句"该面停滞"；**不阻断** | 会话内：`steer`；会话外：下场 prompt |
| L1 硬 | 停滞 ≥ 300s（对齐 `AGENTS.md` 的 5 分钟）**或** 该面累计 ≥ 900s（绝对值上限） | `steer`："该攻击面已关闭，改用其他面" + 写入 `.continuation.json` 死路元数据 | RPC `steer` |
| L2 强制 | L1 后 120s 内同面调用 ≥ 3 次 | `abort` 本轮 turn → `follow_up` 再发一次换面指令（研究文档实测 abort 后进程可复用）；`aborts` 计数 | RPC `abort` + `follow_up` |
| L3 收场 | 单场 `aborts ≥ 2` | 结束本场会话，交给 StopLoss 本来的 `dry/zero_flag` 判据决定是否换题 | 现有 visit 收尾 |

六条护栏：
1. **只关面、不关题**：M3 不新增"停题"判据，停题权仍在 StopLoss（避免两套口径，I2 精神）。
2. **无 RPC 不假装**：print 回退传输（老版 pi）没有 `steer`，只做 L0 软提示 + 场末记账；
   是否启用 L1/L2 由 `rpc_available()`/传输能力决定，能力不足时降级**并上报**。
3. **内容零答案**：steer 文本与 `_multiflag_hint_prompt_note` 同纪律 —— 只谈面与预算，
   不含提示正文、不含候选 flag、不暗示答案（`orchestrator.py:785` 的先例）。
4. **防换皮**（harness-engineering：指标博弈抵抗）：见 §6.2 —— 换工具写法不算换面，
   判定看内容不看措辞，每次改键留 `surface.rekey` 事件。
5. **多样性转向**（long-horizon-prompting：结构化多样性）：L1/L2 的 steer 不能说
   "换个方向"（模型会回到最近的次优面），必须点名**尚未尝试的 surface 家族** —— 从
   ledger 的 tactic 集合与 `.continuation.json` 的已证死路取差集；差集为空时明说
   "没有未试家族，优先深挖已有证据"，不发明新方向。
6. **救活与校准**（advanced-evaluation：置信校准）：关闭后又有新事实 → 记
   `rescued=true` + `surface.rescue` 事件，**不自动重开**（关闭是预算决策，不是能力
   判断）；用 rescued 率校准阈值 —— 误杀率高就调大 `STALL_HARD`/`ABS_S`。

### 6.4 与现有机制的关系

```
StopLoss（题目×轮次：停题）
   ├── dry_cutoff / zero_flag / per_challenge_seconds / deadline        ← 不动
SurfaceLedger（题目×会话内：换面）
   └── 输入：on_event(tool_start) + on_fact(新增数) + 墙钟                 ← 新增
提交路径（grounding → 平台）                                               ← 不动
```

- 与 `_multiflag_hint_prompt_note` 不冲突：那个管"提示复核窗口"的会话间引导，这个管
  "一个面烧太久"的会话内强制；两者都是 steer 文本，按优先级拼接。
- 与 eager 提交不冲突：M3 从不阻止工具执行到一半的候选落盘；提交永远优先。

### 6.5 可观测性

新增 telemetry 事件（`obs` 侧只需透传；如需时间线展示，按 v1 §8.3 纪律同步
`contracts/vocabulary.py` 与前端 `types.ts`）：

| 事件 | 字段 | 用途 |
|---|---|---|
| `surface.rotate` | code / surface / tactic / attempts / facts / stalled_s / action(soft\|steer\|abort) | 观测 SPA 时间线上看见"为什么换面" |
| `surface.closed` | code / surface / reason(stall\|absolute\|ignored_steer) | 续接与复盘 |
| `surface.rekey` | code / old_key / new_key / reason | 换面判定留痕（防换皮） |
| `surface.rescue` | code / surface / closed_reason / seconds_to_fact | 关闭后新事实（校准误杀率） |

### 6.6 执行点

- `tests/worker/test_surface_classify.py`：表驱动（nmap/ffuf/hydra/sqlmap/aws/strings/read/复合/未知
  各 ≥2 例），保证确定性、无网络。
- `tests/worker/test_surface_budget.py`：状态机三态（L0/L1/L2）+ 界值（差 1 秒、差 1 次调用）；
  64 项 LRU；持久化往返。
- `tests/worker/test_surface_steer.py`：假 RPC 对端（复用现有 fake-pi 测试夹具）断言
  ①steer 帧在 L1 后发出且内容不含答案 ②忽略 steer 后出现 `abort`+`follow_up`
  ③print 传输只发 L0。
- 回归：跑既有 404 条，确认正常推进场景**零 steer 误报**（这比新功能更重要）。

---

## 7. M4：`/work` 保留策略（最小）

现状：obs 的 events 有保留天数与分批删除（v1 §7.2），`/work` 没有 —— 题目结束后
靶标产物、扫描输出、临时 venv 永久堆积，会先撑爆 bind 挂载的宿主盘。

设计（与 obs housekeeping 同风格，**先报告后删除**）：

```
题目进入平台终态（solved/done/expired）
  → driver 写 <code>/.closed（含终态与时间戳）
  → housekeeping（driver 内每小时，随机相位）：
       找出 .closed 且 age > ADAPTER_WORK_RETENTION_DAYS（默认 14）的题目目录
       → dry-run（默认）：事件 work.gc_report 列出将删条目与字节数
       → ADAPTER_WORK_GC=1 时才真删，且**白名单保留**：
           MEMORY.md / _blackboard.json / transcript.jsonl / .closed
       → 删除项一律先确认其 digest 已进 obs（<code> 在 .live/digests/ 里）
```

不做的：不删未终态题目、不删 `.harness`（预算状态的证据价值高于空间）、不引入新的
存储层。执行点：`tests/worker/test_work_gc.py`（dry-run 不删 / 真删只删白名单外 /
digest 缺失时跳过并在事件里标 `skipped_no_digest`）。

---

## 8. 配置面（遵守 R7：env 只在六处解析）

| 变量 | 缺省 | 归属解析文件 | 语义 |
|---|---|---|---|
| `ADAPTER_ISOLATION` | `1` | `worker/adapter/config.py` | 0=关闭降权（回 root），1=启用 |
| `ADAPTER_ISOLATION_STRICT` | `0` | 同上 | 1=前置失败即 exit 0（I8 语义） |
| `ADAPTER_SOLVER_USER` | `solver` | 同上 | 降权目标用户名（uid 由 pwd 解析） |
| `ADAPTER_SURFACE_BUDGET` | `1` | 同上 | M3 总开关 |
| `ADAPTER_SURFACE_STALL_SOFT` | `180` | 同上 | L0 阈值（秒） |
| `ADAPTER_SURFACE_STALL_HARD` | `300` | 同上 | L1 阈值（秒，对齐 AGENTS.md） |
| `ADAPTER_SURFACE_ABS_S` | `900` | 同上 | 单面绝对上限（秒） |
| `ADAPTER_SURFACE_IGNORE_S` | `120` | 同上 | L2 观察窗（秒） |
| `ADAPTER_WORK_RETENTION_DAYS` | `14` | 同上 | M4 年龄阈值 |
| `ADAPTER_WORK_GC` | `0` | 同上 | 1=真删（默认 dry-run） |

新变量全部落在 `adapter/config.py`（R7 的既有收集点），`.env.example` 同批补注释；
**不得**新增 env 直读文件（`tests/architecture/test_layers.py` 的 `ENV_DEBT` 只减不增）。

---

## 9. 迁移顺序、验收与回滚

| 片 | 前置 | 验收 | 回滚 |
|---|---|---|---|
| M0 探针 | — | §3 四表判据全有结论 | 无（只测） |
| M1+M2 | M0 | 三哨兵全拒；404 条回归绿；本地靶场（`tsecbench/`）端到端仍出 flag；技能装载数 >0；driver/看门狗/relay 无异常 | `ADAPTER_ISOLATION=0` 回 root（状态已搬走，无兼容问题） |
| M3 | M1+M2（预算状态必须不可被求解者改，否则闸门可被绕过） | 停止面状态机单测 + 假 RPC 集成测；正常场景零误报 | `ADAPTER_SURFACE_BUDGET=0` |
| M4 | — | dry-run 报告与白名单单测 | 不设 `ADAPTER_WORK_GC` 即只报告 |

**整体验收（端到端）**：本地靶场起 `tsecbench/` + 单体 worker，故意在某题里
`rm -rf .`，要求：①预算状态不丢（M2）②会话仍能继续（driver 的 ACL 让目录可重建）
③观测里能看到 `surface.*` 与 `isolation.probe` 事件。

---

## 10. 非目标与已知弱点

- **不做 userns / bwrap / gVisor**：需要额外容器权限，且与"复用 worker-1 netns"的拓扑冲突；
  隔离目标是**事故遏制与记录独立性**，不是对抗蓄意逃逸。
- **不防蓄意提权**：共享 netns、无 userns 的前提下，容器内隔离挡不住内核漏洞级逃逸；
  那是宿主/编排层（seccomp、rootless、专用节点）的职责，写在这里是为了不夸大本设计的强度。
- **不改网络面**：靶标可达性是 VPN 拓扑的定义，M1 不动。
- **不改判分**：flag 双闸门、幻觉/推导分族原样（v1 §6.3）。
- **事实质量不在本设计内**：`blackboard.observe` 是正则抽取，"事实"可被污染这件事
  归 D2/图工程；M3 只消费它，不修它。
- **`/root/.pi/agent/skills` 这份副本在降权后对 Pi 不再可读**：框架的 skill_loader 与
  `_install_skills` 都从 `/app/skills` 起算（`Dockerfile` 注释 + `contracts/paths.py`），
  预期无影响；但必须用"技能装载数 >0"的端到端验收兜底（S6）。
- **宿主侧副作用**：`/work` bind 挂载上会出现 uid 10001 的文件，宿主 `ls` 显示为未知用户；
  这是预期，写进运维须知（`docs/worker.md` 增一节即可）。

### 10.1 锁定面漂移（报警器，不是锁）

M1 挡住了写，但"运行中的路由规则是否等于被评审的那一份"仍无人校验。最小做法：driver
启动时把 `redpilot.__version__` 与锁定面关键文件（`adapter/verify.py`、
`contracts/platform.py`、`pi_ext/bash_guard.js`）的 sha256 折叠摘要随 `isolation.probe`
事件上报（stdlib `hashlib`，零新依赖）；差异由观测/复盘看，**不在运行时拦截** —— 拦截会
把"改配置"变成新的停机故障面。

### 10.2 事故响应（security-observability 六步的落地）

1. **暂停**：`ADAPTER_ISOLATION=0` 回退，或 `ADAPTER_ISOLATION_STRICT=1`（部署面动作，
   属 harness-engineering 的 human-controlled 面）。
2. **保全**：`isolation.*`、`surface.*` 事件与对应 run 的 transcript 已在 obs，无需额外操作。
3. **定位**：区分三类真因 —— 能力降级（P1/P3 清单）、目录权限配置错误、凭据桥接失败。
4. **修补**：改 Dockerfile / 配置 / `_prepare_challenge_dir`。
5. **回归**：把失败用例加进 `tests/worker/test_solver_identity.py`。
6. **渐开**：先开只读/软信号（M4 的 dry-run、M3 的 L0），再收紧。

---

## 11. 决策记录（ADR 摘要）

| 决策 | 选择 | 主要理由 |
|---|---|---|
| 隔离机制 | DAC 降权 + 目录所有权，而不是挂载沙箱 | 零新依赖、与共享 netns 兼容、driver 保持 root 才能做看门狗与进程回收 |
| 降权范围 | 只降 Pi 子进程 | 改动单点（传输层），harness 全部能力不变 |
| 状态搬家 | StopLoss 状态进 `.harness/`，题目记忆留在题目目录 | 裁决状态 ≠ 工作记忆；工作记忆本就该让 Agent 读写 |
| ACL vs 逐写点 chown | 默认 ACL 优先，chown 备选 | 多写者目录里漏一个 chown 就是隐性故障 |
| 预算粒度 | surface（面）而非题/场 | 对齐 `AGENTS.md` 的真实约束粒度；停题权仍归 StopLoss，避免两套口径 |
| 预算动作阶梯 | 软提示 → steer → abort+follow_up → 场末收场 | 每一级都有出路；无 RPC 时只降级不假装 |
| GC 默认 | dry-run | 与 obs housekeeping 的渐进纪律一致；删除不可逆 |
| 失败姿态 | 默认降级告警继续解题，strict 才 fail closed | 求解可用性优先；但绝不静默失去隔离 |
| 防指标博弈 | surface 按内容键 + `surface.rekey` 留痕 | 换工具写法不应等于换面 |
| 多样性转向 | steer 点名未试家族；差集为空则明说 | 模型会回到次优习惯面，"随便换个方向"等于没换 |
| 预算误杀 | `rescued` 记账 + 事件校准，不自动重开 | 关闭是预算决策，不是能力判断 |
| 降权后的错误面 | 能力清单进题面 + bash_guard 命令级翻译 | 非 root 是新失败模式，不能让模型对着 `EACCES` 空转 |
| 锁定面漂移 | 只上报版本与摘要，不在运行时拦截 | 拦截会把配置变更变成停机故障面 |

---

## 12. 与总体架构的关系（回填 v1 缺口）

| v1 缺口 | 本设计对应 | 状态 |
|---|---|---|
| D3 求解 Agent 无文件系统沙箱 | §4 M1 + §5 M2 | 设计完成，未实施 |
| D4 努力地板/爆破上限仍是 prompt 级 | §6 M3 | 设计完成，未实施 |
| D5 `work/` scratch 清理策略 | §7 M4 | 设计完成，未实施 |
| D1 R7 env 债 | §8 新变量全部落在既有收集点（不新增债） | 顺手部分缓解 |
| D2 黑板生命周期 / D6 确定性侦察 / D8 obs 放大 | 不在本文范围 | 另见各自设计 |

---

## 14. 实施记录（2026-09-17，首批）

| 片 | 落地文件 | 执行点 |
|---|---|---|
| M2 状态搬家 | `contracts/paths.py`（`HARNESS_DIR` 单源）、`adapter/stoploss.py`（新路径 + legacy 只读兼容 + `reset()`） | `tests/worker/test_solver_isolation.py`、`tests/worker/test_surface_ledger.py::test_ledger_path_is_control_plane` |
| M1 降权 | `adapter/isolation.py`（新）、`adapter/config.py`（`IsolationConfig`）、`pi_transport.py`（两个传输 `identity=`）、`pi_agent.py`（HOME 属主 + 身份透传）、`driver.py`（控制目录 + 探针 + strict）、`orchestrator.py`（会话前 ACL/chown + 题面能力清单）、`taskprompt.py`（`identity_note`）、`Dockerfile`（uid 10001 + acl/libcap2-bin + nmap/ping file caps） | 两条 Popen 参数的单元测试 + `test_solver_isolation.py` 权限用例（root 环境自动启用） |
| M3 面预算 | `adapter/surface.py`（新：分类表/账本/阶梯/持久化）、`adapter/config.py`（`SurfaceBudgetConfig`）、`orchestrator.py`（每场账本 + `_on_fact` 实时输入 + `_surface_control` + 事件）、`pi_agent.py`（`control` 拉取 + RPC 帧）、`taskprompt.py`（`surface_note`） | `tests/worker/test_surface_ledger.py`（13）+ `tests/worker/test_surface_control.py`（2，假 RPC pi 实测帧到达） |
| M4 保留 | `adapter/workgc.py`（新）、`adapter/config.py`（`WorkGcConfig`）、`orchestrator.py`（心跳 2h 报告 + `_closed` 标记） | `tests/worker/test_workgc.py`（7） |

**实施中修正设计的地方**：

1. **M2 引入的回归**：旧机制靠“清题目目录顺带删旧 `.stoploss.json`”实现 task epoch 作废；
   状态搬走后这条副作用消失，复用的公开 code 会带着旧终态直接 `dropped`。
   已在 preflight 显式调用 `StopLoss.reset()`（§5.2 的“顺序不能反”漏了这一条，回归测试是
   `test_scheduler_stoploss_backoff.py::test_new_task_epoch_reused_code_does_not_inherit_terminal_stoploss`）。
2. **实时事实信号**：场末 `new_facts` 带证据门且只在场末结算；M3 的实时输入改用**影子黑板**
   （`Blackboard()` 不落盘、不碰真实黑板、不带证据门）—— 比正式判据宽松（偏保守：宁可漏判停滞，
   不误杀推进中的面）。这是对 §6.2 “唯一来源是 `blackboard.observe`”的务实修正，
   代价与方向写在 `surface.py` 模块头。
3. **print 回退不消费动作**：`_drain_control` 要求 `transport.terminates_on_settled`（RPC）——
   无双向通道时动作既不送达也不记账，由测试锁住（§6.3-2 的“不假装”）。

**未验证 / 未做**：

- 容器内真降权（§3 四探针）未跑：本机（Termux 非 root、无 `solver` 用户）自动跳过，
  Docker 构建与运行需目标主机/CI；`setcap` 是否对非 root 生效、ACL 在 bind mount 上的行为
  以运行期 `isolation.probe` 事件为准。
- `bash_guard.js` 的命令级 EACCES 翻译（§4.4.1-2）未实施：题面能力清单已生效，护栏翻译留下一批。
- `.continuation.json` 未合并 `blocked_routes()`：面关闭先落自己的账本 + 题面注入，避免动
  受版本约束的 checkpoint schema。
- M4 只处理“有 digest 且超期”的题目；unsolved 终态题目不进候选（保守起步）。

---

## 13. 技能裁决（六个上下文工程技能 + agents-best-practices 参考）

> 与 v1 §2 同一方法：技能给应然判据，逐条对照本设计；能落到执行点的写进正文，
> 落不下的进 §10 / §12。目的不是把技能名贴在文档上，而是让它指出漏洞。

| 技能 | 裁决对象 | 结论 | 落点 / 本轮修正 |
|---|---|---|---|
| **harness-engineering** | 面分类、锁定面、指标博弈 | M1/M2 用 DAC 把 locked（`/app`、预算状态）与 editable（题目目录）分开，符合"锁定评测面"纪律；但**缺防指标博弈与漂移检测** | §6.2 防换皮 + `surface.rekey`；§10.1 漂移只报警；§10.2 事故响应 |
| **long-horizon-prompting** | 努力地板、停止条件、组合多样性 | M3 是"硬预算放 harness"（技能第 14 条）的直接实现；但转向 steer **不保证多样性**（模型会回到最近次优面），且 blocked-route 未复用 | §6.3 护栏 5：点名未试家族；死路写 `.continuation.json`（已有机制） |
| **filesystem-context** | `/work` 作为上下文溢出层 | M4 = "scratch 清理必须在会话/题目边界"（Gotcha 1）；M1/M2 = Pattern 3 的 per-agent 目录隔离 | §7、§4.2 |
| **memory-systems** | 生命周期与时间有效性 | "失效但不丢弃"：surface verdict 必须带 epoch 与关闭原因、只失效不删；跨场只认同 epoch | §6.2 `epoch`/`closed_reason` 字段 |
| **advanced-evaluation** | 确定性预检与置信校准 | M3 是确定性 evaluator（不引 LLM judge），符合"证据先于判分"；但缺**误杀测量与阈值校准**闭环 | §6.3 护栏 6 + `surface.rescue` |
| **tool-design** | 工具错误面 | 降权产生新的 `EACCES` 失败模式，原始错误对模型不可行动 | §4.4.1 能力清单 + bash_guard 翻译 |
| **agents-best-practices**（`tools-and-permissions` / `security-observability` 参考） | 权限矩阵、trace、事故响应 | 权限矩阵（§4.2）与 draft/commit（flag 双闸门已有）对齐；缺事故响应与漂移 trace | §10.1/§10.2 + `isolation.*`/`surface.*` 事件 |

### 13.1 裁决产生并已回填正文的修正

1. 防换皮：surface 键按内容推导，换工具写法不算换面（§6.2 / §6.3-4 / §6.5）。
2. 多样性转向：steer 点名未试 surface 家族，差集为空时明说（§6.3-5）。
3. 救活记账：`rescued` + `surface.rescue`，作为误杀率与阈值校准依据（§6.2 / §6.3-6 / §6.5）。
4. 记忆生命周期：`epoch` + `closed_reason`，失效不删除（§6.2）。
5. 降权后的可行动错误：题面能力清单 + bash_guard 命令翻译（§4.4.1）。
6. 事故响应与漂移 trace：六步响应、`isolation.probe` 带版本与锁定面摘要（§10.1/§10.2）。

### 13.2 明确不采纳（技能建议，但本场景不做）

| 建议 | 为什么不做 |
|---|---|
| 引入语义记忆/图数据库（memory-systems 的选型表） | 现有"文件 + epoch"已满足检索形状；v1 与图工程文档已定"先补三字段" |
| 把预算判据交给 LLM judge（advanced-evaluation 的 PoLL） | 预算必须确定性：它决定"停"，不能引入新模型偏差与新成本；LLM 判断留在 skeptic/推理层 |
| 全量收窄工具可见性（tool-design 的 tool visibility） | pi 已是技能式渐进披露（75 个技能名 + 描述），已满足"不全量暴露" |
| 再加一层独立监控 Agent（harness-engineering 的 monitoring agents） | Heimdall 已受限为只读观察者，再加一层是重复 |
