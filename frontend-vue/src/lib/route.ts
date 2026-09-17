/**
 * hash 路由: #/ 总览 / #/c/<code> 题目详情 / #/runs Runs 历史 / #/runs/<run_id> run 详情 /
 * #/control 控制面。
 *
 * 文法**逐字**对齐旧版 parseHash:code 白名单 [A-Za-z0-9_-]{1,64}(decode 后再校验,
 * decode 抛错即回落)、run_id 白名单 32hex(大写不认),不匹配的 hash 一律回落总览。
 *
 * 为何解析直接读 location.hash 而不读 vue-router 的 route.path:
 * vue-router 会对 path 做自己的归一/解码,而这里的白名单语义是后端契约的一部分
 * (题目 code 与 run_id 都是服务端生成的受限字符集)。读原始 hash 能得到与旧版
 * 完全一致的判定;vue-router 只负责导航与"hash 变了"这个事件。
 */
import { ref, type Ref } from "vue";

export type RouteView = "overview" | "challenge" | "runs" | "run" | "control";

export interface HashRoute {
  view: RouteView;
  code: string | null;
  runId: string | null;
  /** 视图身份键:同视图换 code/runId 时用它强制重建视图(等价于旧版 {#key})。 */
  key: string;
}

export const CODE_RX = /^[A-Za-z0-9_-]{1,64}$/;
/** run_id 的字符类:32 位小写 hex(大写不认)。**只此一处**,下面两个正则都由它拼。 */
const RUN_ID_HEX = "[0-9a-f]{32}";
/** run_id 白名单。router 守卫用。 */
export const RUN_ID_RX = new RegExp(`^${RUN_ID_HEX}$`);
const CHAL_RX = /^#\/c\/(.+)$/;
const RUNS_RX = new RegExp(`^#/runs(?:/(${RUN_ID_HEX}))?$`);
const CONTROL_RX = /^#\/control$/;

export function parseHash(hash = typeof window === "undefined" ? "" : location.hash): HashRoute {
  const fallback: HashRoute = { view: "overview", code: null, runId: null, key: "overview" };
  const h = hash || "#/";
  if (CONTROL_RX.test(h)) return { view: "control", code: null, runId: null, key: "control" };
  const m = h.match(RUNS_RX);
  if (m) {
    return m[1]
      ? { view: "run", code: null, runId: m[1], key: "run:" + m[1] }
      : { view: "runs", code: null, runId: null, key: "runs" };
  }
  const c = h.match(CHAL_RX);
  if (!c) return fallback;
  let code = c[1];
  try {
    code = decodeURIComponent(code);
  } catch {
    return fallback;
  }
  return CODE_RX.test(code)
    ? { view: "challenge", code, runId: null, key: "challenge:" + code }
    : fallback;
}

/** 当前路由(模块级单例;由 router/index.ts 在每次导航后刷新)。 */
export const current: Ref<HashRoute> = ref<HashRoute>(parseHash());

/** 题目行链接统一经此构造(白名单内字符 encodeURIComponent 是恒等,不会二次编码)。 */
export function challengeHref(code: string): string {
  return "#/c/" + encodeURIComponent(code);
}
/** run_id 为 32hex,无需编码;统一经此构造以防未来字符集变化。 */
export function runHref(runId: string): string {
  return "#/runs/" + runId;
}
