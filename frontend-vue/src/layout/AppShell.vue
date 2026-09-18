<script setup lang="ts">
/**
 * 应用外壳:左常驻侧栏 + 主区,窄屏(<900px)侧栏变 off-canvas 抽屉。
 *
 * 为什么**不用 el-drawer** 装窄屏侧栏:抽屉里的挑战列表要保状态(搜索词、滚动位置),
 * 也就是说无论断点如何、抽屉开合几次,都必须是同一个组件实例。el-drawer 关闭即销毁
 * 内容(且 teleport 走),跨断点会重建 —— 搜索框会被清空。所以侧栏元素常驻挂载,
 * 只由 CSS 施加位移;Esc / 遮罩 / 焦点陷阱因此需要本组件自己给(el-drawer 那部分能力
 * 是绑在它自己的 DOM 上的,这里用不上)。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import SideNav from "./SideNav.vue";
import ReadAuthGate from "../components/ReadAuthGate.vue";
import { current } from "../lib/route";
import { live, startLiveWatchers } from "../stores/live";
import { startRosterPolling } from "../stores/roster";

// app 生命周期内各启动一次(单例守卫去重):SSE+兜底轮询、共享花名册轮询;卸载时清理
const stopLive = startLiveWatchers();
const stopRoster = startRosterPolling();

// 窄屏(<900px)侧栏变 off-canvas 抽屉;首帧即按 matchMedia 初始化,避免窄屏首绘抽屉闪开
const narrow = ref(
  typeof window !== "undefined" && window.matchMedia("(max-width: 899px)").matches,
);
const drawerOpen = ref(false);
const mq = typeof window === "undefined" ? null : window.matchMedia("(max-width: 899px)");

const onMqChange = (e: MediaQueryListEvent) => {
  narrow.value = e.matches;
  if (!e.matches) drawerOpen.value = false; // 拉宽即收起
};

const mainEl = ref<HTMLElement | null>(null);
const toggleBtn = ref<HTMLButtonElement | null>(null);
const panelEl = ref<HTMLElement | null>(null);

// 连接状态一处派生:文案进侧栏状态区,圆点色进窄屏顶条
const conn = computed(() => {
  const c = live.value.conn;
  if (c === "live") return { text: "● 实时连接", ok: true, bad: false, pending: false };
  if (c === "unauthorized") return { text: "● 需观测凭据", ok: false, bad: true, pending: false };
  if (c === "reconnecting")
    return { text: "● 重连中…", ok: false, bad: false, pending: true };
  return { text: "● 连接中…", ok: false, bad: false, pending: false };
});

const drawerHidden = computed(() => narrow.value && !drawerOpen.value);

/** 窄屏顶条上的上下文标题(与旧版逐字一致;run 用 8 位截断,是顶条的宽度妥协) */
const contextTitle = computed(() => {
  const r = current.value;
  if (r.view === "challenge" && r.code) return { text: r.code, mono: true };
  if (r.view === "run" && r.runId) return { text: `run ${r.runId.slice(0, 8)}…`, mono: true };
  return null;
});

// 路由变化:窄屏收抽屉 + 主区回顶(瞬跳,不做平滑)。
// 仅路由真正变化才滚动——narrow 的当前值不参与依赖,不因断点穿越而丢阅读位
let prevView = "";
let prevCode: string | null = null;
watch(
  () => [current.value.view, current.value.code] as const,
  ([v, c]) => {
    const changed = v !== prevView || c !== prevCode;
    prevView = v;
    prevCode = c;
    if (!changed) return;
    drawerOpen.value = false;
    mainEl.value?.scrollTo({ top: 0 });
  },
);

// 抽屉打开时:Esc 关闭 + Tab 焦点陷阱(背景主区此时被遮罩挡住,不可达)
function onKeydown(e: KeyboardEvent): void {
  if (!(narrow.value && drawerOpen.value)) return;
  if (e.key === "Escape") {
    e.preventDefault();
    drawerOpen.value = false;
    return;
  }
  if (e.key === "Tab") {
    const panel = panelEl.value;
    if (!panel) return;
    const items = panel.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
}

// 焦点管理:开抽屉 → 焦点进 [data-autofocus] 项;关抽屉 → 归还 ☰。
// 归还需在 rAF 里做——inert 施加后浏览器先把焦点 blur 到 body,同步检查会误判
let raf1 = 0;
let raf2 = 0;
watch(drawerOpen, (open, wasOpen) => {
  if (!narrow.value) return;
  const panel = panelEl.value;
  cancelAnimationFrame(raf1);
  cancelAnimationFrame(raf2);
  if (open) {
    raf1 = requestAnimationFrame(() => {
      (panel?.querySelector<HTMLElement>("[data-autofocus]") ?? panel)?.focus();
    });
  } else if (wasOpen) {
    // inert 施加后浏览器把焦点 blur 到 body 的动作落在下一帧——双 rAF 等两帧再归还
    raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => {
        const ae = document.activeElement;
        const insidePanel = ae instanceof HTMLElement && !!panel?.contains(ae);
        if (ae !== toggleBtn.value && !insidePanel) toggleBtn.value?.focus();
      });
    });
  }
});

