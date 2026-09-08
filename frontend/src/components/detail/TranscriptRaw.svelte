<script lang="ts">
  import { fetchTranscript } from "../../lib/api";

  let {
    code,
    tick,
    open = $bindable(false),
  }: { code: string; tick: number; open?: boolean } = $props();

  // 打开才拉 tail=200;开着时随父轮询(tick++)刷新;关闭时忽略。
  // 语义与旧版一致:每轮先回到“加载中…”,取回后替换(失败/成功都只写存活组件)。
  let text = $state("加载中…");

  $effect(() => {
    if (!open) return;
    void tick; // 仅作 effect 依赖:开着时随父轮询重新拉取
    let stale = false;
    text = "加载中…";
    fetchTranscript(code, 200)
      .then((jd) => {
        if (stale) return;
        text = (jd.lines || []).join("\n") || "(空)";
      })
      .catch((err) => {
        if (stale) return;
        text = "加载失败：" + String(err);
      });
    return () => {
      stale = true;
    };
  });
</script>

<details class="raw" bind:open>
  <summary class="cursor-pointer py-1 text-[12.5px] text-dim select-none">
    原始 transcript 尾部（JSONL，调试用）
  </summary>
  <pre
    class="mt-1 max-h-[340px] overflow-auto whitespace-pre-wrap break-all rounded-[7px] border border-line bg-[#0a0f1b] p-2.5 font-mono text-[11.5px] leading-[1.5] text-[#9fb0cd]">{text}</pre
  >
</details>
