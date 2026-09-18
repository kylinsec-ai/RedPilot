# MEMORY — RedPilot 架构设计会话

## 2026-09-17 模块化单体实施（非多包）

- 任务：把三包 monorepo 实施为单发行版、四模块的模块化单体。
- 结果（工作区已实施，全部本机实测）：
  - `redpilot/{contracts,control,obs,worker,app.py}`；`packages/` 目录已删除；
    `redpilot_contracts` / `redpilot_worker` 全仓归零。
  - 根 `pyproject.toml`（base=httpx；extras: worker=tsec-benchmark/requests/openai，
    platform=fastapi/uvicorn/pydantic/python-dotenv，dev=pytest/pyflakes）；唯一 `__version__`。
  - `worker/dashboard.py`（原 obs/localserver）：worker 的 import 闭包不再触碰 platform。
  - 去重：删 `worker/{task,taskprompt,flags}.py`，façade 指向 adapter 唯一实现；
    两个 `SolveResult` 刻意保留（provider_failure 判据，见 solver/base.py 注释）。
    > 沿革（2026-09-18 死码清扫）：框架侧那份 `SolveResult` 已删 —— 它**零生产消费者**
    > （没有任何生产代码构造它，唯一 import 方是它自己的 3 条测试）。当时"刻意保留"
    > 的理由是判据语义，而那条判据的执行点实际在编排侧（`_is_api_fault` 与 stoploss），
    > 并不在这个类里。"两个都不合并"的决定因此只对**一半**成立：adapter 那份仍在用，
    > 框架这份从未接线。事故教训与"落点应在编排层"已写进 `solver/base.py` 的模块 docstring。
  - 部署面同步：Dockerfile / Dockerfile.redpilot / docker-compose / entrypoint.sh /
    .dockerignore / .gitignore / pytest.ini（testpaths=tests, pythonpath=.）。
  - `tests/architecture/` 17 条边界执行点：R1 contracts stdlib、R2/R3 禁边、
    R5 façade、R6 adapter↛编排、R7 env 收编（12 文件存量债 allowlist，只减不增）、
    R8 数据所有权、运行时 import 足迹（子进程）、单一来源。
  - 测试：`pytest -q` → **404 passed**（387 行为回归 + 17 架构），~32s。
- 遗留：R7 env 收编有 12 个文件存量债；Docker 构建需目标主机/CI 验证（本机 Termux 无法跑）。
- 设计文档：`docs/modular-monolith-design.md`（含 §11 实施记录）。

## 2026-09-17 总体架构设计（skills 驱动）

- 任务：设计 RedPilot 系统级总体架构；调用 6 个上下文工程技能做裁决。
- 产物：`docs/architecture.md`（新增：目标/不变量、运行时拓扑、四模块、求解域、
  平台域、数据与线格式、关键时序、退化矩阵、演进路线、ADR）；README 文档地图补指针。
- 技能映射（每条有执行点或标注缺口）：
  - harness-engineering：面分类 = 锁定（grounding 规则/平台判据/架构测试）/ 可写（逐题目录）/
    追加（ledger/transcript/events）/ 人控（部署/凭据）；紧反馈回路 = 平台 response。
  - multi-agent-patterns：舰队 = 文件协调 swarm（无中央主管）；题目内 = 单求解者 +
    隔离上下文的观察者（Heimdall 只读）与对抗校验者（skeptic 独立会话）；危险能力架构根除。
  - filesystem-context：/work = 上下文溢出层（transcript 落盘、MEMORY.md 计划持久化、
    黑板/Heimdall 文件通信、技能动态装载、日志可 grep）。
  - memory-systems：分层正确；“失效但不丢弃”做到 ledger/epoch，黑板仍只增不改。
  - advanced-evaluation：证据先于分数、确定性预检、面板投票（skeptic_votes）、
    rescue/veto 置信校准、agent_authored 防自我增强、幻觉/推导分族。
  - long-horizon-prompting：AGENTS.md = 题目简报（成功谓词/非计数结果/反停滞/返回条件）。
- 已记缺口（写入 `docs/architecture.md` §13，未修复）：D1 R7 env 债 12 文件；
  D2 黑板缺 origin/evidence/lifecycle；D3 求解 Agent 无文件系统沙箱（锁定面靠进程已加载
  与部署纪律，非权限模型）；D4 爆破 5 分钟上限是 prompt 级而非 harness 级；
  D5 work/ scratch 清理策略；D6 deterministic recon；D7 console 归并；D8 obs 写入放大。

## 2026-09-17 求解面隔离与预算执行设计（补 D3/D4/D5）

- 任务：继续架构设计 —— 把 v1 总体架构里"锁定面靠纪律"的三处缺口做成有执行点的设计。
- 侦察结论（本机实测/代码出处）：
  - 容器无 `USER`、compose 无 `user:` → driver 与 Pi 同为 root；`/app`（判分/护栏）可写。
  - `StopLoss._state_path()` = `<workdir>/<code>/.stoploss.json`（`stoploss.py:143`）——
    预算状态就住在 Pi 的 cwd 里，`rm -rf .`/`git clean -fdx` 即可删；`_lock_path` 的
    docstring 只防了"driver 清目录"那一半，同 uid 的求解者没防。
  - 语法：`pi_transport.py` 是唯一 spawn pi 的地方（`Popen` 两处），逐题 `HOME=<code>/.pi-home`
    已有（`pi_agent.py:1005`）；RPC `steer/abort/follow_up` 已实测可用（研究文档 §5）。
  - `pytest -q tests/architecture` → 17 passed（5.2s），红线仍绿。
