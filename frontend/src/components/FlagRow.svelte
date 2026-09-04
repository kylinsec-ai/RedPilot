<script lang="ts">
  let { flag }: { flag: string } = $props();

  let copied = $state(false);
  let reset: ReturnType<typeof setTimeout> | null = null;

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(flag);
      copied = true;
      if (reset) clearTimeout(reset);
      reset = setTimeout(() => {
        copied = false;
      }, 1200);
    } catch {
      /* 剪贴板不可用时静默 */
    }
  }
</script>

<div class="mb-1.5 flex max-w-full items-center gap-2.5 rounded-md border border-line bg-panel2 px-2.5 py-1.5">
  <span class="break-all font-mono text-[13px] text-green">{flag}</span>
  <button
    type="button"
    class="flex-none cursor-pointer rounded-md border border-line2 bg-transparent px-2 py-0.5 font-sans text-[12px] text-mut hover:border-blue hover:text-ink"
    onclick={copy}
  >
    {copied ? "已复制" : "复制"}
  </button>
</div>
