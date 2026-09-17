/**
 * 路由表。**导航与 URL 由 vue-router 负责,渲染判定仍由 lib/route.ts 单源给出**。
 *
 * 为什么渲染不直接绑 <router-view> 的匹配结果:题目 code 与 run_id 的白名单
 * ([A-Za-z0-9_-]{1,64} / 32hex)是后端契约的一部分,不是前端的美化规则。
 * vue-router 会对 path 段做自己的解码与归一(#/c/ab%20cd 这类),而旧版 parseHash
 * 是先 decodeURIComponent 再整体校验、失败即回落总览。为了不与旧判定漂移,
 * 这里用 beforeEach 守卫把同一套谓词再施加一次(不匹配就重定向到总览),
 * 于是两边不可能给出不同答案;AppShell 再按 lib/route.ts 的 current 渲染。
 */
import { createRouter, createWebHashHistory } from "vue-router";
import { CODE_RX, RUN_ID_RX, current, parseHash } from "../lib/route";
import Overview from "../views/Overview.vue";
import Challenge from "../views/Challenge.vue";
import Control from "../views/Control.vue";
import Runs from "../views/Runs.vue";
import Run from "../views/Run.vue";

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", name: "overview", component: Overview },
    { path: "/control", name: "control", component: Control },
    { path: "/runs", name: "runs", component: Runs },
    { path: "/runs/:runId", name: "run", component: Run },
    { path: "/c/:code", name: "challenge", component: Challenge },
    { path: "/:pathMatch(.*)*", name: "notfound", redirect: "/" },
  ],
});

router.beforeEach((to) => {
  // 白名单判定单源:谓词与 lib/route.ts 的 parseHash 共用 CODE_RX / RUN_ID_RX
  if (to.name === "challenge" && !CODE_RX.test(String(to.params.code ?? ""))) {
    return { name: "overview" };
  }
  if (to.name === "run" && !RUN_ID_RX.test(String(to.params.runId ?? ""))) {
    return { name: "overview" };
  }
  return true;
});

router.afterEach(() => {
  current.value = parseHash();
});

export default router;
