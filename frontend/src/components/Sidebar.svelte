<script lang="ts">
  import LiveChip from "./LiveChip.svelte";
  import ChallengeList from "./ChallengeList.svelte";
  import { NAV } from "../lib/nav";
  import type { NavEntry } from "../lib/nav";
  import { route } from "../lib/route.svelte";
  import { roster, toRows } from "../lib/roster.svelte";

  // 只填充父级给定盒子;宽高与抽屉位移由 App.svelte 施加
  let { connText = "" }: { connText?: string } = $props();

  const total = $derived(toRows(roster.data).length);
  const activeCode = $derived(route.view === "challenge" ? route.code : null);

  function isActive(e: NavEntry): boolean {
    if (e.kind === "link") {
      return e.isActive ? e.isActive(route.view, route.code) : false;
    }
    return false;
  }
</script>

<!-- 外层壳(App 侧)已是地标 aside,此处用 div 避免 complementary 嵌套 -->
<div class="flex h-full min-h-0 flex-col">
  <div class="flex h-11 flex-none items-center gap-2 border-b border-line px-4">
    <div class="font-bold tracking-[0.4px]">
      TSecBench<small class="ml-2 font-normal text-dim">worker 态势台</small>
    </div>
  </div>

  <nav class="scroll-thin min-h-0 flex-1 overflow-y-auto overscroll-contain px-2 py-2">
    {#each NAV as g (g.key)}
      {#if g.label}
        <div class="mb-1 mt-3 flex items-baseline justify-between px-2 first:mt-0">
          <span class="text-[11px] font-semibold text-dim">{g.label}</span>
          {#if roster.data && g.entries.some((e) => e.kind === "challenges")}
            <span class="text-[10.5px] tabular-nums text-dim/70">{total}</span>
          {/if}
        </div>
      {/if}
      {#each g.entries as e (e.key)}
        {#if e.kind === "challenges"}
          <ChallengeList activeCode={activeCode} />
        {:else}
          {@const act = isActive(e)}
          <a
            href={e.to}
            aria-current={act ? "page" : undefined}
            class="block rounded-[7px] px-2.5 py-1.5 text-[13px] {act
              ? 'bg-panel2 text-ink'
              : 'text-mut hover:bg-panel2/60 hover:text-ink'}"
          >
            {e.label}
          </a>
        {/if}
      {/each}
    {/each}
  </nav>

  <div class="flex-none space-y-1 border-t border-line px-4 py-2.5">
    <LiveChip />
    <div class="text-[11px] text-dim">{connText}</div>
  </div>
</div>