onMounted(() => {
  mq?.addEventListener("change", onMqChange);
  window.addEventListener("keydown", onKeydown);
});

onBeforeUnmount(() => {
  mq?.removeEventListener("change", onMqChange);
  window.removeEventListener("keydown", onKeydown);
  cancelAnimationFrame(raf1);
  cancelAnimationFrame(raf2);
  stopLive();
  stopRoster();
});
</script>

<template>
  <div class="shell">
    <!-- 侧栏:≥900px 常驻;窄屏 off-canvas。元素常驻挂载(列表搜索/滚动态不丢),
         关闭时 inert 移出 Tab 序与无障碍树 -->
    <aside
      id="sidebar-panel"
      ref="panelEl"
      class="sidebar"
      :class="{ 'sidebar-narrow': narrow, 'sidebar-open': drawerOpen }"
      :role="narrow && drawerOpen ? 'dialog' : undefined"
      :aria-modal="narrow && drawerOpen ? 'true' : undefined"
      :aria-label="narrow && drawerOpen ? '题目导航' : undefined"
      :aria-hidden="drawerHidden ? 'true' : undefined"
      :inert="drawerHidden ? true : undefined"
    >
      <SideNav :conn-text="conn.text" />
    </aside>

    <button
      v-if="narrow && drawerOpen"
      type="button"
      aria-label="关闭侧栏"
      tabindex="-1"
      class="scrim"
      @click="drawerOpen = false"
    ></button>

    <div class="column">
      <header v-if="narrow" class="topbar">
        <button
          ref="toggleBtn"
          type="button"
          aria-controls="sidebar-panel"
          :aria-expanded="drawerOpen"
          :aria-label="drawerOpen ? '关闭侧栏' : '打开侧栏'"
          class="hamburger"
          @click="drawerOpen = !drawerOpen"
        >
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <path
              d="M2 4.5h12M2 8h12M2 11.5h12"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linecap="round"
            />
          </svg>
        </button>
        <span v-if="contextTitle" class="ctx" :class="{ mono: contextTitle.mono }">
          {{ contextTitle.text }}
        </span>
        <span class="spacer"></span>
        <span
          role="status"
          :title="conn.text"
          :aria-label="conn.text"
          class="dot"
          :class="conn.ok ? 'dot-green' : conn.bad ? 'dot-red' : conn.pending ? 'dot-amber pulse-ring' : 'dot-dim'"
        ></span>
      </header>

      <main ref="mainEl" class="main scroll-thin">
        <div class="page">
          <ReadAuthGate />
          <router-view v-slot="{ Component }">
            <component :is="Component" :key="current.key" />
          </router-view>
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  height: 100dvh;
  overflow: hidden;
}
.sidebar {
  position: fixed;
  top: 0;
  bottom: 0;
  left: 0;
  z-index: 40;
  width: 264px;
  border-right: 1px solid var(--line);
  background: var(--rail);
  transform: translateX(0);
}
.sidebar-narrow {
  transition: transform 220ms cubic-bezier(0.22, 0.61, 0.36, 1);
}
.sidebar-narrow:not(.sidebar-open) {
  transform: translateX(-100%);
}
.sidebar-open {
  box-shadow: 6px 0 24px rgba(0, 0, 0, 0.35);
}
@media (prefers-reduced-motion: reduce) {
  .sidebar-narrow {
    transition: none;
  }
}
.scrim {
  position: fixed;
  inset: 0;
  z-index: 35;
  cursor: default;
  border: 0;
  padding: 0;
  background: rgba(0, 0, 0, 0.45);
}
.column {
  display: flex;
  min-width: 0;
  flex: 1;
  flex-direction: column;
  padding-left: 264px;
}
.topbar {
  display: flex;
  height: 44px;
  flex: none;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid var(--line);
  background: color-mix(in srgb, var(--bg) 93%, transparent);
  padding: 0 12px;
  backdrop-filter: blur(6px);
}
.hamburger {
  display: flex;
  height: 28px;
  width: 28px;
  flex: none;
  cursor: pointer;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--line2);
  border-radius: 6px;
  background: transparent;
  color: var(--mut);
}
.hamburger:hover {
  background: var(--panel2);
  color: var(--ink);
}
.ctx {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
}
.spacer {
  flex: 1;
}
.dot {
  height: 8px;
  width: 8px;
  flex: none;
  border-radius: 999px;
}
.main {
  min-height: 0;
  flex: 1;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.page {
  margin: 0 auto;
  width: 100%;
  max-width: 1240px;
  padding: 20px 20px 64px;
}
@media (max-width: 899px) {
  .column {
    padding-left: 0;
  }
}
@media (max-width: 720px) {
  .page {
    padding-left: 14px;
    padding-right: 14px;
  }
}
</style>
