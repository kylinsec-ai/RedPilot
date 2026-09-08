<script lang="ts">
  let {
    label,
    value,
    suffix = "",
    tone = "plain",
  }: {
    label: string;
    value: string | number;
    suffix?: string;
    tone?: "plain" | "green" | "amber" | "red";
  } = $props();
</script>

<div class="rounded-[10px] border border-line bg-panel px-4 py-3">
  <div class={`text-[26px] font-bold leading-[1.15] tabular-nums ${tone === "green" ? "text-green" : tone === "amber" ? "text-amber" : tone === "red" ? "text-red" : ""}`}>
    {value}<small class="text-[13px] font-normal text-mut">{suffix}</small>
  </div>
  <div class="mt-0.5 text-[12px] text-dim">{label}</div>
</div>
