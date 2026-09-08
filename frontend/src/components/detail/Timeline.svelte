<script lang="ts">
  import { fmtClock } from "../../lib/format";
  import type { SessionEntry, TimelineEntry } from "../../lib/types";
  import TimelineRow from "./TimelineRow.svelte";

  let { entries }: { entries: TimelineEntry[] } = $props();

  /**
   * 良构分组:条目先散排(flat),遇 session 起新块:头行 + 缩进组,其后条目
   * 归入该组直到下个 session 头。
   * (旧版靠未闭合 div 的浏览器自动恢复,2+ 会话产生累积缩进;
   *  单会话 transcript 与此渲染一致。)
   */
  type Block = { header: SessionEntry | null; items: TimelineEntry[] };
  const blocks = $derived.by(() => {
    const out: Block[] = [];
    for (const e of entries) {
      if (e.kind === "session") {
        out.push({ header: e, items: [] });
      } else if (!out.length) {
        out.push({ header: null, items: [e] });
      } else {
        out[out.length - 1].items.push(e);
      }
    }
    return out;
  });
  const blockKey = (b: Block) => (b.header ? `s${b.header.seq}` : "flat");

  // attempt 行时间戳:相隔 15s 内只显示一次(旧版 lastShownTs 规则,按序全流扫描)
  const attemptTs = $derived.by(() => {
    const map = new Map<number, string>();
    let lastShown = 0;
    for (const e of entries) {
      const show = !e.t || e.t - lastShown > 15000;
      if (e.t) lastShown = e.t;
      if (e.kind === "attempt" && show) map.set(e.seq, fmtClock(e.t));
    }
    return map;
  });
</script>

{#each blocks as b (blockKey(b))}
  {#if b.header}
    <div class="mt-[18px] flex items-baseline gap-2.5">
      <span class="text-[14px] font-bold">会话开始</span>
      <span class="font-mono text-[11.5px] text-dim">
        {fmtClock(b.header.t)}{b.header.sid ? ` · id ${b.header.sid}` : ""}
      </span>
    </div>
    <div class="ml-[9px] mt-0.5 border-l border-line2 pb-1 pl-4">
      {#if b.header.note}
        <div class="mt-1.5 font-mono text-[12.5px] text-mut">{b.header.note}</div>
      {/if}
      {#each b.items as e (e.seq)}
        <TimelineRow entry={e} ts={attemptTs.get(e.seq)} />
      {/each}
    </div>
  {:else}
    {#each b.items as e (e.seq)}
      <TimelineRow entry={e} ts={attemptTs.get(e.seq)} />
    {/each}
  {/if}
{/each}