- 产物：`docs/solver-isolation-design.md`（未实施，含 M0 四探针 / M1 降权 / M2 控制面状态
  搬家 / M3 SurfaceLedger 预算阶梯 / M4 work 保留；每片有开关与回滚；每项现状带出处）。
  README 文档地图与 `docs/architecture.md` §13 已回填指针；v1 非目标里"不做沙箱"一条已
  指向本设计（仍标未实施）。
- 关键取舍：DAC 降权 + 默认 ACL（多写者目录漏 chown 是隐性故障）；只降 Pi 子进程、
  driver 保持 root（看门狗/`/proc` 回收依赖）；停题权仍归 StopLoss，M3 只关"面"；
  无 RPC 时预算只做软提示、不假装；GC 默认 dry-run。
- 待办：执行 §3 四条探针（file caps 对非 root / ACL on volume / Kali PEP668 / Popen user=），
  结论回填后再动代码。

## 2026-09-17 调用六个上下文工程技能裁决（隔离与预算设计）

- 任务：对本轮设计（`docs/solver-isolation-design.md`）调用技能做应然 vs 实然裁决，
  与 v1 总体架构同方法（v1 用同一批技能判过总体架构）。
- 载入并裁决：harness-engineering / long-horizon-prompting / filesystem-context /
  memory-systems / advanced-evaluation / tool-design，外加 agents-best-practices 的
  tools-and-permissions 与 security-observability 两个参考。
- 裁决发现并已回填正文的 6 处修正（§13.1）：
  1) 防换皮：surface 键按命令内容（target+tactic）推导，换工具写法不算换面，
     改键发 `surface.rekey` 事件（harness-engineering：指标博弈抵抗）。
  2) 多样性转向：steer 必须点名未试 surface 家族（ledger tactic 集合 − 已证死路），
     差集为空则明说（long-horizon-prompting：结构化多样性）。
  3) 救活记账：`rescued` 字段 + `surface.rescue` 事件，用误杀率校准阈值，
     不自动重开（advanced-evaluation：置信校准）。
  4) 记忆生命周期：SurfaceEntry 带 `epoch` + `closed_reason`，失效不删除
     （memory-systems：invalidate but don't discard）。
  5) 降权后的可行动错误：能力清单进题面 + bash_guard.js 命令级 EACCES 翻译
     （tool-design：错误必须可行动）。抓手是现有 `[PI-SAFETY-REPEAT]` 短路机制。
  6) 事故响应六步 + 锁定面漂移只报警（版本号与关键文件 sha256 随 isolation.probe 上报）。
- 明确不采纳（§13.2）：图数据库/语义记忆、LLM judge 做预算、全量收窄工具可见性、
  再加独立监控 Agent —— 各有现存等价物或与确定性原则冲突。
- 关键新证据（写进 §1）：题面 `_ISOLATION_CONSTRAINT` 早就写了"禁止读别题/`/app`/环境变量"，
  但没有任何执行点；`bash_guard.js` 的 REPEAT_LIMIT 是"harness 级动作约束"的现成先例 ——
  M3 只是把"同一命令"扩到"同一攻击面"。
- 设计文档现 542 行，§13 为技能裁决、§13.1 修正、§13.2 不采纳。仍未实施。

## 2026-09-17 实施 M1–M4（隔离与预算执行首批）

- 任务：把 `docs/solver-isolation-design.md` 从设计落到代码；本机 `pytest -q`
  → **436 passed / 2 skipped**（基线 404；新增 32 条：surface 13、isolation 10、
  surface_control 2、workgc 7）。
- 落地：
  - M2：`StopLoss` 状态搬到 `<workdir>/.harness/stoploss/<code>.json`（legacy 只读
    兼容一个周期）；`contracts/paths.py` 增 `HARNESS_DIR` 单源；`StopLoss.reset()`。
  - M1：新模块 `adapter/isolation.py`（身份解析/控制目录/ACL-chown 交接/Pi HOME/哨兵
    探针/能力清单）；`pi_transport` 两个传输加 `identity=`；`driver` 启动收紧控制目录 +
    探针 + `ADAPTER_ISOLATION_STRICT` exit 0；`orchestrator` 会话前 prepare_challenge_dir、
    题面注入能力清单；Dockerfile 增 uid 10001 solver + acl/libcap2-bin + nmap/ping setcap。
  - M3：新模块 `adapter/surface.py`（确定性分类表、防换皮键、soft→steer→abort 阶梯、
    影子黑板事实信号、epoch 持久化、未试家族建议、`surface.rekey`）；`orchestrator` 每场
    账本 + `_on_fact` 实时输入 + `_surface_control` + `surface.*` 事件；`pi_agent.solve(control=)`
    在循环顶把帧送进 RPC（print 回退不消费，有测试锁）。
  - M4：新模块 `adapter/workgc.py`（默认 dry-run、白名单、digest 前置、`.closed` 年龄基准）；
    心跳每 ~2h 报 `work.gc_report`；solved 分支写 `.closed`。
- 实施中发现并修掉的设计漏洞：M2 让"清题目目录顺带删旧 `.stoploss.json`"的 epoch 作废副作用
  失效 → 复用 code 带旧终态直接 dropped；已改为 preflight 显式 `StopLoss.reset()`
  （回归测试命中）。M3 的实时事实信号改用影子黑板（场末计数带证据门、只在场末结算），
  方向保守（宁可漏判停滞，不误杀推进中的面）。
- 未做/未验证：§3 四条探针需容器（本机非 root 直接 skip）；`bash_guard.js` 的 EACCES
  命令翻译；`.continuation.json` 尚未合并 `blocked_routes()`；M4 只回收有 digest 的超期题目。
- 状态：设计文档 §14 实施记录已回填；architecture §13 D3/D4/D5 与 README 文档地图已同步为
  "已实施首批"。
