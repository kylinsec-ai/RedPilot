<script lang="ts">
  import { untrack } from "svelte";
  import { fetchRuns } from "../lib/api";
  import { goChallenge, goRun } from "../lib/route.svelte";
  import { RUN_STATUSES, RUN_STATUS_LABEL } from "../lib/types";
  import type { RunRow, RunStatus } from "../lib/types";
  import Banner from "../components/Banner.svelte";
  import StatCard from "../components/StatCard.svelte";

  // 查询条件(状态下拉 + worker/code 过滤);每次点"查询"重拉,Enter 同效
  let rows: RunRow[] | null = $state(null);
  let failed = $state("");
  let qStatus = $state<"" | RunStatus>("");
  let qWorker = $state("");
  let qCode = $state("");
  let loadSeq = 0; // 过期响应丢弃(手动连点时最后发起者胜)

  const STATUS_CLS: Record<string, string> = {
    running: "text-amberlight border-[rgba(232,163,61,0.5)]",
    solved: "text-green border-[rgba(53,192,122,0.5)]",
    done: "text-ink border-line2",
    failed: "text-red border-[rgba(229,83,75,0.55)]",
    interrupted: "text-dim border-line2",
  };

  function fmtDur(s?: number | null): string {
    if (!s || s < 0) return "—";
    if (s < 60) return Math.round(s) + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + Math.round(s % 60) + "s";
    const h = Math.floor(s / 3600);
    const m = Math.round((s % 3600) / 60);
    return h + "h" + (m ? " " + m + "m" : "");
  }

  function fmtStart(ts: number): string {
    return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
  }

  function buildQs(): string {
    const p = new URLSearchParams();
    if (qStatus) p.set("status", qStatus);
    const w = qWorker.trim();
    if (w) p.set("worker", w);
    const c = qCode.trim();
    if (c) p.set("challenge", c);
    const qs = p.toString();
    return qs ? "?" + qs : "";
  }

  async function load(): Promise<void> {
    const qs = buildQs();
    const seq = ++loadSeq;
    try {
      const data = (await fetchRuns(qs)).runs;
      if (seq !== loadSeq) return; // 已有更新的查询在飞:丢弃过期响应
      rows = data;
      failed = "";
    } catch (e) {
      if (seq !== loadSeq) return;
      failed = String(e);
    }
  }

  const hasFilter = $derived(
    qStatus !== "" || qWorker.trim() !== "" || qCode.trim() !== "",
  );

  // 仅挂载时拉一次:untrack 防止 buildQs() 对 q* 的同步读把击键变成逐键请求
  // (手动刷新 = 查询按钮 / Enter,与 UI 文案一致)
  $effect(() => {
    untrack(() => void load());
  });

  // 函数边界读取:避免 TS 把 rows 收窄为 null 后 derived 到 never[] 的闭包陷阱
  function list(): RunRow[] {
    return rows ?? [];
  }

  const running = $derived(list().filter((x) => x.status === "running").length);
  const solved = $derived(list().filter((x) => x.status === "solved").length);
  const bad = $derived(
    list().filter((x) => x.status === "failed" || x.status === "interrupted").length,
  );
  const events = $derived(list().reduce((a, x) => a + x.event_count, 0));

  const TH =
    "whitespace-nowrap border-b border-line2 pb-[7px] pt-[7px] pr-2.5 text-left text-[12px] font-semibold text-dim";
</script>

