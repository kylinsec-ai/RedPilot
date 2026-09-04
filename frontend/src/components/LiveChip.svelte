<script lang="ts">
  import { live } from "../lib/live.svelte";
  import { ACTIVE_PHASES } from "../lib/types";

  // 语义与旧版一致:solving 系相位显示“求解中”并呼吸,其余(含 done/error)显示待命;
  // dot 颜色单独区分:error 红 / done 绿 / 活动琥珀呼吸 / idle 灰
  const busy = $derived(ACTIVE_PHASES.includes(live.phase));
  const dotTone = $derived(
    live.phase === "error"
      ? "bg-red"
      : live.phase === "done"
        ? "bg-green"
        : busy
          ? "bg-amber animate-pulse-ring"
          : "bg-dim",
  );
</script>

<span class="flex flex-wrap items-center gap-2 text-[13px]">
  <span class={`inline-block h-[9px] w-[9px] flex-none rounded-full ${dotTone}`}></span>
  {#if busy}
    <span class="text-mut"
      >求解中：<b class="font-mono font-semibold text-ink">{live.challenge_code}</b>
      <span class="text-mut">
        · {live.phase}{live.turns ? ` · ${live.turns} 轮` : ""}
      </span>
    </span>
  {:else}
    <span class="text-mut">待命轮询</span>
  {/if}
</span>
