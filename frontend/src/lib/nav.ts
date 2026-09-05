/**
 * 侧栏导航模型:组 -> 条目(判别联合)。
 * 加组/加条目 = 数组加一行;未来新增条目 kind 只需 Sidebar.svelte 的
 * {#each} 渲染增一个分支。新增"视图"另需扩 route.svelte.ts 的 view 联合 + App 分支。
 */

export interface NavLinkEntry {
  kind: "link";
  key: string;
  label: string;
  /** hash 目标(如 "#/")——不新建路由 API */
  to: string;
  /** view 宽化:新视图只需在本文件加行 + route.svelte.ts 加分支 */
  isActive?: (view: string, code: string | null) => boolean;
}

/** 内嵌整块列表组件(当前即题目主列表) */
export interface NavChallengesEntry {
  kind: "challenges";
  key: string;
  label: string;
}

export type NavEntry = NavLinkEntry | NavChallengesEntry;

export interface NavGroup {
  key: string;
  /** 组标题;空串则整组不渲染组头 */
  label: string;
  entries: NavEntry[];
}

export const NAV: NavGroup[] = [
  {
    key: "g-main",
    label: "",
    entries: [
      { kind: "link", key: "overview", label: "总览", to: "#/", isActive: (view) => view === "overview" },
    ],
  },
  {
    key: "g-chal",
    label: "题目",
    entries: [{ kind: "challenges", key: "challist", label: "全部题目" }],
  },
  {
    key: "g-hist",
    label: "运行记录",
    entries: [
      {
        kind: "link",
        key: "runs",
        label: "Runs 历史",
        to: "#/runs",
        isActive: (view) => view === "runs" || view === "run",
      },
    ],
  },
];
