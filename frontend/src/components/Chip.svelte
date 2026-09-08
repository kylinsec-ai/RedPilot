<script lang="ts">
  let {
    tone = "plain",
    text,
  }: { tone?: "flag" | "crash" | "sess" | "art" | "live" | "plain"; text: string } =
    $props();

  const cls = $derived(
    tone === "flag"
      ? "text-green border-[rgba(53,192,122,0.5)]"
      : tone === "crash"
        ? "text-red border-[rgba(229,83,75,0.55)]"
        : tone === "sess"
          ? "text-cyan border-[rgba(78,201,194,0.45)]"
          : tone === "art"
            ? "text-blue border-[rgba(90,162,255,0.45)]"
            : tone === "live"
              ? "text-amberlight border-[rgba(232,163,61,0.5)]"
              : "text-mut border-line2",
  );
</script>

<span class={`inline-flex rounded border px-[7px] py-px font-mono text-[11.5px] ${cls}`}>
  {text}
</span>
