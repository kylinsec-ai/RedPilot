/**
 * hash 路由: #/ 总览 / #/c/<code> 题目详情 / #/runs Runs 历史 / #/runs/<run_id> run 详情。
 * code 白名单与旧版 parseHash 一致([A-Za-z0-9_-]{1,64});run_id 白名单 32hex ——
 * 不匹配的 hash 一律回落总览。
 */
export type RouteView = "overview" | "challenge" | "runs" | "run" | "control";

export const route = $state<{
  view: RouteView;
  code: string | null;
  runId: string | null;
}>({
  view: "overview",
  code: null,
  runId: null,
});
// 注:模块级 $derived 不可导出——消费组件直接在模板分支里收窄 route.code/runId

const CODE_RX = /^[A-Za-z0-9_-]{1,64}$/;
const CHAL_RX = /^#\/c\/(.+)$/;
const RUNS_RX = /^#\/runs(?:\/([0-9a-f]{32}))?$/;
const CONTROL_RX = /^#\/control$/;

function parseHash(): { view: RouteView; code: string | null; runId: string | null } {
  const fallback = { view: "overview" as RouteView, code: null, runId: null };
  const hash = location.hash || "#/";
  if (CONTROL_RX.test(hash)) return { view: "control", code: null, runId: null };
  const m = hash.match(RUNS_RX);
  if (m) {
    return m[1] ? { view: "run", code: null, runId: m[1] } : { view: "runs", code: null, runId: null };
  }
  const c = hash.match(CHAL_RX);
  if (!c) return fallback;
  let code = c[1];
  try {
    code = decodeURIComponent(code);
  } catch {
    return fallback;
  }
  return CODE_RX.test(code) ? { view: "challenge", code, runId: null } : fallback;
}

export function goOverview(): void {
  location.hash = "#/";
}
/** 题目行链接统一经此构造,与 goChallenge 同编码(白名单内字符输出与原文一致) */
export function challengeHref(code: string): string {
  return "#/c/" + encodeURIComponent(code);
}
export function goChallenge(code: string): void {
  location.hash = challengeHref(code);
}
export function goRuns(): void {
  location.hash = "#/runs";
}
export function goControl(): void {
  location.hash = "#/control";
}
/** run_id 为 32hex,无需编码;统一经此构造以防未来字符集变化 */
export function runHref(runId: string): string {
  return "#/runs/" + runId;
}
export function goRun(runId: string): void {
  location.hash = runHref(runId);
}

if (typeof window !== "undefined") {
  // 首帧即按当前 hash 初始化,深链 #/c/<code>/#/runs/<id> 直达(此前仅 hashchange 回调赋值)
  const init = parseHash();
  route.view = init.view;
  route.code = init.code;
  route.runId = init.runId;
  window.addEventListener("hashchange", () => {
    const next = parseHash();
    route.view = next.view;
    route.code = next.code;
    route.runId = next.runId;
  });
}
