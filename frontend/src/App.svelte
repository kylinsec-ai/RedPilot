<script lang="ts">
  import { untrack } from "svelte";
  import Sidebar from "./components/Sidebar.svelte";
  import ReadAuthGate from "./components/ReadAuthGate.svelte";
  import Overview from "./views/Overview.svelte";
  import Challenge from "./views/Challenge.svelte";
  import Control from "./views/Control.svelte";
  import Runs from "./views/Runs.svelte";
  import Run from "./views/Run.svelte";
  import { route } from "./lib/route.svelte";
  import { live, startLiveWatchers } from "./lib/live.svelte";
  import { startRosterPolling } from "./lib/roster.svelte";

  // app 生命周期内各启动一次(单例守卫去重):SSE+兜底轮询、共享花名册轮询;卸载时清理
  $effect(() => startLiveWatchers());
  $effect(() => startRosterPolling());

  // 窄屏(<900px)侧栏变 off-canvas 抽屉;首帧即按 matchMedia 初始化,避免窄屏首绘抽屉闪开
  let narrow = $state(
    typeof window !== "undefined" && window.matchMedia("(max-width: 899px)").matches,
  );
  let drawerOpen = $state(false);

  $effect(() => {
    const mq = window.matchMedia("(max-width: 899px)");
    narrow = mq.matches;
    const onChange = (e: MediaQueryListEvent) => {
      narrow = e.matches;
      if (!e.matches) drawerOpen = false; // 拉宽即收起
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  });

  let mainEl: HTMLElement | null = $state(null);
  let toggleBtn: HTMLButtonElement | null = $state(null);
  let panelEl: HTMLElement | null = $state(null);

  // 连接状态一处派生:文案进侧栏状态区,圆点色进窄屏顶条
  const conn = $derived(
    live.conn === "live"
      ? { text: "● 实时连接", dot: "bg-green" }
      : live.conn === "unauthorized"
        ? { text: "● 需观测凭据", dot: "bg-red" }
        : live.conn === "reconnecting"
          ? { text: "● 重连中…", dot: "bg-amber animate-pulse-ring" }
          : { text: "● 连接中…", dot: "bg-dim" },
  );

  const drawerHidden = $derived(narrow && !drawerOpen);
  const drawerCls = $derived(
    narrow
      ? drawerOpen
        ? "translate-x-0 shadow-[6px_0_24px_rgba(0,0,0,0.35)]"
        : "-translate-x-full"
      : "translate-x-0",
  );

  // 路由变化:窄屏收抽屉 + 主区回顶(瞬跳,不做平滑)。
  // 仅路由真正变化才滚动——narrow 经 untrack 读取,不因断点穿越而丢阅读位
  let prevView: string | null = null;
  let prevCode: string | null = null;
  $effect(() => {
    const v = route.view;
    const c = route.code;
    const changed = v !== prevView || c !== prevCode;
    prevView = v;
    prevCode = c;
    if (!changed) return;
    if (untrack(() => narrow)) drawerOpen = false;
    mainEl?.scrollTo({ top: 0 });
  });

  // 抽屉打开时:Esc 关闭 + Tab 焦点陷阱(背景主区此时被 scrim 遮挡,不可达)
  $effect(() => {
    if (!(narrow && drawerOpen)) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        drawerOpen = false;
        return;
      }
      if (e.key === "Tab") {
        const panel = document.getElementById("sidebar-panel");
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
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // 焦点管理:开抽屉 → 焦点进 [data-autofocus] 项;关抽屉 → 归还 ☰。
  // 归还需在 rAF 里做——inert 施加后浏览器先把焦点 blur 到 body,同步检查会误判
  let prevOpen = false;
  $effect(() => {
    const open = drawerOpen;
    const justClosed = prevOpen && !open;
    prevOpen = open;
    if (!narrow) return;
    const panel = panelEl;
    let raf = 0;
    let raf2 = 0;
    if (drawerOpen) {
      raf = requestAnimationFrame(() => {
        (panel?.querySelector<HTMLElement>("[data-autofocus]") ?? panel)?.focus();
      });
    } else if (justClosed) {
      // inert 施加后浏览器把焦点 blur 到 body 的动作落在下一帧——双 rAF 等两帧再归还
      raf = requestAnimationFrame(() => {
        raf2 = requestAnimationFrame(() => {
          const ae = document.activeElement;
          const insidePanel = ae instanceof HTMLElement && !!panel?.contains(ae);
          if (ae !== toggleBtn && !insidePanel) toggleBtn?.focus();
        });
      });
    }
    return () => {
      cancelAnimationFrame(raf);
      cancelAnimationFrame(raf2);
    };
  });
</script>

<div class="flex h-dvh overflow-hidden">
  <!-- 侧栏:≥900px 常驻;窄屏 off-canvas。元素常驻挂载(列表搜索/滚动态不丢),
       关闭时 inert 移出 Tab 序与无障碍树 -->
  <aside
    id="sidebar-panel"
    bind:this={panelEl}
    role={narrow && drawerOpen ? "dialog" : undefined}
    aria-modal={narrow && drawerOpen ? "true" : undefined}
    aria-label={narrow && drawerOpen ? "题目导航" : undefined}
    aria-hidden={drawerHidden ? "true" : undefined}
    inert={drawerHidden ? true : undefined}
    class="fixed inset-y-0 left-0 z-[40] w-[264px] border-r border-line bg-rail
      {narrow ? 'drawer-slide' : ''} {drawerCls}"
  >
    <Sidebar connText={conn.text} />
  </aside>

  {#if narrow && drawerOpen}
    <button
      type="button"
      aria-label="关闭侧栏"
      tabindex="-1"
      class="fixed inset-0 z-[35] cursor-default bg-black/45"
      onclick={() => (drawerOpen = false)}
    ></button>
  {/if}

  <div class="flex min-w-0 flex-1 flex-col pl-[264px] max-[899px]:pl-0">
    {#if narrow}
      <header
        class="flex h-11 flex-none items-center gap-2.5 border-b border-line bg-bg/[0.93] px-3 backdrop-blur-[6px]"
      >
        <button
          bind:this={toggleBtn}
          type="button"
          aria-controls="sidebar-panel"
          aria-expanded={drawerOpen}
          aria-label={drawerOpen ? "关闭侧栏" : "打开侧栏"}
          class="flex h-7 w-7 cursor-pointer flex-none items-center justify-center rounded-md border border-line2 text-mut hover:bg-panel2 hover:text-ink"
          onclick={() => (drawerOpen = !drawerOpen)}
        >
          <svg viewBox="0 0 16 16" class="h-3.5 w-3.5" aria-hidden="true">
            <path
              d="M2 4.5h12M2 8h12M2 11.5h12"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linecap="round"
            />
          </svg>
        </button>
        {#if route.view === "challenge" && route.code}
          <span class="truncate font-mono text-[13px] font-semibold text-ink">{route.code}</span>
        {:else if route.view === "run" && route.runId}
          <span class="truncate font-mono text-[13px] font-semibold text-ink"
            >run {route.runId.slice(0, 8)}…</span
          >
        {:else if route.view === "control"}
          <span class="truncate text-[13px] font-semibold text-ink">控制面</span>
        {/if}
        <span class="flex-1"></span>
        <span
          role="status"
          title={conn.text}
          aria-label={conn.text}
          class="h-2 w-2 flex-none rounded-full {conn.dot}"
        ></span>
      </header>
    {/if}

    <main
      bind:this={mainEl}
      class="scroll-thin min-h-0 flex-1 overflow-y-auto overscroll-contain"
    >
      <div class="mx-auto w-full max-w-[1240px] px-5 pb-16 pt-5 max-[720px]:px-3.5">
        <ReadAuthGate />
        {#if route.view === "challenge"}
          {#if route.code}
            {@const c = route.code}
            {#key c}
              <Challenge code={c} />
            {/key}
          {/if}
        {:else if route.view === "control"}
          <Control />
        {:else if route.view === "runs"}
          <Runs />
        {:else if route.view === "run" && route.runId}
          {@const rid = route.runId}
          {#key rid}
            <Run runId={rid} />
          {/key}
        {:else}
          <Overview />
        {/if}
      </div>
    </main>
  </div>
</div>
