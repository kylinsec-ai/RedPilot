# skills/ 溯源与同步

本目录是 **上游 [yaklang/hack-skills](https://github.com/yaklang/hack-skills) 的技能库内联副本**，
不是本仓库原创内容。2026-09-16 整体替换掉了原有的 23 个单题面 SKILL.md（web / pwn /
crypto / ad / java-exploit … 窄题面那套）。

| 字段 | 值 |
|---|---|
| 上游 | `https://github.com/yaklang/hack-skills` |
| 同步点 | `6fbf0bc8d5c71830d62543a308d8744606c43d7c`（2026-09-13，「Merge pull request #9 from yaklang/feat/attack-surface-mapping」） |
| 许可 | MIT，见 `LICENSE.hack-skills`（Copyright (c) 2026 VillanCh） |
| 副本范围 | **只有 `skills/`** —— 上游的 `site/`（静态站）、`assets/`、`scripts/`、`.github/`、两份 README 均未纳入 |

## 上游布局（不要改动它）

```
skills/<semantic-identifier>/SKILL.md          # 技能正文 + frontmatter
skills/<semantic-identifier>/*.md              # 同技能的配套材料（SCENARIOS.md / *_MATRIX.md …）
```

三层入口：`hack`（master router）→ 6 个分类入口（`recon-for-sec` / `api-sec` / `auth-sec` /
`injection-checking` / `file-access-vuln` / `business-logic-vuln`）→ 各深度题面技能。

## 同步方法

```bash
git clone --depth 1 https://github.com/yaklang/hack-skills.git /tmp/hack-skills
rm -rf skills && mkdir skills
cp -r /tmp/hack-skills/skills/. skills/
cp /tmp/hack-skills/LICENSE skills/LICENSE.hack-skills
git -C /tmp/hack-skills log -1 --format=%H   # 把新 hash 填回上表
```

同步后必须跑回归守卫（技能面为 0 是这套的经典静默失败）：

```bash
python -m pytest packages/contracts/tests/test_skills_root.py -v
```

## 本仓库对它的两处依赖

1. **frontmatter 形态**：上游 `description:` 一律是 YAML 块标量（`>-` 折行）。
   `redpilot/worker/adapter/skill_loader.py` 的解析器必须认它，否则技能名录全是
   空的描述（曾实测：解析出 `description: '>-'` 字面量）。
2. **装载面**：`skills/` 逐目录含 `SKILL.md` 才算技能 —— Dockerfile 两处 `COPY skills`
   与 `pi_agent._install_skills` 的软链装载都按这条规则发现。
