/**
 * 展示格式工具 —— 自旧 web/index.html 逐字移植,文案/粒度保持一致。
 */

/** 毫秒时间戳 -> HH:MM:SS(时间线条目 t 是毫秒) */
export function fmtClock(ms?: number | null): string {
  if (!ms) return "";
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 秒级时间戳 -> 相对时间(roster last_activity 等是秒) */
export function fmtRel(ts?: number | null): string {
  if (!ts) return "—";
  const s = Date.now() / 1000 - ts;
  if (s < 5) return "刚刚";
  if (s < 60) return Math.floor(s) + " 秒前";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  return Math.floor(s / 86400) + " 天前";
}

export function fmtMB(b?: number | null): string {
  if (!b) return "";
  return b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : (b || 0) + " B";
}

/** difficulty -> [中文, css 基调];未知原样 */
export const DIFF: Record<string, [string, string]> = {
  easy: ["简单", "easy"],
  medium: ["中等", "medium"],
  hard: ["困难", "hard"],
};
export function diffLabel(d: string | undefined): [string, string] {
  return DIFF[d || ""] || [d || "—", ""];
}

const STOP: Record<string, string> = {
  toolUse: "调用了工具",
  end_turn: "回合结束",
  max_tokens: "token 触顶",
  stop: "停止",
};
export function stopZh(s?: string | null): string {
  return (s && STOP[s]) || s || "";
}
