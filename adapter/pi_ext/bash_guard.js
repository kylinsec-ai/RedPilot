// bash-guard: PI bash 工具安全护栏 (P0 修复 — 2026-09-04 磁盘爆炸事故)
//
// 事故: LLM 生成 `cat A B C | sort -u > wl2.txt && for e in php...; do
//   awk -v x=".$e" "{print $0 x}" wl2.txt >> wl2.txt; done` —— 边读边写同一文件,
//   11 分钟把 wl2.txt 撑到 307GB 逼爆磁盘 (根因: pi 的 bash 工具 timeout "可选、无默认")。
// v2: 新增单场实时重复命令拦截 —— vanilla PI 长会话会忘掉做过的动作, 同命令
//   反复执行 6-8 次白烧 token。spawnHook 记 seen-set, 同命令 >= 阈值直接短路
//   返回 [PI-SAFETY-REPEAT] 警示(不执行), 并把首见命令实时 append 到
//   cwd/tried_commands.md(与 driver 同格式, 跨场保留)。
//
// 本扩展用 spawnHook 拦截每条 bash 命令, 施加三层硬约束:
//   1) 自引用重定向检测: 同一行内某文件同时是读取源与 >> 追加目标 -> 不执行,
//      直接返回警示串给 LLM(治本, 防边读边写无限增长)。
//   2) 强制外层 timeout: timeout -k 15 -s KILL <N> bash -c "<cmd>" —— 兜底长命令。
//   3) ulimit -f 单文件硬上限: 防单命令把某文件写爆(EFBIG/SIGXFSZ 终止进程)。
//
// 注意: 内层 bash -c 必须用单引号包裹(cmd.replace(/'/g, "'\''")), 不能用 JSON 双引号 ——
//   因为 pi 把本脚本整串作为 `bash -c <script>` 执行, 外层 shell 会展开双引号内的
//   $var/反引号/转义, 破坏真命令里的 $0/$e(事故命令就有 awk "{print $0...}")。
//   单引号段内无任何展开, 语义保真。
//
// 部署: pi_agent.py _build_cmd 通过 ADAPTER_PI_EXTENSIONS env 注入 -e <path>。
// 环境变量: PI_BASH_TIMEOUT_SECONDS(默认600), PI_BASH_MAX_FILE_MB(默认4096)。

