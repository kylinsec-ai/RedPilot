<script lang="ts">
  import type { ToolEntry } from "../../lib/types";

  let { entry }: { entry: ToolEntry } = $props();

  // 行内展开态:父级 entries append-only + keyed each ⇒ 轮询追加不重建本组件,展开态天然存续
  let open = $state(false);
  const toggle = () => (open = !open);
</script>

<div
  class={`mt-[7px] mb-[7px] rounded-[7px] border bg-panel2 ${entry.err ? "border-l-[3px] border-l-red" : "border-l-[3px] border-l-green"} border-line`}
>
  <div
    role="button"
    tabindex="0"
    aria-expanded={open}
    class="flex cursor-pointer items-start gap-2 px-2.5 py-1.5"
    onclick={toggle}
    onkeydown={(e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle();
      }
    }}
  >
    <span class="mt-[1px] font-mono text-[12px] text-dim">▶</span>
    <span class="min-w-0 flex-1 break-all font-mono text-[12.5px]">
      {entry.tool || "tool"} {entry.cmd || ""}
    </span>
    <span class={`mt-0.5 whitespace-nowrap font-mono text-[11px] ${entry.err ? "text-red" : "text-green"}`}>
      {entry.err ? "✖ 出错" : "✔ 完成"}{entry.out_len != null ? ` · ${entry.out_len} 字符` : ""}
    </span>
  </div>
  {#if open && entry.out}
    <pre
      class="m-0 max-h-80 overflow-y-auto whitespace-pre-wrap break-words border-t border-dashed border-line px-3 pt-2 pb-2.5 font-mono text-[12px] text-[#c9d3e5]">{entry.out}</pre
    >
  {/if}
</div>
