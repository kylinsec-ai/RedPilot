/**
 * 共享题目花名册:单轮询 10s(页面隐藏时暂停),总览表与侧栏题目列表共用一份。
 * 消费组件不得自行轮询 /api/roster——一律读本 store。
 */
import { fetchRoster } from "./api";
import { diffLabel } from "./format";
import type { ChallengeRow, LocalTrace, RosterSnapshot } from "./types";

export const roster = $state<{ data: RosterSnapshot | null; failed: boolean }>({
  data: null,
  failed: false,
});

let started = false;
let stopped = false;
let inflight: Promise<void> | null = null;

/** 单次拉取(轮询节拍与挂载补刷共用);in-flight 去重,卸载后不再写 store */
export function refreshRoster(): Promise<void> {
  if (!inflight) {
    inflight = (async () => {
      try {
        const snap = await fetchRoster();
        if (stopped) return;
        roster.data = snap;
        roster.failed = false;
      } catch {
        // 有旧数据时保持展示,语义同旧 Overview
        if (!stopped) roster.failed = true;
      } finally {
        inflight = null;
      }
    })();
  }
  return inflight;
}

/** app 生命周期内只启动一次;返回清理函数(App 卸载时用,清理后允许重建) */
export function startRosterPolling(): () => void {
  if (started) return () => {};
  started = true;
  stopped = false;

  void refreshRoster(); // 首帧立即拉(含后台标签页,与旧行为一致)
  const iv = setInterval(() => {
    if (stopped || document.hidden) return;
    void refreshRoster();
  }, 10000);
  return () => {
    stopped = true;
    started = false;
    clearInterval(iv);
  };
}

// ── 纯行处理函数(模块级 $derived 不可导出——消费组件自行 $derived/{@const}) ──

/** challenges 记录 -> 行数组;过滤逻辑自旧 Overview 逐字搬 */
export function toRows(snap: RosterSnapshot | null): ChallengeRow[] {
  return Object.values(snap?.challenges || {}).filter(
    (x): x is ChallengeRow => !!x && !!x.unique_code,
  );
}

export type RowSortKey = "code" | "score" | "prog" | "act";

function keyOf(x: ChallengeRow, k: RowSortKey): string | number {
  if (k === "code") return x.unique_code;
  if (k === "score") return x.total_score || 0;
  if (k === "prog") return x.correct_flag_count || 0;
  return x.local?.last_activity || 0;
}
function diffRank(d: string): number {
  return d === "easy" ? 0 : d === "hard" ? 2 : 1;
}

/** 与旧 Overview 逐字一致的比较器;default 序:未完成在前,难度升序,分值降序 */
export function sortRows(
  list: ChallengeRow[],
  sortKey: RowSortKey | null = null,
  sortAsc = true,
): ChallengeRow[] {
  return list.slice().sort((a, b) => {
    if (sortKey) {
      const ka = keyOf(a, sortKey);
      const kb = keyOf(b, sortKey);
      if (typeof ka === "string" && typeof kb === "string") {
        const c = ka.localeCompare(kb);
        return sortAsc ? c : -c;
      }
      return sortAsc ? (ka as number) - (kb as number) : (kb as number) - (ka as number);
    }
    if ((a.is_completed ? 1 : 0) !== (b.is_completed ? 1 : 0)) return a.is_completed ? 1 : -1;
    const ra = diffRank(a.difficulty);
    const rb = diffRank(b.difficulty);
    if (ra !== rb) return ra - rb;
    return (b.total_score || 0) - (a.total_score || 0);
  });
}

export type TraceChip = { tone: "flag" | "crash" | "sess" | "art"; text: string };

/** 本地痕迹 -> 徽标序列;语义自旧 Overview chips() 逐字搬 */
export function traceChips(local?: LocalTrace): TraceChip[] {
  const out: TraceChip[] = [];
  if (local?.flag) out.push({ tone: "flag", text: "FLAG ✓" });
  if (local?.crashed) out.push({ tone: "crash", text: "core" });
  if (local?.transcript_bytes) out.push({ tone: "sess", text: "会话" });
  const arts = local?.artifacts?.length || 0;
  if (arts) out.push({ tone: "art", text: `产物 ${arts}` });
  return out;
}

// ── 侧栏难度分组:组序固定,未知难度一律归"其他" ──
export type DiffKey = "easy" | "medium" | "hard" | "other";
export const DIFF_ORDER: readonly DiffKey[] = ["easy", "medium", "hard", "other"];

export function diffKey(d: string | undefined): DiffKey {
  return d === "easy" || d === "medium" || d === "hard" ? d : "other";
}
export function diffGroupLabel(k: DiffKey): string {
  // 中文名复用 format.ts 的 DIFF 映射;仅"其他"是侧栏分组自有
  return k === "other" ? "其他" : diffLabel(k)[0];
}
