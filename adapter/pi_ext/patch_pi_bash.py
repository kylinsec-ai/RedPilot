#!/usr/bin/env python3
# 幂等应用 TsecBench 框架护栏到 pi 内置 bash 工具（默认超时+ulimit+重复短路+tried_commands）
# 由 Dockerfile 在构建期调用；也用于运行中容器就地补丁（新 pi 进程自动生效）。
import sys
p = "/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/tools/bash.js"
src = open(p, encoding="utf-8").read()
if "TsecBench 框架护栏" in src:
    print("already patched, skip")
    sys.exit(0)

anchor1 = "export function createLocalBashOperations(options) {"
inject1 = '''// ── TsecBench 框架护栏: 单场重复命令短路 + 实时写 tried_commands.md ──
const _bgRepeatLimit = Number(process.env.PI_BASH_REPEAT_LIMIT || 3);
const _bgSeen = new Map();
function _bgNorm(cmd) {
  let c = String(cmd || "").trim();
  c = c.replace(/^cd\\s+\\S+\\s*&&\\s*/, "");
  c = c.replace(/;?\\s*(2>&1\\s*)?\\|\\s*head\\s*-\\d*\\s*;?$/, "");
  return c.replace(/\\s+/g, " ");
}
function _bgMaybeShortCircuit(cmd, cwd) {
  const key = _bgNorm(cmd);
  if (!key) return null;
  const st = _bgSeen.get(key);
  if (st) {
    if (st.count >= _bgRepeatLimit) {
      return `echo "[PI-SAFETY-REPEAT] 命令已在本场执行 ${st.count} 次, 不再重复执行。此前输出(节选): ${String(st.firstOut || "(无输出)").slice(0, 200)}. 请尝试新的攻击向量或不同参数。"`;
    }
    st.count += 1;
  } else {
    _bgSeen.set(key, { count: 1, firstOut: "" });
    try {
      const _fs = require("node:fs"), _path = require("node:path");
      if (cwd) _fs.appendFileSync(_path.join(cwd, "tried_commands.md"), "$ " + key + "\\n", "utf8");
    } catch (_) {}
  }
  return null;
}
function _bgNoteFirstOut(cmd, outText) {
  const key = _bgNorm(cmd);
  if (!key) return;
  const st = _bgSeen.get(key);
  if (st && !st.firstOut && outText) st.firstOut = String(outText).slice(0, 700);
}
''' + "\n" + anchor1
assert src.count(anchor1) == 1, f"anchor1={src.count(anchor1)}"
src = src.replace(anchor1, inject1, 1)

# 短路: 在 spawnContext 之后、OutputAccumulator 之前
old2 = '''            const spawnContext = resolveSpawnContext(resolvedCommand, cwd, spawnHook);
            const output = new OutputAccumulator({ tempFilePrefix: "pi-bash" });'''
new2 = '''            const spawnContext = resolveSpawnContext(resolvedCommand, cwd, spawnHook);
            const _short = _bgMaybeShortCircuit(spawnContext.command, spawnContext.cwd);
            if (_short) {
              const _txt = _short;
              if (onUpdate) onUpdate({ content: _txt, details: undefined });
              return { content: [{ type: "text", text: _txt }], details: undefined, isError: false };
            }
            const output = new OutputAccumulator({ tempFilePrefix: "pi-bash" });'''
assert src.count(old2) == 1, f"old2={src.count(old2)}"
src = src.replace(old2, new2, 1)

# firstOut 回填
old3 = '''                return { content: [{ type: "text", text: outputText }], details };'''
new3 = '''                _bgNoteFirstOut(spawnContext.command, outputText);
                return { content: [{ type: "text", text: outputText }], details };'''
assert src.count(old3) == 1, f"old3={src.count(old3)}"
src = src.replace(old3, new3, 1)

open(p, "w", encoding="utf-8").write(src)
print("v2b patch OK")
