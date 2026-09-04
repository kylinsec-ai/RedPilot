<script lang="ts">
  import { stopZh } from "../../lib/format";
  import type { TimelineEntry } from "../../lib/types";
  import Banner from "../Banner.svelte";
  import ToolRow from "./ToolRow.svelte";

  let { entry, ts }: { entry: TimelineEntry; ts?: string } = $props();
</script>

{#if entry.kind === "turn"}
  <div class="mt-[13px] mb-0.5 flex items-baseline gap-2.5">
    <span class="font-bold">第 {entry.n} 轮</span>
    <span class="font-mono text-[11.5px] text-dim">
      {stopZh(entry.stop)}{entry.tokens ? ` · ${entry.tokens} tokens` : ""}
    </span>
  </div>
{:else if entry.kind === "attempt"}
  <div class="mt-2 text-[12.5px] text-amber">
    {#if ts}<span class="mr-1.5 font-mono text-[11.5px] text-dim">{ts}</span>{/if}↻ 重试第
    {entry.n ?? 0} 次（stall/超时后自动重启会话）
  </div>
{:else if entry.kind === "tool"}
  <ToolRow entry={entry} />
{:else if entry.kind === "text"}
  <div
    class="mt-2 mb-2 rounded-r-md border-l-2 border-[rgba(90,162,255,0.5)] bg-[rgba(90,162,255,0.06)] px-3 py-2 text-ink break-words whitespace-pre-wrap"
  >{entry.text || ""}{entry.more ? "…" : ""}</div>
{:else if entry.kind === "note"}
  <div class="mt-1.5 text-[12.5px] text-mut">{entry.note || ""}</div>
{:else if entry.kind === "error"}
  <Banner tone="err" text={entry.note || "错误"} />
{/if}
<!-- session 不会出现在条目流里(块头单独渲染),此处静默 -->
