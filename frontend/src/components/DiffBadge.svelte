<script lang="ts">
  import { diffLabel } from "../lib/format";

  let { difficulty }: { difficulty: string | undefined } = $props();

  const [zh, tone] = $derived(diffLabel(difficulty));
  const cls = $derived(
    tone === "easy"
      ? "text-greenlight border-[rgba(53,192,122,0.45)]"
      : tone === "medium"
        ? "text-amberlight border-[rgba(232,163,61,0.45)]"
        : tone === "hard"
          ? "text-redlight border-[rgba(229,83,75,0.45)]"
          : "border-line2 text-ink",
  );
</script>

<span class={`inline-block whitespace-nowrap rounded-full border px-2 py-px text-[12px] ${cls}`}>
  {zh}
</span>