import { createBashTool } from "@earendil-works/pi-coding-agent";
import { appendFileSync, existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const MAX_SECONDS = parseInt(process.env.PI_BASH_TIMEOUT_SECONDS || "600", 10);
const MAX_FILE_MB = parseInt(process.env.PI_BASH_MAX_FILE_MB || "4096", 10);
const INNER_SHELL = process.env.PI_BASH_SHELL || "bash";
const REPEAT_LIMIT = parseInt(process.env.PI_BASH_REPEAT_LIMIT || "3", 10);

// 当前 Pi 进程已执行命令 seen-set: key -> {count, firstOut}；启动时再从
// .bash_guard_state.json 恢复本题实例内的跨 session 计数。
const seen = new Map();
const SEEN_FILE = "tried_commands.md";
// 跨 Pi session 的轻量命令账本。seen Map 本身每个 session 都会重置，
// 只靠它无法阻止「上一场扫过、下一场又原样扫一遍」。账本只保存规范化命令
// 的计数，不保存命令输出或 flag/凭据正文；题目收尾时由工作区清理一起删除。
const STATE_FILE = ".bash_guard_state.json";
const STATE_MAX_KEYS = 2000;
let seenFileWarned = false;

// 命令规范化: 折叠空白 + 去掉前置 "cd <workdir> && " + 去掉尾随 2>&1|head 噪音。
// 保守——只把真正相同的命令判为重复(不误伤换参的探测变体)。
function normCmd(cmd) {
  let c = String(cmd || "").trim();
  c = c.replace(/^cd\s+\S+\s*&&\s*/, "");
  c = c.replace(/;?\s*(2>&1\s*)?\|\s*head\s*-\d*\s*;?$/, "");
  return c.replace(/\s+/g, " ");
}

// 把首见命令实时 append 到 cwd/tried_commands.md (与 driver 同格式 "$ cmd")。
function persistSeen(cwd, key, cmd) {
  try {
    const p = join(cwd, SEEN_FILE);
    appendFileSync(p, "$ " + key + "\n", "utf8");
  } catch (e) {
    if (!seenFileWarned) { seenFileWarned = true; try { console.error("[bash-guard] persistSeen warn: " + e.message); } catch (_) {} }
  }
}

function statePath(cwd) {
  return join(cwd, STATE_FILE);
}

function loadPersistentSeen(cwd) {
  try {
    const p = statePath(cwd);
    if (!existsSync(p)) return;
    const data = JSON.parse(readFileSync(p, "utf8"));
    if (!data || typeof data !== "object" || !data.commands) return;
    for (const [key, value] of Object.entries(data.commands)) {
      const count = Number(value?.count || 0);
      if (key && Number.isFinite(count) && count > 0) {
        seen.set(key, { count, firstOut: "" });
      }
    }
  } catch (_) {
    // 账本损坏只影响去重，不能阻断主解题链路。
  }
}

function savePersistentSeen(cwd) {
  try {
    const entries = Array.from(seen.entries()).slice(-STATE_MAX_KEYS);
    const commands = {};
    for (const [key, value] of entries) {
      commands[key] = {
        count: Number(value?.count || 0),
      };
    }
    const p = statePath(cwd);
    const tmp = p + ".tmp";
    writeFileSync(tmp, JSON.stringify({ version: 1, commands }), "utf8");
    renameSync(tmp, p);
  } catch (_) {
    // 去重账本是优化项，写失败不能让 bash 工具失败。
  }
}

// 把命令安全嵌入 bash -c '...' 单引号串: 每个 ' -> '\'' (闭合-转义-重开)。
function squote(cmd) {
  return "'" + String(cmd).replace(/'/g, `'\\''`) + "'";
}

// 自引用重定向检测: 同一行里某文件名同时作为读取源与 >> 追加/覆盖写目标。
// 保守策略: 只拦"有明确 >> 写目标 + 该文件名在行内出现>=2次"的明显自引用。
function detectSelfRefRedirect(cmd) {
  for (const line of String(cmd).split("\n")) {
    const m = line.match(/>>\s*([^\s;&|"']+)/);
    if (!m) continue;
    const f = m[1];
    if (!f || f.startsWith("/dev/")) continue;
    const esc = f.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const appear = (line.match(new RegExp("\\b" + esc + "\\b", "g")) || []).length;
    if (appear >= 2) return `${f} (>>)`;
  }
  return null;
}

export default function (pi) {
  const cwd = process.cwd();
  loadPersistentSeen(cwd);
  const bashTool = createBashTool(cwd, {
    spawnHook: ({ command, cwd, env }) => {
      const cmd = (command || "").trim();
      if (!cmd) return { command: cmd, cwd, env };

      // 0) 单场实时重复命令拦截 (v2): 同一命令本场已执行 >= 阈值 -> 不执行,
      //    短路返回警示, 让 vanilla PI 立即得知已试过, 省 token/时间。
      //    首见命令实时写入 tried_commands.md, 跨场/崩溃都保留。
      const key = normCmd(cmd);
      const st = seen.get(key);
      if (st) {
        if (st.count >= REPEAT_LIMIT) {
          return {
            command:
              `echo "[PI-SAFETY-REPEAT] 命令已在本场执行 ${st.count} 次, 不再重复执行。` +
              `此前输出(节选): ${String(st.firstOut || "(无输出)").slice(0, 220)}. ` +
              `请基于已获信息尝试新的攻击向量, 或改用不同参数/路径/载荷。"`,
            cwd,
            env: { ...env, PI_SAFETY_REPEAT: key },
          };
        }
        st.count += 1;
        savePersistentSeen(cwd);
      } else {
        seen.set(key, { count: 1, firstOut: "" });
        persistSeen(cwd, key, cmd);
        savePersistentSeen(cwd);
      }

      // 1) 自引用重定向拦截 (治本, 直接不执行)
      const selfRef = detectSelfRefRedirect(cmd);
      if (selfRef) {
        return {
          command:
            `echo "[PI-SAFETY-INTERCEPTED] 检测到对同一文件同时读写: ${selfRef}. ` +
            `此类命令会无限增长写爆磁盘。请改用中间临时文件(e.g. cat - > /tmp/out && mv /tmp/out F) 或拆分多条命令。原命令未执行。"`,
          cwd,
          env: { ...env, PI_SAFETY_INTERCEPTED: selfRef },
        };
      }

      // 2)+3) 强制 timeout + 单文件大小硬上限兜底
      const wrapped =
        `ulimit -f $(( ${MAX_FILE_MB} * 2048 )) 2>/dev/null; ` + // ulimit -f 单位 512B blocks -> MB*2048
        `timeout -k 15 -s KILL ${MAX_SECONDS} ${INNER_SHELL} -c ${squote(cmd)}`;
      return { command: wrapped, cwd, env: { ...env, PI_BASH_GUARD: "1" } };
    },
  });

  pi.registerTool({
    ...bashTool,
    // 执行结束后回填该命令的输出片段, 供后续重复拦截时给 agent 提示(避免重读)。
    execute: (id, params, signal, onUpdate, ctx) => {
      const cmd = String((params || {}).command || "").trim();
      const res = bashTool.execute(id, params, signal, onUpdate, ctx);
      const key = normCmd(cmd);
      try {
        if (res && typeof res.then === "function") {
          res.then((out) => {
            const s = seen.get(key);
            if (s && !s.firstOut) {
              const txt = typeof out === "string" ? out : JSON.stringify(out || "");
              s.firstOut = String(txt).slice(0, 700);
              savePersistentSeen(cwd);
            }
          }).catch(() => {});
        } else if (res) {
          const s = seen.get(key);
          if (s && !s.firstOut) {
            const txt = typeof res === "string" ? res : JSON.stringify(res || "");
            s.firstOut = String(txt).slice(0, 700);
            savePersistentSeen(cwd);
          }
        }
      } catch (_) {}
      return res;
    },
  });
}