<h1 class="mb-1 text-[18px] font-bold">运行记录</h1>
{#if failed}
  <Banner tone="err" text={`加载失败：${failed}`} />
{/if}

<div class="mb-3.5 mt-4 grid gap-2.5 max-[720px]:grid-cols-2 grid-cols-[repeat(auto-fit,minmax(150px,1fr))]">
  <StatCard label="记录数" value={rows?.length ?? "…"} />
  <StatCard label="进行中" value={running} tone="amber" />
  <StatCard label="已解出" value={solved} tone="green" />
  <StatCard label="失败 / 中断" value={bad} tone="red" />
  <StatCard label="入库事件" value={events} suffix=" 行" />
</div>

<div class="mb-3.5 rounded-[10px] border border-line bg-panel px-4.5 py-4">
  <form
    class="mb-3 flex flex-wrap items-center gap-2"
    onsubmit={(e) => {
      e.preventDefault();
      void load();
    }}
  >
    <select
      bind:value={qStatus}
      class="h-8 cursor-pointer rounded-md border border-line2 bg-panel2 px-2 text-[13px] text-ink focus:border-blue focus:outline-none"
      aria-label="按状态过滤"
    >
      <option value="">全部状态</option>
      {#each RUN_STATUSES as s (s)}
        <option value={s}>{RUN_STATUS_LABEL[s] ?? s}</option>
      {/each}
    </select>
    <input
      bind:value={qWorker}
      placeholder="worker 过滤"
      class="h-8 w-36 rounded-md border border-line2 bg-panel2 px-2.5 font-mono text-[12.5px] text-ink placeholder:text-dim focus:border-blue focus:outline-none"
    />
    <input
      bind:value={qCode}
      placeholder="题号过滤(如 a-05)"
      class="h-8 w-40 rounded-md border border-line2 bg-panel2 px-2.5 font-mono text-[12.5px] text-ink placeholder:text-dim focus:border-blue focus:outline-none"
    />
    <button
      type="submit"
      class="h-8 cursor-pointer rounded-md border border-blue bg-blue/10 px-3.5 text-[13px] font-medium text-bluetint hover:bg-blue/20"
    >
      查询
    </button>
    <span class="ml-auto text-[12px] text-dim">
      {rows ? `${rows.length} 条 · 平台库事件 ${events} 行` : "加载中…"}
    </span>
  </form>

  <div class="overflow-x-auto">
    {#if rows}
      <table class="w-full border-collapse text-[13px]">
        <thead>
          <tr>
            <th class={TH}>开始时间</th>
            <th class={TH}>题目</th>
            <th class={TH}>worker</th>
            <th class={TH}>状态</th>
            <th class={TH}>时长</th>
            <th class={TH}>轮次</th>
            <th class={TH}>flag</th>
            <th class={TH}>事件</th>
            <th class={TH}>错误</th>
            <th class={TH}></th>
          </tr>
        </thead>
        <tbody>
          {#each rows as x (x.run_id)}
            <tr
              class="cursor-pointer border-b border-line align-middle hover:bg-panel2"
              tabindex="0"
              onclick={() => goRun(x.run_id)}
              onkeydown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  goRun(x.run_id);
                }
              }}
            >
              <td class="whitespace-nowrap px-2.5 py-2 text-[12px] tabular-nums text-mut">
                {fmtStart(x.started_at)}
              </td>
              <td class="whitespace-nowrap px-2.5 py-2">
                <span
                  role="link"
                  tabindex="0"
                  class="cursor-pointer font-mono font-semibold text-ink underline-offset-2 hover:underline"
                  onclick={(e) => {
                    e.stopPropagation();
                    goChallenge(x.challenge_code);
                  }}
                  onkeydown={(e) => {
                    if (e.key === "Enter") {
                      e.stopPropagation();
                      goChallenge(x.challenge_code);
                    }
                  }}
                >
                  {x.challenge_code}
                </span>
              </td>
              <td class="whitespace-nowrap px-2.5 py-2 font-mono text-[12px] text-mut">
                {x.worker_id}
              </td>
              <td class="whitespace-nowrap px-2.5 py-2">
                <span
                  class={`inline-flex rounded border px-[7px] py-px font-mono text-[11.5px] ${STATUS_CLS[x.status] ?? "text-mut border-line2"}`}
                >
                  {RUN_STATUS_LABEL[x.status] ?? x.status}
                </span>
              </td>
              <td class="whitespace-nowrap px-2.5 py-2 tabular-nums text-mut">
                {fmtDur(x.duration_s)}
              </td>
              <td class="whitespace-nowrap px-2.5 py-2 tabular-nums text-mut">
                {x.turns ?? "—"}
              </td>
              <td
                class="whitespace-nowrap px-2.5 py-2 tabular-nums text-green"
                title={(x.flags_accepted || []).join("\n") || undefined}
              >
                {x.flags_found ?? 0}{(x.flags_accepted || []).length ? " ✓" : ""}
              </td>
              <td class="whitespace-nowrap px-2.5 py-2 tabular-nums text-dim">
                {x.event_count}
              </td>
              <td
                class="max-w-[240px] truncate px-2.5 py-2 text-[12px] text-redlight"
                title={x.error || undefined}
              >
                {x.error || "—"}
              </td>
              <td class="whitespace-nowrap px-2.5 py-2 text-[12px] text-mut">查看 →</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {:else if !failed}
      <div class="py-[26px] text-center text-[13.5px] text-dim">加载中…</div>
    {/if}
  </div>
  {#if rows !== null && !rows.length}
    <div class="py-[26px] text-center text-[13.5px] text-dim">
      {hasFilter
        ? "无匹配记录——调整过滤条件后点查询。"
        : "还没有运行记录——worker 首次解题推送后自动出现。"}
    </div>
  {/if}
</div>
