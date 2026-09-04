/**
 * hash 路由: #/ 总览 ↔ #/c/<code> 详情。
 * code 白名单与旧版 parseHash 一致([A-Za-z0-9_-]{1,64})——不匹配的 hash 一律回落总览。
 */
export const route = $state<{ view: "overview" | "challenge"; code: string | null }>({
  view: "overview",
  code: null,
});
// 注:模块级 $derived 不可导出——消费组件直接在模板分支里收窄 route.code

const CODE_RX = /^#\/c\/([A-Za-z0-9_-]{1,64})$/;

function parseHash(): { view: "overview" | "challenge"; code: string | null } {
  const m = (location.hash || "#/").match(CODE_RX);
  return m ? { view: "challenge", code: m[1] } : { view: "overview", code: null };
}

export function goOverview(): void {
  location.hash = "#/";
}
export function goChallenge(code: string): void {
  location.hash = "#/c/" + encodeURIComponent(code);
}

if (typeof window !== "undefined") {
  window.addEventListener("hashchange", () => {
    const next = parseHash();
    route.view = next.view;
    route.code = next.code;
  });
}
