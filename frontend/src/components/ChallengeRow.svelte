<script lang="ts">
  import Chip from "./Chip.svelte";
  import { traceChips } from "../lib/roster.svelte";
  import { challengeHref } from "../lib/route.svelte";
  import type { ChallengeRow as Row } from "../lib/types";

  let { row, active = false }: { row: Row; active?: boolean } = $props();

  const done = $derived(!!row.is_completed);
  const flags = $derived(row.flag_count || 0);
  const got = $derived(row.correct_flag_count || 0);
  // 进度条只出现在"进行中"(未完成且有确认数);完成/未触碰行用题号色 + ✓ 区分,不加条
  const inProg = $derived(!done && got > 0);
  const pct = $derived(flags > 0 ? Math.round((got / flags) * 100) : 0);
  // 本地痕迹徽标最多 2 个护密度(traceChips 已按意义排序:FLAG✓ 最先),余数以 +N 明示
  const allChips = $derived(traceChips(row.local));
  const chips = $derived(allChips.slice(0, 2));
  const extra = $derived(allChips.length - chips.length);
</script>

<a
  href={challengeHref(row.unique_code)}
  data-code={row.unique_code}
  aria-current={active ? "page" : undefined}
  class="block rounded-[7px] px-2.5 py-[5px] leading-tight {active
    ? 'bg-panel2'
    : 'hover:bg-panel2/50'}"
>
  <span class="flex items-baseline gap-1.5">
    <span
      class="truncate font-mono text-[12px] font-semibold {done
        ? 'text-green'
        : active
          ? 'text-ink'
          : 'text-mut'}"
    >
      {row.unique_code}
    </span>
    {#if row.local_only}
      <span class="flex-none text-[10.5px] text-amber">仅本地</span>
    {/if}
    <span class="ml-auto flex-none text-[11px] tabular-nums {done ? 'text-green' : 'text-dim'}">
      {got}/{flags}{done ? ' ✓' : ''}
    </span>
  </span>
  {#if inProg}
    <span class="mt-[4px] flex items-center gap-2">
      <span class="h-[2px] min-w-0 flex-1 overflow-hidden rounded-full bg-line">
        <span class="block h-full rounded-full bg-amber/80" style="width: {pct}%"></span>
      </span>
      {#if chips.length}
        <span class="flex flex-none gap-1">
          {#each chips as c (c.text)}
            <Chip tone={c.tone} text={c.text} />
          {/each}
          {#if extra}
            <span class="text-[10.5px] tabular-nums text-dim" title="更多本地痕迹">+{extra}</span>
          {/if}
        </span>
      {/if}
    </span>
  {:else if chips.length}
    <span class="mt-[4px] flex gap-1">
      {#each chips as c (c.text)}
        <Chip tone={c.tone} text={c.text} />
      {/each}
      {#if extra}
        <span class="text-[10.5px] tabular-nums text-dim" title="更多本地痕迹">+{extra}</span>
      {/if}
    </span>
  {/if}
</a>
