<script lang="ts">
  import LiveChip from "./components/LiveChip.svelte";
  import Overview from "./views/Overview.svelte";
  import Challenge from "./views/Challenge.svelte";
  import { route } from "./lib/route.svelte";
  import { live, startLiveWatchers } from "./lib/live.svelte";

  const connText = $derived(
    live.conn === "live"
      ? "● 实时连接"
      : live.conn === "reconnecting"
        ? "● 重连中…"
        : "● connecting…",
  );

  // app 生命周期内启动一次 SSE + 兜底轮询;卸载时清理
  $effect(() => startLiveWatchers());
</script>

<header
  class="sticky top-0 z-[5] border-b border-line bg-bg/[0.93] backdrop-blur-[6px]"
>
  <div class="mx-auto flex max-w-[1180px] flex-wrap items-center gap-4 px-5 py-2.5">
    <div class="font-bold tracking-[0.4px]">
      TSecBench<small class="ml-2 font-normal text-dim">worker 态势台</small>
    </div>
    <LiveChip />
    <div class="flex-1"></div>
    <span class="text-[12px] text-dim">{connText}</span>
  </div>
</header>

<main class="mx-auto max-w-[1180px] px-5 pb-20">
  {#if route.view === "challenge"}
    {#if route.code}
      {@const c = route.code}
      {#key c}
        <Challenge code={c} />
      {/key}
    {/if}
  {:else}
    <Overview />
  {/if}
</main>
