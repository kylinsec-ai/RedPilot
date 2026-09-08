<script lang="ts">
  let { tone, text }: { tone: "info" | "warn" | "err"; text: string } = $props();

  const cls = $derived(
    tone === "warn"
      ? "bg-[rgba(232,163,61,0.09)] border-[rgba(232,163,61,0.4)] text-amberlight"
      : tone === "err"
        ? "bg-[rgba(229,83,75,0.09)] border-[rgba(229,83,75,0.45)] text-redlight"
        : "bg-[rgba(90,162,255,0.06)] border-[rgba(90,162,255,0.35)] text-bluetint",
  );
</script>

<div class={`mb-3 rounded-lg border px-3.5 py-2 text-[13px] ${cls}`}>{text}</div>
