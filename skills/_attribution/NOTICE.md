# skills/ 来源与许可

本目录下的技能（SKILL.md 及其附属 `references/`、`scripts/` 等）来自：

- **项目**：SpecterOps Skills — https://github.com/SpecterOps/skills
- **版权**：Copyright SpecterOps, Inc. 及各技能 frontmatter 中标注的作者
- **许可**：
  - 仓库整体：**Apache License 2.0**（全文见本目录 `LICENSE-SpecterOps`）
  - 各技能：多数在 frontmatter 标注 `license: MIT`
- **引入方式**：由 `plugins/<plugin>/skills/<skill>/` 与 `skills/<skill>/` **扁平化**到
  `skills/<skill>/`，并**移除 `assets/`**（图标与 Windows 二进制；无任何 SKILL.md 正文引用）。
  `references/` 与 `scripts/` 保留，因为 36 个技能正文按相对路径引用它们。

## 修改记录（本地）

- 改名/重排：无（目录名与 frontmatter `name` 逐一对齐，已校验 75/75）。
- 删除：`assets/`（图标、COM 代理 DLL/EXE 等，见上）。
- 加载侧适配：`packages/worker/redpilot_worker/adapter/skill_loader.py` 的
  `_DOMAIN_SIGNALS` 已重指向本目录的技能名（旧技能面已整体替换）。

## 注意

- 部分技能面向 **MCP 服务或外部基础设施**（BloodHound CE、Ghostwriter、Binary Ninja、
  Ghidra、SCCM、Mythic、Cobalt Strike 等），本 worker 容器**未安装**这些依赖；
  这类技能在容器内属于「方法论文档」，不能直接执行。
- `proxychains-tunnel` 与 `adapter/taskprompt.py` 中「本环境禁用 proxychains4」的既有约束
  冲突；以 `taskprompt.py` 的环境约束为准（用 `chisel` / `ssh -L/-D`）。
- 若需回滚到替换前的技能面：`git checkout <替换前提交> -- skills`
  （被删除的 23 个旧技能仍在 git 历史中，`git status` 显示为 `D`）。
- 加载器注意：`skill_loader._scan()` 会把 `skills/` 下**任意顶层 `.md` 文件**也当技能
  （单文件形式）。因此归属说明放在子目录 `_attribution/`（无 SKILL.md，不会被扫描，
  也不会被 `_install_skills` 装载）。
